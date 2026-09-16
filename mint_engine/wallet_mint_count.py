from __future__ import annotations

from mint_engine.evm import checksum, encode_call, decode_uint
from mint_engine.rpc.pool import RpcPool

_WALLET_MINT_SIGS = (
    "numberMinted(address)",
    "minted(address)",
    "walletMints(address)",
    "mintedByWallet(address)",
    "addressMintCount(address)",
    "totalMintedByAddress(address)",
)


async def read_wallet_mint_count(
    pool: RpcPool,
    contract: str,
    wallet: str,
    *,
    prefer: str | None = None,
) -> int | None:
    """Best-effort on-chain mint count for wallet; None if no view method works."""
    target = checksum(contract)
    owner = checksum(wallet)
    for sig in _WALLET_MINT_SIGS:
        try:
            raw = await pool.eth_call(
                {"to": target, "data": encode_call(sig, ["address"], [owner])},
                prefer=prefer,
            )
            if raw and raw != "0x":
                value = decode_uint(raw)
                if value is not None:
                    return int(value)
        except Exception:
            continue
    return None
