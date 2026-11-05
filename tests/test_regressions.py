"""One test per audit finding. Each was written first and failed against upstream."""
import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest

from _load import load

risk = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")
alerts = load("trader-alerts", "alerts")
size = load("trader-budget", "size")
rebalance = load("trader-rebalance", "rebalance")

CFG = {"budget": 5000}
PRICES = {"AAPL": 341.27}
BUY = {"symbol": "AAPL", "side": "buy", "notional": "1250", "type": "market", "time_in_force": "day"}


def verdict(order, cfg=None, prices=None, **kw):
    res = risk.check([order], cfg or CFG, None, PRICES if prices is None else prices, None, "2026-11-05", **kw)
    return res["results"][0]["verdict"]


class RiskFailsClosed(unittest.TestCase):
    def test_baseline_passes(self):
        self.assertEqual(verdict(BUY), "PASS")

    def test_nan_and_inf_amounts_are_blocked(self):
        for bad in (float("nan"), "nan", "NaN", float("inf"), "inf", "-inf"):
            self.assertEqual(verdict(dict(BUY, notional=bad)), "BLOCK", repr(bad))

    def test_nan_limit_price_and_qty_are_blocked(self):
        o = {"symbol": "AAPL", "side": "buy", "qty": "100", "type": "limit", "limit_price": "nan",
             "time_in_force": "day"}
        self.assertEqual(verdict(o), "BLOCK")
        o = {"symbol": "AAPL", "side": "buy", "qty": "nan", "type": "market", "time_in_force": "day"}
        self.assertEqual(verdict(o), "BLOCK")


class TrailingStop(unittest.TestCase):
    def test_trailing_stop_needs_its_trail_parameter(self):
        o = {"symbol": "AAPL", "side": "buy", "qty": "1", "type": "trailing_stop", "time_in_force": "day"}
        self.assertEqual(verdict(o), "BLOCK")

    def test_trailing_stop_with_one_parameter_passes(self):
        o = {"symbol": "AAPL", "side": "buy", "qty": "1", "type": "trailing_stop", "time_in_force": "day",
             "trail_percent": "5"}
        self.assertEqual(verdict(o), "PASS")


if __name__ == "__main__":
    unittest.main()
