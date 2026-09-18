from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mint_engine.discovery.opensea_stages import (
    build_stage_sequence,
    stage_bounds,
    stage_index_in_sequence,
    stage_window_open,
    use_chain_public_mint,
)


@dataclass
class ChaseWallet:
    wallet: Any
    stage_index: int = 0
    done: bool = False
    result: dict[str, Any] | None = None
    chase_status: str = "pending"
    last_probe_at: float = 0.0
    presigned: dict[str, Any] | None = None
    opensea_probe_cached: Any | None = None
    opensea_probe_cached_at: float = 0.0

    @property
    def label(self) -> str:
        return self.wallet.label

    @property
    def address(self) -> str:
        return self.wallet.address

    def finish(self, result: dict[str, Any], status: str) -> None:
        self.result = result
        self.chase_status = status
        self.done = True


@dataclass
class ChaseContext:
    sequence: list[dict[str, Any]] = field(default_factory=list)
    wallets: list[ChaseWallet] = field(default_factory=list)
    blind_gas_limit: int | None = None

    @property
    def active_wallets(self) -> list[ChaseWallet]:
        return [w for w in self.wallets if not w.done]

    def advance_stage(self, wallet: ChaseWallet) -> bool:
        """Move to next stage. Returns False if no more stages."""
        wallet.presigned = None
        wallet.opensea_probe_cached = None
        wallet.opensea_probe_cached_at = 0.0
        self.blind_gas_limit = None
        wallet.stage_index += 1
        if wallet.stage_index >= len(self.sequence):
            return False
        return True

    def current_stage(self, wallet: ChaseWallet) -> dict[str, Any] | None:
        if not self.sequence or wallet.stage_index >= len(self.sequence):
            return None
        return self.sequence[wallet.stage_index]


def init_chase_wallets(signers, sequence: list[dict[str, Any]], auto_stage: dict[str, Any] | None) -> ChaseContext:
    start = stage_index_in_sequence(sequence, auto_stage) if sequence else 0
    rows = [ChaseWallet(wallet=w, stage_index=start) for w in signers]
    return ChaseContext(sequence=sequence, wallets=rows)


def is_terminal_chase_stage(
    ctx: ChaseContext,
    chase_wallet: ChaseWallet,
    stage: dict[str, Any] | None,
) -> bool:
    """No further stage to try after ineligible / stage-local sold out."""
    if use_chain_public_mint(stage):
        return True
    if not ctx.sequence:
        return True
    return chase_wallet.stage_index >= len(ctx.sequence) - 1


def is_public_final_stage(stage: dict[str, Any] | None) -> bool:
    """Deprecated alias; prefer is_terminal_chase_stage with context."""
    return use_chain_public_mint(stage)
