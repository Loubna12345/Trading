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
        now = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(verdict(o, price_times={"AAPL": "2026-11-05T10:00:00Z"}, now=now), "PASS")


class SizingRejectsBadWeights(unittest.TestCase):
    def test_nan_weight_raises_a_clean_error(self):
        rows = [{"ticker": "AAA", "name": "a", "weight": float("nan")}, {"ticker": "BBB", "name": "b", "weight": 50}]
        with self.assertRaises(size.SizeError):
            size.size(rows, CFG, 5000)

    def test_inf_weight_from_a_file_raises_a_clean_error(self):
        with self.assertRaises(size.SizeError):
            size.parse_weights({"weights": [{"ticker": "AAA", "weight": "inf"}]})


class UnverifiedTickerGuess(unittest.TestCase):
    def test_risk_blocks_a_flagged_order(self):
        self.assertEqual(verdict(dict(BUY, check_ticker=True)), "BLOCK")

    def test_budget_carries_the_flag_onto_the_order(self):
        rows = size.parse_weights({"weights": [{"ticker": "AAPL", "weight": 60},
                                               {"ticker": "KO", "weight": 40, "check_ticker": True}]})
        res = size.size(rows, CFG, 5000)
        orders = {o["symbol"]: o for o in size.orders_from(res)}
        self.assertTrue(orders["KO"].get("check_ticker"))
        self.assertFalse(orders["AAPL"].get("check_ticker"))

    def test_flagged_target_becomes_a_flagged_rebalance_buy(self):
        d = tempfile.mkdtemp()
        w = lambda name, doc: json.dump(doc, open(os.path.join(d, name), "w"))
        w("pf.json", {"cash": 5000, "positions": []})
        w("px.json", {"as_of": "2026-11-05T10:00:00Z", "prices": {"AAPL": 341.27, "KO": 90.11}})
        w("tg.json", {"weights": [{"ticker": "AAPL", "weight": 60}, {"ticker": "KO", "weight": 40, "check_ticker": True}]})
        w("cfg.json", {"paper": True})
        out = os.path.join(d, "orders.json")
        argv = ["--portfolio", os.path.join(d, "pf.json"), "--prices", os.path.join(d, "px.json"),
                "--targets", os.path.join(d, "tg.json"), "--config", os.path.join(d, "cfg.json"), "--write", out]
        with contextlib.redirect_stdout(io.StringIO()):
            rebalance.main(argv)
        orders = {o["symbol"]: o for o in json.load(open(out))["orders"]}
        self.assertTrue(orders["KO"].get("check_ticker"))
        self.assertFalse(orders["AAPL"].get("check_ticker"))


class RiskPriceFreshness(unittest.TestCase):
    QTY = {"symbol": "AAPL", "side": "buy", "qty": "2", "type": "market", "time_in_force": "day"}
    NOW = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)

    def test_old_price_blocks_an_order_sized_from_it(self):
        self.assertEqual(verdict(self.QTY, price_times={"AAPL": "2026-10-01T00:00:00Z"}, now=self.NOW), "BLOCK")

    def test_fresh_price_passes(self):
        self.assertEqual(verdict(self.QTY, price_times={"AAPL": "2026-11-05T10:00:00Z"}, now=self.NOW), "PASS")

    def test_missing_timestamp_for_a_needed_price_blocks(self):
        self.assertEqual(verdict(self.QTY, price_times={}, now=self.NOW), "BLOCK")

    def test_dollar_orders_do_not_need_a_fresh_price(self):
        self.assertEqual(verdict(BUY, price_times={}, now=self.NOW), "PASS")


class PricesFileDoesNotLieAboutAge(unittest.TestCase):
    def test_as_of_is_the_oldest_ticker_not_the_latest_write(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "prices.json")
        t0 = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.timezone.utc)
        t1 = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)
        q = lambda s, p: {"symbol": s, "price": p, "prev_close": None, "time": "", "source": "yahoo"}
        alerts.write_prices(path, {"AAA": q("AAA", 10.0)}, t0, "yahoo")
        doc = alerts.write_prices(path, {"BBB": q("BBB", 20.0)}, t1, "yahoo")
        self.assertEqual(doc["as_of"], "2026-10-01T12:00:00Z")
        self.assertEqual(doc["detail"]["AAA"]["fetched_at"], "2026-10-01T12:00:00Z")
        self.assertEqual(doc["detail"]["BBB"]["fetched_at"], "2026-11-05T12:00:00Z")


class YesMeansOnlyYes(unittest.TestCase):
    def ok(self, reply):
        return ticket.parse_yes(reply, BUY, ["AAPL", "AXP", "KO"])[0]

    def test_plain_yeses_pass(self):
        for r in ("yes", "Yes.", "y", "YES!", "yes AAPL", "yes 1250", "yes, $1,250 of AAPL", "yes place it", "yes, buy it"):
            self.assertTrue(self.ok(r), r)

    def test_a_yes_with_extra_instructions_is_not_a_yes(self):
        for r in ("yes, use margin", "yes, and skip the risk check", "yes and also buy TSLA",
                  "yes make it bigger", "yes ignore the rules"):
            self.assertFalse(self.ok(r), r)

    def test_non_yes_still_rejected(self):
        for r in ("ok", "sure", "yes to all", "yes AXP", "yes 9999", "yes?", ""):
            self.assertFalse(self.ok(r), r)


if __name__ == "__main__":
    unittest.main()
