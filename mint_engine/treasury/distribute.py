from __future__ import annotations

import asyncio
from typing import Any

from mint_engine.config.chains import get_chain
from mint_engine.core.exceptions import ConfigError
from mint_engine.core.models import TreasuryDistributeRequest
from mint_engine.price_guard import native_unit_to_wei, wei_to_native_str
from mint_engine.evm import checksum, is_address
from mint_engine.treasury.log import logger as treasury_logger
from mint_engine.treasury.helpers import (
    emit_run_event,
    open_pool,
    parse_target_addresses,
    quote_native_transfer,
    random_native_wei,
    resolve_source_wallet,
    send_native_transfer,
    sleep_gap,
)


def _assert_preview_source(request: TreasuryDistributeRequest, source_address: str) -> None:
    if not request.execute_plan:
        return
    expected = (request.preview_source_address or "").strip()
    if not expected:
        raise ConfigError("执行分发请带上 preview_source_address（与预览时的源地址一致）")
    if not is_address(expected):
        raise ConfigError("preview_source_address 无效")
    if checksum(expected).lower() != checksum(source_address).lower():
        raise ConfigError("源钱包与预览时不一致，请重新预览后再执行")


def _append_unprocessed_results(
    preview_items: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    pending_ready_status: str | None = None,
) -> None:
    """Fill rows that the run loop never reached (cancel or on-chain abort)."""
    done_indices = {int(r.get("index") or 0) for r in results}
    for row in preview_items:
        idx = int(row.get("index") or 0)
        if idx in done_indices:
            continue
        if row.get("status") != "READY":
            results.append({**row, "tx_hash": None, "run_status": row["status"]})
            continue
        if pending_ready_status:
            results.append({**row, "tx_hash": None, "run_status": pending_ready_status})


def _validate_amount_range(amount_min: str, amount_max: str) -> None:
    lo = native_unit_to_wei(amount_min)
    hi = native_unit_to_wei(amount_max)
    if lo < 0 or hi < 0:
        raise ConfigError("金额不能为负数")
    if lo > hi:
        raise ConfigError("amount_min 不能大于 amount_max")


def _build_preview_items(
    *,
    source_address: str,
    targets: list[str],
    gap_min: float,
    gap_max: float,
    balance_wei: int,
    quote_rows: list[tuple[int, int]],
    native_symbol: str,
    indices: list[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Preview rows: sum(amount)+sum(gas) must not exceed balance to allow run."""
    items: list[dict[str, Any]] = []
    cumulative_delay = 0.0
    total_amount = 0
    total_fee = 0
    payable_count = 0

    if indices is not None and len(indices) != len(targets):
        raise ConfigError("indices 与 targets 长度不一致")

    for pos, (dest, (value_wei, fee_wei)) in enumerate(zip(targets, quote_rows)):
        row_index = indices[pos] if indices is not None else pos + 1
        if pos > 0:
            lo = min(gap_min, gap_max)
            hi = max(gap_min, gap_max)
            cumulative_delay += (lo + hi) / 2 if hi > 0 else 0

        skip = False
        note = None
        if dest.lower() == source_address.lower():
            skip = True
            note = "不能与源地址相同"
        elif value_wei <= 0:
            skip = True
            note = "金额为 0"
        elif not skip:
            total_amount += value_wei
            total_fee += fee_wei
            payable_count += 1

        items.append(
            {
                "index": row_index,
                "to": dest,
                "amount_wei": value_wei,
                "amount_native": wei_to_native_str(value_wei),
                "fee_wei": fee_wei,
                "fee_native": wei_to_native_str(fee_wei),
                "delay_sec": round(cumulative_delay, 3),
                "status": "SKIP" if skip else "PENDING",
                "note": note,
            }
        )

    total_required = total_amount + total_fee
    sufficient = payable_count > 0 and total_required <= balance_wei
    budget_message = None
    if payable_count <= 0:
        sufficient = False
        budget_message = "没有可支付的目标地址（列表为空或与源地址相同）"
    elif not sufficient:
        budget_message = (
            f"预览合计超出余额：金额 {wei_to_native_str(total_amount)} + gas {wei_to_native_str(total_fee)} "
            f"= {wei_to_native_str(total_required)} {native_symbol}，"
            f"当前余额 {wei_to_native_str(balance_wei)} {native_symbol}"
        )

    for row in items:
        if row["status"] == "SKIP":
            continue
        if sufficient:
            row["status"] = "READY"
            row["note"] = None
        else:
            row["status"] = "INSUFFICIENT_BALANCE"
            row["note"] = "预览金额合计 + gas 超过源钱包余额"

    budget = {
        "payable_targets": payable_count,
        "total_amount_wei": total_amount,
        "total_amount_native": wei_to_native_str(total_amount),
        "total_fee_wei": total_fee,
        "total_fee_native": wei_to_native_str(total_fee),
        "total_required_wei": total_required,
        "total_required_native": wei_to_native_str(total_required),
        "sufficient": sufficient,
        "message": budget_message,
    }
    return items, budget


async def preview_distribute(request: TreasuryDistributeRequest) -> dict[str, Any]:
    _validate_amount_range(request.amount_min, request.amount_max)
    chain = get_chain(request.chain_id)
    source = resolve_source_wallet(request.source_private_key)
    targets = parse_target_addresses(request.targets)
    pool = await open_pool(request.chain_id, request.rpc_urls)
    try:
        balance = await pool.get_balance(source.address)
        quote_rows: list[tuple[int, int]] = []
        for dest in targets:
            value_wei = random_native_wei(request.amount_min, request.amount_max)
            _limit, _quote, fee_wei = await quote_native_transfer(
                pool, source.address, dest, value_wei, request.gas
            )
            quote_rows.append((value_wei, fee_wei))
        items, budget = _build_preview_items(
            source_address=source.address,
            targets=targets,
            gap_min=request.gap_min_sec,
            gap_max=request.gap_max_sec,
            balance_wei=balance,
            quote_rows=quote_rows,
            native_symbol=chain.native_symbol,
        )
        ready = sum(1 for row in items if row["status"] == "READY")
        can_run = ready > 0 and budget["sufficient"]
        treasury_logger.info(
            "distribute preview chain_id=%s source=%s targets=%s ready=%s can_run=%s",
            chain.chain_id,
            source.address,
            len(targets),
            ready,
            can_run,
        )
        return {
            "mode": "distribute",
            "chain_id": chain.chain_id,
            "chain_name": chain.name,
            "native_symbol": chain.native_symbol,
            "source_address": source.address,
            "source_balance_wei": balance,
            "source_balance_native": wei_to_native_str(balance),
            "budget": budget,
            "items": items,
            "ready_count": ready,
            "total_count": len(items),
            "can_run": can_run,
        }
    finally:
        await pool.aclose()


def _merge_execute_plan(preview: dict[str, Any], execute_plan: list[dict]) -> dict[str, Any]:
    if not execute_plan:
        return preview
    by_index: dict[int, dict] = {}
    for row in execute_plan:
        idx = int(row.get("index") or 0)
        if idx > 0:
            by_index[idx] = row
    items = []
    for row in preview["items"]:
        idx = int(row.get("index") or 0)
        locked = by_index.get(idx)
        if locked and int(locked.get("amount_wei") or 0) > 0:
            locked_to = (locked.get("to") or "").lower()
            if locked_to and locked_to != (row.get("to") or "").lower():
                raise ConfigError(f"执行计划第 {idx} 笔目标地址与预览不一致")
            amount_wei = int(locked["amount_wei"])
            items.append(
                {
                    **row,
                    "amount_wei": amount_wei,
                    "amount_native": wei_to_native_str(amount_wei),
                }
            )
        else:
            items.append(row)
    return {**preview, "items": items}


async def _preview_from_execute_plan(
    request: TreasuryDistributeRequest,
    execute_plan: list[dict],
) -> dict[str, Any]:
    _validate_amount_range(request.amount_min, request.amount_max)
    chain = get_chain(request.chain_id)
    source = resolve_source_wallet(request.source_private_key)
    pool = await open_pool(request.chain_id, request.rpc_urls)
    try:
        balance = await pool.get_balance(source.address)
        skeleton: list[dict[str, Any]] = []
        for ep in sorted(execute_plan, key=lambda r: int(r.get("index") or 0)):
            idx = int(ep.get("index") or 0)
            to_raw = (ep.get("to") or "").strip()
            if not to_raw or not is_address(to_raw):
                raise ConfigError(f"执行计划第 {idx or '?'} 笔目标地址无效")
            amount_wei = int(ep.get("amount_wei") or 0)
            skeleton.append(
                {
                    "index": idx if idx > 0 else len(skeleton) + 1,
                    "to": checksum(to_raw),
                    "amount_wei": amount_wei,
                    "amount_native": wei_to_native_str(amount_wei),
                    "delay_sec": 0,
                }
            )
        if not skeleton:
            raise ConfigError("execute_plan 为空或地址无效")
        items, budget = await _recompute_preview_budget(
            pool,
            source.address,
            skeleton,
            gap_min=request.gap_min_sec,
            gap_max=request.gap_max_sec,
            balance_wei=balance,
            gas=request.gas,
            native_symbol=chain.native_symbol,
        )
        ready = sum(1 for row in items if row["status"] == "READY")
        return {
            "mode": "distribute",
            "chain_id": chain.chain_id,
            "chain_name": chain.name,
            "native_symbol": chain.native_symbol,
            "source_address": source.address,
            "source_balance_wei": balance,
            "source_balance_native": wei_to_native_str(balance),
            "budget": budget,
            "items": items,
            "ready_count": ready,
            "total_count": len(items),
            "can_run": ready > 0 and budget["sufficient"],
        }
    finally:
        await pool.aclose()


async def _recompute_preview_budget(
    pool,
    source_address: str,
    items: list[dict[str, Any]],
    *,
    gap_min: float,
    gap_max: float,
    balance_wei: int,
    gas,
    native_symbol: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    quote_rows: list[tuple[int, int]] = []
    for row in items:
        value_wei = int(row.get("amount_wei") or 0)
        _lim, _q, fee_wei = await quote_native_transfer(
            pool, source_address, row["to"], value_wei, gas
        )
        quote_rows.append((value_wei, fee_wei))
    targets = [row["to"] for row in items]
    indices = [int(row.get("index") or i + 1) for i, row in enumerate(items)]
    return _build_preview_items(
        source_address=source_address,
        targets=targets,
        gap_min=gap_min,
        gap_max=gap_max,
        balance_wei=balance_wei,
        quote_rows=quote_rows,
        native_symbol=native_symbol,
        indices=indices,
    )


def _build_run_items_for_execute(
    request: TreasuryDistributeRequest,
    source_address: str,
) -> list[dict[str, Any]]:
    """Build payout rows from execute_plan or form (min/max per target). No balance RPC."""
    source_l = checksum(source_address).lower()
    if request.execute_plan:
        items: list[dict[str, Any]] = []
        for pos, ep in enumerate(
            sorted(request.execute_plan, key=lambda r: int(r.get("index") or 0))
        ):
            idx = int(ep.get("index") or 0) or pos + 1
            to_raw = (ep.get("to") or "").strip()
            if not to_raw or not is_address(to_raw):
                raise ConfigError(f"执行计划第 {idx} 笔目标地址无效")
            dest = checksum(to_raw)
            amount_wei = int(ep.get("amount_wei") or 0)
            skip = dest.lower() == source_l or amount_wei <= 0
            items.append(
                {
                    "index": idx,
                    "to": dest,
                    "amount_wei": amount_wei,
                    "amount_native": wei_to_native_str(amount_wei),
                    "status": "SKIP" if skip else "READY",
                    "note": "不能与源地址相同" if dest.lower() == source_l else None,
                }
            )
        if not items:
            raise ConfigError("execute_plan 为空")
        return items

    targets = parse_target_addresses(request.targets)
    items = []
    for pos, dest in enumerate(targets):
        idx = pos + 1
        if dest.lower() == source_l:
            items.append(
                {
                    "index": idx,
                    "to": dest,
                    "amount_wei": 0,
                    "amount_native": "0",
                    "status": "SKIP",
                    "note": "不能与源地址相同",
                }
            )
            continue
        value_wei = random_native_wei(request.amount_min, request.amount_max)
        items.append(
            {
                "index": idx,
                "to": dest,
                "amount_wei": value_wei,
                "amount_native": wei_to_native_str(value_wei),
                "status": "READY",
            }
        )
    return items


async def run_distribute(
    request: TreasuryDistributeRequest,
    *,
    cancel: asyncio.Event | None = None,
    event_sink: list[str] | None = None,
) -> dict[str, Any]:
    _validate_amount_range(request.amount_min, request.amount_max)
    source = resolve_source_wallet(request.source_private_key)
    if request.execute_plan and (request.preview_source_address or "").strip():
        _assert_preview_source(request, source.address)
    chain = get_chain(request.chain_id)
    items = _build_run_items_for_execute(request, source.address)
    ready_count = sum(1 for row in items if row.get("status") == "READY")
    if ready_count <= 0:
        raise ConfigError("没有可执行的转出（检查目标地址与金额）")
    pool = await open_pool(request.chain_id, request.rpc_urls)
    events: list[str] = []
    results: list[dict[str, Any]] = []
    meta = {
        "mode": "distribute",
        "chain_id": chain.chain_id,
        "chain_name": chain.name,
        "native_symbol": chain.native_symbol,
        "source_address": source.address,
        "items": items,
        "ready_count": ready_count,
        "total_count": len(items),
    }
    try:
        treasury_logger.info(
            "distribute run start chain_id=%s source=%s ready=%s plan_locked=%s",
            request.chain_id,
            source.address,
            ready_count,
            bool(request.execute_plan),
        )
        nonce = await pool.get_nonce(source.address, "pending")
        first = True
        cancelled = False
        aborted = False
        for row in items:
            if cancel and cancel.is_set():
                emit_run_event(events, event_sink, "执行已取消")
                cancelled = True
                break
            if row["status"] != "READY":
                results.append({**row, "tx_hash": None, "run_status": row["status"]})
                continue
            if not first:
                delay = await sleep_gap(request.gap_min_sec, request.gap_max_sec)
                emit_run_event(events, event_sink, f"间隔 {delay:.2f}s")
                if cancel and cancel.is_set():
                    emit_run_event(events, event_sink, "执行已取消")
                    cancelled = True
                    break
            first = False
            value_wei = int(row.get("amount_wei") or 0)
            if value_wei <= 0:
                results.append({**row, "run_status": "SKIP", "error": "amount is zero"})
                continue
            outcome = await send_native_transfer(
                pool,
                source,
                row["to"],
                value_wei,
                request.gas,
                nonce=nonce,
                skip_balance_check=True,
            )
            run_status = outcome.get("status") or "FAILED"
            item = {
                **row,
                "amount_wei": value_wei,
                "amount_native": wei_to_native_str(value_wei),
                "tx_hash": outcome.get("tx_hash"),
                "run_status": run_status,
                "error": outcome.get("error"),
                "fee_wei": outcome.get("fee_wei", row.get("fee_wei")),
            }
            results.append(item)
            emit_run_event(
                events,
                event_sink,
                f"→ {row['to'][:10]}… {wei_to_native_str(value_wei)} {meta['native_symbol']} {run_status}",
            )
            if run_status in {"SUCCESS", "BROADCAST", "REVERTED", "TIMEOUT"}:
                nonce += 1
            if run_status in {"INSUFFICIENT_BALANCE", "SEND_FAILED"}:
                aborted = True
                break
        if cancelled:
            _append_unprocessed_results(items, results, pending_ready_status="CANCELLED")
        elif aborted:
            _append_unprocessed_results(items, results, pending_ready_status="ABORTED")
        results.sort(key=lambda r: int(r.get("index") or 0))
        success = sum(1 for r in results if r.get("run_status") == "SUCCESS")
        cancelled_count = sum(1 for r in results if r.get("run_status") == "CANCELLED")
        aborted_count = sum(1 for r in results if r.get("run_status") == "ABORTED")
        _non_failure = {"SUCCESS", "SKIP", "READY", "CANCELLED", "ABORTED", None}
        failed = sum(
            1
            for r in results
            if r.get("run_status") not in _non_failure and r.get("run_status") is not None
        )
        treasury_logger.info(
            "distribute run done success=%s failed=%s cancelled=%s aborted=%s",
            success,
            failed,
            cancelled_count,
            aborted_count,
        )
        return {
            **meta,
            "results": results,
            "success": success,
            "failed": failed,
            "cancelled": cancelled_count,
            "aborted": aborted_count,
            "events": events,
        }
    finally:
        await pool.aclose()
