from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from mint_engine.config.settings import get_settings

_log = logging.getLogger("mint_engine")

OPENSEA_BASE = "https://api.opensea.io"

_CLIENT: httpx.AsyncClient | None = None
_CLIENT_LOCK = asyncio.Lock()


def opensea_headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "x-api-key": settings.opensea_api_key or "",
        "accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "mint-engine/0.1",
    }


def loads_json(content: bytes) -> Any:
    if not content:
        return {}
    return json.loads(content)


async def ensure_opensea_client(
    *,
    connect_timeout: float = 2.0,
    read_timeout: float = 10.0,
    http2: bool = False,
) -> httpx.AsyncClient:
    """Process-wide keep-alive client for api.opensea.io."""
    global _CLIENT
    if _CLIENT is not None and not _CLIENT.is_closed:
        return _CLIENT
    async with _CLIENT_LOCK:
        if _CLIENT is not None and not _CLIENT.is_closed:
            return _CLIENT
        limits = httpx.Limits(
            max_keepalive_connections=20,
            max_connections=40,
            keepalive_expiry=120.0,
        )
        timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
        base_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "limits": limits,
            "headers": opensea_headers(),
            "follow_redirects": True,
        }
        if http2:
            try:
                _CLIENT = httpx.AsyncClient(http2=True, **base_kwargs)
            except ImportError:
                _log.warning("[OPENSEA] HTTP/2 unavailable (install httpx[http2]); using HTTP/1.1")
                _CLIENT = httpx.AsyncClient(**base_kwargs)
        else:
            _CLIENT = httpx.AsyncClient(**base_kwargs)
        return _CLIENT


async def close_opensea_client() -> None:
    global _CLIENT
    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            await _CLIENT.aclose()
            _CLIENT = None


async def warm_opensea_drop(slug: str) -> None:
    """Keep-alive + TLS session warm before stage open."""
    text = (slug or "").strip()
    if not text:
        return
    try:
        client = await ensure_opensea_client()
        url = f"{OPENSEA_BASE}/api/v2/drops/{text}"
        response = await client.get(url)
        _log.info("[OPENSEA] warm GET drops/%s HTTP %s", text, response.status_code)
    except Exception as exc:
        _log.warning("[OPENSEA] warm connection failed: %s", exc)


async def opensea_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    client = await ensure_opensea_client()
    for attempt in range(2):
        response = await client.request(method, url, **kwargs)
        if response.status_code != 429 or attempt == 1:
            if response.status_code == 429:
                _log.warning("[OPENSEA] HTTP 429 on %s %s", method, url)
            return response
        _log.warning("[OPENSEA] HTTP 429; retry once after 0.5s")
        await asyncio.sleep(0.5)
    return response  # pragma: no cover
