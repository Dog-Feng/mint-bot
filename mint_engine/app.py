from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field

from mint_engine.config.chains import CHAINS, public_rpc_urls
from mint_engine.controller import MintController
from mint_engine.run_registry import (
    active_run_for_client,
    cancel_client_runs,
    cancel_run,
    get_run,
    heartbeat,
    snapshot_run,
    start_mint_run,
    start_treasury_collect_run,
    start_treasury_distribute_run,
    start_watchdog,
    stop_watchdog,
)
from mint_engine.core.exceptions import EngineError
from mint_engine.core.models import (
    GasConfig,
    RunConfig,
    SweepRequest,
    TreasuryBalanceRequest,
    TreasuryCollectRequest,
    TreasuryDistributeRequest,
)
from mint_engine.discovery.opensea import prepare_config, resolve_opensea
from mint_engine.discovery.opensea_http import close_opensea_client
from mint_engine.rpc.pool import RpcPool
from mint_engine.sweep import preview_sweep, run_sweep
from mint_engine.treasury import (
    preview_collect,
    preview_distribute,
    query_balances,
    run_collect,
    run_distribute,
)
from mint_engine.transaction.gas import public_gas_snapshot, quote_gas

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    await start_watchdog()
    yield
    await stop_watchdog()
    await close_opensea_client()


app = FastAPI(title="Mint Engine", version="0.1.0", lifespan=_app_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
async def index():
    page = WEB_DIR / "console.html"
    if not page.exists():
        return {"ok": True, "service": "mint-engine"}
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/treasury")
async def treasury_page():
    """与首页同一 SPA；路径仅用于默认打开「钱包分发」页签。"""
    page = WEB_DIR / "console.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="console page not found")
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
async def favicon():
    icon = WEB_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return Response(status_code=204)


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/chains")
async def chains():
    return [
        {
            "key": c.key,
            "name": c.name,
            "chain_id": c.chain_id,
            "native_symbol": c.native_symbol,
            "public_rpc": c.public_rpc,
            "public_rpcs": public_rpc_urls(c.chain_id),
            "explorer": c.explorer,
            "has_public_mempool": c.has_public_mempool,
        }
        for c in CHAINS.values()
    ]


class OpenSeaResolveRequest(BaseModel):
    url: str


async def _controller(config: RunConfig) -> MintController:
    resolved, preview = await prepare_config(config)
    return MintController(resolved, preview)


@app.post("/api/opensea/resolve")
async def opensea_resolve(payload: OpenSeaResolveRequest):
    try:
        preview = await resolve_opensea(payload.url)
        chain = CHAINS[preview.chain_id]
        return {
            "opensea": preview.model_dump(),
            "chain": {
                "key": chain.key,
                "name": chain.name,
                "chain_id": chain.chain_id,
                "public_rpc": chain.public_rpc,
                "public_rpcs": public_rpc_urls(chain.chain_id),
                "has_public_mempool": chain.has_public_mempool,
            },
            "contract": preview.contract,
        }
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc


class GasWatchRequest(BaseModel):
    chain_id: int
    rpc_urls: list[str] = []
    gas: GasConfig = Field(default_factory=GasConfig)


async def _quote_on_urls(chain_id: int, urls: list[str], gas: GasConfig) -> dict:
    cleaned = [u.strip() for u in urls if u and u.strip()]
    if not cleaned:
        cleaned = public_rpc_urls(chain_id)
        if not cleaned:
            raise EngineError("CONFIG_ERROR", "no public RPC for this chain")
    pool = RpcPool(cleaned, chain_id, timeout_ms=4000)
    pool.primary_url = cleaned[0]
    pool.broadcast_urls = cleaned
    try:
        quote = await quote_gas(pool, gas, raise_on_cap=False)
        return public_gas_snapshot(quote, gas)
    finally:
        await pool.aclose()


@app.post("/api/rpc/probe")
async def probe(config: RunConfig):
    resolved, preview = await prepare_config(config)
    urls = [u.strip() for u in resolved.rpc.urls if u and u.strip()]
    if not urls:
        urls = public_rpc_urls(resolved.chain.chain_id)
        resolved.rpc.urls = urls
    if not urls:
        raise HTTPException(
            status_code=400,
            detail={"code": "CONFIG_ERROR", "message": "no RPC url provided"},
        )
    controller = MintController(resolved, preview)
    try:
        result = await controller.pool.probe()
        gas = None
        try:
            gas = await _quote_on_urls(
                resolved.chain.chain_id,
                public_rpc_urls(resolved.chain.chain_id),
                resolved.gas,
            )
        except Exception:
            gas = None
        return {"rpc": [item.model_dump() for item in result], "gas": gas}
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    finally:
        await controller.aclose()


@app.post("/api/gas/quote")
async def gas_quote(payload: GasWatchRequest):
    try:
        return await _quote_on_urls(payload.chain_id, payload.rpc_urls, payload.gas)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc


@app.post("/api/inspect")
async def inspect(config: RunConfig):
    controller = await _controller(config)
    try:
        report = await controller.inspect()
        return report.model_dump()
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    finally:
        await controller.aclose()


@app.post("/api/dry-run")
async def dry_run(config: RunConfig):
    controller = await _controller(config)
    try:
        return await controller.dry_run()
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    finally:
        await controller.aclose()


class RunStartRequest(BaseModel):
    client_id: str = Field(min_length=8, max_length=128)
    config: RunConfig


class RunHeartbeatRequest(BaseModel):
    run_id: str
    client_id: str


class RunCancelRequest(BaseModel):
    run_id: str | None = None
    client_id: str | None = None


@app.post("/api/run/start")
async def run_start(payload: RunStartRequest):
    try:
        run_id = await start_mint_run(payload.config, payload.client_id.strip())
        return {"run_id": run_id, "status": "running"}
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc


@app.get("/api/run/{run_id}")
async def run_status(run_id: str):
    record = get_run(run_id)
    if not record:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "run not found"})
    return snapshot_run(record)


@app.post("/api/run/heartbeat")
async def run_heartbeat(payload: RunHeartbeatRequest):
    ok = await heartbeat(payload.run_id.strip(), payload.client_id.strip())
    if not ok:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "run not active"})
    return {"ok": True}


@app.post("/api/run/cancel")
async def run_cancel(payload: RunCancelRequest):
    if payload.run_id:
        await cancel_run(payload.run_id.strip(), reason="client cancel")
        return {"ok": True}
    if payload.client_id:
        await cancel_client_runs(payload.client_id.strip(), reason="client cancel")
        return {"ok": True}
    raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": "run_id or client_id required"})


@app.get("/api/run/active")
async def run_active(client_id: str):
    record = active_run_for_client(client_id.strip())
    if not record:
        return {"active": False}
    return {"active": True, **snapshot_run(record)}


@app.post("/api/treasury/balance")
async def treasury_balance(payload: TreasuryBalanceRequest):
    try:
        return await query_balances(payload.chain_id, payload.rpc_urls, payload.private_keys)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc


@app.post("/api/treasury/distribute/preview")
async def treasury_distribute_preview(payload: TreasuryDistributeRequest):
    try:
        return await preview_distribute(payload)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc


@app.post("/api/treasury/distribute/run")
async def treasury_distribute_run(payload: TreasuryDistributeRequest):
    try:
        return await run_distribute(payload)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc


class TreasuryDistributeStartRequest(TreasuryDistributeRequest):
    client_id: str = Field(min_length=8, max_length=128)


class TreasuryCollectStartRequest(TreasuryCollectRequest):
    client_id: str = Field(min_length=8, max_length=128)


@app.post("/api/treasury/distribute/start")
async def treasury_distribute_start(payload: TreasuryDistributeStartRequest):
    try:
        body = payload.model_dump(exclude={"client_id"})
        req = TreasuryDistributeRequest.model_validate(body)
        run_id = await start_treasury_distribute_run(req, payload.client_id.strip())
        return {"run_id": run_id, "status": "running"}
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc


@app.post("/api/treasury/collect/start")
async def treasury_collect_start(payload: TreasuryCollectStartRequest):
    try:
        body = payload.model_dump(exclude={"client_id"})
        req = TreasuryCollectRequest.model_validate(body)
        run_id = await start_treasury_collect_run(req, payload.client_id.strip())
        return {"run_id": run_id, "status": "running"}
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc


@app.post("/api/treasury/collect/preview")
async def treasury_collect_preview(payload: TreasuryCollectRequest):
    try:
        return await preview_collect(payload)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc


@app.post("/api/treasury/collect/run")
async def treasury_collect_run(payload: TreasuryCollectRequest):
    try:
        return await run_collect(payload)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc


@app.post("/api/sweep/preview")
async def sweep_preview(payload: SweepRequest):
    try:
        return await preview_sweep(payload)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc


@app.post("/api/sweep/run")
async def sweep_run(payload: SweepRequest):
    try:
        return await run_sweep(payload)
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "CONFIG_ERROR", "message": str(exc)}) from exc
