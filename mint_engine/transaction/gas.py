from __future__ import annotations

from mint_engine.core.exceptions import ConfigError
from mint_engine.core.models import GasConfig, SaleStatus, TxPlan
from mint_engine.rpc.pool import RpcPool

GWEI = 10**9


def wei_to_gwei(wei: int) -> float:
    return round(wei / GWEI, 4)


def public_gas_snapshot(quote: dict[str, int], config: GasConfig) -> dict:
    worst = int(quote["gas_limit"]) * int(quote["max_fee"])
    cap = float(config.bump.hard_cap_gwei or 0)
    max_fee_gwei = wei_to_gwei(int(quote["max_fee"]))
    return {
        "base_fee_gwei": wei_to_gwei(int(quote["base_fee"])),
        "network_tip_gwei": wei_to_gwei(int(quote.get("network_tip") or 0)),
        "priority_fee_gwei": wei_to_gwei(int(quote["priority_fee"])),
        "max_fee_gwei": max_fee_gwei,
        "hard_cap_gwei": cap,
        "over_cap": bool(cap and max_fee_gwei > cap),
        "gas_limit": int(quote["gas_limit"]),
        "worst_fee_wei": worst,
        "worst_fee_eth": round(worst / 10**18, 8),
    }


def compute_eip1559_fees(
    base: int,
    tip: int,
    config: GasConfig,
    *,
    attempt: int = 0,
    raise_on_cap: bool = True,
) -> tuple[int, int]:
    """Return (priority_fee, max_fee_per_gas) in wei."""
    bump = config.bump
    extra_gwei = bump.extra_priority_gwei or 0
    extra = int(max(extra_gwei, 0) * GWEI)
    if bump.mode == "fixed" and bump.fixed_priority_gwei is not None:
        priority = int(bump.fixed_priority_gwei * GWEI)
    else:
        priority = max(tip, 1) + extra
    priority = int(priority * (config.retry_priority_multiplier ** attempt))
    fee_mul = bump.max_fee_multiplier if bump.max_fee_multiplier and bump.max_fee_multiplier > 0 else 1.0
    fee_mul = fee_mul * (config.retry_max_fee_multiplier ** attempt)
    max_fee = int(base * fee_mul + priority)
    cap = int((bump.hard_cap_gwei or 0) * GWEI)
    if cap and max_fee > cap:
        if raise_on_cap and attempt == 0:
            raise ConfigError(
                f"maxFeePerGas {max_fee / GWEI:.4f} gwei exceeds hard cap {bump.hard_cap_gwei} gwei"
            )
        max_fee = cap
        if priority >= max_fee:
            priority = max(max_fee - 1, 1)
    return priority, max_fee


async def quote_gas(
    pool: RpcPool,
    config: GasConfig,
    fallback_limit: int | None = None,
    attempt: int = 0,
    raise_on_cap: bool = True,
) -> dict[str, int]:
    base, tip = await pool.fee_hint()
    priority, max_fee = compute_eip1559_fees(
        base, tip, config, attempt=attempt, raise_on_cap=raise_on_cap
    )
    return {
        "base_fee": base,
        "network_tip": tip,
        "priority_fee": priority,
        "max_fee": max_fee,
        "gas_limit": fallback_limit or config.fallback_gas_limit or 280000,
        "attempt": attempt,
    }


async def worst_case_gas_reserve_wei(pool: RpcPool, config: GasConfig) -> int:
    """Balance check: gas_limit × maxFee (same rules as mint quoting)."""
    limit = config.fallback_gas_limit or 280000
    try:
        quote = await quote_gas(pool, config, raise_on_cap=False)
        return int(quote["gas_limit"]) * int(quote["max_fee"])
    except Exception:
        pass
    try:
        base, tip = await pool.fee_hint()
        _priority, max_fee = compute_eip1559_fees(base, tip, config, raise_on_cap=False)
        return limit * max(max_fee, 1)
    except Exception:
        pass
    bump = config.bump
    mul = bump.max_fee_multiplier if bump.max_fee_multiplier and bump.max_fee_multiplier > 0 else 1.0
    extra = int(max(bump.extra_priority_gwei or 0, 0) * GWEI)
    # RPC 不可用时：仅按配置估，默认 base≈1 gwei，不用固定 30 gwei
    max_fee = int(GWEI * mul + extra)
    return limit * max(max_fee, extra + 1)


async def resolve_gas_limit(
    pool: RpcPool,
    tx: dict[str, object],
    config: GasConfig,
    sale_status: SaleStatus | None = None,
    prefer: str | None = None,
) -> tuple[int, str]:
    fallback = config.fallback_gas_limit or 280000
    if config.gas_limit_mode == "fallback":
        return fallback, "forced fallback"
    try:
        estimated = await pool.estimate_gas(tx, prefer=prefer)
        return max(int(estimated * 1.2), 21000), f"estimate {estimated}+20%"
    except Exception:
        why = sale_status.value if sale_status else "estimate failed"
        return fallback, f"fallback ({why})"


def apply_gas(plan: TxPlan, quote: dict[str, int]) -> TxPlan:
    plan.gas = quote["gas_limit"]
    plan.max_fee_per_gas = quote["max_fee"]
    plan.max_priority_fee_per_gas = quote["priority_fee"]
    return plan
