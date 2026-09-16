from __future__ import annotations

import logging

import httpx

from mint_engine.config.chains import chain_id_from_opensea
from mint_engine.config.settings import get_settings
from mint_engine.core.exceptions import ConfigError, EngineError
from dataclasses import dataclass

from mint_engine.core.models import TxPlan
from mint_engine.evm import checksum

_log = logging.getLogger("mint_engine")

_MINT_ERROR_LOG_MAX = 280

_SOLD_OUT_HINTS = (
    "sold out",
    "soldout",
    "sold-out",
    "max supply",
    "maximum supply",
    "fully minted",
    "no remaining",
    "no longer available",
    "exceeds max",
    "already minted the maximum",
)


def mint_errors_indicate_sold_out(message: str) -> bool:
    lower = (message or "").lower()
    return any(hint in lower for hint in _SOLD_OUT_HINTS)


def mint_errors_indicate_drop_fully_sold_out(message: str) -> bool:
    """Whole drop exhausted (not merely ineligible for one stage)."""
    lower = (message or "").lower()
    if "fully minted out" in lower:
        return True
    if "drop is fully" in lower or "drop fully" in lower:
        return True
    return "minted out" in lower and "drop" in lower


@dataclass(frozen=True)
class OpenSeaMintProbe:
    """ok: True=eligible, False=not eligible, None=inactive (retry soon)."""

    ok: bool | None
    detail: str = ""
    sold_out: bool = False
    drop_fully_sold_out: bool = False
    plan: TxPlan | None = None


def tx_plan_from_opensea_mint(
    tx: dict[str, str | int],
    recipient: str,
    chain_id: int,
    *,
    gas_limit: int,
) -> TxPlan:
    return TxPlan(
        to=checksum(str(tx["to"])),
        data=str(tx["data"]),
        value=int(tx["value"]),
        gas=gas_limit,
        chain_id=chain_id,
        from_address=checksum(recipient),
    )


def _mint_error_snippet(body: object, response_text: str) -> str:
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            text = "; ".join(str(item).strip() for item in errors if str(item).strip())
            if text:
                return text[:_MINT_ERROR_LOG_MAX]
    text = (response_text or "").strip()
    return text[:_MINT_ERROR_LOG_MAX] if text else ""


def _log_opensea_mint_response(
    *,
    slug: str,
    minter: str,
    status_code: int,
    body: object,
    response_text: str,
    engine_code: str | None = None,
) -> None:
    detail = _mint_error_snippet(body, response_text)
    if status_code < 400:
        value_hint = ""
        if isinstance(body, dict):
            value_hint = f" value={body.get('value')}"
        _log.info(
            "[OPENSEA MINT] HTTP %s slug=%s minter=%s ok%s",
            status_code,
            slug.strip(),
            minter,
            value_hint,
        )
        return
    suffix = f" code={engine_code}" if engine_code else ""
    if detail:
        _log.info(
            "[OPENSEA MINT] HTTP %s slug=%s minter=%s%s errors=%s",
            status_code,
            slug.strip(),
            minter,
            suffix,
            detail,
        )
    else:
        _log.info(
            "[OPENSEA MINT] HTTP %s slug=%s minter=%s%s",
            status_code,
            slug.strip(),
            minter,
            suffix,
        )


def _parse_wei(value: str | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return 0
    if text.startswith(("0x", "0X")):
        return int(text, 16)
    return int(text)


async def build_drop_mint_transaction(
    slug: str,
    minter: str,
    quantity: int,
    *,
    expected_chain_id: int,
) -> dict[str, str | int]:
    settings = get_settings()
    if not settings.opensea_api_key:
        raise ConfigError("OPENSEA_API_KEY is missing")
    if not slug:
        raise ConfigError("OpenSea drop slug is required for staged mint")
    if quantity < 1 or quantity > 100:
        raise ConfigError("quantity must be between 1 and 100 for OpenSea mint")
    address = checksum(minter)
    headers = {
        "x-api-key": settings.opensea_api_key,
        "accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "mint-engine/0.1",
    }
    url = f"https://api.opensea.io/api/v2/drops/{slug.strip()}/mint"
    payload = {"minter": address, "quantity": int(quantity)}
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        response = await client.post(url, json=payload)
        raw_text = response.text or ""
        if response.status_code == 401:
            raise ConfigError("OpenSea API key was rejected")
        if response.status_code == 404:
            raise ConfigError(f"OpenSea drop not found: {slug}")
        body = response.json() if response.content else {}
        if response.status_code >= 400:
            message = _mint_error_snippet(body, raw_text) or f"OpenSea mint HTTP {response.status_code}"
            code = "OPENSEA_MINT_ERROR"
            if response.status_code == 422:
                if mint_errors_indicate_sold_out(message):
                    code = "OPENSEA_SOLD_OUT"
                else:
                    code = "OPENSEA_NOT_ELIGIBLE"
            elif response.status_code == 409:
                code = "OPENSEA_DROP_INACTIVE"
            _log_opensea_mint_response(
                slug=slug,
                minter=address,
                status_code=response.status_code,
                body=body,
                response_text=raw_text,
                engine_code=code,
            )
            raise EngineError(code, message)
        _log_opensea_mint_response(
            slug=slug,
            minter=address,
            status_code=response.status_code,
            body=body,
            response_text=raw_text,
        )
        if not isinstance(body, dict):
            raise EngineError("OPENSEA_MINT_ERROR", "invalid OpenSea mint response")
        to_addr = body.get("to")
        data = body.get("data")
        if not to_addr or not data:
            raise EngineError("OPENSEA_MINT_ERROR", "OpenSea mint response missing to/data")
        chain_name = str(body.get("chain") or "").strip()
        if not chain_name:
            raise EngineError("OPENSEA_MINT_ERROR", "OpenSea mint response missing chain")
        try:
            mint_chain_id = chain_id_from_opensea(chain_name)
        except ValueError as exc:
            raise EngineError(
                "OPENSEA_MINT_ERROR",
                f"unsupported OpenSea mint chain: {chain_name}",
            ) from exc
        if mint_chain_id != expected_chain_id:
            raise ConfigError(
                f"OpenSea mint chain {chain_name} ({mint_chain_id}) != "
                f"configured chain_id {expected_chain_id}"
            )
        return {
            "to": checksum(str(to_addr)),
            "data": str(data) if str(data).startswith("0x") else "0x" + str(data),
            "value": _parse_wei(body.get("value")),
            "chain": chain_name,
        }


def build_opensea_mint_plan(
    tx: dict[str, str | int],
    recipient: str,
    chain_id: int,
    *,
    fallback_gas_limit: int,
) -> TxPlan:
    gas = fallback_gas_limit or 280000
    return tx_plan_from_opensea_mint(tx, recipient, chain_id, gas_limit=gas)
