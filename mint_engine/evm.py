from __future__ import annotations

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address


def checksum(address: str) -> str:
    return to_checksum_address(address)


def is_address(value: str) -> bool:
    try:
        checksum(value)
        return True
    except Exception:
        return False


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


def encode_call(signature: str, types: list[str], values: list) -> str:
    data = keccak(text=signature)[:4] + encode(types, values)
    return "0x" + data.hex()


def decode_data(types: list[str], data: str):
    raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    if not raw:
        return None
    return decode(types, raw)


def decode_string(data: str) -> str | None:
    if not data or data in {"0x", "0x0"}:
        return None
    try:
        return decode_data(["string"], data)[0]
    except Exception:
        return None


def decode_uint(data: str) -> int | None:
    if not data or data in {"0x", "0x0"}:
        return 0 if data == "0x0" else None
    try:
        return int(data, 16)
    except Exception:
        return None


def decode_address(data: str) -> str | None:
    if not data or data == "0x":
        return None
    try:
        return checksum("0x" + data[-40:])
    except Exception:
        return None


def word_address(word: str) -> str:
    return checksum("0x" + word[-40:])


ERC1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1167_PREFIX = "363d3d373d3d3d363d73"
EIP1167_SUFFIX = "5af43d82803e903d91602b57fd5bf3"

ERC721_IID = "0x80ac58cd"
ERC1155_IID = "0xd9b67a26"
ERC165_IID = "0x01ffc9a7"

SEADROP_V1 = "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5"

KNOWN_MINTS = [
    ("mint(uint256)", ["uint256"], True),
    ("mint(address,uint256)", ["address", "uint256"], True),
    ("publicMint(uint256)", ["uint256"], True),
    ("claim(uint256)", ["uint256"], True),
]
