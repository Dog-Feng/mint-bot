import unittest

from mint_engine.monitor.receipt import is_sold_out, parse_token_ids, revert_custom_selectors


class TestSoldOutDetection(unittest.TestCase):
    def test_custom_error_selector_in_revert_hex(self):
        self.assertEqual(revert_custom_selectors("0x" + "08c379a0" + "00" * 8), set())
        self.assertEqual(revert_custom_selectors(""), set())
        from eth_utils import keccak

        sold_out_sel = keccak(text="SoldOut()").hex()[:8]
        payload = f"0x{sold_out_sel}" + "00" * 4
        self.assertIn(sold_out_sel, revert_custom_selectors(payload))
        self.assertTrue(is_sold_out(payload))

    def test_no_false_positive_from_tx_hash_substring(self):
        fake = "0x" + "a" * 62 + "08c379a0"
        self.assertFalse(is_sold_out(fake))

    def test_decoded_error_string(self):
        self.assertTrue(is_sold_out("execution reverted: MaxSupplyReached()"))
        self.assertFalse(is_sold_out("execution reverted: AlreadyMinted()"))

    def test_transfer_token_id_in_log_data(self):
        recipient = "0x" + "1" * 40
        topic_to = "0x" + "0" * 24 + recipient[2:].lower()
        receipt = {
            "logs": [
                {
                    "topics": [
                        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                        "0x" + "0" * 64,
                        topic_to,
                    ],
                    "data": "0x" + "0" * 63 + "2a",
                }
            ]
        }
        self.assertEqual(parse_token_ids(receipt, recipient), [42])


if __name__ == "__main__":
    unittest.main()
