from __future__ import annotations

from typing import Any

from mint_engine.adapters.base import Detection, MintAdapter
from mint_engine.core.models import Gap, MintMethod, SaleState, SaleStatus
from mint_engine.evm import checksum, encode_call, selector
from mint_engine.rpc.pool import RpcPool

_QUANTITY_UINT_NAMES = frozenset(
    {
        "quantity",
        "qty",
        "amount",
        "count",
        "num",
        "number",
        "mintamount",
        "tokenamount",
        "nftamount",
        "_quantity",
    }
)
_OTHER_UINT_NAMES = frozenset(
    {
        "deadline",
        "expires",
        "expiry",
        "timestamp",
        "nonce",
        "phase",
        "phaseid",
        "index",
        "maxamount",
        "limit",
    }
)
_RECIPIENT_ADDR_NAMES = frozenset(
    {
        "to",
        "recipient",
        "receiver",
        "owner",
        "minter",
        "account",
        "buyer",
        "caller",
        "_to",
    }
)
_OTHER_ADDR_NAMES = frozenset(
    {
        "feerecipient",
        "fee_recipient",
        "referrer",
        "affiliate",
        "partner",
        "royaltyrecipient",
    }
)


def _signature(item: dict) -> str:
    types = ",".join(i.get("type", "") for i in item.get("inputs") or [])
    return f"{item.get('name')}({types})"


def _extra_lookup(extra: dict[str, Any], name: str) -> Any | None:
    if not name:
        return None
    for key in (name, name.lower(), name.replace("_", "")):
        if key in extra:
            return extra[key]
    return None


def _parse_uint(extra_val: Any, quantity: int) -> int:
    if extra_val is None:
        return quantity
    if isinstance(extra_val, int):
        return extra_val
    text = str(extra_val).strip()
    if text.startswith(("0x", "0X")):
        return int(text, 16)
    return int(text)


def _parse_address(extra_val: Any, recipient: str) -> str:
    if extra_val is None:
        return checksum(recipient)
    return checksum(str(extra_val).strip())


def direct_mint_input_gaps(method: MintMethod | None, extra_params: dict[str, Any] | None) -> list[Gap]:
    if not method:
        return []
    extra = extra_params or {}
    gaps: list[Gap] = []
    uints = [i for i in method.inputs if i.get("type") == "uint256"]
    addrs = [i for i in method.inputs if i.get("type") == "address"]
    ambiguous_uint = []
    for item in uints:
        name = (item.get("name") or "").lower()
        if name in _QUANTITY_UINT_NAMES or len(uints) == 1:
            continue
        if name in _OTHER_UINT_NAMES and _extra_lookup(extra, item.get("name") or name) is not None:
            continue
        if _extra_lookup(extra, item.get("name") or "") is not None:
            continue
        ambiguous_uint.append(item.get("name") or "?")
    if ambiguous_uint:
        gaps.append(
            Gap(
                key="mint_args",
                message=f"请在 extra_params 填写 uint256 参数：{', '.join(ambiguous_uint)}",
                blocking=True,
            )
        )
    ambiguous_addr = []
    for item in addrs:
        name = (item.get("name") or "").lower()
        if name in _RECIPIENT_ADDR_NAMES or len(addrs) == 1:
            continue
        if name in _OTHER_ADDR_NAMES and _extra_lookup(extra, item.get("name") or name) is not None:
            continue
        if _extra_lookup(extra, item.get("name") or "") is not None:
            continue
        ambiguous_addr.append(item.get("name") or "?")
    if ambiguous_addr:
        gaps.append(
            Gap(
                key="mint_args",
                message=f"请在 extra_params 填写 address 参数：{', '.join(ambiguous_addr)}",
                blocking=True,
            )
        )
    return gaps


def resolve_direct_mint_arg(
    item: dict[str, Any],
    *,
    quantity: int,
    recipient: str,
    extra_params: dict[str, Any],
    uint_count: int,
    addr_count: int,
) -> Any:
    typ = item.get("type")
    name = (item.get("name") or "").lower()
    raw_name = item.get("name") or ""
    extra = extra_params or {}
    if typ == "uint256":
        if name in _QUANTITY_UINT_NAMES or uint_count == 1:
            return quantity
        found = _extra_lookup(extra, raw_name)
        if found is not None:
            return _parse_uint(found, quantity)
        if name in _OTHER_UINT_NAMES:
            raise ValueError(f"mint 参数 {raw_name} 需在 extra_params 中填写")
        if uint_count > 1:
            raise ValueError(f"无法推断 uint256 参数 {raw_name}，请在 extra_params 填写")
        return quantity
    if typ == "address":
        if name in _RECIPIENT_ADDR_NAMES or addr_count == 1:
            return checksum(recipient)
        found = _extra_lookup(extra, raw_name)
        if found is not None:
            return _parse_address(found, recipient)
        if name in _OTHER_ADDR_NAMES:
            raise ValueError(f"mint 参数 {raw_name} 需在 extra_params 中填写")
        if addr_count > 1:
            raise ValueError(f"无法推断 address 参数 {raw_name}，请在 extra_params 填写")
        return checksum(recipient)
    raise ValueError(f"unsupported argument {raw_name}:{typ}")


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
        method = context.get("method")
        if method:
            out.extend(direct_mint_input_gaps(method, context.get("extra_params")))
        return out

    def build_call(self, context: dict[str, Any], sale: SaleState, recipient: str) -> tuple[str, str, int]:
        method: MintMethod = context["method"]
        quantity = int(context["quantity"])
        extra = context.get("extra_params") or {}
        inputs = method.inputs or []
        uint_count = sum(1 for i in inputs if i.get("type") == "uint256")
        addr_count = sum(1 for i in inputs if i.get("type") == "address")
        values = []
        types = []
        for item in inputs:
            typ = item.get("type")
            types.append(typ)
            values.append(
                resolve_direct_mint_arg(
                    item,
                    quantity=quantity,
                    recipient=recipient,
                    extra_params=extra,
                    uint_count=uint_count,
                    addr_count=addr_count,
                )
            )
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
