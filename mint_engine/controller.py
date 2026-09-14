from __future__ import annotations

import asyncio
import time
from typing import Any

from mint_engine.analyzer.contract_analyzer import ContractAnalyzer
from mint_engine.config.chains import get_chain
from mint_engine.core.exceptions import ConfigError, EngineError
from mint_engine.core.models import (
    Capability,
    InspectReport,
    OpenSeaPreview,
    RunConfig,
    SaleStatus,
    StartStrategy,
    TxPlan,
    WalletReady,
    WalletStatus,
)
from mint_engine.evm import checksum
from mint_engine.monitor.receipt import decode_revert, is_sold_out, parse_token_ids, revert_blob
from mint_engine.rpc.pool import RpcPool, short_rpc_url
from mint_engine.transaction.gas import quote_gas, worst_case_gas_reserve_wei
from mint_engine.transaction.signer import sign_tx
from mint_engine.wallet.manager import WalletManager


class _SoldOut(Exception):
    """Mint supply exhausted; unsent wallets should skip."""


class MintController:
    def __init__(self, config: RunConfig, opensea: OpenSeaPreview | None = None):
        if config.chain.chain_id is None:
            raise ConfigError("chain_id is required")
        self.config = config
        self.opensea = opensea
        self.chain = get_chain(config.chain.chain_id)
        self.pool = RpcPool(
            urls=config.rpc.urls,
            expected_chain_id=config.chain.chain_id,
            timeout_ms=config.rpc.probe_timeout_ms,
            primary=config.rpc.primary,
            selection=config.rpc.selection,
            broadcast_backups=config.rpc.broadcast_backups,
        )
        self.wallets = WalletManager(config.wallets.items) if config.wallets.items else WalletManager([])
        self.events: list[str] = []

    async def aclose(self) -> None:
        await self.pool.aclose()

    async def inspect(self) -> InspectReport:
        rpc = await self.pool.probe()
        analysis = await self._analyze()
        sale = analysis.get("sale")
        method = analysis.get("method")
        quantity = self.config.mint.quantity
        value = (sale.price * quantity) if sale else 0
        gas_reserve = await worst_case_gas_reserve_wei(self.pool, self.config.gas)
        wallets = await self._wallet_states(sale, value, gas_reserve)
        report = InspectReport(
            chain_id=self.chain.chain_id,
            chain_name=self.chain.name,
            contract=analysis["contract"],
            is_proxy=bool((analysis.get("proxy") or {}).get("is_proxy")),
            implementation=(analysis.get("proxy") or {}).get("implementation"),
            proxy_type=(analysis.get("proxy") or {}).get("proxy_type"),
            contract_type=analysis.get("contract_type") or "Unknown",
            protocol=analysis["detection"].protocol if analysis.get("detection") else None,
            contract_name=(analysis.get("meta") or {}).get("name"),
            symbol=(analysis.get("meta") or {}).get("symbol"),
            abi_source=analysis.get("abi_source") or "获取失败",
            abi_function_count=len(
                [x for x in (analysis.get("abi") or []) if x.get("type") == "function"]
            ),
            capability=analysis.get("capability") or Capability.UNSUPPORTED,
            method=method,
            candidates=analysis.get("candidates") or [],
            sale=sale or sale_unknown(),
            value=value,
            gaps=analysis.get("gaps") or [],
            wallets=wallets,
            rpc=rpc,
            notes=(analysis.get("notes") or []) + self._opensea_notes(sale),
            opensea=self.opensea,
        )
        return report

    def _opensea_notes(self, sale) -> list[str]:
        if not self.opensea:
            return []
        notes = list(self.opensea.notes)
        stage = self.opensea.next_stage or {}
        if stage.get("start_time") and sale and sale.start_time:
            notes.append(
                f"OpenSea next stage {stage.get('start_time')}; on-chain startTime={sale.start_time}"
            )
        if self.opensea.is_minting is False and sale and sale.status:
            notes.append(f"OpenSea is_minting=false; on-chain status={sale.status.value if hasattr(sale.status, 'value') else sale.status}")
        return notes

    async def dry_run(self, from_address: str | None = None) -> dict[str, Any]:
        report = await self.inspect()
        if not report.method:
            raise ConfigError("no mint method detected")
        wallet = self._pick_from(report, from_address)
        plan = self._build_plan(report, wallet.address)
        tx = {"from": wallet.address, "to": plan.to, "data": plan.data, "value": hex(plan.value)}
        shard = self._shard_url_for(wallet.address)
        try:
            await self.pool.eth_call(tx, prefer=shard)
            simulation = {"ok": True, "message": "eth_call passed"}
        except EngineError as exc:
            revert = decode_revert(str(exc.details.get("data") if exc.details else "")) or exc.message
            expected = report.sale.status in {SaleStatus.NOT_STARTED, SaleStatus.ENDED, SaleStatus.SOLD_OUT}
            simulation = {
                "ok": False,
                "expected": expected,
                "message": revert,
                "sale_status": report.sale.status,
            }
        return {
            "report": report.model_dump(),
            "plan": plan.model_dump(),
            "simulation": simulation,
        }

    def log(self, message: str) -> None:
        self.events.append(message)

    def _concurrency(self) -> int:
        return max(1, self.config.wallets.concurrency)

    def _shard_urls(self, count: int) -> list[str]:
        return self.pool.assign_shard_urls(count, self._concurrency())

    def _shard_url_for(self, address: str) -> str | None:
        wallets = self.wallets.wallets
        urls = self._shard_urls(len(wallets)) if wallets else []
        want = (address or "").lower()
        for wallet, url in zip(wallets, urls):
            if wallet.address.lower() == want:
                return url
        return urls[0] if urls else self.pool.primary_url

    def _log_shards(self, wallets, urls: list[str], conc: int) -> None:
        groups: dict[str, int] = {}
        for url in urls:
            groups[url] = groups.get(url, 0) + 1
        self.log(f"SHARD concurrency={conc}/node nodes={len(groups)} wallets={len(wallets)}")
        latency = {row.url: row.latency_ms for row in self.pool.results}
        for url, n in groups.items():
            ms = latency.get(url)
            delay = f"{ms}ms" if ms is not None else "?"
            self.log(f"  {short_rpc_url(url)} {delay} x{n}")

    async def mint(self) -> dict[str, Any]:
        dry = await self.dry_run()
        report = InspectReport.model_validate(dry["report"])
        if report.sale.status in {SaleStatus.ENDED, SaleStatus.SOLD_OUT}:
            raise ConfigError(f"refusing to mint: {report.sale.status.value}")
        if report.capability == Capability.UNSUPPORTED:
            raise ConfigError("capability UNSUPPORTED")
        if self.config.safety.dry_run_required and not dry["simulation"]["ok"]:
            if report.sale.status not in {SaleStatus.NOT_STARTED} and not dry["simulation"].get("expected"):
                raise ConfigError(f"dry run failed: {dry['simulation']['message']}")
        self._autofill_fee_recipient(report)
        if report.sale.max_per_wallet is not None and self.config.mint.quantity > report.sale.max_per_wallet:
            raise ConfigError(
                f"quantity={self.config.mint.quantity} > maxPerWallet={report.sale.max_per_wallet}"
            )

        ready = [w for w in self.wallets.signers()]
        if not ready:
            raise ConfigError("no wallet with a private key")

        conc = self._concurrency()
        start_time = self._resolve_start_time(report)
        if start_time:
            prepare_lead = self.config.schedule.prepare_lead_sec
            sign_lead = self.config.schedule.sign_lead_sec
            if prepare_lead > sign_lead and time.time() < start_time - prepare_lead:
                await self._wait_until(start_time - prepare_lead, "PREPARE_LEAD")
            await self._wait_until(start_time - sign_lead, "PREPARE")
            self.log(
                f"T-{sign_lead}s: skip mass pre-sign; each wallet will estimateGas+sign+send at T=0"
            )
            await self._wait_until(start_time - self.config.schedule.final_check_lead_sec, "FINAL REFRESH")
            report = await self._refresh_report(report)
            if report.sale.status in {SaleStatus.ENDED, SaleStatus.SOLD_OUT}:
                raise ConfigError(f"sale became {report.sale.status.value} before launch")
            if report.sale.start_time:
                start_time = report.sale.start_time
            self._autofill_fee_recipient(report)
            await self._wait_until(start_time, "ARMED")
            report = await self._refresh_report(report)
            if report.sale.status in {SaleStatus.ENDED, SaleStatus.SOLD_OUT}:
                raise ConfigError(f"sale became {report.sale.status.value} before launch")

        if self.config.rpc.probe_on_start:
            await self.pool.probe()
            self.log("RPC re-probe before launch")
        shard_urls = self._shard_urls(len(ready))
        self.log("BLAST sign-and-send per wallet")
        self._log_shards(ready, shard_urls, conc)
        quote = await quote_gas(self.pool, self.config.gas, attempt=0)
        launch = time.perf_counter()
        sems = {url: asyncio.Semaphore(conc) for url in dict.fromkeys(shard_urls)}
        stop = asyncio.Event()
        results = []

        def note_sold_out(source: str, detail: str) -> None:
            if not self.config.wallets.stop_on_sold_out:
                return
            if stop.is_set():
                return
            stop.set()
            self.log(f"[SOLD_OUT] {source} {detail}; skip wallets that have not sent")

        async def run_one(wallet, rpc_url: str):
            if self.config.wallets.stop_on_sold_out and stop.is_set():
                return self._skip_unsent(report, wallet, "sold out, not sent")
            async with sems[rpc_url]:
                if self.config.wallets.stop_on_sold_out and stop.is_set():
                    return self._skip_unsent(report, wallet, "sold out, not sent")
                try:
                    prepared = await self._prepare_wallet(report, wallet, quote, rpc_url=rpc_url)
                except _SoldOut as exc:
                    note_sold_out(wallet.address, str(exc))
                    return self._skip_unsent(report, wallet, f"sold out, not sent ({exc})")
                except Exception as exc:
                    self.log(f"[PREPARE FAILED] {wallet.address} {exc}")
                    return {
                        "wallet": wallet.label,
                        "address": wallet.address,
                        "status": "ERROR",
                        "error": str(exc),
                    }
                if self.config.wallets.stop_on_sold_out and stop.is_set():
                    return self._skip_unsent(report, wallet, "sold out, not sent")
                item = await self._mint_with_retry(report, prepared, stop=stop)
                if item.get("sold_out"):
                    note_sold_out(wallet.address, str(item.get("error") or "sold out"))
                return item

        for item in await asyncio.gather(
            *(run_one(wallet, rpc_url) for wallet, rpc_url in zip(ready, shard_urls)),
            return_exceptions=True,
        ):
            if isinstance(item, Exception):
                results.append({"status": "ERROR", "error": str(item)})
                continue
            results.append(item)

        return {
            "report": report.model_dump(),
            "results": results,
            "events": self.events,
            "elapsed_sec": round(time.perf_counter() - launch, 3),
            "prepared": len(ready),
            "success": sum(1 for row in results if row.get("status") == "SUCCESS"),
            "skipped": sum(1 for row in results if row.get("status") == "SKIP"),
        }

    def _autofill_fee_recipient(self, report: InspectReport) -> None:
        extra = self.config.mint.extra_params
        if extra.get("feeRecipient") or extra.get("fee_recipient"):
            return
        fees = (report.sale.extra or {}).get("fee_recipients") or []
        if fees:
            extra["feeRecipient"] = fees[0]
            self.log(f"feeRecipient auto-selected {fees[0]}")

    def _resolve_start_time(self, report: InspectReport) -> int | None:
        start = report.sale.start_time or self.config.schedule.start_time_unix
        if not start or time.time() >= start:
            return None
        if self.config.run.start_strategy == StartStrategy.IMMEDIATE:
            return None
        return int(start)

    async def _wait_until(self, timestamp: float, label: str) -> None:
        while True:
            remaining = timestamp - time.time()
            if remaining <= 0:
                self.log(f"{label} now")
                return
            if remaining > 10:
                self.log(f"[WAIT {label}] {remaining:.2f}s")
                if remaining > 3600:
                    await asyncio.sleep(min(60, remaining - 0.05))
                elif remaining > 60:
                    await asyncio.sleep(min(15, remaining - 0.05))
                else:
                    await asyncio.sleep(min(5, remaining - 0.05))
            elif remaining > 2:
                await asyncio.sleep(0.2)
            elif remaining > 0.2:
                await asyncio.sleep(0.02)
            else:
                await asyncio.sleep(0.002)

    async def _refresh_report(self, report: InspectReport) -> InspectReport:
        analysis = await self._analyze()
        sale = analysis.get("sale")
        if sale:
            report.sale = sale
            report.value = sale.price * self.config.mint.quantity
            self.log(
                f"sale refresh status={sale.status.value} start={sale.start_time} price={sale.price}"
            )
        return report

    async def _prepare_wallet(
        self,
        report: InspectReport,
        wallet,
        quote: dict[str, int],
        nonce: int | None = None,
        rpc_url: str | None = None,
    ) -> dict[str, Any]:
        plan = self._build_plan(report, wallet.address)
        if nonce is None:
            nonce = await self.pool.get_nonce(wallet.address, "pending", prefer=rpc_url)
        tx = {
            "from": plan.from_address,
            "to": plan.to,
            "data": plan.data,
            "value": hex(plan.value),
        }
        fallback = self.config.gas.fallback_gas_limit or 280000
        if self.config.gas.gas_limit_mode == "fallback":
            gas_limit, source = fallback, "forced fallback"
        else:
            try:
                estimated = await self.pool.estimate_gas(tx, prefer=rpc_url)
                gas_limit, source = max(int(estimated * 1.2), 21000), f"estimate {estimated}+20%"
            except Exception as exc:
                blob = revert_blob(exc)
                if is_sold_out(blob) and self.config.wallets.stop_on_sold_out:
                    raise _SoldOut(blob) from exc
                why = report.sale.status.value if report.sale else "estimate failed"
                gas_limit, source = fallback, f"fallback ({why})"
        local_quote = dict(quote)
        local_quote["gas_limit"] = gas_limit
        plan.nonce = nonce
        plan.gas = gas_limit
        plan.max_fee_per_gas = local_quote["max_fee"]
        plan.max_priority_fee_per_gas = local_quote["priority_fee"]
        raw, tx_hash = sign_tx(plan, wallet.private_key)
        node = short_rpc_url(rpc_url or self.pool.primary_url or "")
        self.log(
            f"[SIGNED] {wallet.label} {wallet.address} nonce={nonce} "
            f"rpc={node} gas={plan.gas} ({source}) tip={plan.max_priority_fee_per_gas} tx={tx_hash}"
        )
        return {
            "wallet": wallet,
            "plan": plan,
            "raw": raw,
            "tx_hash": tx_hash,
            "quote": local_quote,
            "rpc_url": rpc_url,
        }

    def _skip_unsent(self, report: InspectReport, wallet, why: str) -> dict[str, Any]:
        return {
            "wallet": wallet.label,
            "address": wallet.address,
            "method": report.method.signature if report.method else None,
            "quantity": self.config.mint.quantity,
            "tx_hash": None,
            "status": "SKIP",
            "sold_out": True,
            "error": why,
        }

    async def _mint_with_retry(
        self,
        report: InspectReport,
        prepared: dict[str, Any],
        stop: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        wallet = prepared["wallet"]
        current = prepared
        last = None
        retries = self.config.gas.max_retries
        for attempt in range(retries + 1):
            stopping = bool(stop and stop.is_set() and self.config.wallets.stop_on_sold_out)
            if stopping and last is not None:
                return last
            if stopping:
                return self._skip_unsent(report, wallet, "sold out, not sent")
            if attempt > 0:
                self.log(f"[RETRY] {wallet.address} attempt {attempt + 1}/{retries + 1}")
                quote = await quote_gas(
                    self.pool,
                    self.config.gas,
                    fallback_limit=current["quote"].get("gas_limit"),
                    attempt=attempt,
                )
                try:
                    current = await self._prepare_wallet(
                        report,
                        wallet,
                        quote,
                        nonce=current["plan"].nonce,
                        rpc_url=current.get("rpc_url"),
                    )
                except _SoldOut as exc:
                    return self._skip_unsent(report, wallet, f"sold out, not sent ({exc})")
            last = await self._send_prepared(report, current, stop=stop)
            if last["status"] == "SUCCESS":
                return last
            if last.get("sold_out"):
                return last
            if last["status"] == "TIMEOUT":
                late = await self.pool.wait_receipt(
                    current["tx_hash"],
                    timeout=10,
                    prefer=current.get("rpc_url"),
                )
                if late:
                    last = await self._result_from_receipt(report, current, late, last.get("broadcasts") or [])
                    if last["status"] == "SUCCESS":
                        self.log(f"[LATE SUCCESS] {wallet.address} tx={last['tx_hash']}")
                        return last
                    if last.get("sold_out"):
                        return last
            if last["status"] not in {"SEND_FAILED", "TIMEOUT"}:
                return last
        return last

    async def _send_prepared(
        self,
        report: InspectReport,
        prepared: dict[str, Any],
        stop: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        wallet = prepared["wallet"]
        if stop and stop.is_set() and self.config.wallets.stop_on_sold_out:
            return self._skip_unsent(report, wallet, "sold out, not sent")
        prefer = prepared.get("rpc_url")
        broadcasts = await self.pool.broadcast(prepared["raw"], prefer=prefer)
        for row in broadcasts:
            if row.get("rate_limited"):
                self.log(f"[RPC 429] {short_rpc_url(str(row.get('url') or ''))} 立即切换下一节点")
        sent = [row for row in broadcasts if row.get("ok")]
        if not sent:
            blob = " ".join(str(row.get("error") or "") for row in broadcasts)
            sold = is_sold_out(blob)
            self.log(f"[SEND_FAILED] {wallet.address}")
            return {
                "wallet": wallet.label,
                "address": wallet.address,
                "method": report.method.signature if report.method else None,
                "quantity": self.config.mint.quantity,
                "tx_hash": prepared["tx_hash"],
                "status": "SEND_FAILED",
                "broadcasts": broadcasts,
                "gas_attempt": prepared["quote"].get("attempt", 0),
                "sold_out": sold,
                "error": blob or "all RPC broadcasts failed",
            }
        sent_url = sent[0].get("url") or prefer
        self.log(
            f"[SENT] {wallet.address} tx={prepared['tx_hash']} rpc={short_rpc_url(str(sent_url or ''))}"
        )
        receipt = await self.pool.wait_receipt(
            prepared["tx_hash"],
            timeout=self.config.gas.receipt_timeout_sec,
            prefer=sent_url,
        )
        if receipt is None:
            return {
                "wallet": wallet.label,
                "address": wallet.address,
                "method": report.method.signature if report.method else None,
                "quantity": self.config.mint.quantity,
                "tx_hash": prepared["tx_hash"],
                "status": "TIMEOUT",
                "broadcasts": broadcasts,
                "gas_attempt": prepared["quote"].get("attempt", 0),
                "error": "receipt timeout",
            }
        return await self._result_from_receipt(report, prepared, receipt, broadcasts)

    async def _revert_reason(self, prepared: dict[str, Any]) -> str | None:
        plan = prepared["plan"]
        tx = {
            "from": plan.from_address,
            "to": plan.to,
            "data": plan.data,
            "value": hex(plan.value),
        }
        try:
            await self.pool.eth_call(tx, prefer=prepared.get("rpc_url"))
            return None
        except Exception as exc:
            data = None
            if isinstance(exc, EngineError):
                data = (exc.details or {}).get("data")
            decoded = decode_revert(data)
            return decoded or revert_blob(exc)

    async def _result_from_receipt(
        self,
        report: InspectReport,
        prepared: dict[str, Any],
        receipt: dict[str, Any],
        broadcasts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        wallet = prepared["wallet"]
        ok = _hex_int(receipt.get("status")) == 1
        token_ids = parse_token_ids(receipt, wallet.address) if ok else []
        error = None
        sold = False
        if not ok:
            error = await self._revert_reason(prepared) or "execution reverted"
            sold = is_sold_out(error)
        return {
            "wallet": wallet.label,
            "address": wallet.address,
            "method": report.method.signature if report.method else None,
            "quantity": self.config.mint.quantity,
            "tx_hash": prepared["tx_hash"],
            "status": "SUCCESS" if ok else "REVERTED",
            "block_number": _hex_int(receipt.get("blockNumber")) if receipt.get("blockNumber") else None,
            "gas_used": _hex_int(receipt.get("gasUsed")) if receipt.get("gasUsed") else None,
            "token_ids": token_ids,
            "broadcasts": broadcasts,
            "gas_attempt": prepared["quote"].get("attempt", 0),
            "sold_out": sold,
            "error": error,
        }

    async def _analyze(self) -> dict[str, Any]:
        analyzer = ContractAnalyzer(self.pool, self.chain)
        mint = self.config.mint
        return await analyzer.analyze(
            mint.contract,
            mint.quantity,
            manual_abi=mint.abi.payload,
            require_manual=mint.abi.source.value == "manual" and mint.abi.payload is None,
            extra_params=mint.extra_params,
        )

    def _build_plan(self, report: InspectReport, recipient: str) -> TxPlan:
        if not report.method or not self._adapter_from_report(report):
            raise ConfigError("cannot build transaction without a mint method")
        analyzer_adapter = None
        for adapter in __import__("mint_engine.adapters", fromlist=["ADAPTERS"]).ADAPTERS:
            if report.protocol and adapter.name == report.protocol.lower():
                analyzer_adapter = adapter
                break
            if report.protocol == "Direct" and adapter.name == "direct":
                analyzer_adapter = adapter
                break
            if report.protocol == "SeaDrop" and adapter.name == "seadrop":
                analyzer_adapter = adapter
                break
        if analyzer_adapter is None:
            raise ConfigError("no adapter for detected protocol")
        context = {
            "contract": report.contract,
            "quantity": self.config.mint.quantity,
            "method": report.method,
            "extra_params": self.config.mint.extra_params,
        }
        to, data, value = analyzer_adapter.build_call(context, report.sale, checksum(recipient))
        return TxPlan(
            to=checksum(to),
            data=data,
            value=value,
            gas=self.config.gas.fallback_gas_limit,
            chain_id=self.chain.chain_id,
            from_address=checksum(recipient),
        )

    def _adapter_from_report(self, report: InspectReport):
        return report.protocol

    def _pick_from(self, report: InspectReport, from_address: str | None) -> WalletReady:
        if from_address:
            match = next((w for w in report.wallets if w.address.lower() == from_address.lower()), None)
            if match:
                return match
        if report.wallets:
            ready = next((w for w in report.wallets if w.status == WalletStatus.READY), report.wallets[0])
            return ready
        raise ConfigError("dry run needs at least one wallet address")

    async def _wallet_states(self, sale, value: int, gas_reserve: int) -> list[WalletReady]:
        wallets = self.wallets.wallets
        conc = self._concurrency()
        shard_urls = self._shard_urls(len(wallets)) if wallets else []
        sems = {url: asyncio.Semaphore(conc) for url in dict.fromkeys(shard_urls)} if shard_urls else {}

        async def one(wallet, rpc_url: str | None) -> WalletReady:
            try:
                if rpc_url and rpc_url in sems:
                    async with sems[rpc_url]:
                        balance = await self.pool.get_balance(wallet.address, prefer=rpc_url)
                        nonce = await self.pool.get_nonce(wallet.address, "pending", prefer=rpc_url)
                else:
                    balance = await self.pool.get_balance(wallet.address)
                    nonce = await self.pool.get_nonce(wallet.address, "pending")
            except Exception as exc:
                return WalletReady(
                    label=wallet.label,
                    address=wallet.address,
                    status=WalletStatus.SKIPPED,
                    note=str(exc),
                )
            status = WalletStatus.READY
            note = None
            if not wallet.can_sign:
                status = WalletStatus.MISSING_KEY
                note = "inspect-only, no private key"
            if balance < value + gas_reserve and status == WalletStatus.READY:
                status = WalletStatus.INSUFFICIENT_ETH
            if sale and sale.max_per_wallet is not None and sale.max_per_wallet < self.config.mint.quantity:
                status = WalletStatus.WALLET_LIMIT
                note = f"quantity {self.config.mint.quantity} > maxPerWallet {sale.max_per_wallet}"
            return WalletReady(
                label=wallet.label,
                address=wallet.address,
                status=status,
                balance_wei=balance,
                nonce=nonce,
                note=note,
            )

        if not wallets:
            return []
        return list(
            await asyncio.gather(
                *(one(wallet, url) for wallet, url in zip(wallets, shard_urls))
            )
        )


def sale_unknown():
    from mint_engine.core.models import SaleState

    return SaleState()


def _hex_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return default
    return int(text, 16)
