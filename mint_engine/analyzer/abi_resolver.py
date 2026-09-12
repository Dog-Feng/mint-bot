from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from mint_engine.config.chains import ChainPreset
from mint_engine.config.settings import get_settings


def parse_abi_json(raw: Any) -> list[dict]:
    if raw is None:
        raise ValueError("ABI is empty")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            raise ValueError("ABI is empty")
        raw = json.loads(raw)
    if isinstance(raw, dict):
        raw = raw.get("abi", raw)
    if not isinstance(raw, list):
        raise ValueError("ABI must be a JSON array or an object with an abi field")
    return raw


def function_items(abi: list[dict]) -> list[dict]:
    return [x for x in abi if isinstance(x, dict) and x.get("type") == "function"]


class AbiResolver:
    def __init__(self, cache_dir: Path | None = None):
        settings = get_settings()
        self.cache_dir = cache_dir or settings.abi_cache_dir
        self.api_key = settings.etherscan_api_key
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def cache_path(self, chain_id: int, address: str) -> Path:
        return self.cache_dir / str(chain_id) / f"{address.lower()}.json"

    def load_cache(self, chain_id: int, address: str) -> list[dict] | None:
        path = self.cache_path(chain_id, address)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        abi = data.get("abi")
        return abi if isinstance(abi, list) else None

    def save_cache(self, chain_id: int, address: str, abi: list[dict], source: str) -> None:
        path = self.cache_path(chain_id, address)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "address": address.lower(),
                    "chain_id": chain_id,
                    "source": source,
                    "abi": abi,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    async def resolve(
        self,
        chain: ChainPreset,
        address: str,
        implementation: str | None = None,
        manual: Any | None = None,
        require_manual: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[list[dict] | None, str]:
        if manual is not None:
            abi = parse_abi_json(manual)
            self.save_cache(chain.chain_id, address, abi, "manual")
            return abi, "用户手动输入"
        if require_manual:
            return None, "需要手动 ABI"

        cached = self.load_cache(chain.chain_id, address)
        if cached:
            return cached, "本地缓存"

        own_client = client is None
        client = client or httpx.AsyncClient(timeout=12.0, headers={"User-Agent": "mint-engine/0.1"})
        try:
            for target, label in (
                (address, "浏览器已验证 ABI"),
                (implementation, "Implementation ABI"),
            ):
                if not target:
                    continue
                abi = await self._fetch_remote(client, chain, target)
                if abi:
                    self.save_cache(chain.chain_id, address, abi, label)
                    return abi, label
        finally:
            if own_client:
                await client.aclose()
        return None, "获取失败"

    async def _fetch_remote(
        self, client: httpx.AsyncClient, chain: ChainPreset, address: str
    ) -> list[dict] | None:
        if chain.etherscan_chain_id and chain.explorer_api and "etherscan.io" in chain.explorer_api:
            abi = await self._fetch_etherscan(client, chain, address)
            if abi:
                return abi
        if chain.explorer_api and "blockscout" in (chain.explorer_api + (chain.explorer or "")):
            abi = await self._fetch_blockscout(client, chain, address)
            if abi:
                return abi
        if chain.sourcify:
            abi = await self._fetch_sourcify(client, chain.chain_id, address)
            if abi:
                return abi
        return None

    async def _fetch_etherscan(
        self, client: httpx.AsyncClient, chain: ChainPreset, address: str
    ) -> list[dict] | None:
        params = {
            "chainid": chain.etherscan_chain_id,
            "module": "contract",
            "action": "getsourcecode",
            "address": address,
        }
        if self.api_key:
            params["apikey"] = self.api_key
        try:
            response = await client.get(chain.explorer_api, params=params)
            body = response.json()
            rows = body.get("result")
            if not rows or not isinstance(rows, list):
                return None
            abi_text = rows[0].get("ABI")
            if not abi_text or abi_text == "Contract source code not verified":
                return None
            return parse_abi_json(abi_text)
        except Exception:
            return None

    async def _fetch_blockscout(
        self, client: httpx.AsyncClient, chain: ChainPreset, address: str
    ) -> list[dict] | None:
        try:
            response = await client.get(
                chain.explorer_api,
                params={"module": "contract", "action": "getabi", "address": address},
            )
            body = response.json()
            result = body.get("result")
            if body.get("status") != "1" or not result:
                return None
            return parse_abi_json(result)
        except Exception:
            return None

    async def _fetch_sourcify(
        self, client: httpx.AsyncClient, chain_id: int, address: str
    ) -> list[dict] | None:
        urls = [
            f"https://sourcify.dev/server/v2/contract/{chain_id}/{address}",
            f"https://repo.sourcify.dev/contracts/full_match/{chain_id}/{address}/metadata.json",
            f"https://repo.sourcify.dev/contracts/partial_match/{chain_id}/{address}/metadata.json",
        ]
        for url in urls:
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    continue
                data = response.json()
                abi = data.get("abi") or (data.get("output") or {}).get("abi")
                if abi:
                    return parse_abi_json(abi)
            except Exception:
                continue
        return None
