from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field

from mint_engine.config.chains import CHAINS, public_rpc_urls
from mint_engine.controller import MintController
from mint_engine.core.exceptions import EngineError
from mint_engine.core.models import GasConfig, RunConfig, SweepRequest
from mint_engine.discovery.opensea import prepare_config, resolve_opensea
from mint_engine.rpc.pool import RpcPool
from mint_engine.sweep import preview_sweep, run_sweep
from mint_engine.transaction.gas import public_gas_snapshot, quote_gas

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

app = FastAPI(title="Mint Engine", version="0.1.0")
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
    return FileResponse(page)


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
    urls = public_rpc_urls(resolved.chain.chain_id)
    if not urls:
        raise HTTPException(
            status_code=400,
            detail={"code": "CONFIG_ERROR", "message": "no public RPC for this chain"},
        )
    resolved.rpc.urls = urls
    controller = MintController(resolved, preview)
    try:
        result = await controller.pool.probe()
        gas = None
        try:
            quote = await quote_gas(controller.pool, resolved.gas, raise_on_cap=False)
            gas = public_gas_snapshot(quote, resolved.gas)
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


@app.post("/api/run/start")
async def run_start(config: RunConfig):
    controller = await _controller(config)
    try:
        return await controller.mint()
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    finally:
        await controller.aclose()


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
