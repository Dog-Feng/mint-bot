from __future__ import annotations

import httpx

from mint_engine.config.chains import chain_id_from_opensea
from mint_engine.config.settings import get_settings
from mint_engine.core.exceptions import ConfigError, EngineError
from mint_engine.evm import checksum


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
        if response.status_code == 401:
            raise ConfigError("OpenSea API key was rejected")
        if response.status_code == 404:
            raise ConfigError(f"OpenSea drop not found: {slug}")
        body = response.json() if response.content else {}
        if response.status_code >= 400:
            errors = body.get("errors") if isinstance(body, dict) else None
            message = "; ".join(errors) if isinstance(errors, list) else response.text[:300]
            code = "OPENSEA_MINT_ERROR"
            if response.status_code == 422:
                code = "OPENSEA_NOT_ELIGIBLE"
            elif response.status_code == 409:
                code = "OPENSEA_DROP_INACTIVE"
            raise EngineError(code, message or f"OpenSea mint HTTP {response.status_code}")
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
