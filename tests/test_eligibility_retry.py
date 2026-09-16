from mint_engine.discovery.opensea_http import loads_json
from mint_engine.discovery.opensea_mint import (
    _mint_error_snippet,
    mint_errors_indicate_drop_fully_sold_out,
    mint_errors_indicate_sold_out,
)
from mint_engine.discovery.opensea_stages import (
    eligibility_retry_window_open,
    hot_path_active,
    sync_stage_times_in_sequence,
)


def test_eligibility_retry_window():
    stage = {"uuid": "a", "start_time": 1000, "label": "FCFS"}
    assert not eligibility_retry_window_open(stage, 999, 120)
    assert eligibility_retry_window_open(stage, 1000, 120)
    assert eligibility_retry_window_open(stage, 1119, 120)
    assert not eligibility_retry_window_open(stage, 1120, 120)


def test_advance_stage_clears_presigned():
    from mint_engine.chase import ChaseContext, ChaseWallet

    class _W:
        label = "w"
        address = "0x0"

    ctx = ChaseContext(sequence=[{"uuid": "1"}, {"uuid": "2"}], wallets=[])
    cw = ChaseWallet(wallet=_W(), stage_index=0, presigned={"raw": "0x"})
    ctx.wallets.append(cw)
    assert ctx.advance_stage(cw)
    assert cw.presigned is None
    assert cw.stage_index == 1


def test_hot_path_active_matches_retry_window():
    stage = {"uuid": "a", "start_time": 1000, "label": "FCFS"}
    assert not hot_path_active(stage, 999, 120)
    assert hot_path_active(stage, 1000, 120)
    assert hot_path_active(stage, 1119, 120)
    assert not hot_path_active(stage, 1120, 120)
    assert not hot_path_active(stage, 1000, 120, enabled=False)


def test_sync_stage_times_by_uuid():
    sequence = [{"uuid": "u1", "label": "A", "start_time": 100, "end_time": 200}]
    fresh = [{"uuid": "u1", "label": "A", "start_time": 500, "end_time": 600}]
    changes = sync_stage_times_in_sequence(sequence, fresh)
    assert changes == [("A", 100, 500)]
    assert sequence[0]["start_time"] == 500


def test_loads_json_bytes():
    assert loads_json(b'{"a":1}') == {"a": 1}
    assert loads_json(b"") == {}


def test_mint_error_snippet_from_errors_list():
    body = {"errors": ["Wallet not eligible", "Drop is fully minted out"]}
    assert "Wallet not eligible" in _mint_error_snippet(body, "")
    assert "fully minted out" in _mint_error_snippet(body, "")


def test_mint_errors_sold_out():
    assert mint_errors_indicate_sold_out("This drop is sold out")
    assert not mint_errors_indicate_sold_out("Wallet not eligible")


def test_drop_fully_sold_out_message():
    assert mint_errors_indicate_drop_fully_sold_out("Drop is fully minted out")
    assert not mint_errors_indicate_drop_fully_sold_out("Wallet not eligible")
