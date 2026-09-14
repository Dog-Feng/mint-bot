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
        if len(topics) < 3:
            continue
        topic0 = "0x" + _topic_hex(topics[0])
        if topic0 == TRANSFER:
            if _topic_hex(topics[2])[-40:] != want:
                continue
            if len(topics) >= 4:
                token_ids.append(int(_topic_hex(topics[3]), 16))
            else:
                data = _topic_hex(log.get("data") or "0x")
                if len(data) >= 64:
                    token_ids.append(int(data[-64:], 16))
            continue
        if len(topics) < 4:
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

_ERROR_STRING_SELECTOR = "08c379a0"


def revert_custom_selectors(data: str | None) -> set[str]:
    """Return 4-byte custom error selectors present at the start of revert hex payloads."""
    found: set[str] = set()
    if not data:
        return found
    text = data if isinstance(data, str) else str(data)
    lower = text.lower()
    pos = 0
    while True:
        idx = lower.find("0x", pos)
        if idx < 0:
            break
        j = idx + 2
        while j < len(lower) and lower[j] in "0123456789abcdef":
            j += 1
        chunk = lower[idx + 2 : j]
        if len(chunk) >= 8:
            head = chunk[:8]
            if head == _ERROR_STRING_SELECTOR:
                pos = j
                continue
            if head in _SOLD_OUT_SELECTORS:
                found.add(head)
        pos = j if j > idx + 2 else idx + 2
    return found


def _sold_out_in_decoded_message(blob: str) -> bool:
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
    if not text:
        return False
    blob = str(text)
    if revert_custom_selectors(blob):
        return True
    decoded = decode_revert(extract_revert_data(blob) or blob)
    if decoded and decoded != blob and _sold_out_in_decoded_message(decoded.lower()):
        return True
    return _sold_out_in_decoded_message(blob.lower())
