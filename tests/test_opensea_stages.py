import unittest

from mint_engine.core.models import SaleStatus
from mint_engine.discovery.opensea_stages import (
    build_stage_sequence,
    drop_has_future_mint_window,
    pick_auto_stage,
    resolve_drop_stage,
    sale_from_stage,
    stage_group_key,
    stage_status,
    use_chain_public_mint,
)


def _stage(label, stage_type, start, end, uuid="u1", price="1000"):
    return {
        "uuid": uuid,
        "label": label,
        "stage_type": stage_type,
        "start_time": start,
        "end_time": end,
        "price": price,
        "max_per_wallet": "1",
    }


class TestOpenSeaStages(unittest.TestCase):
    def test_auto_prefers_active_presale(self):
        stages = [
            _stage("PUBLIC", "public_sale", 1000, 2000, uuid="pub"),
            _stage("GTD", "signed_presale", 100, 500, uuid="gtd"),
        ]
        picked = pick_auto_stage(stages, 200)
        self.assertEqual(picked["uuid"], "gtd")
        self.assertFalse(use_chain_public_mint(picked))

    def test_auto_public_sale_when_active(self):
        stages = [
            _stage("PUBLIC", "public_sale", "2026-09-14T16:00:00Z", "2026-09-14T17:00:00Z", uuid="pub"),
        ]
        picked = resolve_drop_stage(stages, 1789400000)
        self.assertTrue(use_chain_public_mint(picked))

    def test_sale_from_stage_active(self):
        stage = _stage("GTD", "signed_presale", 100, 200, price="3900000000000000")
        sale = sale_from_stage(stage, 150)
        self.assertEqual(sale.status, SaleStatus.ACTIVE)
        self.assertEqual(sale.price, 3900000000000000)

    def test_auto_active_presale_picks_latest_start(self):
        stages = [
            _stage("GTD", "signed_presale", 100, 600, uuid="gtd"),
            _stage("FCFS", "signed_presale", 500, 900, uuid="fcfs"),
        ]
        picked = pick_auto_stage(stages, 550)
        self.assertEqual(picked["uuid"], "fcfs")

    def test_auto_skips_team_for_upcoming(self):
        stages = [
            _stage("TEAM", "team", 50, 80, uuid="team"),
            _stage("GTD", "signed_presale", 100, 200, uuid="gtd"),
        ]
        picked = pick_auto_stage(stages, 10)
        self.assertEqual(picked["uuid"], "gtd")

    def test_auto_uses_next_stage_hint(self):
        stages = [
            _stage("TEAM", "team", 50, 80, uuid="team"),
            _stage("GTD", "signed_presale", 100, 200, uuid="gtd"),
        ]
        picked = pick_auto_stage(stages, 60, next_stage={"uuid": "gtd", "start_time": 100})
        self.assertEqual(picked["uuid"], "gtd")

    def test_next_stage_does_not_skip_earlier_upcoming(self):
        stages = [
            _stage("GTD", "signed_presale", 100, 200, uuid="gtd"),
            _stage("FCFS", "signed_presale", 500, 900, uuid="fcfs"),
        ]
        picked = pick_auto_stage(stages, 10, next_stage={"uuid": "fcfs", "start_time": 500})
        self.assertEqual(picked["uuid"], "gtd")

    def test_build_stage_sequence_sorted(self):
        stages = [
            _stage("FCFS", "signed_presale", 500, 900, uuid="fcfs"),
            _stage("GTD", "signed_presale", 100, 200, uuid="gtd"),
        ]
        seq = build_stage_sequence(stages)
        self.assertEqual([s["uuid"] for s in seq], ["gtd", "fcfs"])

    def test_drop_has_future_mint_window(self):
        stages = [
            _stage("GTD", "signed_presale", 100, 200, uuid="gtd"),
            _stage("PUBLIC", "public_sale", 500, 900, uuid="pub"),
        ]
        self.assertTrue(drop_has_future_mint_window(stages, 50))
        self.assertFalse(drop_has_future_mint_window(stages, 950))

    def test_stage_group_key_stable(self):
        a = _stage("GTD", "signed_presale", 100, 200, uuid="gtd")
        b = _stage("GTD", "signed_presale", 100, 200, uuid="gtd")
        self.assertEqual(stage_group_key(a), stage_group_key(b))

    def test_auto_overlap_prefers_later_start_public(self):
        stages = [
            _stage("FCFS", "signed_presale", 500, 1000, uuid="fcfs"),
            _stage("PUBLIC", "public_sale", 900, 1200, uuid="pub"),
        ]
        picked = pick_auto_stage(stages, 950)
        self.assertEqual(picked["uuid"], "pub")
        self.assertTrue(use_chain_public_mint(picked))


if __name__ == "__main__":
    unittest.main()
