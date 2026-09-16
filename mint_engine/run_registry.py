from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mint_engine.core.exceptions import RunCancelled
from mint_engine.core.models import RunConfig, TreasuryCollectRequest, TreasuryDistributeRequest
from mint_engine.controller import MintController
from mint_engine.discovery.opensea import prepare_config

_log = logging.getLogger("mint_engine.run")

HEARTBEAT_STALE_SEC = 12.0
WATCHDOG_INTERVAL_SEC = 3.0
MAX_RUN_RECORDS = 200


@dataclass
class RunRecord:
    run_id: str
    client_id: str
    kind: str = "mint"
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "running"  # running | completed | cancelled | failed
    task: asyncio.Task[Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    last_heartbeat: float = field(default_factory=time.time)
    finished_at: float | None = None
    controller: MintController | None = None
    cancel_reason: str | None = None
    live_events: list[str] = field(default_factory=list)


_runs: dict[str, RunRecord] = {}
_client_run: dict[str, str] = {}
_lock = asyncio.Lock()


def _now() -> float:
    return time.time()


def _trim_old_runs() -> None:
    if len(_runs) <= MAX_RUN_RECORDS:
        return
    finished = [
        (run_id, record)
        for run_id, record in _runs.items()
        if record.status != "running"
    ]
    finished.sort(key=lambda pair: pair[1].finished_at or 0)
    overflow = len(_runs) - MAX_RUN_RECORDS
    for run_id, _ in finished[:overflow]:
        rec = _runs.pop(run_id, None)
        if rec and _client_run.get(rec.client_id) == run_id:
            _client_run.pop(rec.client_id, None)


async def start_watchdog() -> None:
    global _watchdog_task
    if _watchdog_task and not _watchdog_task.done():
        return

    async def loop() -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SEC)
            stale = [
                row
                for row in list(_runs.values())
                if row.status == "running" and _now() - row.last_heartbeat > HEARTBEAT_STALE_SEC
            ]
            for row in stale:
                await cancel_run(row.run_id, reason="heartbeat timeout")

    _watchdog_task = asyncio.create_task(loop())


_watchdog_task: asyncio.Task[Any] | None = None


async def stop_watchdog() -> None:
    global _watchdog_task
    if _watchdog_task:
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass
        _watchdog_task = None


def get_run(run_id: str) -> RunRecord | None:
    return _runs.get(run_id)


def snapshot_run(record: RunRecord) -> dict[str, Any]:
    if record.controller:
        events = list(record.controller.events)
    elif record.live_events:
        events = list(record.live_events)
    elif isinstance(record.result, dict):
        events = list(record.result.get("events") or [])
    else:
        events = []
    payload: dict[str, Any] = {
        "run_id": record.run_id,
        "client_id": record.client_id,
        "kind": record.kind,
        "status": record.status,
        "cancel_reason": record.cancel_reason,
        "events": events,
    }
    if record.result is not None:
        payload["result"] = record.result
        if record.kind == "mint":
            payload["success"] = record.result.get("success")
            payload["skipped"] = record.result.get("skipped")
            payload["prepared"] = record.result.get("prepared")
            payload["elapsed_sec"] = record.result.get("elapsed_sec")
            payload["results"] = record.result.get("results")
            payload["report"] = record.result.get("report")
        else:
            payload["success"] = record.result.get("success")
            payload["failed"] = record.result.get("failed")
    if record.error:
        payload["error"] = record.error
    return payload


async def _cancel_record(record: RunRecord, *, reason: str) -> None:
    if record.status != "running":
        return
    record.cancel_reason = reason
    record.cancel_event.set()


async def cancel_run(run_id: str, *, reason: str = "cancelled") -> bool:
    record = _runs.get(run_id)
    if not record:
        return False
    async with _lock:
        await _cancel_record(record, reason=reason)
    return True


async def cancel_client_runs(client_id: str, *, reason: str = "superseded") -> None:
    if not client_id:
        return
    run_id = _client_run.get(client_id)
    if run_id:
        await cancel_run(run_id, reason=reason)


async def heartbeat(run_id: str, client_id: str) -> bool:
    record = _runs.get(run_id)
    if not record or record.client_id != client_id:
        return False
    if record.status != "running":
        return False
    record.last_heartbeat = _now()
    return True


async def _start_run(
    client_id: str,
    kind: str,
    runner: Callable[[RunRecord], Awaitable[dict[str, Any] | None]],
) -> str:
    if not client_id or len(client_id) < 8:
        raise ValueError("client_id is required")
    async with _lock:
        await cancel_client_runs(client_id, reason="new run started")
        run_id = uuid.uuid4().hex
        record = RunRecord(run_id=run_id, client_id=client_id, kind=kind, last_heartbeat=_now())
        _runs[run_id] = record
        _client_run[client_id] = run_id

        async def task_wrapper() -> None:
            try:
                outcome = await runner(record)
                if record.cancel_event.is_set():
                    record.status = "cancelled"
                else:
                    record.status = "completed"
                if outcome is not None:
                    record.result = outcome
            except RunCancelled:
                record.status = "cancelled"
            except asyncio.CancelledError:
                record.status = "cancelled"
            except Exception as exc:
                record.status = "failed"
                record.error = str(exc)
                _log.exception("%s run %s failed", kind, run_id)
            finally:
                record.controller = None
                record.finished_at = _now()
                if _client_run.get(client_id) == run_id and record.status != "running":
                    _client_run.pop(client_id, None)
                _trim_old_runs()

        record.task = asyncio.create_task(task_wrapper())
        return run_id


async def start_mint_run(config: RunConfig, client_id: str) -> str:
    async def runner(record: RunRecord) -> dict[str, Any]:
        resolved, preview = await prepare_config(config)
        controller = MintController(resolved, preview)
        record.controller = controller
        try:
            result = await controller.mint(cancel=record.cancel_event)
            if record.cancel_event.is_set():
                result = controller.build_cancel_snapshot()
            return result
        except RunCancelled:
            return controller.build_cancel_snapshot()
        finally:
            await controller.aclose()

    return await _start_run(client_id, "mint", runner)


async def start_treasury_distribute_run(request: TreasuryDistributeRequest, client_id: str) -> str:
    from mint_engine.treasury.distribute import run_distribute

    async def runner(record: RunRecord) -> dict[str, Any]:
        return await run_distribute(
            request,
            cancel=record.cancel_event,
            event_sink=record.live_events,
        )

    return await _start_run(client_id, "treasury_distribute", runner)


async def start_treasury_collect_run(request: TreasuryCollectRequest, client_id: str) -> str:
    from mint_engine.treasury.collect import run_collect

    async def runner(record: RunRecord) -> dict[str, Any]:
        return await run_collect(
            request,
            cancel=record.cancel_event,
            event_sink=record.live_events,
        )

    return await _start_run(client_id, "treasury_collect", runner)


def active_run_for_client(client_id: str) -> RunRecord | None:
    run_id = _client_run.get(client_id)
    if not run_id:
        return None
    record = _runs.get(run_id)
    if record and record.status == "running":
        return record
    return None
