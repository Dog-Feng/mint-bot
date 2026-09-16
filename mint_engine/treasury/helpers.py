from __future__ import annotations

import asyncio
import random
from decimal import Decimal
from typing import Any

from mint_engine.config.chains import get_chain
from mint_engine.core.exceptions import ConfigError
from mint_engine.core.models import GasBump, GasConfig, TxPlan, WalletItem
from mint_engine.evm import checksum, is_address
from mint_engine.price_guard import native_unit_to_wei, wei_to_native_str
from mint_engine.rpc.pool import RpcPool
from mint_engine.sweep.collector import _open_pool
from mint_engine.transaction.gas import apply_gas, quote_gas, resolve_gas_limit
from mint_engine.transaction.signer import sign_tx
from mint_engine.treasury.log import logger as treasury_logger
from mint_engine.wallet.manager import ResolvedWallet, WalletManager

NATIVE_TRANSFER_FALLBACK_GAS = 21_000


def default_treasury_gas() -> GasConfig:
    return GasConfig(
        bump=GasBump(extra_priority_gwei=0, max_fee_multiplier=1.0, hard_cap_gwei=0),
        gas_limit_mode="estimate_or_fallback",
        fallback_gas_limit=NATIVE_TRANSFER_FALLBACK_GAS,
    )


def random_native_wei(amount_min: str, amount_max: str) -> int:
    lo = native_unit_to_wei(amount_min)
    hi = native_unit_to_wei(amount_max)
    if lo > hi:
        lo, hi = hi, lo
    if hi <= 0:
        return 0
    if lo == hi:
        return lo
    span = hi - lo
    pick = Decimal(str(random.random()))
    chosen = lo + int(Decimal(span) * pick)
    return max(lo, min(chosen, hi))


def parse_private_key_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def emit_run_event(events: list[str], sink: list[str] | None, message: str) -> None:
    events.append(message)
    if sink is not None:
        sink.append(message)
    treasury_logger.info("%s", message)


def parse_distribute_source_key(text: str) -> str:
    """One source wallet for distribute; uses first non-empty line only."""
    lines = parse_private_key_lines(text)
    if not lines:
        raise ConfigError("请填写源钱包私钥")
    return lines[0]


def parse_target_addresses(targets: list[str]) -> list[str]:
    out: list[str] = []
    for raw in targets:
        text = (raw or "").strip()
        if not text:
            continue
        if not is_address(text):
            raise ConfigError(f"无效目标地址: {text[:18]}…")
        out.append(checksum(text))
    if not out:
        raise ConfigError("请至少填写一个目标地址")
    return out


def resolve_source_wallet(private_key: str) -> ResolvedWallet:
    key = parse_distribute_source_key(private_key)
    mgr = WalletManager([WalletItem(label="source", private_key=key)])
    signers = mgr.signers()
    if not signers:
        raise ConfigError("源钱包私钥无效")
    return signers[0]


def resolve_source_wallets(private_keys: list[str]) -> list[ResolvedWallet]:
    lines: list[str] = []
    for item in private_keys:
        lines.extend(parse_private_key_lines(item or ""))
    if not lines:
        raise ConfigError("请至少填写一个源钱包私钥")
    items = [WalletItem(label=f"wallet_{i + 1}", private_key=key) for i, key in enumerate(lines)]
    mgr = WalletManager(items)
    signers = mgr.signers()
    if not signers:
        raise ConfigError("没有可用的源钱包私钥")
    return signers


async def open_pool(chain_id: int, rpc_urls: list[str]) -> RpcPool:
    return await _open_pool(chain_id, rpc_urls)


def _estimate_tx_dict(from_addr: str, to_addr: str, value_wei: int) -> dict[str, str]:
    return {
        "from": checksum(from_addr),
        "to": checksum(to_addr),
        "value": hex(max(value_wei, 0)),
        "data": "0x",
    }


async def quote_native_transfer(
    pool: RpcPool,
    from_addr: str,
    to_addr: str,
    value_wei: int,
    gas: GasConfig,
) -> tuple[int, dict[str, int], int]:
    tx = _estimate_tx_dict(from_addr, to_addr, value_wei)
    gas_limit, _source = await resolve_gas_limit(pool, tx, gas)
    quote = await quote_gas(pool, gas, fallback_limit=gas_limit, raise_on_cap=False)
    quote["gas_limit"] = gas_limit
    fee_wei = int(gas_limit) * int(quote["max_fee"])
    return gas_limit, quote, fee_wei


async def send_native_transfer(
    pool: RpcPool,
    wallet: ResolvedWallet,
    to_addr: str,
    value_wei: int,
    gas: GasConfig,
    *,
    nonce: int | None = None,
    wait_receipt: bool = True,
) -> dict[str, Any]:
    if value_wei <= 0:
        return {
            "status": "SKIP",
            "error": "amount is zero",
            "tx_hash": None,
            "value_wei": 0,
        }
    if not wallet.private_key:
        return {"status": "SKIP", "error": "missing private key", "tx_hash": None, "value_wei": value_wei}
    dest = checksum(to_addr)
    chain = get_chain(pool.expected_chain_id)
    use_nonce = nonce if nonce is not None else await pool.get_nonce(wallet.address, "pending")
    gas_limit, quote, fee_wei = await quote_native_transfer(pool, wallet.address, dest, value_wei, gas)
    balance = await pool.get_balance(wallet.address)
    if balance < value_wei + fee_wei:
        return {
            "status": "INSUFFICIENT_BALANCE",
            "error": f"need {wei_to_native_str(value_wei + fee_wei)} {chain.native_symbol}, "
            f"have {wei_to_native_str(balance)}",
            "tx_hash": None,
            "value_wei": value_wei,
            "balance_wei": balance,
            "fee_wei": fee_wei,
        }
    plan = TxPlan(
        to=dest,
        data="0x",
        value=value_wei,
        gas=gas_limit,
        chain_id=pool.expected_chain_id,
        nonce=use_nonce,
        from_address=wallet.address,
    )
    apply_gas(plan, quote)
    raw, tx_hash = sign_tx(plan, wallet.private_key)
    sends = await pool.broadcast(raw)
    ok_send = next((row for row in sends if row.get("ok")), None)
    if not ok_send:
        err = sends[-1].get("error") if sends else "broadcast failed"
        return {
            "status": "SEND_FAILED",
            "error": str(err),
            "tx_hash": None,
            "value_wei": value_wei,
            "nonce": use_nonce,
        }
    tx_hash = ok_send.get("tx_hash") or tx_hash
    if not wait_receipt:
        return {
            "status": "BROADCAST",
            "tx_hash": tx_hash,
            "value_wei": value_wei,
            "fee_wei": fee_wei,
            "gas_limit": gas_limit,
            "nonce": use_nonce,
        }
    receipt = await pool.wait_receipt(tx_hash, timeout=gas.receipt_timeout_sec)
    if receipt is None:
        return {
            "status": "TIMEOUT",
            "tx_hash": tx_hash,
            "value_wei": value_wei,
            "error": "receipt timeout",
            "nonce": use_nonce,
        }
    success = int(receipt.get("status") or "0x0", 16) == 1
    return {
        "status": "SUCCESS" if success else "REVERTED",
        "tx_hash": tx_hash,
        "value_wei": value_wei,
        "fee_wei": fee_wei,
        "gas_limit": gas_limit,
        "error": None if success else "execution reverted",
        "nonce": use_nonce,
    }


async def sleep_gap(gap_min: float, gap_max: float) -> float:
    lo = min(gap_min, gap_max)
    hi = max(gap_min, gap_max)
    if hi <= 0:
        return 0.0
    delay = random.uniform(lo, hi)
    if delay > 0:
        await asyncio.sleep(delay)
    return delay
