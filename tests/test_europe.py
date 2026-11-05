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
size = load("trader-budget", "size")
rebalance = load("trader-rebalance", "rebalance")
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


def at(text):
    return alerts.parse_now(text)


class EuropeanClocks(unittest.TestCase):
    # Closed days: Deutsche Boerse trading calendar (cashmarket.deutsche-boerse.com, read 2026-11-05)
    XETRA_CLOSED = ["2026-01-01", "2026-04-03", "2026-04-06", "2026-05-01", "2026-12-24", "2026-12-25", "2026-12-31",
                    "2027-01-01", "2027-03-26", "2027-03-29", "2027-12-24", "2027-12-31"]

    def test_xetra_closed_days(self):
        for d in self.XETRA_CLOSED:
            day = alerts.datetime.date.fromisoformat(d)
            with self.subTest(d=d):
                self.assertFalse(alerts.eu_trading_day("XETRA", day)[0])

    def test_xetra_trades_on_german_holidays_it_does_not_close_for(self):
        # Deutsche Boerse says regular trading on Ascension Day, Whit Monday, Corpus Christi (2026) and Dec 28.
        for d in ("2026-05-14", "2026-05-25", "2026-06-04", "2026-12-28"):
            with self.subTest(d=d):
                self.assertTrue(alerts.eu_trading_day("XETRA", alerts.datetime.date.fromisoformat(d))[0])

    def test_weekend_is_closed(self):
        self.assertFalse(alerts.eu_trading_day("XETRA", alerts.datetime.date(2026, 10, 17))[0])

    def test_xetra_hours_in_summer_time(self):  # CEST = UTC+2
        self.assertFalse(alerts.eu_market_status(at("2026-10-13T06:59Z"), "XETRA")["open"])   # 08:59
        self.assertTrue(alerts.eu_market_status(at("2026-10-13T07:00Z"), "XETRA")["open"])    # 09:00
        self.assertTrue(alerts.eu_market_status(at("2026-10-13T15:29Z"), "XETRA")["open"])    # 17:29
        self.assertFalse(alerts.eu_market_status(at("2026-10-13T15:30Z"), "XETRA")["open"])   # 17:30

    def test_xetra_hours_in_winter_time(self):  # CET = UTC+1
        self.assertFalse(alerts.eu_market_status(at("2026-11-05T07:59Z"), "XETRA")["open"])
        self.assertTrue(alerts.eu_market_status(at("2026-11-05T08:00Z"), "XETRA")["open"])
        self.assertFalse(alerts.eu_market_status(at("2026-11-05T16:30Z"), "XETRA")["open"])

    def test_the_clocks_change_on_the_last_sunday_of_march_and_october(self):
        self.assertEqual(alerts.cet_offset(at("2026-03-29T00:59Z")), 1)
        self.assertEqual(alerts.cet_offset(at("2026-03-29T01:00Z")), 2)
        self.assertEqual(alerts.cet_offset(at("2026-10-25T00:59Z")), 2)
        self.assertEqual(alerts.cet_offset(at("2026-10-25T01:00Z")), 1)
        # Monday after the autumn change: 09:00 CET is 08:00 UTC
        self.assertFalse(alerts.eu_market_status(at("2026-10-26T07:59Z"), "XETRA")["open"])
        self.assertTrue(alerts.eu_market_status(at("2026-10-26T08:00Z"), "XETRA")["open"])

    def test_a_time_without_an_offset_is_local_to_the_market(self):
        self.assertTrue(alerts.eu_market_status(alerts.parse_now("2026-10-13T09:00", "XETRA"), "XETRA")["open"])
        self.assertFalse(alerts.eu_market_status(alerts.parse_now("2026-10-13T08:59", "XETRA"), "XETRA")["open"])

    def test_next_open_skips_the_weekend_and_holidays(self):
        st = alerts.eu_market_status(at("2026-04-02T16:00Z"), "XETRA")  # Thursday evening before Good Friday
        self.assertEqual(st["next_open"], "2026-04-07 09:00")

    def test_euronext_2026_and_the_unpublished_2027(self):
        for d in ("2026-01-01", "2026-04-03", "2026-04-06", "2026-05-01", "2026-12-25"):
            self.assertFalse(alerts.eu_trading_day("EURONEXT", alerts.datetime.date.fromisoformat(d))[0], d)
        self.assertTrue(alerts.eu_market_status(at("2026-12-28T10:00Z"), "EURONEXT")["open"])
        self.assertTrue(alerts.eu_market_status(at("2026-10-13T10:00Z"), "EURONEXT")["calendar_known"])
        self.assertFalse(alerts.eu_market_status(at("2027-03-01T10:00Z"), "EURONEXT")["calendar_known"])

    def test_euronext_half_days_have_no_announced_hours_so_they_count_as_closed(self):
        st = alerts.eu_market_status(at("2026-12-24T10:00Z"), "EURONEXT")
        self.assertFalse(st["open"])
        self.assertIn("early close", st["reason"])

    def test_which_market_a_ticker_trades_on(self):
        self.assertEqual(alerts.market_for("KO"), "NYSE")
        self.assertEqual(alerts.market_for("BRK.B"), "NYSE")
        self.assertEqual(alerts.market_for("SAP.DE"), "XETRA")
        self.assertEqual(alerts.market_for("MC.PA"), "EURONEXT")
        self.assertEqual(alerts.market_for("ASML.AS"), "EURONEXT")
        self.assertIsNone(alerts.market_for("SHEL.L"))  # no clock built for London

    def test_clock_command_lists_the_markets(self):
        code, out, _ = run(["clock", "--now", "2026-10-13T10:00Z"])
        self.assertIn("Xetra", out)
        self.assertIn("Euronext", out)
        self.assertIn("NYSE", out)
        code, out, _ = run(["clock", "--market", "XETRA", "--now", "2026-10-13T10:00Z"])
        self.assertEqual(code, 0)
        self.assertIn("Berlin", out)
        self.assertEqual(run(["clock", "--market", "XETRA", "--now", "2026-12-25T10:00Z"])[0], 1)

    def test_opening_bell_for_xetra_rings_once_a_day(self):
        rule = [{"id": "xetra-bell", "type": "opening_bell", "market": "XETRA"}]
        now = at("2026-10-13T07:05Z")
        markets = {"XETRA": alerts.eu_market_status(now, "XETRA")}
        rings, st = alerts.evaluate(rule, {}, alerts.market_status(now), {}, now, markets=markets)
        self.assertEqual(len(rings), 1)
        self.assertIn("Xetra", rings[0]["message"])
        rings, st = alerts.evaluate(rule, {}, alerts.market_status(now), st, now, markets=markets)
        self.assertEqual(rings, [])

    def test_alert_prices_are_shown_in_their_own_currency(self):
        q, _ = alerts.get_quotes(["SAP.DE"], "yahoo", QDIR)
        now = at("2026-10-13T08:00Z")
        rules = [{"id": "sap", "symbol": "SAP.DE", "type": "above", "price": 140}]
        rings, _ = alerts.evaluate(rules, q, alerts.market_status(now), {}, now)
        self.assertIn("SAP.DE is €142.30", rings[0]["message"])
        self.assertIn("€140.00", rings[0]["message"])


def asset(sym, side="buy", cfg=None, portfolio=None):
    o = {"symbol": sym, "side": side, "notional": "100", "type": "market", "time_in_force": "day"}
    if side == "sell":
        o = {"symbol": sym, "side": "sell", "qty": "1", "type": "market", "time_in_force": "day"}
    c = {"budget": 5000}
    c.update(cfg or {})
    res = risk.check([o], c, portfolio, {sym: 100.0}, None, "2026-11-05", price_times={sym: "2026-11-05T10:00:00Z"},
                     now=NOW, price_meta={sym: EUR_META})
    r = res["results"][0]
    return r["verdict"], " | ".join(r["reasons"])


class EuropeanAssets(unittest.TestCase):
    def test_european_tickers_are_accepted(self):
        for sym in ("SAP.DE", "1COV.DE", "BMW.DE", "ASML.AS", "MC.PA", "ENI.MI", "UCG.MI", "NOVO-B.CO", "SHEL.L"):
            with self.subTest(sym=sym):
                self.assertEqual(asset(sym)[0], "PASS")

    def test_us_tickers_still_work(self):
        for sym in ("KO", "AAPL", "BRK.B", "BF-B"):
            self.assertEqual(asset(sym)[0], "PASS", sym)

    def test_bad_symbols_are_still_blocked(self):
        for sym in ("SAP.XX", "SAP.DE.DE", ".DE", "SAP DE", "BTC/EUR", "sap.de!", "WAYTOOLONGTICKER.DE"):
            with self.subTest(sym=sym):
                v, why = asset(sym)
                self.assertEqual(v, "BLOCK")
                self.assertIn("stocks and ETFs only", why)

    def test_us_domiciled_etfs_cannot_be_bought(self):
        for sym in ("SPY", "QQQ", "VOO", "VTI", "IVV"):
            with self.subTest(sym=sym):
                v, why = asset(sym)
                self.assertEqual(v, "BLOCK")
                self.assertIn("PRIIPs", why)
                self.assertIn("UCITS", why)

    def test_a_us_etf_you_already_hold_can_be_sold(self):
        pf = {"cash": 0, "positions": [{"ticker": "SPY", "shares": 5, "lots": [{"date": "2026-01-02", "shares": 5, "cost": 400}]}]}
        self.assertEqual(asset("SPY", "sell", portfolio=pf)[0], "PASS")

    def test_the_us_etf_block_can_be_lifted_on_purpose(self):
        v, _ = asset("SPY", cfg={"allow_us_etfs": True})
        self.assertEqual(v, "PASS")

    def test_leveraged_and_inverse_etfs_are_blocked(self):
        for sym in ("TQQQ", "SQQQ", "SOXL", "UVXY", "UPRO"):
            with self.subTest(sym=sym):
                v, why = asset(sym)
                self.assertEqual(v, "BLOCK")
                self.assertIn("leveraged or inverse", why)
        self.assertEqual(asset("TQQQ", cfg={"allow_leveraged": True})[0], "PASS")

    def test_a_ucits_etf_on_xetra_passes(self):
        self.assertEqual(asset("EUNL.DE")[0], "PASS")
        self.assertEqual(asset("VWCE.DE")[0], "PASS")


class SizingNeedsEuroPrices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, doc):
        p = os.path.join(self.tmp, "prices.json")
        json.dump(doc, open(p, "w"))
        return p

    def test_budget_accepts_euro_prices(self):
        p = self.write({"currency": "EUR", "prices": {"SAP.DE": 142.3}})
        self.assertEqual(size.load_prices(p), {"SAP.DE": 142.3})

    def test_budget_refuses_prices_that_do_not_say_euro(self):
        for doc in ({"prices": {"KO": 90.0}}, {"KO": 90.0}, {"currency": "USD", "prices": {"KO": 90.0}},
                    {"currency": "eur ", "prices": {"KO": 1}}):
            with self.subTest(doc=doc):
                if doc.get("currency", "").strip().upper() == "EUR":
                    self.assertEqual(size.load_prices(self.write(doc)), {"KO": 1.0})
                    continue
                with self.assertRaises(size.SizeError) as cm:
                    size.load_prices(self.write(doc))
                self.assertIn("euro", str(cm.exception))

    def test_rebalance_refuses_prices_that_do_not_say_euro(self):
        pf = os.path.join(self.tmp, "pf.json")
        json.dump({"cash": 1000, "positions": []}, open(pf, "w"))
        tg = os.path.join(self.tmp, "tg.json")
        json.dump({"targets": [{"ticker": "KO", "weight": 100}]}, open(tg, "w"))
        pr = self.write({"prices": {"KO": 90.0}})
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rebalance.main(["--portfolio", pf, "--targets", tg, "--prices", pr, "--config",
                                   os.path.join(self.tmp, "none.json")])
        self.assertEqual(code, 1)
        self.assertIn("euro", err.getvalue())


if __name__ == "__main__":
    unittest.main()
