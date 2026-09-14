from __future__ import annotations

from typing import Any

from mint_engine.adapters.base import Detection, MintAdapter
from mint_engine.core.models import Gap, MintMethod, SaleState, SaleStatus
from mint_engine.evm import SEADROP_V1, checksum, encode_call, selector
from mint_engine.rpc.pool import RpcPool

MINT_PUBLIC = "mintPublic(address,address,address,uint256)"
GET_PUBLIC_DROP = "getPublicDrop(address)"
GET_ALLOWED_FEES = "getAllowedFeeRecipients(address)"
GET_CREATOR = "getCreatorPayoutAddress(address)"


class SeaDropAdapter(MintAdapter):
    name = "seadrop"

    async def detect(self, context: dict[str, Any]) -> Detection:
        abi = context.get("abi") or []
        names = {item.get("name") for item in abi if item.get("type") == "function"}
        contract_name = context.get("contract_name") or ""
        score = 0.0
        notes = []
        if "mintSeaDrop" in names or "OnlyAllowedSeaDrop" in {
            item.get("name") for item in abi if item.get("type") == "error"
        }:
            score += 0.7
            notes.append("ABI contains SeaDrop mint entry")
        if "SeaDrop" in contract_name or "ERC721SeaDrop" in contract_name:
            score += 0.2
        if context.get("proxy_type") == "EIP1167":
            score += 0.05
        drop = context.get("seadrop_drop")
        if drop:
            score = max(score, 0.95)
            notes.append("SeaDrop getPublicDrop returned sale config")
        if score < 0.5:
            return Detection(False, score)
        method = MintMethod(
            name="mintPublic",
            signature=MINT_PUBLIC,
            selector=selector(MINT_PUBLIC),
            to=SEADROP_V1,
            inputs=[
                {"name": "nftContract", "type": "address"},
                {"name": "feeRecipient", "type": "address"},
                {"name": "minterIfNotPayer", "type": "address"},
                {"name": "quantity", "type": "uint256"},
            ],
            payable=True,
            confidence=min(score, 0.99),
            source="seadrop_adapter",
        )
        return Detection(True, method.confidence, method, "SeaDrop", notes)

    async def read_sale_state(self, pool: RpcPool, context: dict[str, Any]) -> SaleState:
        nft = context["contract"]
        drop = context.get("seadrop_drop") or await fetch_public_drop(pool, nft)
        context["seadrop_drop"] = drop
        fees = context.get("seadrop_fees")
        if fees is None:
            fees = await fetch_allowed_fees(pool, nft)
            context["seadrop_fees"] = fees
        creator = context.get("seadrop_creator")
        if creator is None:
            creator = await fetch_creator(pool, nft)
            context["seadrop_creator"] = creator

        now = context.get("now") or await pool.get_block_timestamp()
        total = context.get("total_supply")
        max_supply = context.get("max_supply")
        remaining = None
        if total is not None and max_supply is not None:
            remaining = max(max_supply - total, 0)

        status = SaleStatus.UNKNOWN
        active = False
        if drop:
            start, end, price = drop["start_time"], drop["end_time"], drop["price"]
            if remaining == 0:
                status = SaleStatus.SOLD_OUT
            elif end and now > end:
                status = SaleStatus.ENDED
            elif start and now < start:
                status = SaleStatus.NOT_STARTED
            else:
                status = SaleStatus.ACTIVE
                active = True
            return SaleState(
                active=active,
                status=status,
                start_time=start,
                end_time=end,
                price=price,
                max_per_wallet=drop["max_per_wallet"],
                remaining_supply=remaining,
                total_supply=total,
                max_supply=max_supply,
                extra={
                    "fee_bps": drop["fee_bps"],
                    "restrict_fee_recipients": drop["restrict_fee_recipients"],
                    "fee_recipients": fees,
                    "creator_payout": creator,
                    "seadrop": SEADROP_V1,
                },
            )
        return SaleState(
            status=SaleStatus.UNKNOWN,
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
        return out

    def build_call(self, context: dict[str, Any], sale: SaleState, recipient: str) -> tuple[str, str, int]:
        fee = _seadrop_fee_recipient(sale)
        quantity = int(context["quantity"])
        data = encode_call(
            MINT_PUBLIC,
            ["address", "address", "address", "uint256"],
            [checksum(context["contract"]), checksum(fee), checksum(recipient), quantity],
        )
        return SEADROP_V1, data, sale.price * quantity


def _seadrop_fee_recipient(sale: SaleState) -> str:
    """Project-configured SeaDrop fee payee; users do not choose this."""
    fees = sale.extra.get("fee_recipients") or []
    if fees:
        return checksum(fees[0])
    creator = sale.extra.get("creator_payout")
    if creator:
        return checksum(creator)
    raise ValueError("SeaDrop fee recipient unavailable from chain")


async def fetch_public_drop(pool: RpcPool, nft: str) -> dict | None:
    data = encode_call(GET_PUBLIC_DROP, ["address"], [checksum(nft)])
    try:
        raw = await pool.eth_call({"to": SEADROP_V1, "data": data})
    except Exception:
        return None
    if not raw or raw == "0x":
        return None
    hexdata = raw[2:]
    if len(hexdata) < 64 * 6:
        return None
    words = [int(hexdata[i : i + 64], 16) for i in range(0, 64 * 6, 64)]
    return {
        "price": words[0],
        "start_time": words[1],
        "end_time": words[2],
        "max_per_wallet": words[3],
        "fee_bps": words[4],
        "restrict_fee_recipients": bool(words[5]),
    }


async def fetch_allowed_fees(pool: RpcPool, nft: str) -> list[str]:
    data = encode_call(GET_ALLOWED_FEES, ["address"], [checksum(nft)])
    try:
        raw = await pool.eth_call({"to": SEADROP_V1, "data": data})
    except Exception:
        return []
    if not raw or raw == "0x" or len(raw) < 2 + 64 * 3:
        return []
    hexdata = raw[2:]
    count = int(hexdata[64:128], 16)
    fees = []
    for i in range(count):
        start = 128 + i * 64
        word = hexdata[start : start + 64]
        if len(word) < 64:
            break
        fees.append(checksum("0x" + word[-40:]))
    return fees


async def fetch_creator(pool: RpcPool, nft: str) -> str | None:
    data = encode_call(GET_CREATOR, ["address"], [checksum(nft)])
    try:
        raw = await pool.eth_call({"to": SEADROP_V1, "data": data})
    except Exception:
        return None
    if not raw or raw == "0x":
        return None
    return checksum("0x" + raw[-40:])
