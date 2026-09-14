from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mint_engine.core.models import SaleState, SaleStatus

_SKIP_STAGE_TYPES = frozenset({"team", "team_mint"})


def _auto_eligible(stage: dict[str, Any]) -> bool:
    stage_type = (stage.get("stage_type") or "").lower().strip()
    if stage_type in _SKIP_STAGE_TYPES or stage_type.startswith("team_"):
        return False
    label = (stage.get("label") or "").strip().lower()
    if label == "team" or label.startswith("team "):
        return False
    return True


def _stage_uuid(stage: dict[str, Any]) -> str:
    return str(stage.get("uuid") or "").lower()


def _match_next_stage(
    stages: list[dict[str, Any]],
    next_stage: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not next_stage:
        return None
    want = _stage_uuid(next_stage)
    if not want:
        return None
    for stage in stages:
        if _stage_uuid(stage) == want:
            return stage
    return None


def parse_stage_time(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def stage_bounds(stage: dict[str, Any]) -> tuple[int | None, int | None]:
    start = parse_stage_time(stage.get("start_time") or stage.get("startTime"))
    end = parse_stage_time(stage.get("end_time") or stage.get("endTime"))
    return start, end


def stage_status(stage: dict[str, Any], now: int) -> SaleStatus:
    start, end = stage_bounds(stage)
    if start and now < start:
        return SaleStatus.NOT_STARTED
    if end and now > end:
        return SaleStatus.ENDED
    if start or end:
        return SaleStatus.ACTIVE
    return SaleStatus.UNKNOWN


def _price_wei(stage: dict[str, Any]) -> int:
    raw = stage.get("price")
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if not text:
        return 0
    return int(text, 16) if text.startswith(("0x", "0X")) else int(text)


def sale_from_stage(
    stage: dict[str, Any],
    now: int,
    *,
    total_supply: int | None = None,
    max_supply: int | None = None,
) -> SaleState:
    start, end = stage_bounds(stage)
    status = stage_status(stage, now)
    remaining = None
    if total_supply is not None and max_supply is not None:
        remaining = max(max_supply - total_supply, 0)
        if remaining == 0:
            status = SaleStatus.SOLD_OUT
    max_wallet = stage.get("max_per_wallet")
    if max_wallet is not None and str(max_wallet).strip() != "":
        max_wallet = int(max_wallet)
    else:
        max_wallet = None
    active = status == SaleStatus.ACTIVE
    return SaleState(
        active=active,
        status=status,
        start_time=start,
        end_time=end,
        price=_price_wei(stage),
        max_per_wallet=max_wallet,
        remaining_supply=remaining,
        total_supply=total_supply,
        max_supply=max_supply,
        extra={
            "opensea_stage_uuid": stage.get("uuid"),
            "opensea_stage_type": stage.get("stage_type"),
            "opensea_stage_label": stage.get("label"),
        },
    )


def pick_auto_stage(
    stages: list[dict[str, Any]],
    now: int,
    *,
    next_stage: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not stages:
        return None
    eligible = [s for s in stages if _auto_eligible(s)]
    pool = eligible if eligible else list(stages)

    active = [s for s in pool if stage_status(s, now) == SaleStatus.ACTIVE]
    if active:
        return max(active, key=lambda s: stage_bounds(s)[0] or 0)

    upcoming: list[tuple[int, dict[str, Any]]] = []
    for stage in pool:
        start, _ = stage_bounds(stage)
        if start and now < start:
            upcoming.append((start, stage))
    upcoming.sort(key=lambda item: item[0])

    if upcoming:
        earliest_start, earliest = upcoming[0]
        hinted = _match_next_stage(pool, next_stage) or _match_next_stage(stages, next_stage)
        if hinted and _auto_eligible(hinted):
            hinted_start, _ = stage_bounds(hinted)
            if stage_status(hinted, now) == SaleStatus.NOT_STARTED and (
                _stage_uuid(hinted) == _stage_uuid(earliest)
                or hinted_start == earliest_start
            ):
                return hinted
        return earliest

    hinted = _match_next_stage(pool, next_stage) or _match_next_stage(stages, next_stage)
    if hinted and _auto_eligible(hinted):
        status = stage_status(hinted, now)
        if status in {SaleStatus.ACTIVE, SaleStatus.NOT_STARTED}:
            return hinted
    ended = [s for s in pool if stage_status(s, now) == SaleStatus.ENDED]
    if ended:
        return ended[-1]
    return pool[-1]


def resolve_drop_stage(
    stages: list[dict[str, Any]],
    now: int,
    *,
    next_stage: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not stages:
        return None
    return pick_auto_stage(stages, now, next_stage=next_stage)


def use_chain_public_mint(stage: dict[str, Any] | None) -> bool:
    if stage is None:
        return True
    return (stage.get("stage_type") or "").lower() == "public_sale"
