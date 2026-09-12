from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from mint_engine.core.exceptions import RpcError
from mint_engine.core.models import ProbeResult


class RpcPool:
    def __init__(
        self,
        urls: list[str],
        expected_chain_id: int,
        timeout_ms: int = 1500,
        primary: str | None = None,
        selection: str = "auto",
    ):
        self.expected_chain_id = expected_chain_id
        self.timeout_ms = timeout_ms
        self.selection = selection
        self.forced_primary = primary
        self.urls = [u.strip() for u in urls if u and u.strip()]
        if not self.urls:
            raise RpcError("no RPC urls provided")
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_ms / 1000, connect=min(1.2, timeout_ms / 1000)),
            headers={"User-Agent": "mint-engine/0.1"},
        )
        self.results: list[ProbeResult] = []
        self.primary_url: str | None = None
        self.broadcast_urls: list[str] = []

    async def aclose(self) -> None:
        await self.client.aclose()

    async def probe(self) -> list[ProbeResult]:
        results = list(await asyncio.gather(*(self._probe_one(url) for url in self.urls)))
        healthy = [r for r in results if r.status in {"HEALTHY", "DEGRADED"}]
        healthy.sort(key=lambda r: r.score, reverse=True)
        if self.selection == "manual" and self.forced_primary:
            match = next((r for r in healthy if r.url == self.forced_primary), None)
            if match:
                match.role = "主"
                self.primary_url = match.url
            elif healthy:
                healthy[0].role = "主"
                self.primary_url = healthy[0].url
        elif healthy:
            healthy[0].role = "主"
            self.primary_url = healthy[0].url
        for item in healthy[1:]:
            item.role = "广播备份" if item.status == "HEALTHY" else "仅备份"
        self.broadcast_urls = [r.url for r in healthy]
        self.results = results
        if not self.primary_url:
            raise RpcError("no healthy RPC matched the selected chain")
        return results

    async def _probe_one(self, url: str) -> ProbeResult:
        started = time.perf_counter()
        try:
            chain_hex = await self._call_url(url, "eth_chainId", [])
            block_hex = await self._call_url(url, "eth_blockNumber", [])
            latency = int((time.perf_counter() - started) * 1000)
            chain_id = int(chain_hex, 16)
            block_number = int(block_hex, 16)
            if chain_id != self.expected_chain_id:
                return ProbeResult(
                    url=url,
                    latency_ms=latency,
                    block_number=block_number,
                    chain_id=chain_id,
                    status="CHAIN_MISMATCH",
                    role="禁用",
                    error=f"chain_id {chain_id} != {self.expected_chain_id}",
                )
            status = "HEALTHY" if latency <= 800 else "DEGRADED"
            latency_score = max(0.0, 1 - latency / 2000)
            return ProbeResult(
                url=url,
                latency_ms=latency,
                block_number=block_number,
                chain_id=chain_id,
                status=status,
                score=round(0.75 * latency_score + 0.25, 4),
            )
        except Exception as exc:
            return ProbeResult(
                url=url,
                status="UNHEALTHY",
                role="禁用",
                error=str(exc),
            )

    async def call(self, method: str, params: list[Any], url: str | None = None) -> Any:
        target = url or self.primary_url
        if not target:
            raise RpcError("RPC pool has not been probed")
        errors = []
        candidates = [target] + [u for u in self.broadcast_urls if u != target]
        for candidate in candidates:
            for attempt in range(3):
                try:
                    return await self._call_url(candidate, method, params)
                except Exception as exc:
                    errors.append(f"{candidate}: {exc}")
                    await asyncio.sleep(0.25 * (attempt + 1))
            if url is not None:
                break
        raise RpcError(f"{method} failed: {errors[-1] if errors else 'unknown'}")

    async def eth_call(self, tx: dict[str, Any], block: str = "latest") -> str:
        return await self.call("eth_call", [tx, block])

    async def estimate_gas(self, tx: dict[str, Any]) -> int:
        value = await self.call("eth_estimateGas", [tx])
        return int(value, 16) if isinstance(value, str) else int(value)

    async def get_code(self, address: str) -> str:
        return await self.call("eth_getCode", [address, "latest"])

    async def get_storage(self, address: str, slot: str) -> str:
        return await self.call("eth_getStorageAt", [address, slot, "latest"])

    async def get_balance(self, address: str) -> int:
        value = await self.call("eth_getBalance", [address, "latest"])
        return int(value, 16)

    async def get_nonce(self, address: str, tag: str = "pending") -> int:
        value = await self.call("eth_getTransactionCount", [address, tag])
        return int(value, 16)

    async def get_block_number(self) -> int:
        return int(await self.call("eth_blockNumber", []), 16)

    async def get_block_timestamp(self) -> int:
        block = await self.call("eth_getBlockByNumber", ["latest", False])
        return int(block["timestamp"], 16)

    async def fee_hint(self) -> tuple[int, int]:
        try:
            history = await self.call("eth_feeHistory", [1, "latest", [50]])
            base = int(history["baseFeePerGas"][-1], 16)
            rewards = history.get("reward") or []
            tip = int(rewards[0][0], 16) if rewards and rewards[0] else 1_000_000
            return base, max(tip, 1)
        except Exception:
            price = int(await self.call("eth_gasPrice", []), 16)
            return price, max(price // 10, 1)

    async def broadcast(self, raw_tx: str) -> list[dict[str, Any]]:
        urls = [u for u in (self.broadcast_urls or [self.primary_url]) if u]

        async def one(url: str) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                tx_hash = await self._call_url(url, "eth_sendRawTransaction", [raw_tx])
                return {
                    "url": url,
                    "ok": True,
                    "tx_hash": tx_hash,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }
            except Exception as exc:
                return {
                    "url": url,
                    "ok": False,
                    "error": str(exc),
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }

        return list(await asyncio.gather(*(one(url) for url in urls)))

    async def get_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        receipt = await self.call("eth_getTransactionReceipt", [tx_hash])
        return receipt

    async def wait_receipt(self, tx_hash: str, timeout: float = 180) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        urls = [u for u in (self.broadcast_urls or [self.primary_url]) if u]
        if not urls:
            return None
        while time.time() < deadline:
            results = await asyncio.gather(
                *(self._call_url(url, "eth_getTransactionReceipt", [tx_hash]) for url in urls),
                return_exceptions=True,
            )
            for receipt in results:
                if isinstance(receipt, Exception) or not receipt:
                    continue
                return receipt
            await asyncio.sleep(0.25)
        return None

    async def _call_url(self, url: str, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        response = await self.client.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise RpcError(str(body["error"]))
        return body["result"]
