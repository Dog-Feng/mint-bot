import unittest

from mint_engine.core.exceptions import ConfigError
from mint_engine.price_guard import (
    PriceGuardError,
    enforce_price_guard,
    native_unit_to_wei,
)


class TestPriceGuard(unittest.TestCase):
    def test_native_to_wei(self):
        self.assertEqual(native_unit_to_wei("0"), 0)
        self.assertEqual(native_unit_to_wei("0.0015"), 1500000000000000)

    def test_enforce_max_zero(self):
        enforce_price_guard(0, 1, 0)
        with self.assertRaises(PriceGuardError):
            enforce_price_guard(1, 1, 0)

    def test_enforce_max_upper_bound(self):
        cap = native_unit_to_wei("0.003")
        unit = native_unit_to_wei("0.0015")
        enforce_price_guard(unit, 1, cap)
        with self.assertRaises(PriceGuardError):
            enforce_price_guard(native_unit_to_wei("0.004"), 1, cap)

    def test_enforce_quantity_divisible(self):
        with self.assertRaises(PriceGuardError):
            enforce_price_guard(5, 2, native_unit_to_wei("1"))

    def test_invalid_price_string(self):
        with self.assertRaises(ConfigError):
            native_unit_to_wei("abc")


if __name__ == "__main__":
    unittest.main()
