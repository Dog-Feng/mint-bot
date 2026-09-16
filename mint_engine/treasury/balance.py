from __future__ import annotations

from typing import Any

from mint_engine.config.chains import get_chain
from mint_engine.core.exceptions import ConfigError
from mint_engine.core.models import WalletItem
from mint_engine.price_guard import wei_to_native_str
from mint_engine.treasury.helpers import open_pool, parse_private_key_lines
from mint_engine.wallet.manager import WalletManager

async def query_balances(
    chain_id: int,
    rpc_urls: list[str],
    private_keys: list[str],
) -> dict[str, Any]:
    keys: list[str] = []
    for item in private_keys:
        keys.extend(parse_private_key_lines(item or ""))
    if not keys:
        raise ConfigError("请至少提供一个私钥")

    chain = get_chain(chain_id)
    items: list[dict[str, Any]] = []
    pool = await open_pool(chain_id, rpc_urls)
    try:
        for idx, key in enumerate(keys):
            label = "source" if len(keys) == 1 else f"wallet_{idx + 1}"
            row: dict[str, Any] = {
                "label": label,
                "address": None,
                "balance_wei": 0,
                "balance_native": "0",
                "status": "ERROR",
                "error": None,
            }
            try:
                mgr = WalletManager([WalletItem(label=label, private_key=key)])
                signers = mgr.signers()
                if not signers:
                    row["error"] = "私钥无效"
                    items.append(row)
                    continue
                wallet = signers[0]
                balance = await pool.get_balance(wallet.address)
                row.update(
                    {
                        "address": wallet.address,
                        "balance_wei": balance,
                        "balance_native": wei_to_native_str(balance),
                        "status": "OK",
                    }
                )
            except ConfigError as exc:
                row["error"] = exc.message
            except Exception as exc:
                row["error"] = str(exc)
            items.append(row)
        return {
            "chain_id": chain.chain_id,
            "chain_name": chain.name,
            "native_symbol": chain.native_symbol,
            "items": items,
        }
    finally:
        await pool.aclose()
