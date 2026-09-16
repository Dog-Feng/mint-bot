from __future__ import annotations

import re
from urllib.parse import urlparse

from mint_engine.config.chains import chain_id_from_opensea, get_chain, public_rpc_urls
from mint_engine.discovery.opensea_http import ensure_opensea_client, loads_json, opensea_request
from mint_engine.config.settings import get_settings
from mint_engine.core.exceptions import ConfigError
from mint_engine.core.models import OpenSeaPreview, RunConfig
from mint_engine.evm import checksum

COLLECTION_RE = re.compile(
    r"/(?:collection|drops)/([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)
ASSET_RE = re.compile(
    r"/assets(?:/ethereum)?/([a-zA-Z0-9_-]+)/(0x[a-fA-F0-9]{40})",
    re.IGNORECASE,
)


def parse_opensea_input(value: str) -> dict:
    text = (value or "").strip()
    if not text:
        raise ConfigError("OpenSea URL is empty")
    if re.fullmatch(r"[a-zA-Z0-9_-]+", text) and "opensea.io" not in text:
        return {"kind": "slug", "slug": text}
    parsed = urlparse(text if "://" in text else "https://" + text)
    host = (parsed.netloc or "").lower()
    if "opensea.io" not in host:
        raise ConfigError("not an OpenSea URL")
    asset = ASSET_RE.search(parsed.path or "")
    if asset:
        return {
            "kind": "asset",
            "chain": asset.group(1),
            "contract": checksum(asset.group(2)),
        }
    collection = COLLECTION_RE.search(parsed.path or "")
    if collection:
        return {"kind": "slug", "slug": collection.group(1)}
    raise ConfigError(f"cannot parse OpenSea URL: {text}")


async def resolve_opensea(url_or_slug: str) -> OpenSeaPreview:
    settings = get_settings()
    if not settings.opensea_api_key:
        raise ConfigError("OPENSEA_API_KEY is missing")
    parsed = parse_opensea_input(url_or_slug)
    await ensure_opensea_client()
    if parsed["kind"] == "asset":
        chain = parsed["chain"]
        contract = parsed["contract"]
        return OpenSeaPreview(
            slug=contract,
            url=url_or_slug,
            chain=chain,
            chain_id=chain_id_from_opensea(chain),
            contract=contract,
            contracts=[{"address": contract, "chain": chain}],
            notes=["parsed from OpenSea asset URL"],
        )
    slug = parsed["slug"]
    collection = await _get(f"https://api.opensea.io/api/v2/collections/{slug}")
    drop = await _get_optional(f"https://api.opensea.io/api/v2/drops/{slug}")
    contracts = collection.get("contracts") or []
    drop_contract = (drop or {}).get("contract_address")
    drop_chain = (drop or {}).get("chain")
    if drop_contract:
        contract = checksum(drop_contract)
        chain_name = drop_chain or (contracts[0].get("chain") if contracts else None)
    elif len(contracts) == 1:
        contract = checksum(contracts[0]["address"])
        chain_name = contracts[0].get("chain")
    elif not contracts:
        raise ConfigError(f"OpenSea collection {slug} has no contract")
    else:
        raise ConfigError(
            "OpenSea collection has multiple contracts; pass mint.contract to choose one",
            details={"contracts": contracts},
        )
    if not chain_name:
        raise ConfigError("OpenSea response has no chain")
    try:
        chain_id = chain_id_from_opensea(chain_name)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    notes = [f"resolved slug {slug}"]
    if drop and drop.get("drop_type"):
        notes.append(f"drop_type={drop.get('drop_type')}")
    return OpenSeaPreview(
        slug=slug,
        url=collection.get("opensea_url") or f"https://opensea.io/collection/{slug}",
        chain=chain_name,
        chain_id=chain_id,
        contract=contract,
        contracts=[{"address": item.get("address", ""), "chain": item.get("chain", "")} for item in contracts],
        collection_name=collection.get("name") or (drop or {}).get("collection_name"),
        drop_type=(drop or {}).get("drop_type"),
        is_minting=(drop or {}).get("is_minting"),
        max_supply=(drop or {}).get("max_supply"),
        total_supply=(drop or {}).get("total_supply"),
        next_stage=(drop or {}).get("next_stage"),
        stages=(drop or {}).get("stages") or [],
        notes=notes,
    )


async def fetch_drop_stages(slug: str) -> list[dict]:
    settings = get_settings()
    if not settings.opensea_api_key:
        raise ConfigError("OPENSEA_API_KEY is missing")
    if not (slug or "").strip():
        raise ConfigError("OpenSea drop slug is required")
    await ensure_opensea_client()
    drop = await _get_optional(f"https://api.opensea.io/api/v2/drops/{slug.strip()}")
    if not drop:
        raise ConfigError(f"OpenSea drop not found: {slug}")
    return list(drop.get("stages") or [])


async def prepare_config(config: RunConfig) -> tuple[RunConfig, OpenSeaPreview | None]:
    contract = (config.mint.contract or "").strip()
    url = (config.mint.opensea_url or "").strip()
    if not url and "opensea.io" in contract.lower():
        url = contract
        contract = ""
    preview = None
    if url:
        preview = await resolve_opensea(url)
        if config.chain.chain_id and config.chain.chain_id != preview.chain_id:
            raise ConfigError(
                f"selected chain_id {config.chain.chain_id} != OpenSea chain {preview.chain} ({preview.chain_id})"
            )
        if contract and checksum(contract) != checksum(preview.contract):
            preview.notes.append(
                f"OpenSea contract {preview.contract} overrides pasted address {checksum(contract)}"
            )
        config.chain.chain_id = preview.chain_id
        config.chain.key = get_chain(preview.chain_id).key
        config.mint.contract = preview.contract
        config.mint.opensea_url = preview.url
    if config.chain.chain_id is None:
        raise ConfigError("chain_id is required")
    if not (config.mint.contract or "").strip():
        raise ConfigError("contract is required")
    if not config.rpc.urls:
        urls = public_rpc_urls(config.chain.chain_id)
        if not urls:
            raise ConfigError("no RPC url provided")
        config.rpc.urls = urls
        if preview:
            preview.notes.append(f"filled public RPC {urls[0]}")
    return config, preview


async def _get(url: str) -> dict:
    response = await opensea_request("GET", url)
    if response.status_code == 401:
        raise ConfigError("OpenSea API key was rejected")
    if response.status_code == 404:
        raise ConfigError(f"OpenSea resource not found: {url}")
    if response.status_code >= 400:
        snippet = (response.content or b"")[:200].decode("utf-8", errors="replace")
        raise ConfigError(f"OpenSea API {response.status_code}: {snippet}")
    data = loads_json(response.content or b"")
    if isinstance(data, dict) and data.get("errors"):
        raise ConfigError(str(data["errors"]))
    return data


async def _get_optional(url: str) -> dict | None:
    response = await opensea_request("GET", url)
    if response.status_code == 404:
        return None
    if response.status_code == 401:
        raise ConfigError("OpenSea API key was rejected")
    if response.status_code >= 400:
        snippet = (response.content or b"")[:200].decode("utf-8", errors="replace")
        raise ConfigError(f"OpenSea API {response.status_code}: {snippet}")
    data = loads_json(response.content or b"")
    if isinstance(data, dict) and data.get("errors"):
        return None
    return data if isinstance(data, dict) else None
