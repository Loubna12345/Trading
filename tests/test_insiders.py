"""Directors' dealings (MAR Art. 19) reader. Fixtures are SYNTHETIC."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

ins = load("trader-insiders", "dealings")
CSV = os.path.join(FIX, "trader-insiders", "bafin-style.csv")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ins.main(argv)
    return code, out.getvalue(), err.getvalue()


class Numbers(unittest.TestCase):
    def test_both_german_and_english_styles(self):
        cases = {"61.700,00": 61700.0, "1.015.000,00": 1015000.0, "101,5": 101.5, "8.5": 8.5, "42500": 42500.0,
                 "1,234.56": 1234.56, "0,00": 0.0, "12,34": 12.34, " 7 ": 7.0, "1 234,50": 1234.5}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertAlmostEqual(ins.parse_number(text), want)

    def test_ambiguous_or_broken_numbers_fail_closed(self):
        for text in ("1.234", "1,234", "abc", "", "nan", "inf", "1,2,3", "--5"):
            with self.subTest(text=text):
                with self.assertRaises(ins.DealingsError):
                    ins.parse_number(text)

    def test_the_decimal_flag_settles_the_ambiguous_ones(self):
        self.assertEqual(ins.parse_number("1.234", decimal="comma"), 1234.0)
        self.assertEqual(ins.parse_number("1.234", decimal="dot"), 1.234)
        self.assertEqual(ins.parse_number("1,234", decimal="comma"), 1.234)
        self.assertEqual(ins.parse_number("1,234", decimal="dot"), 1234.0)


class Dates(unittest.TestCase):
    def test_formats(self):
        for text in ("05.11.2026", "2026-11-05", "05/11/2026", "5.11.2026", "2026-11-05T10:00:00"):
            self.assertEqual(ins.parse_date(text).isoformat(), "2026-11-05", text)

    def test_bad_dates(self):
        for text in ("", "31.02.2026", "2026-13-01", "yesterday", "11/05/26x"):
            with self.assertRaises(ins.DealingsError):
                ins.parse_date(text)


class Reading(unittest.TestCase):
    def test_reads_the_german_table(self):
        rows, errors = ins.read_table(CSV)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 5)
        r = rows[0]
        self.assertEqual((r["issuer"], r["isin"], r["person"], r["role"]),
                         ("Beispiel AG", "DE0000000001", "Erika Muster", "Vorstand"))
        self.assertEqual(r["side"], "buy")
        self.assertAlmostEqual(r["value"], 61700.0)
        self.assertEqual((r["traded"], r["notified"]), ("2026-11-03", "2026-11-05"))

    def test_buy_sell_and_other(self):
        rows, _ = ins.read_table(CSV)
        self.assertEqual([r["side"] for r in rows], ["buy", "buy", "sell", "other", "buy"])

    def test_delay_counts_business_days_and_flags_late(self):
        rows, _ = ins.read_table(CSV)
        self.assertEqual(rows[0]["delay_days"], 2)
        self.assertFalse(rows[0]["late"])
        self.assertEqual(rows[2]["delay_days"], 8)   # Wed 14 Oct -> Mon 26 Oct
        self.assertTrue(rows[2]["late"])

    def test_unknown_columns_give_a_clear_error(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "x.csv")
        open(p, "w").write("a;b;c\n1;2;3\n")
        with self.assertRaises(ins.DealingsError) as cm:
            ins.read_table(p)
        self.assertIn("issuer", str(cm.exception))

    def test_a_bad_row_is_reported_not_guessed(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "x.csv")
        lines = open(CSV, encoding="utf-8").read().splitlines()
        lines[1] = lines[1].replace("05.11.2026", "kaputt")
        open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        rows, errors = ins.read_table(p)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(errors), 1)
        self.assertIn("line 2", errors[0])

    def test_english_headers(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "x.csv")
        open(p, "w").write("Issuer,ISIN,Notifying person,Position,Transaction type,Price,Total volume,Currency,"
                           "Date of transaction,Notification date\n"
                           "Demo NV,NL0000000003,Jan Voorbeeld,CFO,Purchase,8.5,42500,EUR,2026-11-02,2026-11-04\n")
        rows, errors = ins.read_table(p)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["side"], "buy")


class Signals(unittest.TestCase):
    def test_cluster_means_two_insiders_buying_the_same_company_within_30_days(self):
        rows, _ = ins.read_table(CSV)
        cl = ins.clusters(rows)
        self.assertEqual(len(cl), 1)
        self.assertEqual(cl[0]["issuer"], "Beispiel AG")
        self.assertEqual(cl[0]["people"], 2)
        self.assertAlmostEqual(cl[0]["value"], 86700.0)

    def test_one_insider_buying_twice_is_not_a_cluster(self):
        base = {"issuer": "A", "isin": "X", "side": "buy", "value": 10.0, "traded": "2026-11-01", "person": "P"}
        self.assertEqual(ins.clusters([base, dict(base, traded="2026-11-05")]), [])

    def test_holdings_flag_matches_by_isin(self):
        rows, _ = ins.read_table(CSV)
        pf = {"positions": [{"ticker": "BSP.DE", "isin": "DE0000000001", "shares": 3, "lots": []}]}
        flagged = ins.flag_holdings(rows, pf, {})
        self.assertEqual([r["held"] for r in flagged], ["BSP.DE", "BSP.DE", None, None, None])

    def test_isin_map_file_adds_tickers(self):
        rows, _ = ins.read_table(CSV)
        pf = {"positions": [{"ticker": "MST.DE", "shares": 1, "lots": []}]}
        flagged = ins.flag_holdings(rows, pf, {"DE0000000002": "MST.DE"})
        self.assertEqual(flagged[2]["held"], "MST.DE")


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.state = os.path.join(self.d, "insiders.json")

    def test_list_shows_the_rows_and_the_caveat(self):
        code, out, _ = run(["list", CSV])
        self.assertEqual(code, 0)
        self.assertIn("Beispiel AG", out)
        self.assertIn("BUY", out)
        self.assertIn("not inside information", out)

    def test_filters(self):
        code, out, _ = run(["list", CSV, "--side", "buy", "--min-value", "50000"])
        self.assertIn("Erika Muster", out)
        self.assertNotIn("Hans Probe", out)
        self.assertNotIn("Max Test", out)
        code, out, _ = run(["list", CSV, "--since", "2026-11-03"])
        self.assertNotIn("Max Test", out)

    def test_new_shows_each_row_once(self):
        code, out, _ = run(["new", CSV, "--state", self.state])
        self.assertEqual(code, 0)
        self.assertIn("5 new", out)
        code, out, _ = run(["new", CSV, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("nothing new", out.lower())

    def test_a_row_that_failed_to_read_is_not_marked_seen(self):
        p = os.path.join(self.d, "x.csv")
        lines = open(CSV, encoding="utf-8").read().splitlines()
        good = lines[1]
        lines[1] = good.replace("05.11.2026", "kaputt")
        open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        run(["new", p, "--state", self.state])
        lines[1] = good
        open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        code, out, _ = run(["new", p, "--state", self.state])
        self.assertIn("Erika Muster", out)

    def test_ambiguous_numbers_are_reported_and_the_flag_fixes_them(self):
        p = os.path.join(self.d, "x.csv")
        lines = open(CSV, encoding="utf-8").read().splitlines()
        lines[1] = lines[1].replace("61.700,00", "1.234")
        open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        code, out, err = run(["list", p])
        self.assertIn("ambiguous", out + err)
        code, out, err = run(["list", p, "--decimal", "comma"])
        self.assertNotIn("ambiguous", out + err)

    def test_missing_file(self):
        code, _, err = run(["list", os.path.join(self.d, "nope.csv")])
        self.assertEqual(code, 1)
        self.assertIn("no file", err)


if __name__ == "__main__":
    unittest.main()
