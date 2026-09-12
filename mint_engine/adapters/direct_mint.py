from __future__ import annotations

from typing import Any

from mint_engine.adapters.base import Detection, MintAdapter
from mint_engine.core.models import Gap, MintMethod, SaleState, SaleStatus
from mint_engine.evm import encode_call, selector
from mint_engine.rpc.pool import RpcPool


def _signature(item: dict) -> str:
    types = ",".join(i.get("type", "") for i in item.get("inputs") or [])
    return f"{item.get('name')}({types})"


class DirectMintAdapter(MintAdapter):
    name = "direct"

    async def detect(self, context: dict[str, Any]) -> Detection:
        abi = context.get("abi") or []
        fns = [x for x in abi if x.get("type") == "function"]
        scored: list[tuple[float, MintMethod]] = []
        for item in fns:
            name = (item.get("name") or "").lower()
            if name not in {"mint", "publicmint", "claim", "purchase", "buy"}:
                continue
            if any(i.get("type") in {"bytes32[]", "bytes"} for i in item.get("inputs") or []):
                continue
            inputs = item.get("inputs") or []
            if not any(i.get("type") == "uint256" for i in inputs):
                continue
            mut = item.get("stateMutability")
            payable = mut == "payable"
            sig = _signature(item)
            score = 40
            if name == "publicmint":
                score += 20
            if name == "claim":
                score += 15
            if payable:
                score += 20
            if len(inputs) <= 2:
                score += 10
            method = MintMethod(
                name=item.get("name"),
                signature=sig,
                selector=selector(sig),
                to=context["contract"],
                inputs=inputs,
                payable=payable,
                confidence=min(score / 100, 0.9),
                source="verified_abi",
            )
            scored.append((method.confidence, method))
        if not scored:
            return Detection(False, 0)
        scored.sort(key=lambda x: x[0], reverse=True)
        best = scored[0][1]
        return Detection(True, best.confidence, best, "Direct", [])

    async def read_sale_state(self, pool: RpcPool, context: dict[str, Any]) -> SaleState:
        contract = context["contract"]
        now = context.get("now") or await pool.get_block_timestamp()
        price = await _try_uint(pool, contract, ["mintPrice()", "price()", "cost()", "publicPrice()"])
        start = await _try_uint(pool, contract, ["startTime()", "saleStart()", "publicSaleStart()"])
        end = await _try_uint(pool, contract, ["endTime()", "saleEnd()", "publicSaleEnd()"])
        limit = await _try_uint(pool, contract, ["maxPerWallet()", "maxMintPerWallet()", "maxWallet()"])
        total = context.get("total_supply")
        max_supply = context.get("max_supply")
        remaining = None
        if total is not None and max_supply is not None:
            remaining = max(max_supply - total, 0)
        status = SaleStatus.UNKNOWN
        active = False
        if remaining == 0:
            status = SaleStatus.SOLD_OUT
        elif end and now > end:
            status = SaleStatus.ENDED
        elif start and now < start:
            status = SaleStatus.NOT_STARTED
        elif start or price is not None:
            status = SaleStatus.ACTIVE
            active = True
        return SaleState(
            active=active,
            status=status,
            start_time=start,
            end_time=end,
            price=price or 0,
            max_per_wallet=limit,
            remaining_supply=remaining,
            total_supply=total,
            max_supply=max_supply,
        )

    def gaps(self, context: dict[str, Any], sale: SaleState) -> list[Gap]:
        out = []
        if sale.status == SaleStatus.SOLD_OUT:
            out.append(Gap(key="supply", message="已售罄", blocking=True))
        if sale.status == SaleStatus.ENDED:
            out.append(Gap(key="sale", message="销售已结束", blocking=True))
        if not context.get("abi"):
            out.append(Gap(key="abi", message="无 ABI，当前仅按常见 mint 签名猜测", blocking=False))
        return out

    def build_call(self, context: dict[str, Any], sale: SaleState, recipient: str) -> tuple[str, str, int]:
        method: MintMethod = context["method"]
        quantity = int(context["quantity"])
        values = []
        types = []
        for item in method.inputs:
            typ = item.get("type")
            name = (item.get("name") or "").lower()
            types.append(typ)
            if typ == "uint256":
                values.append(quantity)
            elif typ == "address":
                values.append(recipient)
            else:
                raise ValueError(f"unsupported argument {name}:{typ}")
        data = encode_call(method.signature, types, values)
        return method.to, data, sale.price * quantity


async def _try_uint(pool: RpcPool, contract: str, signatures: list[str]) -> int | None:
    for sig in signatures:
        try:
            raw = await pool.eth_call({"to": contract, "data": encode_call(sig, [], [])})
            if raw and raw != "0x":
                return int(raw, 16)
        except Exception:
            continue
    return None
