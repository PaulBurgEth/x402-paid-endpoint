"""Offline checks of the people's checkout on Base. No network.

    python -m unittest discover -s tests
The agent endpoint has its own guards against a running server: agents/test_guards.py.
"""
import os
import sys
import unittest
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "people")]
os.environ.setdefault("PAY_TO", "0xf68bc1098469356eb57e70db9fed0bb524707132")

import base  # noqa: E402
import checkout  # noqa: E402

PAY_TO = os.environ["PAY_TO"]


def log(value, emitter=base.USDC, to=PAY_TO):
    return {"address": emitter.lower(), "data": hex(value), "transactionHash": "0x" + "ab" * 32,
            "blockNumber": "0x10",
            "topics": [base.TRANSFER_TOPIC, "0x" + base.word("0x" + "11" * 20), "0x" + base.word(to)]}


class Reading(unittest.TestCase):
    def test_a_usdc_transfer_to_us_is_read_in_6_decimals(self):
        self.assertEqual(base.transfer_from_log(log(100_000), PAY_TO)["units"], 100_000)

    def test_a_transfer_event_from_another_contract_is_not_usdc(self):
        self.assertIsNone(base.transfer_from_log(log(100_000, emitter="0x" + "99" * 20), PAY_TO))

    def test_money_to_someone_else_is_not_ours(self):
        self.assertIsNone(base.transfer_from_log(log(100_000, to="0x" + "22" * 20), PAY_TO))


class Checkout(unittest.TestCase):
    def setUp(self):
        checkout._reservations.clear()
        checkout._used_tx.clear()
        checkout.PAY_TO = PAY_TO

    def test_the_link_is_an_erc20_transfer_on_base(self):
        r = checkout.reserve(Decimal("0.10"))
        self.assertEqual(r["uri"], f"ethereum:{base.USDC}@8453/transfer?address={PAY_TO}&uint256=100000")

    def test_two_open_reservations_never_share_an_amount(self):
        self.assertNotEqual(checkout.reserve(Decimal("0.10"))["units"], checkout.reserve(Decimal("0.10"))["units"])

    def test_one_transfer_pays_one_reservation(self):
        a = checkout.reserve(Decimal("0.10"))
        t = base.transfer_from_log(log(a["units"]), PAY_TO)
        self.assertTrue(checkout._settle(a["id"], t))
        b = checkout.reserve(Decimal("0.10"))
        checkout._reservations[b["id"]]["units"] = a["units"]
        self.assertFalse(checkout._settle(b["id"], t))


if __name__ == "__main__":
    unittest.main()
