from mint_engine.chase import ChaseContext, ChaseWallet, is_terminal_chase_stage
from mint_engine.discovery.opensea_stages import strictest_max_per_wallet


def _stage(label, stage_type, max_wallet, uuid):
    return {
        "uuid": uuid,
        "label": label,
        "stage_type": stage_type,
        "start_time": 100,
        "end_time": 200,
        "max_per_wallet": str(max_wallet),
    }


class _W:
    def __init__(self, name: str):
        self.label = name
        self.address = "0x" + "1" * 40


def test_strictest_max_per_wallet():
    stages = [
        _stage("GTD", "signed_presale", 10, "gtd"),
        _stage("FCFS", "signed_presale", 1, "fcfs"),
    ]
    assert strictest_max_per_wallet(stages) == 1


def test_terminal_chase_stage_last_presale():
    seq = [
        {"uuid": "a", "stage_type": "signed_presale"},
        {"uuid": "b", "stage_type": "signed_presale"},
    ]
    ctx = ChaseContext(sequence=seq, wallets=[])
    cw = ChaseWallet(wallet=_W("w"), stage_index=1)
    assert is_terminal_chase_stage(ctx, cw, seq[1]) is True
    cw.stage_index = 0
    assert is_terminal_chase_stage(ctx, cw, seq[0]) is False


def test_terminal_chase_stage_public():
    seq = [{"uuid": "p", "stage_type": "public_sale"}]
    ctx = ChaseContext(sequence=seq, wallets=[])
    cw = ChaseWallet(wallet=_W("w"), stage_index=0)
    assert is_terminal_chase_stage(ctx, cw, seq[0]) is True
