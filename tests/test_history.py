import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

history = load("trader-data", "history")
engine = load("trader-backtest", "engine")

T0 = 1767600000  # 2026-01-05 08:00:00 UTC, a Monday


def yahoo_doc(n=6, step=300, start=T0, nulls=(), currency="EUR", symbol="SAP.DE", error=None, price=100.0):
    ts = [start + i * step for i in range(n)]
    o = [price + i for i in range(n)]
    c = [price + i + 0.5 for i in range(n)]
    h = [x + 1 for x in c]
    l = [x - 1 for x in o]
    v = [1000 + i for i in range(n)]
    for i in nulls:
        o[i] = h[i] = l[i] = c[i] = v[i] = None
    return {"chart": {"result": None if error else [{
        "meta": {"symbol": symbol, "currency": currency, "exchangeTimezoneName": "Europe/Berlin",
                 "gmtoffset": 3600},
        "timestamp": ts,
        "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v}]}}],
        "error": error}}


def kraken_doc(n=5, step=3600, start=T0, errors=None):
    rows = [[start + i * step, "100.0", "102.0", "99.0", "101.0", "100.5", "3.2", 10] for i in range(n)]
    return {"error": errors or [], "result": {"XXBTZEUR": rows, "last": start + (n - 1) * step}}


class YahooParse(unittest.TestCase):
    def test_parses_bars_in_utc(self):
        bars, meta, notes = history.parse_yahoo(yahoo_doc(), 5)
        self.assertEqual(len(bars), 6)
        self.assertEqual(bars[0]["t"], "2026-01-05T08:00:00Z")
        self.assertEqual(bars[1]["t"], "2026-01-05T08:05:00Z")
        self.assertEqual((bars[0]["o"], bars[0]["c"]), (100.0, 100.5))
        self.assertEqual(meta["currency"], "EUR")

    def test_null_rows_are_dropped_and_counted(self):
        bars, meta, notes = history.parse_yahoo(yahoo_doc(nulls=(2,)), 5)
        self.assertEqual(len(bars), 5)
        self.assertTrue(any("1 empty" in n for n in notes))

    def test_all_null_is_refused(self):
        with self.assertRaises(ValueError):
            history.parse_yahoo(yahoo_doc(n=3, nulls=(0, 1, 2)), 5)

    def test_yahoo_error_is_refused(self):
        with self.assertRaises(ValueError):
            history.parse_yahoo(yahoo_doc(error={"code": "Not Found", "description": "No data"}), 5)

    def test_garbage_is_refused(self):
        for bad in ({}, {"chart": {}}, {"chart": {"result": []}}, [], None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                history.parse_yahoo(bad, 5)

    def test_bars_must_be_regular(self):
        with self.assertRaises(ValueError):
            history.parse_yahoo(yahoo_doc(step=60), 5)  # one-minute spacing asked as five-minute

    def test_pence_quote_is_flagged(self):
        _, meta, notes = history.parse_yahoo(yahoo_doc(currency="GBp", symbol="SHEL.L"), 5)
        self.assertTrue(any("pence" in n.lower() for n in notes))

    def test_inconsistent_bar_is_refused_by_the_shared_check(self):
        doc = yahoo_doc()
        doc["chart"]["result"][0]["indicators"]["quote"][0]["high"][1] = 1.0
        with self.assertRaises(ValueError):
            history.parse_yahoo(doc, 5)

    def test_result_passes_the_backtester_checks(self):
        bars, _, _ = history.parse_yahoo(yahoo_doc(), 5)
        engine.check_bars(bars)


class KrakenParse(unittest.TestCase):
    def test_parses_and_drops_the_unfinished_last_bar(self):
        bars, notes = history.parse_kraken(kraken_doc(5), 60)
        self.assertEqual(len(bars), 4)
        self.assertEqual(bars[0]["t"], "2026-01-05T08:00:00Z")
        self.assertEqual(bars[0]["h"], 102.0)
        self.assertTrue(any("unfinished" in n for n in notes))

    def test_errors_are_refused(self):
        with self.assertRaises(ValueError):
            history.parse_kraken(kraken_doc(errors=["EQuery:Unknown asset pair"]), 60)

    def test_garbage_is_refused(self):
        for bad in ({}, {"error": [], "result": {}}, {"error": [], "result": {"last": 1}}, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                history.parse_kraken(bad, 60)


class Urls(unittest.TestCase):
    def test_yahoo_url_has_interval_and_period(self):
        u = history.yahoo_url("SAP.DE", 5, 10, now=T0)
        self.assertIn("/v8/finance/chart/SAP.DE", u)
        self.assertIn("interval=5m", u)
        self.assertIn("period2=%d" % T0, u)
        self.assertIn("period1=%d" % (T0 - 10 * 86400), u)

    def test_yahoo_days_limit_per_interval(self):
        with self.assertRaises(ValueError):
            history.yahoo_url("SAP.DE", 5, 90, now=T0)
        with self.assertRaises(ValueError):
            history.yahoo_url("SAP.DE", 1, 40, now=T0)
        history.yahoo_url("SAP.DE", 60, 400, now=T0)

    def test_yahoo_interval_must_be_known(self):
        with self.assertRaises(ValueError):
            history.yahoo_url("SAP.DE", 7, 5, now=T0)

    def test_yahoo_symbol_is_quoted(self):
        u = history.yahoo_url("A/B C", 5, 5, now=T0)
        self.assertNotIn(" ", u)
        self.assertNotIn("A/B", u)

    def test_kraken_url(self):
        u = history.kraken_url("XBTEUR", 60, since=T0)
        self.assertIn("pair=XBTEUR", u)
        self.assertIn("interval=60", u)
        self.assertIn("since=%d" % T0, u)
        with self.assertRaises(ValueError):
            history.kraken_url("XBTEUR", 7)


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "SAP.DE_5m.csv")

    def bars(self, n=6, start=T0, price=100.0):
        return history.parse_yahoo(yahoo_doc(n=n, start=start, price=price), 5)[0]

    def test_merge_creates_and_reads_back(self):
        r = history.merge_store(self.path, self.bars())
        self.assertEqual((r["added"], r["revised"], r["total"]), (6, 0, 6))
        back = history.read_store(self.path)
        self.assertEqual(len(back), 6)
        self.assertEqual(back[0]["t"], "2026-01-05T08:00:00Z")

    def test_merge_twice_is_idempotent(self):
        history.merge_store(self.path, self.bars())
        r = history.merge_store(self.path, self.bars())
        self.assertEqual((r["added"], r["revised"], r["total"]), (0, 0, 6))

    def test_overlap_adds_only_new_bars_and_keeps_order(self):
        history.merge_store(self.path, self.bars(n=4))
        r = history.merge_store(self.path, self.bars(n=4, start=T0 + 2 * 300))
        self.assertEqual((r["added"], r["total"]), (2, 6))
        ts = [b["t"] for b in history.read_store(self.path)]
        self.assertEqual(ts, sorted(ts))

    def test_a_changed_bar_is_counted_as_revised(self):
        history.merge_store(self.path, self.bars())
        changed = self.bars(price=105.0)
        r = history.merge_store(self.path, changed)
        self.assertEqual(r["revised"], 6)
        self.assertEqual(history.read_store(self.path)[0]["o"], 105.0)

    def test_refuses_to_merge_bad_bars_and_keeps_the_old_file(self):
        history.merge_store(self.path, self.bars())
        with open(self.path) as fh:
            before = fh.read()
        bad = self.bars()
        bad[1]["h"] = 1.0
        with self.assertRaises(ValueError):
            history.merge_store(self.path, bad)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), before)

    def test_read_store_refuses_a_damaged_file(self):
        with open(self.path, "w") as fh:
            fh.write("t,o,h,l,c,v\n2026-01-05T08:00:00Z,1,2,0.5,abc,3\n2026-01-05T08:05:00Z,1,2,0.5,1,3\n")
        with self.assertRaises(ValueError):
            history.read_store(self.path)

    def test_store_file_loads_in_the_backtester(self):
        history.merge_store(self.path, self.bars())
        bars = engine.load_bars_csv(self.path)
        self.assertEqual(len(bars), 6)

    def test_no_temp_files_left_behind(self):
        history.merge_store(self.path, self.bars())
        self.assertEqual(os.listdir(self.dir), ["SAP.DE_5m.csv"])


class Quality(unittest.TestCase):
    def bars(self, n=12, step=300, skip=()):
        b, _, _ = history.parse_yahoo(yahoo_doc(n=n, step=step), step // 60)
        return [x for i, x in enumerate(b) if i not in skip]

    def test_complete_data_is_ok(self):
        q = history.quality(self.bars(), 5)
        self.assertEqual(q["verdict"], "OK")
        self.assertEqual(q["missing_bars"], 0)

    def test_missing_bars_inside_a_day_are_found(self):
        q = history.quality(self.bars(skip=(3, 4)), 5)
        self.assertEqual(q["missing_bars"], 2)
        self.assertEqual(len(q["gaps"]), 1)
        self.assertEqual(q["gaps"][0]["missing"], 2)

    def test_overnight_and_weekend_gaps_are_not_counted(self):
        d1 = self.bars(n=4)
        d2, _, _ = history.parse_yahoo(yahoo_doc(n=4, start=T0 + 86400), 5)
        d3, _, _ = history.parse_yahoo(yahoo_doc(n=4, start=T0 + 4 * 86400), 5)
        q = history.quality(d1 + d2 + d3, 5)
        self.assertEqual(q["missing_bars"], 0)

    def test_many_gaps_give_a_warning_verdict(self):
        q = history.quality(self.bars(n=12, skip=(2, 3, 4, 5, 6, 7)), 5)
        self.assertEqual(q["verdict"], "WARN")

    def test_always_open_market_counts_gaps_across_midnight(self):
        b, _, _ = history.parse_yahoo(yahoo_doc(n=3, step=3600, start=T0 + 20 * 3600), 60)
        b2, _, _ = history.parse_yahoo(yahoo_doc(n=3, step=3600, start=T0 + 28 * 3600), 60)
        q = history.quality(b + b2, 60, always_open=True)
        self.assertGreater(q["missing_bars"], 0)

    def test_too_few_bars_is_not_ok(self):
        q = history.quality(self.bars(n=2), 5)
        self.assertEqual(q["verdict"], "WARN")


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def write_json(self, name, doc):
        p = os.path.join(self.dir, name)
        with open(p, "w") as fh:
            json.dump(doc, fh)
        return p

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = history.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_import_yahoo_writes_the_store(self):
        src = self.write_json("y.json", yahoo_doc())
        code, out, err = self.run_cli("import", "yahoo", src, "--symbol", "SAP.DE", "--interval", "5",
                                      "--store", self.dir)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "SAP.DE_5m.csv")))
        self.assertIn("added", out)

    def test_import_kraken_writes_the_store(self):
        src = self.write_json("k.json", kraken_doc())
        code, out, err = self.run_cli("import", "kraken", src, "--symbol", "XBTEUR", "--interval", "60",
                                      "--store", self.dir)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "XBTEUR_60m.csv")))

    def test_import_refuses_bad_input_with_a_message(self):
        src = self.write_json("bad.json", {"chart": {"result": None, "error": {"code": "x"}}})
        code, out, err = self.run_cli("import", "yahoo", src, "--symbol", "SAP.DE", "--interval", "5",
                                      "--store", self.dir)
        self.assertNotEqual(code, 0)
        self.assertIn("Refused", err)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".csv")], [])

    def test_unsafe_symbol_cannot_escape_the_store_folder(self):
        src = self.write_json("y.json", yahoo_doc())
        code, out, err = self.run_cli("import", "yahoo", src, "--symbol", "../evil", "--interval", "5",
                                      "--store", self.dir)
        self.assertNotEqual(code, 0)

    def test_fetch_uses_the_network_function_and_stores(self):
        from unittest import mock
        seen = []

        def fake(url, timeout=20):
            seen.append(url)
            return yahoo_doc()

        with mock.patch.object(history, "fetch_json", fake):
            code, out, err = self.run_cli("fetch", "yahoo", "SAP.DE", "--interval", "5", "--days", "10",
                                          "--store", self.dir)
        self.assertEqual(code, 0, err)
        self.assertIn("interval=5m", seen[0])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "SAP.DE_5m.csv")))

    def test_fetch_refuses_more_days_than_yahoo_keeps(self):
        code, out, err = self.run_cli("fetch", "yahoo", "SAP.DE", "--interval", "5", "--days", "90",
                                      "--store", self.dir)
        self.assertNotEqual(code, 0)
        self.assertIn("Refused", err)

    def test_check_prints_quality(self):
        src = self.write_json("y.json", yahoo_doc(n=12))
        self.run_cli("import", "yahoo", src, "--symbol", "SAP.DE", "--interval", "5", "--store", self.dir)
        code, out, err = self.run_cli("check", "--symbol", "SAP.DE", "--interval", "5", "--store", self.dir)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["verdict"], "OK")


if __name__ == "__main__":
    unittest.main()
