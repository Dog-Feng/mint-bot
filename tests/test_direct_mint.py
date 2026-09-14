import unittest

from mint_engine.adapters.direct_mint import direct_mint_input_gaps, resolve_direct_mint_arg
from mint_engine.core.models import MintMethod


class TestDirectMintArgs(unittest.TestCase):
    def _method(self, inputs):
        return MintMethod(
            name="mint",
            signature="mint(...)",
            selector="0x",
            to="0x" + "2" * 40,
            inputs=inputs,
            payable=True,
            confidence=0.9,
            source="test",
        )

    def test_single_uint_uses_quantity(self):
        item = {"name": "amount", "type": "uint256"}
        val = resolve_direct_mint_arg(
            item, quantity=5, recipient="0x" + "1" * 40, extra_params={}, uint_count=1, addr_count=0
        )
        self.assertEqual(val, 5)

    def test_deadline_requires_extra(self):
        item = {"name": "deadline", "type": "uint256"}
        with self.assertRaises(ValueError):
            resolve_direct_mint_arg(
                item, quantity=1, recipient="0x" + "1" * 40, extra_params={}, uint_count=2, addr_count=0
            )
        val = resolve_direct_mint_arg(
            item,
            quantity=1,
            recipient="0x" + "1" * 40,
            extra_params={"deadline": 999},
            uint_count=2,
            addr_count=0,
        )
        self.assertEqual(val, 999)

    def test_gaps_for_ambiguous_uint(self):
        method = self._method(
            [
                {"name": "quantity", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ]
        )
        gaps = direct_mint_input_gaps(method, {})
        self.assertTrue(any(g.blocking and g.key == "mint_args" for g in gaps))


if __name__ == "__main__":
    unittest.main()
