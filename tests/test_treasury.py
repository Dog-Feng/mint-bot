from mint_engine.core.exceptions import ConfigError
import pytest

from mint_engine.core.models import GasConfig, TreasuryDistributeRequest
from mint_engine.treasury.distribute import (
    _append_unprocessed_results,
    _assert_preview_source,
    _merge_execute_plan,
    _validate_amount_range,
)
from mint_engine.treasury.helpers import parse_distribute_source_key, parse_private_key_lines, random_native_wei


def test_parse_private_key_lines_and_distribute_source():
    text = "0xaaa\n\n# comment\n0xbbb\n"
    assert parse_private_key_lines(text) == ["0xaaa", "0xbbb"]
    assert parse_distribute_source_key(text) == "0xaaa"


def test_random_native_wei_in_range():
    for _ in range(50):
        wei = random_native_wei("0.001", "0.002")
        assert 10**15 <= wei <= 2 * 10**15


def test_validate_amount_range():
    _validate_amount_range("0.1", "0.2")
    with pytest.raises(ConfigError):
        _validate_amount_range("0.3", "0.1")


def test_append_unprocessed_results_abort_and_cancel():
    preview = [
        {"index": 1, "to": "0x2", "status": "READY", "amount_wei": 1},
        {"index": 2, "to": "0x3", "status": "INSUFFICIENT_BALANCE", "amount_wei": 2},
        {"index": 3, "to": "0x4", "status": "READY", "amount_wei": 3},
    ]
    results = [{"index": 1, "run_status": "SUCCESS", "to": "0x2"}]
    _append_unprocessed_results(preview, results, pending_ready_status="ABORTED")
    by_idx = {r["index"]: r for r in results}
    assert by_idx[2]["run_status"] == "INSUFFICIENT_BALANCE"
    assert by_idx[3]["run_status"] == "ABORTED"
    assert len(results) == 3


def test_assert_preview_source():
    addr = "0x00000000000000000000000000000000000000Aa"
    base = dict(
        chain_id=1,
        source_private_key="0x" + "1" * 64,
        amount_min="0.001",
        amount_max="0.002",
        execute_plan=[{"index": 1, "to": "0x0000000000000000000000000000000000000002", "amount_wei": 1}],
        gas=GasConfig(),
    )
    with pytest.raises(ConfigError):
        _assert_preview_source(TreasuryDistributeRequest(**base), "0x00000000000000000000000000000000000000Bb")
    with pytest.raises(ConfigError):
        _assert_preview_source(TreasuryDistributeRequest(**base), addr)
    req = TreasuryDistributeRequest(**base, preview_source_address=addr)
    _assert_preview_source(req, addr)


def test_merge_execute_plan():
    preview = {
        "items": [
            {
                "index": 1,
                "to": "0x0000000000000000000000000000000000000001",
                "amount_wei": 100,
                "status": "READY",
            },
            {
                "index": 2,
                "to": "0x0000000000000000000000000000000000000002",
                "amount_wei": 200,
                "status": "READY",
            },
        ]
    }
    plan = [
        {
            "index": 1,
            "to": "0x0000000000000000000000000000000000000001",
            "amount_wei": 999,
        },
    ]
    merged = _merge_execute_plan(preview, plan)
    assert merged["items"][0]["amount_wei"] == 999
    assert merged["items"][1]["amount_wei"] == 200
