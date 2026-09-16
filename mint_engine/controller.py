from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

_log = logging.getLogger("mint_engine")

from mint_engine.analyzer.contract_analyzer import ContractAnalyzer
from mint_engine.config.chains import get_chain
from mint_engine.core.exceptions import ConfigError, EngineError
from mint_engine.chase import ChaseContext, ChaseWallet, init_chase_wallets, is_public_final_stage
from mint_engine.price_guard import PriceGuardError, enforce_price_guard, native_unit_to_wei
from mint_engine.core.models import (
    Capability,
    InspectReport,
    OpenSeaPreview,
    RunConfig,
    SaleStatus,
    TxPlan,
    WalletReady,
    WalletStatus,
)
from mint_engine.evm import checksum
from mint_engine.discovery.opensea import fetch_drop_stages
from mint_engine.discovery.opensea_mint import (
    OpenSeaMintProbe,
    build_drop_mint_transaction,
    build_opensea_mint_plan,
    mint_errors_indicate_drop_fully_sold_out,
)
from mint_engine.discovery.opensea_stages import (
    build_stage_sequence,
    drop_has_future_mint_window,
    eligibility_retry_window_open,
    hot_path_active,
    resolve_drop_stage,
    sale_from_stage,
    stage_bounds,
    stage_window_open,
    sync_stage_times_in_sequence,
    use_chain_public_mint,
)
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
        method = analysis.get("method")
        chain_sale = analysis.get("sale")
        sale, mint_route, stage_dict, stage_notes = await self._effective_sale(chain_sale, analysis)
        quantity = self.config.mint.quantity
        value = (sale.price * quantity) if sale else 0
        gas_reserve = await worst_case_gas_reserve_wei(self.pool, self.config.gas)
        wallets = await self._wallet_states(sale, value, gas_reserve)
        notes = (analysis.get("notes") or []) + self._opensea_notes(chain_sale) + stage_notes
        notes.extend(self._price_guard_notes(sale or sale_unknown()))
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
            notes=notes,
            opensea=self.opensea,
            mint_route=mint_route,
            drop_stage=stage_dict,
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
        wallet = self._pick_from(report, from_address)
        try:
            plan = await self._build_mint_plan(report, wallet.address)
        except EngineError as exc:
            expected = report.sale.status in {SaleStatus.NOT_STARTED, SaleStatus.ENDED, SaleStatus.SOLD_OUT}
            if exc.code == "PRICE_GUARD":
                expected = False
            return {
                "report": report.model_dump(),
                "plan": None,
                "simulation": {
                    "ok": False,
                    "expected": expected,
                    "message": exc.message,
                    "sale_status": report.sale.status,
                },
            }
        if report.mint_route != "opensea_drop" and not report.method:
            raise ConfigError("no mint method detected")
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
        _log.info("%s", message)

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
        multi_stage = bool(self.opensea and self.opensea.stages)
        now = await self.pool.get_block_timestamp()
        if multi_stage:
            if not drop_has_future_mint_window(self.opensea.stages, now):
                if report.sale.status in {SaleStatus.ENDED, SaleStatus.SOLD_OUT}:
                    raise ConfigError(f"refusing to mint: drop stages ended ({report.sale.status.value})")
        elif report.sale.status in {SaleStatus.ENDED, SaleStatus.SOLD_OUT}:
            raise ConfigError(f"refusing to mint: {report.sale.status.value}")
        if report.capability == Capability.UNSUPPORTED and report.mint_route != "opensea_drop":
            raise ConfigError("capability UNSUPPORTED")
        if self.config.safety.dry_run_required and not dry["simulation"]["ok"]:
            if not multi_stage:
                if report.sale.status not in {SaleStatus.NOT_STARTED} and not dry["simulation"].get(
                    "expected"
                ):
                    raise ConfigError(f"dry run failed: {dry['simulation']['message']}")
        if report.sale.max_per_wallet is not None and self.config.mint.quantity > report.sale.max_per_wallet:
            raise ConfigError(
                f"quantity={self.config.mint.quantity} > maxPerWallet={report.sale.max_per_wallet}"
            )

        ready = [w for w in self.wallets.signers()]
        if not ready:
            raise ConfigError("no wallet with a private key")

        if self.config.rpc.probe_on_start:
            await self.pool.probe()
            self.log("RPC re-probe before launch")

        self.log("STAGE CHASE: per-wallet stage eligibility, mint when eligible")
        launch = time.perf_counter()
        if multi_stage:
            payload = await self._run_stage_chase(report, ready)
        else:
            payload = await self._run_chain_chase(report, ready)
        payload["elapsed_sec"] = round(time.perf_counter() - launch, 3)
        payload["events"] = self.events
        payload["prepared"] = len(ready)
        payload["success"] = sum(1 for row in payload["results"] if row.get("status") == "SUCCESS")
        payload["skipped"] = sum(1 for row in payload["results"] if row.get("status") == "SKIP")
        return payload

    async def _run_chain_chase(self, report: InspectReport, ready) -> dict[str, Any]:
        while True:
            report = await self._refresh_report(report)
            if report.sale.status == SaleStatus.SOLD_OUT:
                raise ConfigError("refusing to mint: SOLD_OUT")
            if report.sale.status == SaleStatus.ENDED:
                raise ConfigError("refusing to mint: ENDED")
            start = report.sale.start_time
            if start and time.time() < int(start):
                report = await self._wait_stage_schedule(int(start), report)
                continue
            break
        results = await self._blast_all_wallets(report, ready)
        return {"report": report.model_dump(), "results": results, "chase": None}

    async def _run_stage_chase(self, report: InspectReport, ready) -> dict[str, Any]:
        assert self.opensea is not None
        sequence = build_stage_sequence(self.opensea.stages)
        if not sequence:
            return await self._run_chain_chase(report, ready)

        now = await self.pool.get_block_timestamp()
        auto_stage = resolve_drop_stage(
            self.opensea.stages,
            now,
            next_stage=self.opensea.next_stage,
        )
        ctx = init_chase_wallets(ready, sequence, auto_stage)
        labels = [(s.get("label") or s.get("stage_type") or "?") for s in sequence]
        self.log(f"CHASE sequence={labels} wallets={len(ready)}")

        stop = asyncio.Event()
        analysis: dict[str, Any] | None = None
        cached_quote: dict[str, int] | None = None
        last_stage_sync_at = 0.0
        url_map = {
            w.address.lower(): url for w, url in zip(ready, self._shard_urls(len(ready)))
        }

        schedule = self.config.schedule
        hot_announced = False

        async def on_prepare_hot() -> None:
            nonlocal analysis, cached_quote
            if not schedule.hot_path_enabled:
                return
            analysis = await self._analyze()
            cached_quote = await quote_gas(self.pool, self.config.gas, attempt=0)
            self.log("[HOT] PREPARE analysis+gas ready")

        async def on_public_presign(stage_start: int, launch_report: InspectReport) -> None:
            nonlocal analysis, cached_quote
            lead = float(schedule.public_presign_lead_sec)
            if not schedule.public_presign_enabled or lead <= 0:
                return
            presign_at = stage_start - lead
            if time.time() < presign_at:
                await self._wait_until(presign_at, "PUBLIC PRESIGN")
            if analysis is None:
                analysis = await self._analyze()
            chain_sale = analysis.get("sale")
            meta = (analysis or {}).get("meta") or {}
            if cached_quote is None:
                cached_quote = await quote_gas(self.pool, self.config.gas, attempt=0)
            quote = cached_quote
            for cw in ctx.active_wallets:
                if cw.done:
                    continue
                stage = ctx.current_stage(cw)
                if not stage or not use_chain_public_mint(stage):
                    continue
                st, _ = stage_bounds(stage)
                if st != stage_start:
                    continue
                stage_report = self._report_for_stage(
                    launch_report,
                    stage,
                    chain_sale,
                    meta,
                    int(time.time()),
                )
                rpc_url = url_map.get(cw.address.lower())
                try:
                    cw.presigned = await self._prepare_wallet(
                        stage_report,
                        cw.wallet,
                        quote,
                        rpc_url=rpc_url,
                    )
                    self.log(
                        f"[PUBLIC] {cw.label} presigned estimateGas+sign "
                        f"T-{lead:g}s before stage open"
                    )
                except Exception as exc:
                    cw.presigned = None
                    self.log(f"[PUBLIC] {cw.label} presign failed: {exc}")

        async def maybe_sync_stages(*, force: bool = False) -> None:
            nonlocal last_stage_sync_at
            if not force and time.time() - last_stage_sync_at < 15:
                return
            last_stage_sync_at = time.time()
            await self._sync_chase_stage_times(ctx)

        def note_sold_out(source: str, detail: str) -> None:
            if not self.config.wallets.stop_on_sold_out:
                return
            if stop.is_set():
                return
            stop.set()
            self.log(f"[SOLD_OUT] {source} {detail}; skip wallets that have not sent")

        def skip_no_eligible(chase_wallet: ChaseWallet) -> dict[str, Any]:
            return {
                "wallet": chase_wallet.label,
                "address": chase_wallet.address,
                "status": "SKIP",
                "error": "no eligible stage for wallet",
                "chase_status": "NO_ELIGIBLE_STAGE",
            }

        while ctx.active_wallets:
            if stop.is_set() and self.config.wallets.stop_on_sold_out:
                for chase_wallet in ctx.active_wallets:
                    chase_wallet.finish(
                        self._skip_unsent(report, chase_wallet.wallet, "sold out, not sent"),
                        "skipped",
                    )
                break

            now = int(time.time())
            next_wake: int | None = None
            probe_batch: list[ChaseWallet] = []

            for chase_wallet in list(ctx.active_wallets):
                stage = ctx.current_stage(chase_wallet)
                if stage is None:
                    chase_wallet.finish(skip_no_eligible(chase_wallet), "NO_ELIGIBLE_STAGE")
                    continue
                start, end = stage_bounds(stage)
                if end and now > end:
                    label = (stage.get("label") or stage.get("stage_type") or "stage").strip()
                    self.log(f"[CHASE] {chase_wallet.label} stage [{label}] ended -> next")
                    if not ctx.advance_stage(chase_wallet):
                        chase_wallet.finish(skip_no_eligible(chase_wallet), "NO_ELIGIBLE_STAGE")
                    continue
                if start and now < start:
                    wake = int(start)
                    next_wake = wake if next_wake is None else min(next_wake, wake)
                    continue
                if stage_window_open(stage, now):
                    probe_batch.append(chase_wallet)

            if next_wake is not None and not probe_batch:
                await maybe_sync_stages(force=True)
                now = int(time.time())
                next_wake = None
                for chase_wallet in list(ctx.active_wallets):
                    stage = ctx.current_stage(chase_wallet)
                    if stage is None:
                        continue
                    start, _ = stage_bounds(stage)
                    if start and now < start:
                        wake = int(start)
                        next_wake = wake if next_wake is None else min(next_wake, wake)
                if next_wake is None:
                    continue
                report = await self._wait_stage_schedule(
                    next_wake,
                    report,
                    on_after_prepare=on_prepare_hot,
                    on_before_armed=on_public_presign,
                    skip_armed_refresh=schedule.hot_path_enabled,
                )
                # FINAL refresh inside wait updates report; PREPARE analysis may be stale.
                analysis = None
                await maybe_sync_stages(force=True)
                continue

            if not probe_batch:
                for chase_wallet in ctx.active_wallets:
                    if not chase_wallet.done:
                        chase_wallet.finish(skip_no_eligible(chase_wallet), "NO_ELIGIBLE_STAGE")
                break

            probe_stage = ctx.current_stage(probe_batch[0]) if probe_batch else None
            wall = int(time.time())
            hot = bool(
                probe_stage
                and hot_path_active(
                    probe_stage,
                    wall,
                    schedule.eligibility_retry_sec,
                    enabled=schedule.hot_path_enabled,
                )
            )
            public_only = bool(
                probe_batch
                and probe_stage
                and all(
                    use_chain_public_mint(stage)
                    for cw in probe_batch
                    if (stage := ctx.current_stage(cw)) is not None
                )
            )
            if hot:
                if not hot_announced:
                    if public_only:
                        self.log("[HOT] stage window: skip heavy refresh; public chain mint")
                    else:
                        self.log("[HOT] stage window: skip heavy refresh; OpenSea /mint first")
                    hot_announced = True
            else:
                hot_announced = False
                await maybe_sync_stages()
            if hot and schedule.hot_skip_refresh:
                if analysis is None:
                    analysis = await self._analyze()
            else:
                report = await self._refresh_report(report, analysis=analysis)
                if analysis is None:
                    analysis = await self._analyze()
            chain_sale = analysis.get("sale")
            meta = (analysis or {}).get("meta") or {}
            sold_status = report.sale.status
            if hot and schedule.hot_skip_refresh and probe_stage:
                total = meta.get("total_supply")
                max_supply = meta.get("max_supply")
                sold_status = sale_from_stage(
                    probe_stage,
                    wall,
                    total_supply=total,
                    max_supply=max_supply,
                ).status
            elif chain_sale is not None and hot and schedule.hot_skip_refresh:
                sold_status = chain_sale.status
            if sold_status == SaleStatus.SOLD_OUT:
                note_sold_out("refresh", "SOLD_OUT")
                for chase_wallet in ctx.active_wallets:
                    chase_wallet.finish(
                        self._skip_unsent(report, chase_wallet.wallet, "sold out, not sent"),
                        "skipped",
                    )
                break
            if hot and cached_quote is not None:
                quote = cached_quote
            else:
                quote = await quote_gas(self.pool, self.config.gas, attempt=0)
                if hot:
                    cached_quote = quote
            conc = self._concurrency()
            shard_urls = [u for u in url_map.values() if u]
            sems = {url: asyncio.Semaphore(conc) for url in dict.fromkeys(shard_urls)}

            async def handle(chase_wallet: ChaseWallet) -> None:
                if chase_wallet.done:
                    return
                stage = ctx.current_stage(chase_wallet)
                if stage is None:
                    chase_wallet.finish(skip_no_eligible(chase_wallet), "NO_ELIGIBLE_STAGE")
                    return
                try:
                    stage_report = self._report_for_stage(
                        report,
                        stage,
                        chain_sale,
                        meta,
                        int(time.time()),
                    )
                    label = (stage.get("label") or stage.get("stage_type") or "stage").strip()
                    wallet = chase_wallet.wallet
                    rpc_url = url_map.get(wallet.address.lower())

                    opensea_plan: TxPlan | None = None
                    if use_chain_public_mint(stage):
                        prepared = chase_wallet.presigned
                        if prepared is not None:
                            start_bound, _ = stage_bounds(stage)
                            if start_bound and time.time() < start_bound:
                                await self._wait_until(start_bound, "PUBLIC OPEN")
                            probe = await self._probe_chain_eligible(
                                stage_report, wallet.address, rpc_url
                            )
                            if probe is False:
                                chase_wallet.presigned = None
                                self.log(
                                    f"[CHASE] {wallet.label} not eligible [{label}] -> next stage"
                                )
                                if is_public_final_stage(stage):
                                    chase_wallet.finish(
                                        {
                                            "wallet": wallet.label,
                                            "address": wallet.address,
                                            "status": "SKIP",
                                            "error": f"not eligible for public stage [{label}]",
                                            "chase_status": "NOT_ELIGIBLE",
                                        },
                                        "NOT_ELIGIBLE",
                                    )
                                    return
                                if not ctx.advance_stage(chase_wallet):
                                    chase_wallet.finish(
                                        skip_no_eligible(chase_wallet), "NO_ELIGIBLE_STAGE"
                                    )
                                return
                            if probe is not True:
                                await asyncio.sleep(
                                    max(0.05, float(schedule.public_not_started_probe_sec))
                                )
                                return
                            chase_wallet.presigned = None
                            self.log(
                                f"[CHASE] {wallet.label} eligible [{label}] -> send presigned tx"
                            )
                            item = await self._mint_with_retry(
                                stage_report, prepared, stop=stop
                            )
                            if item.get("sold_out"):
                                note_sold_out(wallet.address, str(item.get("error") or "sold out"))
                            chase_wallet.finish(item, str(item.get("status") or "ERROR"))
                            return

                        probe = await self._probe_chain_eligible(
                            stage_report, wallet.address, rpc_url
                        )
                        if probe is None:
                            await asyncio.sleep(1)
                            return
                        if probe is False:
                            self.log(f"[CHASE] {wallet.label} not eligible [{label}] -> next stage")
                            if is_public_final_stage(stage):
                                chase_wallet.finish(
                                    {
                                        "wallet": wallet.label,
                                        "address": wallet.address,
                                        "status": "SKIP",
                                        "error": f"not eligible for public stage [{label}]",
                                        "chase_status": "NOT_ELIGIBLE",
                                    },
                                    "NOT_ELIGIBLE",
                                )
                                return
                            if not ctx.advance_stage(chase_wallet):
                                chase_wallet.finish(
                                    skip_no_eligible(chase_wallet), "NO_ELIGIBLE_STAGE"
                                )
                            return
                    else:
                        mint_probe = await self._probe_opensea_mint(wallet.address)
                        if mint_probe.ok is None:
                            await asyncio.sleep(1)
                            return
                        if mint_probe.ok is False:
                            await self._on_opensea_not_eligible(
                                chase_wallet,
                                stage,
                                label,
                                mint_probe,
                                ctx,
                                report,
                                skip_no_eligible,
                                note_sold_out,
                            )
                            return
                        opensea_plan = mint_probe.plan

                    self.log(f"[CHASE] {wallet.label} eligible [{label}] -> mint")
                    if rpc_url and rpc_url in sems:
                        async with sems[rpc_url]:
                            item = await self._mint_one_wallet(
                                stage_report,
                                wallet,
                                quote,
                                rpc_url,
                                stop,
                                note_sold_out,
                                opensea_plan=opensea_plan,
                            )
                    else:
                        item = await self._mint_one_wallet(
                            stage_report,
                            wallet,
                            quote,
                            rpc_url,
                            stop,
                            note_sold_out,
                            opensea_plan=opensea_plan,
                        )
                    chase_wallet.finish(item, str(item.get("status") or "ERROR"))
                except PriceGuardError as exc:
                    self.log(f"[PRICE_GUARD] {chase_wallet.label} {exc.message}")
                    chase_wallet.finish(
                        {
                            "wallet": chase_wallet.label,
                            "address": chase_wallet.address,
                            "status": "ERROR",
                            "error": exc.message,
                            "chase_status": "PRICE_GUARD",
                        },
                        "PRICE_GUARD",
                    )
                except Exception as exc:
                    self.log(f"[CHASE] {chase_wallet.label} error {exc}")
                    chase_wallet.finish(
                        {
                            "wallet": chase_wallet.label,
                            "address": chase_wallet.address,
                            "status": "ERROR",
                            "error": str(exc),
                        },
                        "ERROR",
                    )

            for item in await asyncio.gather(
                *(handle(cw) for cw in probe_batch),
                return_exceptions=True,
            ):
                if isinstance(item, Exception):
                    self.log(f"[CHASE] error {item}")

            await asyncio.sleep(0.05)

        results = [w.result for w in ctx.wallets if w.result]
        chase_summary = [
            {
                "wallet": w.label,
                "address": w.address,
                "chase_status": w.chase_status,
                "stage_index": w.stage_index,
            }
            for w in ctx.wallets
        ]
        return {
            "report": report.model_dump(),
            "results": results,
            "chase": {"wallets": chase_summary},
        }

    async def _mint_one_wallet(
        self,
        report: InspectReport,
        wallet,
        quote: dict[str, int],
        rpc_url: str | None,
        stop: asyncio.Event,
        note_sold_out,
        *,
        opensea_plan: TxPlan | None = None,
    ) -> dict[str, Any]:
        if self.config.wallets.stop_on_sold_out and stop.is_set():
            return self._skip_unsent(report, wallet, "sold out, not sent")
        try:
            prepared = await self._prepare_wallet(
                report, wallet, quote, rpc_url=rpc_url, opensea_plan=opensea_plan
            )
        except _SoldOut as exc:
            note_sold_out(wallet.address, str(exc))
            return self._skip_unsent(report, wallet, f"sold out, not sent ({exc})")
        except PriceGuardError as exc:
            self.log(f"[PRICE_GUARD] {wallet.address} {exc.message}")
            return {
                "wallet": wallet.label,
                "address": wallet.address,
                "status": "ERROR",
                "error": exc.message,
                "chase_status": "PRICE_GUARD",
            }
        except Exception as exc:
            self.log(f"[PREPARE FAILED] {wallet.address} {exc}")
            return {
                "wallet": wallet.label,
                "address": wallet.address,
                "status": "ERROR",
                "error": str(exc),
            }
        item = await self._mint_with_retry(report, prepared, stop=stop)
        if item.get("sold_out"):
            note_sold_out(wallet.address, str(item.get("error") or "sold out"))
        return item

    async def _blast_all_wallets(self, report: InspectReport, ready) -> list[dict[str, Any]]:
        conc = self._concurrency()
        shard_urls = self._shard_urls(len(ready))
        self.log("BLAST sign-and-send per wallet")
        self._log_shards(ready, shard_urls, conc)
        quote = await quote_gas(self.pool, self.config.gas, attempt=0)
        sems = {url: asyncio.Semaphore(conc) for url in dict.fromkeys(shard_urls)}
        stop = asyncio.Event()
        results: list[dict[str, Any]] = []

        def note_sold_out(source: str, detail: str) -> None:
            if not self.config.wallets.stop_on_sold_out:
                return
            if stop.is_set():
                return
            stop.set()
            self.log(f"[SOLD_OUT] {source} {detail}; skip wallets that have not sent")

        async def run_one(wallet, rpc_url: str):
            async with sems[rpc_url]:
                return await self._mint_one_wallet(
                    report, wallet, quote, rpc_url, stop, note_sold_out
                )

        for item in await asyncio.gather(
            *(run_one(wallet, rpc_url) for wallet, rpc_url in zip(ready, shard_urls)),
            return_exceptions=True,
        ):
            if isinstance(item, Exception):
                results.append({"status": "ERROR", "error": str(item)})
                continue
            results.append(item)
        return results

    async def _wait_stage_schedule(
        self,
        start: int,
        report: InspectReport,
        *,
        on_after_prepare: Any = None,
        on_before_armed: Any = None,
        skip_armed_refresh: bool = False,
    ) -> InspectReport:
        prepare_lead = self.config.schedule.prepare_lead_sec
        sign_lead = self.config.schedule.sign_lead_sec
        if prepare_lead > sign_lead and time.time() < start - prepare_lead:
            await self._wait_until(start - prepare_lead, "PREPARE_LEAD")
        await self._wait_until(start - sign_lead, "PREPARE")
        if on_after_prepare is not None:
            await on_after_prepare()
        self.log(
            f"T-{sign_lead}s: stage chase; wallets estimateGas+sign+send when eligible at stage open"
        )
        await self._wait_until(start - self.config.schedule.final_check_lead_sec, "FINAL REFRESH")
        report = await self._refresh_report(report)
        if report.sale.status == SaleStatus.SOLD_OUT:
            raise ConfigError(f"sale became SOLD_OUT before launch")
        if report.sale.status == SaleStatus.ENDED:
            still_chase = bool(
                self.opensea
                and self.opensea.stages
                and drop_has_future_mint_window(self.opensea.stages, int(time.time()))
            )
            if not still_chase:
                raise ConfigError("sale became ENDED before launch")
        if on_before_armed is not None:
            await on_before_armed(start, report)
        await self._wait_until(start, "ARMED")
        if skip_armed_refresh:
            self.log("[HOT] ARMED — skip post-ARMED refresh; chase uses OpenSea /mint first")
            return report
        return await self._refresh_report(report)

    async def _sync_chase_stage_times(self, ctx: ChaseContext) -> None:
        if not self.opensea or not self.opensea.slug:
            return
        try:
            fresh = await fetch_drop_stages(self.opensea.slug)
        except ConfigError as exc:
            self.log(f"[CHASE] stage refresh skipped: {exc}")
            return
        self.opensea.stages = fresh
        for label, old_start, new_start in sync_stage_times_in_sequence(ctx.sequence, fresh):
            self.log(
                f"[CHASE] stage [{label}] start moved {old_start or '?'} -> {new_start or '?'}"
            )

    async def _probe_opensea_mint(self, address: str) -> OpenSeaMintProbe:
        if not self.opensea or not self.opensea.slug:
            raise ConfigError("OpenSea drop slug missing")
        try:
            tx = await build_drop_mint_transaction(
                self.opensea.slug,
                address,
                self.config.mint.quantity,
                expected_chain_id=self.chain.chain_id,
            )
            enforce_price_guard(
                int(tx["value"]),
                self.config.mint.quantity,
                self._max_unit_price_wei(),
                native_symbol=self.chain.native_symbol,
            )
            fallback = self.config.gas.fallback_gas_limit or 280000
            plan = build_opensea_mint_plan(
                tx,
                address,
                self.chain.chain_id,
                fallback_gas_limit=fallback,
            )
            return OpenSeaMintProbe(True, plan=plan)
        except PriceGuardError:
            raise
        except EngineError as exc:
            detail = exc.message or ""
            if exc.code == "OPENSEA_NOT_ELIGIBLE":
                return OpenSeaMintProbe(False, detail=detail, sold_out=False)
            if exc.code == "OPENSEA_SOLD_OUT":
                full = mint_errors_indicate_drop_fully_sold_out(detail)
                return OpenSeaMintProbe(
                    False,
                    detail=detail,
                    sold_out=True,
                    drop_fully_sold_out=full,
                )
            if exc.code == "OPENSEA_DROP_INACTIVE":
                return OpenSeaMintProbe(None, detail=detail, sold_out=False)
            raise

    async def _on_opensea_not_eligible(
        self,
        chase_wallet: ChaseWallet,
        stage: dict[str, Any],
        label: str,
        probe: OpenSeaMintProbe,
        ctx: ChaseContext,
        report: InspectReport,
        skip_no_eligible,
        note_sold_out,
    ) -> None:
        wallet = chase_wallet.wallet
        if probe.sold_out:
            self.log(
                f"[CHASE] {wallet.label} OpenSea sold out [{label}]"
                + (f": {probe.detail}" if probe.detail else "")
            )
            if probe.drop_fully_sold_out or is_public_final_stage(stage):
                note_sold_out("OpenSea", probe.detail or label)
                chase_wallet.finish(
                    self._skip_unsent(report, wallet, probe.detail or "drop sold out"),
                    "SOLD_OUT",
                )
                return
            if not ctx.advance_stage(chase_wallet):
                chase_wallet.finish(skip_no_eligible(chase_wallet), "NO_ELIGIBLE_STAGE")
            return

        wall = int(time.time())
        retry_sec = self.config.schedule.eligibility_retry_sec
        interval = max(0.1, float(self.config.schedule.eligibility_retry_interval_sec))
        if eligibility_retry_window_open(stage, wall, retry_sec):
            start, _ = stage_bounds(stage)
            window_end = int(start or wall) + retry_sec
            remaining = max(0, window_end - wall)
            if time.time() - chase_wallet.last_probe_at >= interval:
                chase_wallet.last_probe_at = time.time()
                detail = f" ({probe.detail})" if probe.detail else ""
                self.log(
                    f"[CHASE] {wallet.label} not eligible [{label}]{detail}; "
                    f"retry {remaining:.0f}s left in window"
                )
            await asyncio.sleep(interval)
            return

        self.log(f"[CHASE] {wallet.label} not eligible [{label}] -> next stage")
        if is_public_final_stage(stage):
            chase_wallet.finish(
                {
                    "wallet": wallet.label,
                    "address": wallet.address,
                    "status": "SKIP",
                    "error": f"not eligible for public stage [{label}]",
                    "chase_status": "NOT_ELIGIBLE",
                },
                "NOT_ELIGIBLE",
            )
            return
        if not ctx.advance_stage(chase_wallet):
            chase_wallet.finish(skip_no_eligible(chase_wallet), "NO_ELIGIBLE_STAGE")

    async def _probe_chain_eligible(
        self,
        report: InspectReport,
        address: str,
        rpc_url: str | None,
    ) -> bool | None:
        if report.sale.status == SaleStatus.NOT_STARTED:
            return None
        if report.sale.status in {SaleStatus.ENDED, SaleStatus.SOLD_OUT}:
            return False
        if report.mint_route != "opensea_drop" and not report.method:
            return False
        try:
            plan = await self._build_mint_plan(report, address)
        except (ConfigError, EngineError):
            return False
        tx = {"from": address, "to": plan.to, "data": plan.data, "value": hex(plan.value)}
        try:
            await self.pool.eth_call(tx, prefer=rpc_url)
            return True
        except EngineError:
            return False

    def _report_for_stage(
        self,
        base: InspectReport,
        stage: dict[str, Any],
        chain_sale,
        meta: dict[str, Any],
        now: int,
    ) -> InspectReport:
        report = base.model_copy(deep=True)
        total = meta.get("total_supply")
        max_supply = meta.get("max_supply")
        if use_chain_public_mint(stage):
            report.mint_route = "chain_public"
            report.sale = chain_sale or sale_unknown()
        else:
            report.mint_route = "opensea_drop"
            report.sale = sale_from_stage(stage, now, total_supply=total, max_supply=max_supply)
        report.value = report.sale.price * self.config.mint.quantity
        stage_label = stage.get("label") or stage.get("stage_type")
        report.drop_stage = stage
        if stage_label:
            report.notes = list(report.notes or [])
            note = f"CHASE stage [{stage_label}]"
            if note not in report.notes:
                report.notes.append(note)
        return report

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

    async def _refresh_report(
        self,
        report: InspectReport,
        *,
        analysis: dict[str, Any] | None = None,
    ) -> InspectReport:
        if analysis is None:
            analysis = await self._analyze()
        chain_sale = analysis.get("sale")
        sale, mint_route, stage_dict, stage_notes = await self._effective_sale(chain_sale, analysis)
        report.sale = sale
        report.value = sale.price * self.config.mint.quantity
        report.mint_route = mint_route
        report.drop_stage = stage_dict
        for note in stage_notes:
            if note not in report.notes:
                report.notes.append(note)
        self.log(
            f"sale refresh route={mint_route} status={sale.status.value} "
            f"start={sale.start_time} price={sale.price}"
        )
        return report

    async def _prepare_wallet(
        self,
        report: InspectReport,
        wallet,
        quote: dict[str, int],
        nonce: int | None = None,
        rpc_url: str | None = None,
        *,
        opensea_plan: TxPlan | None = None,
    ) -> dict[str, Any]:
        if opensea_plan is not None:
            plan = opensea_plan
            self._enforce_price_guard_on_plan(plan)
        else:
            plan = await self._build_mint_plan(report, wallet.address)
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
            "method": self._result_method(report),
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
                "method": self._result_method(report),
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
                "method": self._result_method(report),
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
            "method": self._result_method(report),
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

    async def _effective_sale(self, chain_sale, analysis: dict[str, Any]):
        notes: list[str] = []
        meta = (analysis or {}).get("meta") or {}
        total = meta.get("total_supply")
        max_supply = meta.get("max_supply")
        if not self.opensea or not self.opensea.stages:
            return chain_sale or sale_unknown(), "chain_public", None, notes
        now = await self.pool.get_block_timestamp()
        stage = resolve_drop_stage(
            self.opensea.stages,
            now,
            next_stage=self.opensea.next_stage,
        )
        if stage is None:
            return chain_sale or sale_unknown(), "chain_public", None, notes
        label = (stage.get("label") or stage.get("stage_type") or "stage").strip()
        if use_chain_public_mint(stage):
            notes.append(f"Drop 自动 [{label}]：链上公开轮 mintPublic")
            return chain_sale or sale_unknown(), "chain_public", stage, notes
        if not self.opensea.slug:
            raise ConfigError("OpenSea drop slug missing for staged mint")
        sale = sale_from_stage(stage, now, total_supply=total, max_supply=max_supply)
        notes.append(
            f"Drop 自动 [{label}]：OpenSea mint API（signed/预售轮，非 mintPublic）"
        )
        return sale, "opensea_drop", stage, notes

    def _max_unit_price_wei(self) -> int:
        return native_unit_to_wei(self.config.mint.max_unit_price)

    def _price_guard_notes(self, sale) -> list[str]:
        notes: list[str] = []
        try:
            cap = self._max_unit_price_wei()
        except ConfigError as exc:
            notes.append(f"价格上限配置无效：{exc.message}")
            return notes
        sym = self.chain.native_symbol
        notes.append(
            f"Mint 单价上限：{self.config.mint.max_unit_price.strip() or '0'} {sym}（强制）"
        )
        if sale and sale.price is not None and sale.price > cap:
            notes.append(
                f"当前分析单价超过上限（链上/OpenSea 展示价），mint 将被拒绝"
            )
        return notes

    def _enforce_price_guard_on_plan(self, plan: TxPlan) -> None:
        enforce_price_guard(
            plan.value,
            self.config.mint.quantity,
            self._max_unit_price_wei(),
            native_symbol=self.chain.native_symbol,
        )

    async def _build_mint_plan(self, report: InspectReport, recipient: str) -> TxPlan:
        if report.mint_route == "opensea_drop":
            plan = await self._plan_from_opensea(recipient)
        else:
            plan = self._build_plan(report, recipient)
        self._enforce_price_guard_on_plan(plan)
        return plan

    async def _plan_from_opensea(self, recipient: str) -> TxPlan:
        if not self.opensea or not self.opensea.slug:
            raise ConfigError("OpenSea drop slug is required for staged mint")
        tx = await build_drop_mint_transaction(
            self.opensea.slug,
            recipient,
            self.config.mint.quantity,
            expected_chain_id=self.chain.chain_id,
        )
        plan = TxPlan(
            to=checksum(tx["to"]),
            data=str(tx["data"]),
            value=int(tx["value"]),
            gas=self.config.gas.fallback_gas_limit,
            chain_id=self.chain.chain_id,
            from_address=checksum(recipient),
        )
        return plan

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

    def _result_method(self, report: InspectReport) -> str | None:
        if report.mint_route == "opensea_drop":
            slug = self.opensea.slug if self.opensea else "{slug}"
            return f"OpenSea POST /drops/{slug}/mint"
        return report.method.signature if report.method else None

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
