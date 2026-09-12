from __future__ import annotations

import asyncio
from typing import Any

from mint_engine.adapters import ADAPTERS
from mint_engine.adapters.seadrop import fetch_public_drop
from mint_engine.analyzer.abi_resolver import AbiResolver
from mint_engine.analyzer.proxy import resolve_proxy
from mint_engine.config.chains import ChainPreset
from mint_engine.core.models import Capability, MintMethod
from mint_engine.evm import (
    ERC1155_IID,
    ERC721_IID,
    checksum,
    decode_string,
    decode_uint,
    encode_call,
)
from mint_engine.rpc.pool import RpcPool


class ContractAnalyzer:
    def __init__(self, pool: RpcPool, chain: ChainPreset):
        self.pool = pool
        self.chain = chain
        self.abi_resolver = AbiResolver()

    async def analyze(
        self,
        address: str,
        quantity: int,
        manual_abi: Any | None = None,
        require_manual: bool = False,
        extra_params: dict | None = None,
    ) -> dict[str, Any]:
        contract = checksum(address)
        code = await self.pool.get_code(contract)
        if not code or code == "0x":
            return {
                "contract": contract,
                "is_contract": False,
                "notes": ["address has no bytecode"],
            }

        proxy = await resolve_proxy(self.pool, contract)
        abi, abi_source = await self.abi_resolver.resolve(
            self.chain,
            contract,
            implementation=proxy["implementation"],
            manual=manual_abi,
            require_manual=require_manual,
        )

        meta = await self._read_meta(contract)
        contract_type = await self._detect_type(contract, abi)
        now = await self.pool.get_block_timestamp()
        drop = await fetch_public_drop(self.pool, contract)

        context = {
            "contract": contract,
            "quantity": quantity,
            "abi": abi or [],
            "contract_name": meta.get("name"),
            "proxy_type": proxy["proxy_type"],
            "implementation": proxy["implementation"],
            "total_supply": meta.get("total_supply"),
            "max_supply": meta.get("max_supply"),
            "now": now,
            "seadrop_drop": drop,
            "extra_params": extra_params or {},
            "contract_type": contract_type,
        }

        detections = []
        for adapter in ADAPTERS:
            result = await adapter.detect(context)
            if result.supported and result.method:
                detections.append((adapter, result))
        detections.sort(key=lambda item: item[1].confidence, reverse=True)

        adapter = detections[0][0] if detections else None
        detection = detections[0][1] if detections else None
        method = detection.method if detection else None
        if method:
            context["method"] = method

        sale = None
        gaps = []
        if adapter:
            sale = await adapter.read_sale_state(self.pool, context)
            gaps = adapter.gaps(context, sale)

        capability = self._capability(abi_source, method, gaps, detection)
        candidates = [item[1].method for item in detections if item[1].method]
        return {
            "contract": contract,
            "is_contract": True,
            "proxy": proxy,
            "abi": abi,
            "abi_source": abi_source,
            "meta": meta,
            "contract_type": contract_type,
            "adapter": adapter,
            "detection": detection,
            "method": method,
            "candidates": candidates,
            "sale": sale,
            "gaps": gaps,
            "capability": capability,
            "context": context,
            "notes": (detection.notes if detection else []) + self._notes(proxy, abi_source, method),
        }

    async def _read_meta(self, contract: str) -> dict[str, Any]:
        specs = (
            ("name", "name()", "string"),
            ("symbol", "symbol()", "string"),
            ("total_supply", "totalSupply()", "uint"),
            ("max_supply", "maxSupply()", "uint"),
            ("owner", "owner()", "address"),
        )

        async def one(key: str, sig: str, kind: str):
            raw = await self.pool.eth_call({"to": contract, "data": encode_call(sig, [], [])})
            if kind == "string":
                return key, decode_string(raw)
            if kind == "uint":
                return key, decode_uint(raw)
            if raw and raw != "0x":
                return key, checksum("0x" + raw[-40:])
            return key, None

        results = await asyncio.gather(*(one(*spec) for spec in specs), return_exceptions=True)
        out: dict[str, Any] = {}
        for item in results:
            if isinstance(item, Exception):
                continue
            key, value = item
            if value is not None:
                out[key] = value
        return out

    async def _detect_type(self, contract: str, abi: list[dict] | None) -> str:
        if await self._supports(contract, ERC721_IID):
            return "ERC721"
        if await self._supports(contract, ERC1155_IID):
            return "ERC1155"
        names = {x.get("name") for x in (abi or []) if x.get("type") == "function"}
        if {"balanceOf", "ownerOf", "tokenURI"} <= names:
            return "ERC721"
        if {"balanceOf", "transfer", "totalSupply"} <= names:
            return "ERC20"
        return "Unknown"

    async def _supports(self, contract: str, iid: str) -> bool:
        try:
            raw = await self.pool.eth_call(
                {
                    "to": contract,
                    "data": encode_call("supportsInterface(bytes4)", ["bytes4"], [bytes.fromhex(iid[2:])]),
                }
            )
            return bool(int(raw, 16)) if raw and raw != "0x" else False
        except Exception:
            return False

    def _capability(
        self,
        abi_source: str,
        method: MintMethod | None,
        gaps: list,
        detection,
    ) -> Capability:
        blocking = [g for g in gaps if getattr(g, "blocking", False)]
        if not method:
            return Capability.UNSUPPORTED
        if any(g.key in {"proof", "signature"} for g in blocking):
            return Capability.INTEGRATION_REQUIRED
        if abi_source == "获取失败" and method.source == "known_selector":
            return Capability.SEMI_AUTO
        if blocking:
            return Capability.SEMI_AUTO
        if detection and detection.confidence >= 0.8:
            return Capability.AUTO
        return Capability.SEMI_AUTO

    def _notes(self, proxy: dict, abi_source: str, method: MintMethod | None) -> list[str]:
        notes = [f"ABI 来源：{abi_source}"]
        if proxy.get("is_proxy"):
            notes.append(f"Proxy {proxy.get('proxy_type')} -> {proxy.get('implementation')}")
        if method and method.to.lower() != method.to:
            notes.append(f"mint target {method.to}")
        return notes
