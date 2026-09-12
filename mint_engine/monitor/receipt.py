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


def decode_revert(data: str | None) -> str | None:
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
