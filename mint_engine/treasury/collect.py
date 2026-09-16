from __future__ import annotations

from typing import Any

import asyncio

from mint_engine.config.chains import get_chain
from mint_engine.core.models import TreasuryCollectRequest
from mint_engine.evm import checksum, is_address
from mint_engine.core.exceptions import ConfigError
from mint_engine.price_guard import native_unit_to_wei, wei_to_native_str
from mint_engine.treasury.helpers import (
    emit_run_event,
    open_pool,
    quote_native_transfer,
    resolve_source_wallets,
    send_native_transfer,
    sleep_gap,
)


async def _send_amount_for_wallet(
    pool,
    wallet,
    dest: str,
    mode: str,
    fixed_wei: int,
    gas,
) -> tuple[int, int, str]:
    balance = await pool.get_balance(wallet.address)
    if mode == "all":
        _lim, _q, fee = await quote_native_transfer(pool, wallet.address, dest, 0, gas)
        value = balance - fee
        if value <= 0:
            return 0, fee, "INSUFFICIENT_BALANCE"
        _lim2, _q2, fee2 = await quote_native_transfer(pool, wallet.address, dest, value, gas)
        if balance < value + fee2:
            value = balance - fee2
        return max(value, 0), fee2, "READY" if value > 0 else "INSUFFICIENT_BALANCE"
    fee_basis = fixed_wei if fixed_wei > 0 else 1
    _lim, _q, fee = await quote_native_transfer(pool, wallet.address, dest, fee_basis, gas)
    value = min(fixed_wei, max(balance - fee, 0))
    if value <= 0:
        return 0, fee, "INSUFFICIENT_BALANCE"
    _lim2, _q2, fee2 = await quote_native_transfer(pool, wallet.address, dest, value, gas)
    if balance < value + fee2:
        value = max(balance - fee2, 0)
    status = "READY" if value > 0 else "INSUFFICIENT_BALANCE"
    return value, fee2, status


async def preview_collect(request: TreasuryCollectRequest) -> dict[str, Any]:
    if not request.destination or not is_address(request.destination):
        raise ConfigError("归集目标必须是有效的 0x 地址")
    dest = checksum(request.destination)
    mode = (request.mode or "fixed").lower()
    if mode not in {"fixed", "all"}:
        raise ConfigError("mode 必须是 fixed 或 all")
    fixed_wei = 0
    if mode == "fixed":
        if not request.fixed_amount:
            raise ConfigError("指定数量模式下请填写 fixed_amount")
        fixed_wei = native_unit_to_wei(request.fixed_amount)
        if fixed_wei <= 0:
            raise ConfigError("归集数量必须大于 0")

    chain = get_chain(request.chain_id)
    wallets = resolve_source_wallets(request.source_private_keys)
    pool = await open_pool(request.chain_id, request.rpc_urls)
    items: list[dict[str, Any]] = []
    try:
        for wallet in wallets:
            value, fee, status = await _send_amount_for_wallet(
                pool, wallet, dest, mode, fixed_wei, request.gas
            )
            balance = await pool.get_balance(wallet.address)
            items.append(
                {
                    "label": wallet.label,
                    "address": wallet.address,
                    "balance_wei": balance,
                    "balance_native": wei_to_native_str(balance),
                    "amount_wei": value,
                    "amount_native": wei_to_native_str(value),
                    "fee_wei": fee,
                    "fee_native": wei_to_native_str(fee),
                    "destination": dest,
                    "status": status,
                    "note": None if status == "READY" else "余额不足以支付 gas + 转出",
                }
            )
        ready = sum(1 for row in items if row["status"] == "READY")
        return {
            "mode": "collect",
            "chain_id": chain.chain_id,
            "chain_name": chain.name,
            "native_symbol": chain.native_symbol,
            "destination": dest,
            "collect_mode": mode,
            "items": items,
            "ready_count": ready,
            "total_count": len(items),
            "can_run": ready > 0,
        }
    finally:
        await pool.aclose()


def _format_collect_event(row: dict[str, Any], native_symbol: str) -> str:
    return (
        f"{row.get('label')} {row.get('run_status')} "
        f"{row.get('amount_native', '')} {native_symbol} "
        f"{row.get('tx_hash') or ''}".strip()
    )


async def run_collect(
    request: TreasuryCollectRequest,
    *,
    cancel: asyncio.Event | None = None,
    event_sink: list[str] | None = None,
) -> dict[str, Any]:
    preview = await preview_collect(request)
    dest = preview["destination"]
    mode = preview["collect_mode"]
    fixed_wei = native_unit_to_wei(request.fixed_amount) if mode == "fixed" else 0
    wallets = resolve_source_wallets(request.source_private_keys)
    by_label = {w.label: w for w in wallets}
    pool = await open_pool(request.chain_id, request.rpc_urls)
    events: list[str] = []
    results: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(max(1, request.concurrency))

    sym = preview["native_symbol"]

    async def one(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if cancel and cancel.is_set():
            out = {**row, "run_status": "CANCELLED", "tx_hash": None}
            emit_run_event(events, event_sink, _format_collect_event(out, sym))
            return row["label"], out
        if row["status"] != "READY":
            out = {**row, "run_status": row["status"], "tx_hash": None}
            emit_run_event(events, event_sink, _format_collect_event(out, sym))
            return row["label"], out
        wallet = by_label.get(row["label"])
        if not wallet:
            out = {**row, "run_status": "SKIP", "error": "wallet not found"}
            emit_run_event(events, event_sink, _format_collect_event(out, sym))
            return row["label"], out
        if cancel and cancel.is_set():
            out = {**row, "run_status": "CANCELLED", "tx_hash": None}
            emit_run_event(events, event_sink, _format_collect_event(out, sym))
            return row["label"], out
        await sleep_gap(request.gap_min_sec, request.gap_max_sec)
        if cancel and cancel.is_set():
            out = {**row, "run_status": "CANCELLED", "tx_hash": None}
            emit_run_event(events, event_sink, _format_collect_event(out, sym))
            return row["label"], out
        async with sem:
            value, _fee, status = await _send_amount_for_wallet(
                pool, wallet, dest, mode, fixed_wei, request.gas
            )
            if status != "READY" or value <= 0:
                out = {
                    **row,
                    "amount_wei": value,
                    "amount_native": wei_to_native_str(value),
                    "run_status": status,
                    "tx_hash": None,
                }
                emit_run_event(events, event_sink, _format_collect_event(out, sym))
                return row["label"], out
            outcome = await send_native_transfer(pool, wallet, dest, value, request.gas)
            out = {
                **row,
                "amount_wei": value,
                "amount_native": wei_to_native_str(value),
                "tx_hash": outcome.get("tx_hash"),
                "run_status": outcome.get("status"),
                "error": outcome.get("error"),
                "fee_wei": outcome.get("fee_wei", row.get("fee_wei")),
            }
            emit_run_event(events, event_sink, _format_collect_event(out, sym))
            return row["label"], out

    try:
        tasks = [asyncio.create_task(one(row)) for row in preview["items"]]
        by_label: dict[str, dict[str, Any]] = {}
        for task in asyncio.as_completed(tasks):
            label, row_out = await task
            by_label[label] = row_out
        results = [by_label[row["label"]] for row in preview["items"] if row["label"] in by_label]
        success = sum(1 for r in results if r.get("run_status") == "SUCCESS")
        failed = sum(1 for r in results if r.get("run_status") not in {"SUCCESS", "SKIP", "READY"})
        return {
            **preview,
            "results": results,
            "success": success,
            "failed": failed,
            "events": events,
        }
    finally:
        await pool.aclose()
