from __future__ import annotations

from eth_utils import keccak

def _topic0(signature: str) -> str:
    digest = keccak(text=signature).hex()
    return digest if digest.startswith("0x") else "0x" + digest


TRANSFER = _topic0("Transfer(address,address,uint256)")
CONSECUTIVE_TRANSFER = _topic0("ConsecutiveTransfer(uint256,uint256,address,address)")


def _topic_hex(value) -> str:
    text = str(value).lower()
    return text[2:] if text.startswith("0x") else text


def parse_token_ids(receipt: dict | None, recipient: str) -> list[int]:
    if not receipt:
        return []
    token_ids = []
    want = recipient.lower().replace("0x", "")
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if len(topics) < 4:
            continue
        topic0 = "0x" + _topic_hex(topics[0])
        if topic0 == TRANSFER:
            if _topic_hex(topics[2])[-40:] != want:
                continue
            token_ids.append(int(_topic_hex(topics[3]), 16))
            continue
        if topic0 == CONSECUTIVE_TRANSFER:
            if _topic_hex(topics[3])[-40:] != want:
                continue
            start = int(_topic_hex(topics[1]), 16)
            data = _topic_hex(log.get("data") or "0x")
            end = int(data, 16) if data else start
            token_ids.extend(range(start, end + 1))
    return token_ids


def extract_revert_data(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, dict):
        for key in ("data", "reason", "message"):
            found = extract_revert_data(value.get(key))
            if found:
                return found
    return None


def decode_revert(data: str | None) -> str | None:
    data = extract_revert_data(data) or data
    if not data or data == "0x":
        return None
    raw = data[2:] if data.startswith("0x") else data
    if raw.startswith("08c379a0") and len(raw) >= 8 + 64 * 2:
        try:
            from eth_abi import decode

            message = decode(["string"], bytes.fromhex(raw[8:]))[0]
            return message
        except Exception:
            return data
    return data


_SOLD_OUT_ERRORS = (
    "SoldOut()",
    "MintSoldOut()",
    "MaxSupply()",
    "MaxSupplyReached()",
    "MaxSupplyExceeded()",
    "ExceedsMaxSupply()",
    "ExceedsSupply()",
    "TotalSupplyExceeded()",
    "InsufficientSupply()",
    "NoSupplyLeft()",
    "SupplyExceeded()",
    "CapReached()",
    "MintCap()",
    "PublicSaleEnded()",
    "SaleEnded()",
)


def _selector4(name: str) -> str:
    digest = keccak(text=name).hex()
    if digest.startswith("0x"):
        digest = digest[2:]
    return digest[:8].lower()


_SOLD_OUT_SELECTORS = {_selector4(name) for name in _SOLD_OUT_ERRORS}


def revert_blob(exc: Exception) -> str:
    parts = [str(exc)]
    from mint_engine.core.exceptions import EngineError

    if isinstance(exc, EngineError):
        details = exc.details or {}
        data = extract_revert_data(details.get("data")) or details.get("data")
        if data:
            parts.append(str(data))
            decoded = decode_revert(data if isinstance(data, str) else None)
            if decoded:
                parts.append(str(decoded))
        if details.get("message"):
            parts.append(str(details["message"]))
        parts.append(str(details))
    return " ".join(parts)


def is_sold_out(text: str | None) -> bool:
    blob = (text or "").lower()
    if not blob:
        return False
    if any(sel in blob for sel in _SOLD_OUT_SELECTORS):
        return True
    compact = blob.replace(" ", "").replace("_", "").replace("-", "")
    compact = compact.replace("presaleended", "").replace("alreadyminted", "").replace("alreadyclaimed", "")
    return any(
        needle in compact
        for needle in (
            "soldout",
            "maxsupply",
            "exceedssupply",
            "exceedsmaxsupply",
            "insufficientsupply",
            "nosupplyleft",
            "supplyexceeded",
            "mintcap",
            "capreached",
            "publicsaleended",
            "saleended",
        )
    )
