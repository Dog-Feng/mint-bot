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
from mint_engine.monitor.receipt import decode_revert, parse_token_ids
from mint_engine.rpc.pool import RpcPool
from mint_engine.transaction.gas import quote_gas, resolve_gas_limit
from mint_engine.transaction.signer import sign_tx
from mint_engine.wallet.manager import WalletManager


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
        wallets = await self._wallet_states(sale, value)
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
        try:
            await self.pool.eth_call(tx)
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

        start_time = self._resolve_start_time(report)
        if start_time:
            await self._wait_until(start_time - self.config.schedule.sign_lead_sec, "PREPARE")

        self.log("PREPARING wallets")
        prepared = await self._prepare_all(report, ready, attempt=0)
        if not prepared:
            raise ConfigError("all wallets failed to prepare")
        self.log(f"READY {len(prepared)}/{len(ready)}")

        if start_time:
            await self._wait_until(start_time - self.config.schedule.final_check_lead_sec, "FINAL REFRESH")
            report = await self._refresh_report(report)
            if report.sale.status in {SaleStatus.ENDED, SaleStatus.SOLD_OUT}:
                raise ConfigError(f"sale became {report.sale.status.value} before launch")
            if report.sale.start_time:
                start_time = report.sale.start_time
            self._autofill_fee_recipient(report)
            self.log("RE-SIGNING after final sale refresh")
            prepared = await self._prepare_all(report, ready, attempt=0)
            if not prepared:
                raise ConfigError("all wallets failed to re-sign")
            await self._wait_until(start_time, "ARMED")
            report = await self._refresh_report(report)
            self.log("LIVE estimateGas+20% at T=0")
            live = await self._prepare_all(report, ready, attempt=0)
            if live:
                prepared = live

        self.log("BLAST multi-RPC")
        launch = time.perf_counter()
        sem = asyncio.Semaphore(max(1, self.config.wallets.concurrency))
        results = []

        async def run_one(item: dict[str, Any]):
            async with sem:
                return await self._mint_with_retry(report, item)

        for item in await asyncio.gather(*(run_one(p) for p in prepared), return_exceptions=True):
            if isinstance(item, Exception):
                results.append({"status": "ERROR", "error": str(item)})
                continue
            results.append(item)
            if (
                self.config.wallets.stop_on_sold_out
                and item.get("status") == "REVERTED"
                and "sold" in str(item.get("error", "")).lower()
            ):
                self.log("SOLD_OUT detected, remaining wallets will still finish in-flight sends")

        return {
            "report": report.model_dump(),
            "results": results,
            "events": self.events,
            "elapsed_sec": round(time.perf_counter() - launch, 3),
            "prepared": len(prepared),
            "success": sum(1 for row in results if row.get("status") == "SUCCESS"),
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

    async def _prepare_all(self, report: InspectReport, wallets, attempt: int) -> list[dict[str, Any]]:
        quote = await quote_gas(self.pool, self.config.gas, attempt=attempt)
        if wallets:
            sample = self._build_plan(report, wallets[0].address)
            quote["gas_limit"], source = await resolve_gas_limit(
                self.pool,
                {
                    "from": sample.from_address,
                    "to": sample.to,
                    "data": sample.data,
                    "value": hex(sample.value),
                },
                self.config.gas,
                report.sale.status,
            )
            self.log(f"gasLimit={quote['gas_limit']} qty={self.config.mint.quantity} {source}")
        prepared = []
        for wallet in wallets:
            try:
                prepared.append(await self._prepare_wallet(report, wallet, quote))
            except Exception as exc:
                self.log(f"[PREPARE FAILED] {wallet.address} {exc}")
        return prepared

    async def _prepare_wallet(
        self,
        report: InspectReport,
        wallet,
        quote: dict[str, int],
        nonce: int | None = None,
    ) -> dict[str, Any]:
        if nonce is None:
            nonce = await self.pool.get_nonce(wallet.address, "pending")
        plan = self._build_plan(report, wallet.address)
        plan.nonce = nonce
        plan.gas = quote["gas_limit"]
        plan.max_fee_per_gas = quote["max_fee"]
        plan.max_priority_fee_per_gas = quote["priority_fee"]
        raw, tx_hash = sign_tx(plan, wallet.private_key)
        self.log(
            f"[SIGNED] {wallet.label} {wallet.address} nonce={nonce} "
            f"gas={plan.gas} tip={plan.max_priority_fee_per_gas} tx={tx_hash}"
        )
        return {
            "wallet": wallet,
            "plan": plan,
            "raw": raw,
            "tx_hash": tx_hash,
            "quote": quote,
        }

    async def _mint_with_retry(self, report: InspectReport, prepared: dict[str, Any]) -> dict[str, Any]:
        wallet = prepared["wallet"]
        current = prepared
        last = None
        retries = self.config.gas.max_retries
        for attempt in range(retries + 1):
            if attempt > 0:
                self.log(f"[RETRY] {wallet.address} attempt {attempt + 1}/{retries + 1}")
                quote = await quote_gas(
                    self.pool,
                    self.config.gas,
                    fallback_limit=current["quote"].get("gas_limit"),
                    attempt=attempt,
                )
                current = await self._prepare_wallet(
                    report,
                    wallet,
                    quote,
                    nonce=current["plan"].nonce,
                )
            last = await self._send_prepared(report, current)
            if last["status"] == "SUCCESS":
                return last
            if last["status"] == "TIMEOUT":
                late = await self.pool.wait_receipt(current["tx_hash"], timeout=10)
                if late:
                    last = self._result_from_receipt(report, current, late, last.get("broadcasts") or [])
                    if last["status"] == "SUCCESS":
                        self.log(f"[LATE SUCCESS] {wallet.address} tx={last['tx_hash']}")
                        return last
            if last["status"] not in {"SEND_FAILED", "TIMEOUT"}:
                return last
        return last

    async def _send_prepared(self, report: InspectReport, prepared: dict[str, Any]) -> dict[str, Any]:
        wallet = prepared["wallet"]
        broadcasts = await self.pool.broadcast(prepared["raw"])
        for row in broadcasts:
            if row.get("rate_limited"):
                self.log(f"[RPC 429] {row.get('url')} 立即切换下一节点")
        sent = [row for row in broadcasts if row.get("ok")]
        if not sent:
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
                "error": "all RPC broadcasts failed",
            }
        self.log(f"[SENT] {wallet.address} tx={prepared['tx_hash']} rpcs={len(sent)}")
        receipt = await self.pool.wait_receipt(
            prepared["tx_hash"],
            timeout=self.config.gas.receipt_timeout_sec,
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
        return self._result_from_receipt(report, prepared, receipt, broadcasts)

    def _result_from_receipt(
        self,
        report: InspectReport,
        prepared: dict[str, Any],
        receipt: dict[str, Any],
        broadcasts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        wallet = prepared["wallet"]
        ok = _hex_int(receipt.get("status")) == 1
        token_ids = parse_token_ids(receipt, wallet.address) if ok else []
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
            "error": None if ok else "execution reverted",
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

    async def _wallet_states(self, sale, value: int) -> list[WalletReady]:
        out = []
        for wallet in self.wallets.wallets:
            try:
                balance = await self.pool.get_balance(wallet.address)
                nonce = await self.pool.get_nonce(wallet.address, "pending")
            except Exception as exc:
                out.append(
                    WalletReady(
                        label=wallet.label,
                        address=wallet.address,
                        status=WalletStatus.SKIPPED,
                        note=str(exc),
                    )
                )
                continue
            status = WalletStatus.READY
            note = None
            if not wallet.can_sign:
                status = WalletStatus.MISSING_KEY
                note = "inspect-only, no private key"
            gas_reserve = self.config.gas.fallback_gas_limit * 10**9
            if balance < value + gas_reserve and status == WalletStatus.READY:
                status = WalletStatus.INSUFFICIENT_ETH
            if sale and sale.max_per_wallet is not None and sale.max_per_wallet < self.config.mint.quantity:
                status = WalletStatus.WALLET_LIMIT
                note = f"quantity {self.config.mint.quantity} > maxPerWallet {sale.max_per_wallet}"
            out.append(
                WalletReady(
                    label=wallet.label,
                    address=wallet.address,
                    status=status,
                    balance_wei=balance,
                    nonce=nonce,
                    note=note,
                )
            )
        return out


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
