from __future__ import annotations

from typing import Any

from mint_engine.core.models import Gap, MintMethod, SaleState
from mint_engine.rpc.pool import RpcPool


class Detection:
    def __init__(
        self,
        supported: bool,
        confidence: float,
        method: MintMethod | None = None,
        protocol: str | None = None,
        notes: list[str] | None = None,
    ):
        self.supported = supported
        self.confidence = confidence
        self.method = method
        self.protocol = protocol
        self.notes = notes or []


class MintAdapter:
    name = "base"

    async def detect(self, context: dict[str, Any]) -> Detection:
        raise NotImplementedError

    async def read_sale_state(self, pool: RpcPool, context: dict[str, Any]) -> SaleState:
        raise NotImplementedError

    def gaps(self, context: dict[str, Any], sale: SaleState) -> list[Gap]:
        return []

    def build_call(self, context: dict[str, Any], sale: SaleState, recipient: str) -> tuple[str, str, int]:
        raise NotImplementedError
