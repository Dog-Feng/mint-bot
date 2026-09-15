from mint_engine.discovery.opensea_mint import mint_errors_indicate_sold_out
from mint_engine.discovery.opensea_stages import (
    eligibility_retry_window_open,
    sync_stage_times_in_sequence,
)


def test_eligibility_retry_window():
    stage = {"uuid": "a", "start_time": 1000, "label": "FCFS"}
    assert not eligibility_retry_window_open(stage, 999, 120)
    assert eligibility_retry_window_open(stage, 1000, 120)
    assert eligibility_retry_window_open(stage, 1119, 120)
    assert not eligibility_retry_window_open(stage, 1120, 120)


def test_sync_stage_times_by_uuid():
    sequence = [{"uuid": "u1", "label": "A", "start_time": 100, "end_time": 200}]
    fresh = [{"uuid": "u1", "label": "A", "start_time": 500, "end_time": 600}]
    changes = sync_stage_times_in_sequence(sequence, fresh)
    assert changes == [("A", 100, 500)]
    assert sequence[0]["start_time"] == 500


def test_mint_errors_sold_out():
    assert mint_errors_indicate_sold_out("This drop is sold out")
    assert not mint_errors_indicate_sold_out("Wallet not eligible")
