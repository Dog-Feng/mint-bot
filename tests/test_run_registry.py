import asyncio

import pytest

from mint_engine.run_registry import (
    RunRecord,
    _client_run,
    _runs,
    cancel_client_runs,
    cancel_run,
    heartbeat,
    snapshot_run,
)


@pytest.fixture(autouse=True)
def clear_registry():
    _runs.clear()
    _client_run.clear()
    yield
    _runs.clear()
    _client_run.clear()


@pytest.mark.asyncio
async def test_cancel_run_sets_event():
    ev = asyncio.Event()
    rec = RunRecord(run_id="run-a", client_id="client-11111111", cancel_event=ev, status="running")
    _runs["run-a"] = rec
    await cancel_run("run-a", reason="test")
    assert ev.is_set()
    assert rec.cancel_reason == "test"


@pytest.mark.asyncio
async def test_cancel_client_runs():
    ev = asyncio.Event()
    rec = RunRecord(run_id="run-b", client_id="client-22222222", cancel_event=ev, status="running")
    _runs["run-b"] = rec
    _client_run["client-22222222"] = "run-b"
    await cancel_client_runs("client-22222222", reason="superseded")
    assert ev.is_set()


def test_snapshot_prefers_live_events_for_treasury():
    rec = RunRecord(
        run_id="run-live",
        client_id="client-44444444",
        kind="treasury_distribute",
        status="running",
        live_events=["间隔 1.00s", "→ 0x… SUCCESS"],
    )
    snap = snapshot_run(rec)
    assert snap["events"] == rec.live_events


@pytest.mark.asyncio
async def test_heartbeat_only_when_running():
    ev = asyncio.Event()
    rec = RunRecord(run_id="run-c", client_id="client-33333333", cancel_event=ev, status="running")
    _runs["run-c"] = rec
    assert await heartbeat("run-c", "client-33333333") is True
    rec.status = "completed"
    assert await heartbeat("run-c", "client-33333333") is False
