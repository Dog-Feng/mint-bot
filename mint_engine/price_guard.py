from __future__ import annotations

from decimal import Decimal, InvalidOperation

from mint_engine.core.exceptions import ConfigError, EngineError

WEI_SCALE = Decimal(10) ** 18


class PriceGuardError(EngineError):
    def __init__(self, message: str) -> None:
        super().__init__("PRICE_GUARD", message)


def native_unit_to_wei(amount: str | int | float | None) -> int:
    if amount is None:
        return 0
    if isinstance(amount, int):
        if amount < 0:
            raise ConfigError("max_unit_price must be non-negative")
        return amount
    text = str(amount).strip()
    if not text:
        return 0
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ConfigError(f"invalid max_unit_price: {amount}") from exc
    if value < 0:
        raise ConfigError("max_unit_price must be non-negative")
    return int(value * WEI_SCALE)


def wei_to_native_str(wei: int) -> str:
    if wei == 0:
        return "0"
    as_eth = (Decimal(wei) / WEI_SCALE).normalize()
    return format(as_eth, "f")


def enforce_price_guard(
    value_wei: int,
    quantity: int,
    max_unit_price_wei: int,
    *,
    native_symbol: str = "ETH",
) -> None:
    if quantity < 1:
        raise ConfigError("quantity must be >= 1 for price guard")
    if value_wei < 0:
        raise PriceGuardError("mint tx value is negative")
    if value_wei % quantity != 0:
        raise PriceGuardError(
            f"mint value {value_wei} wei is not divisible by quantity {quantity}"
        )
    unit = value_wei // quantity
    if unit > max_unit_price_wei:
        raise PriceGuardError(
            f"unit price {wei_to_native_str(unit)} {native_symbol} "
            f"exceeds max {wei_to_native_str(max_unit_price_wei)} {native_symbol}"
        )
