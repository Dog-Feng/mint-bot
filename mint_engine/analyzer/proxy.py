from __future__ import annotations

from mint_engine.evm import EIP1167_PREFIX, ERC1967_IMPL_SLOT, checksum, decode_address
from mint_engine.rpc.pool import RpcPool


async def resolve_proxy(pool: RpcPool, address: str) -> dict:
    code = await pool.get_code(address)
    hexcode = (code or "").lower().replace("0x", "")
    if not hexcode or hexcode == "0" * len(hexcode):
        return {
            "is_proxy": False,
            "implementation": None,
            "proxy_type": None,
            "bytecode": code,
        }

    if EIP1167_PREFIX in hexcode:
        start = hexcode.find(EIP1167_PREFIX) + len(EIP1167_PREFIX)
        impl = "0x" + hexcode[start : start + 40]
        return {
            "is_proxy": True,
            "implementation": checksum(impl),
            "proxy_type": "EIP1167",
            "bytecode": code,
        }

    slot = await pool.get_storage(address, ERC1967_IMPL_SLOT)
    impl = decode_address(slot)
    if impl and int(impl, 16) != 0:
        return {
            "is_proxy": True,
            "implementation": impl,
            "proxy_type": "ERC1967",
            "bytecode": code,
        }

    return {
        "is_proxy": False,
        "implementation": None,
        "proxy_type": None,
        "bytecode": code,
    }
