from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from mint_engine.core.exceptions import RpcError, is_execution_reverted, is_rate_limited
from mint_engine.core.models import ProbeResult


def short_rpc_url(url: str) -> str:
    parsed = urlparse(url or "")
    return parsed.netloc or (url or "")[:48]


def _rpc_error_payload(error: Any) -> tuple[str | None, Any]:
    if not isinstance(error, dict):
        return None, None
    message = error.get("message")
    data = error.get("data")
    for _ in range(4):
        if isinstance(data, dict):
            message = message or data.get("message")
            data = data.get("data") or data.get("reason") or data.get("message")
            continue
        break
    return (str(message) if message else None), data


def assign_shard_urls(urls: list[str], count: int, concurrency: int) -> list[str]:
    """Map each wallet index to an RPC. Fastest nodes fill first.

    Groups of `concurrency` wallets share one node. Extra groups wrap to the
    same nodes (queued by a per-node semaphore). Unused slower nodes stay idle
    when a single wave fits on fewer nodes.
    """
    if count <= 0:
        return []
    ranked = [u for u in urls if u]
    if not ranked:
        raise RpcError("no RPC urls for shard assignment")
    conc = max(1, int(concurrency))
    n_chunks = (count + conc - 1) // conc
    n_shards = min(len(ranked), max(1, n_chunks))
    shards = ranked[:n_shards]
    assigned: list[str] = []
    for index in range(count):
        assigned.append(shards[(index // conc) % n_shards])
    return assigned


class RpcPool:
    def __init__(
        self,
        urls: list[str],
        expected_chain_id: int,
        timeout_ms: int = 1500,
        primary: str | None = None,
        selection: str = "auto",
        broadcast_backups: bool = False,
    ):
        self.expected_chain_id = expected_chain_id
        self.timeout_ms = timeout_ms
        self.selection = selection
        self.forced_primary = primary
        self.broadcast_backups = broadcast_backups
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
        self.healthy_urls: list[str] = []

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
        self.healthy_urls = [r.url for r in healthy]
        for offset, item in enumerate(healthy[1:], start=2):
            if self.broadcast_backups:
                item.role = "广播备份" if item.status == "HEALTHY" else "仅备份"
            else:
                item.role = f"分片{offset}"
        if self.broadcast_backups:
            self.broadcast_urls = list(self.healthy_urls)
        else:
            self.broadcast_urls = [self.primary_url] if self.primary_url else []
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

    def latency_ranked_urls(self) -> list[str]:
        ranked = [row for row in self.results if row.status in {"HEALTHY", "DEGRADED"} and row.url]
        ranked.sort(key=lambda row: (row.latency_ms is None, row.latency_ms or 10**9))
        urls = [row.url for row in ranked]
        if not urls:
            urls = [u for u in (self.healthy_urls or self.urls) if u]
        return urls

    def assign_shard_urls(self, count: int, concurrency: int) -> list[str]:
        return assign_shard_urls(self.latency_ranked_urls(), count, concurrency)

    def _failover_urls(self, primary: str | None = None) -> list[str]:
        ranked = self.latency_ranked_urls()
        start = primary or self.primary_url
        if not ranked:
            return [start] if start else []
        if not start:
            return ranked
        if start in ranked:
            idx = ranked.index(start)
            return ranked[idx:] + ranked[:idx]
        return [start] + [url for url in ranked if url != start]

    async def call(self, method: str, params: list[Any], url: str | None = None) -> Any:
        target = url or self.primary_url
        if not target:
            raise RpcError("RPC pool has not been probed")
        errors = []
        last_exc: Exception | None = None
        candidates = self._failover_urls(target)
        for candidate in candidates:
            for attempt in range(3):
                try:
                    return await self._call_url(candidate, method, params)
                except Exception as exc:
                    last_exc = exc
                    errors.append(f"{candidate}: {exc}")
                    if is_execution_reverted(exc):
                        raise
                    if is_rate_limited(exc):
                        break
                    await asyncio.sleep(0.25 * (attempt + 1))
        if last_exc:
            raise last_exc
        raise RpcError(f"{method} failed: {errors[-1] if errors else 'unknown'}")

    async def eth_call(self, tx: dict[str, Any], block: str = "latest", prefer: str | None = None) -> str:
        return await self.call("eth_call", [tx, block], url=prefer)

    async def estimate_gas(self, tx: dict[str, Any], prefer: str | None = None) -> int:
        value = await self.call("eth_estimateGas", [tx], url=prefer)
        return int(value, 16) if isinstance(value, str) else int(value)

    async def get_code(self, address: str) -> str:
        return await self.call("eth_getCode", [address, "latest"])

    async def get_storage(self, address: str, slot: str) -> str:
        return await self.call("eth_getStorageAt", [address, slot, "latest"])

    async def get_balance(self, address: str, prefer: str | None = None) -> int:
        value = await self.call("eth_getBalance", [address, "latest"], url=prefer)
        return int(value, 16)

    async def get_nonce(self, address: str, tag: str = "pending", prefer: str | None = None) -> int:
        value = await self.call("eth_getTransactionCount", [address, tag], url=prefer)
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

    async def broadcast(self, raw_tx: str, prefer: str | None = None) -> list[dict[str, Any]]:
        if self.broadcast_backups:
            urls = [u for u in (self.broadcast_urls or self._failover_urls(prefer)) if u]
            return list(await asyncio.gather(*(self._send_one(url, raw_tx) for url in urls)))

        results: list[dict[str, Any]] = []
        for url in self._failover_urls(prefer):
            item = await self._send_one(url, raw_tx)
            results.append(item)
            if item.get("ok"):
                break
            if is_rate_limited(Exception(str(item.get("error") or ""))):
                continue
            break
        return results

    async def _send_one(self, url: str, raw_tx: str) -> dict[str, Any]:
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
                "rate_limited": is_rate_limited(exc),
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }

    async def get_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        receipt = await self.call("eth_getTransactionReceipt", [tx_hash])
        return receipt

    async def wait_receipt(
        self,
        tx_hash: str,
        timeout: float = 180,
        prefer: str | None = None,
    ) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        urls = self._failover_urls(prefer)
        if not urls:
            return None
        cursor = 0
        while time.time() < deadline:
            url = urls[cursor % len(urls)]
            try:
                receipt = await self._call_url(url, "eth_getTransactionReceipt", [tx_hash])
                if receipt:
                    return receipt
            except Exception as exc:
                if len(urls) > 1:
                    cursor += 1
            await asyncio.sleep(0.25)
        return None

    async def _call_url(self, url: str, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        response = await self.client.post(url, json=payload)
        if response.status_code == 429:
            raise RpcError("rate limited 429", details={"rate_limited": True, "url": url})
        response.raise_for_status()
        body = response.json()
        error = body.get("error")
        if error:
            details = {"url": url}
            message, data = _rpc_error_payload(error)
            if isinstance(error, dict) and error.get("code") is not None:
                details["code"] = error.get("code")
            if message:
                details["message"] = message
            if data:
                details["data"] = data
            if is_rate_limited(Exception(str(error))):
                details["rate_limited"] = True
                raise RpcError(str(error), details=details)
            raise RpcError(str(error), details=details)
        return body["result"]
