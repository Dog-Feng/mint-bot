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


def build_stage_sequence(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not stages:
        return []
    eligible = [s for s in stages if _auto_eligible(s)]
    pool = eligible if eligible else list(stages)
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for stage in sorted(pool, key=lambda s: stage_bounds(s)[0] or 0):
        uid = _stage_uuid(stage)
        if not uid or uid in seen:
            continue
        seen.add(uid)
        ordered.append(stage)
    return ordered


def stage_index_in_sequence(sequence: list[dict[str, Any]], stage: dict[str, Any] | None) -> int:
    if not sequence:
        return 0
    if stage is None:
        return 0
    want = _stage_uuid(stage)
    for index, item in enumerate(sequence):
        if _stage_uuid(item) == want:
            return index
    return 0


def eligibility_retry_window_open(stage: dict[str, Any], wall_now: int, retry_sec: int) -> bool:
    """True while wall clock is within [stage_start, stage_start + retry_sec)."""
    if retry_sec <= 0:
        return False
    start, _ = stage_bounds(stage)
    if not start:
        return False
    start_i = int(start)
    return start_i <= wall_now < start_i + retry_sec


def sync_stage_times_in_sequence(
    sequence: list[dict[str, Any]],
    fresh_stages: list[dict[str, Any]],
) -> list[tuple[str, int | None, int | None]]:
    """Update start/end on sequence entries matched by uuid. Returns (label, old_start, new_start)."""
    by_uuid = {_stage_uuid(s): s for s in fresh_stages if _stage_uuid(s)}
    changes: list[tuple[str, int | None, int | None]] = []
    for stage in sequence:
        fresh = by_uuid.get(_stage_uuid(stage))
        if not fresh:
            continue
        old_start, _ = stage_bounds(stage)
        for key in ("start_time", "startTime", "end_time", "endTime", "price", "max_per_wallet"):
            if key in fresh and fresh.get(key) is not None:
                stage[key] = fresh[key]
        new_start, _ = stage_bounds(stage)
        if old_start != new_start:
            label = (stage.get("label") or stage.get("stage_type") or "?").strip()
            changes.append((label, old_start, new_start))
    return changes


def stage_window_open(stage: dict[str, Any], now: int) -> bool:
    start, end = stage_bounds(stage)
    if start and now < start:
        return False
    if end and now > end:
        return False
    return True


def next_stage_wake_time(stage: dict[str, Any], now: int) -> int | None:
    start, end = stage_bounds(stage)
    if end and now > end:
        return None
    if start and now < start:
        return int(start)
    return None


def drop_has_future_mint_window(stages: list[dict[str, Any]], now: int) -> bool:
    for stage in build_stage_sequence(stages):
        start, end = stage_bounds(stage)
        if start and now < start:
            return True
        if stage_status(stage, now) == SaleStatus.ACTIVE:
            return True
        if stage_status(stage, now) == SaleStatus.UNKNOWN and (start or end):
            return True
    return False


def use_chain_public_mint(stage: dict[str, Any] | None) -> bool:
    if stage is None:
        return True
    return (stage.get("stage_type") or "").lower() == "public_sale"
