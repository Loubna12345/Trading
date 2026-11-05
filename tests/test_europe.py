"""European trader support: tickers, euro prices, exchange clocks.

Quote fixtures SAP.DE and SHEL.L are SYNTHETIC (made up for tests), not market data.
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

alerts = load("trader-alerts", "alerts")
risk = load("trader-risk", "check")
QDIR = os.path.join(FIX, "trader-alerts")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


class EuropeanSymbols(unittest.TestCase):
    def test_exchange_suffix_is_kept_for_european_tickers(self):
        self.assertIn("/chart/SAP.DE?", alerts.yahoo_url("sap.de"))
        self.assertIn("/chart/MC.PA?", alerts.yahoo_url("MC.PA"))
        self.assertIn("/chart/SHEL.L?", alerts.yahoo_url("shel.l"))

    def test_us_share_classes_still_use_a_dash(self):
        self.assertIn("/chart/BRK-B?", alerts.yahoo_url("BRK.B"))

    def test_stooq_covers_us_and_xetra_only(self):
        self.assertEqual(alerts.stooq_url("SAP.DE"), "https://stooq.com/q/l/?s=sap.de&f=sd2t2ohlcv&h&e=csv")
        with self.assertRaises(alerts.AlertError):
            alerts.stooq_url("MC.PA")


class FxRates(unittest.TestCase):
    def test_parse_means_one_euro_buys_this_many(self):
        self.assertEqual(alerts.parse_fx(["USD=1.08", "gbp=0.86"]), {"USD": 1.08, "GBP": 0.86})

    def test_bad_rates_are_refused(self):
        for bad in ("USD=0", "USD=-1", "USD=nan", "USD=inf", "USD", "EUR=1", "USDX=1.1", "US=1.1", "USD=abc"):
            with self.subTest(bad=bad):
                with self.assertRaises(alerts.AlertError):
                    alerts.parse_fx([bad])

    def test_convert_usd_quote_to_euro(self):
        q, _ = alerts.get_quotes(["KO"], "yahoo", QDIR)
        out, errors = alerts.convert_quotes(q, {"USD": 1.08}, "2026-10-13", alerts.parse_now("2026-10-13T10:00"))
        self.assertEqual(errors, [])
        self.assertAlmostEqual(out["KO"]["price"], 90.11 / 1.08, places=6)
        self.assertEqual(out["KO"]["price_currency"], "EUR")
        self.assertEqual(out["KO"]["native_price"], 90.11)
        self.assertEqual(out["KO"]["currency"], "USD")
        self.assertEqual(out["KO"]["fx_rate"], 1.08)

    def test_euro_quote_needs_no_rate(self):
        q, _ = alerts.get_quotes(["SAP.DE"], "yahoo", QDIR)
        out, errors = alerts.convert_quotes(q, {}, None, alerts.parse_now("2026-10-13T10:00"))
        self.assertEqual(errors, [])
        self.assertEqual(out["SAP.DE"]["price"], 142.3)
        self.assertEqual(out["SAP.DE"]["price_currency"], "EUR")

    def test_pence_are_pounds_over_100(self):
        q, _ = alerts.get_quotes(["SHEL.L"], "yahoo", QDIR)
        out, errors = alerts.convert_quotes(q, {"GBP": 0.86}, "2026-10-13", alerts.parse_now("2026-10-13T10:00"))
        self.assertEqual(errors, [])
        self.assertAlmostEqual(out["SHEL.L"]["price"], 26.5 / 0.86, places=6)

    def test_missing_rate_drops_the_quote_and_says_why(self):
        q, _ = alerts.get_quotes(["KO", "SAP.DE"], "yahoo", QDIR)
        out, errors = alerts.convert_quotes(q, {}, None, alerts.parse_now("2026-10-13T10:00"))
        self.assertEqual(sorted(out), ["SAP.DE"])
        self.assertEqual(len(errors), 1)
        self.assertIn("USD", errors[0])

    def test_old_or_future_rate_date_is_refused(self):
        q, _ = alerts.get_quotes(["KO"], "yahoo", QDIR)
        now = alerts.parse_now("2026-10-13T10:00")
        for d in ("2026-10-09", "2026-10-14", "not a date", None):
            with self.subTest(d=d):
                out, errors = alerts.convert_quotes(q, {"USD": 1.08}, d, now)
                self.assertEqual(out, {})
                self.assertEqual(len(errors), 1)
        out, errors = alerts.convert_quotes(q, {"USD": 1.08}, "2026-10-10", now)  # 3 days old is fine
        self.assertEqual(errors, [])

    def test_a_quote_without_a_currency_is_never_guessed(self):
        out, errors = alerts.convert_quotes({"X": {"symbol": "X", "price": 10.0}}, {"USD": 1.08},
                                            "2026-10-13", alerts.parse_now("2026-10-13T10:00"))
        self.assertEqual(out, {})
        self.assertIn("currency", errors[0])


class PricesFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "prices.json")

    def test_prices_json_is_in_euro_and_says_so(self):
        code, out, err = run(["quote", "KO", "SAP.DE", "--offline", QDIR, "--fx", "USD=1.08",
                              "--fx-date", "2026-10-13", "--now", "2026-10-13T10:00", "--write", self.path])
        self.assertEqual(code, 0, err)
        doc = json.load(open(self.path))
        self.assertAlmostEqual(doc["prices"]["KO"], 90.11 / 1.08, places=4)
        self.assertEqual(doc["prices"]["SAP.DE"], 142.3)
        self.assertEqual(doc["detail"]["KO"]["price_currency"], "EUR")
        self.assertEqual(doc["detail"]["KO"]["native_currency"], "USD")
        self.assertEqual(doc["detail"]["KO"]["fx_date"], "2026-10-13")
        self.assertEqual(doc["currency"], "EUR")
        self.assertIn("$90.11", out)
        self.assertIn("€", out)

    def test_without_a_rate_dollar_stocks_are_not_written(self):
        code, out, err = run(["quote", "KO", "SAP.DE", "--offline", QDIR, "--now", "2026-10-13T10:00",
                              "--write", self.path])
        doc = json.load(open(self.path))
        self.assertEqual(sorted(doc["prices"]), ["SAP.DE"])
        self.assertIn("error:", out)


NOW = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)
FRESH = {"AAPL": "2026-11-05T10:00:00Z", "SAP.DE": "2026-11-05T10:00:00Z"}
EUR_META = {"price_currency": "EUR", "native_currency": "USD", "fx_rate": 1.25}


def verdict(order, meta, prices=None, cfg=None):
    res = risk.check([order], cfg or {"budget": 5000, "max_position_pct": 100}, None, prices or {"AAPL": 273.0}, None, "2026-11-05",
                     price_times=FRESH, now=NOW, price_meta=meta)
    return res["results"][0]["verdict"], " | ".join(res["results"][0]["reasons"])


QTY = {"symbol": "AAPL", "side": "buy", "qty": "2", "type": "market", "time_in_force": "day"}


class RiskCountsInEuro(unittest.TestCase):
    def test_a_euro_price_sizes_a_share_order(self):
        self.assertEqual(verdict(QTY, {"AAPL": EUR_META})[0], "PASS")

    def test_no_currency_information_blocks(self):
        v, why = verdict(QTY, {})
        self.assertEqual(v, "BLOCK")
        self.assertIn("currency", why)
        self.assertEqual(verdict(QTY, None)[0], "BLOCK")

    def test_a_price_not_in_euro_blocks(self):
        v, why = verdict(QTY, {"AAPL": dict(EUR_META, price_currency="USD")})
        self.assertEqual(v, "BLOCK")
        self.assertIn("not in EUR", why)

    def test_limit_price_is_converted_from_the_stock_currency(self):
        # 10 shares at a $250 limit is $2,500 = EUR 2,000 at 1.25: exactly the 2,000 max order, so PASS.
        o = {"symbol": "AAPL", "side": "buy", "qty": "10", "type": "limit", "limit_price": "250", "time_in_force": "day"}
        self.assertEqual(verdict(o, {"AAPL": EUR_META})[0], "PASS")
        # 10 shares at $251 is EUR 2,008: over the max. Unconverted $2,510 would also block, so use a lower cap check:
        o2 = dict(o, qty="9", limit_price="270")  # $2,430 = EUR 1,944 passes only because it is converted
        self.assertEqual(verdict(o2, {"AAPL": EUR_META})[0], "PASS")
        o3 = dict(o, qty="10", limit_price="251")
        self.assertEqual(verdict(o3, {"AAPL": EUR_META})[0], "BLOCK")

    def test_limit_price_without_currency_information_blocks(self):
        o = {"symbol": "AAPL", "side": "buy", "qty": "1", "type": "limit", "limit_price": "100", "time_in_force": "day"}
        v, why = verdict(o, {})
        self.assertEqual(v, "BLOCK")
        self.assertIn("currency", why)

    def test_pence_limit_price_is_converted(self):
        meta = {"price_currency": "EUR", "native_currency": "GBp", "fx_rate": 0.86}
        o = {"symbol": "AAPL", "side": "buy", "qty": "100", "type": "limit", "limit_price": "2650", "time_in_force": "day"}
        # 100 x 2650p = GBP 2,650 = EUR 3,081: over the 2,000 max
        self.assertEqual(verdict(o, {"AAPL": meta})[0], "BLOCK")
        o = dict(o, qty="50")  # EUR 1,540
        self.assertEqual(verdict(o, {"AAPL": meta})[0], "PASS")

    def test_notional_orders_are_in_account_currency_and_need_no_meta(self):
        o = {"symbol": "AAPL", "side": "buy", "notional": "1250", "type": "market", "time_in_force": "day"}
        self.assertEqual(verdict(o, None)[0], "PASS")


if __name__ == "__main__":
    unittest.main()
