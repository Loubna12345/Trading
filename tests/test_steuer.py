"""German tax tracker (trader-tax). Rules and where they come from:

  25 % flat tax, church-tax formula        § 32d Abs. 1 EStG   (tax = (e - 4q) / (4 + k))
  gain = proceeds - costs - purchase cost  § 20 Abs. 4 Satz 1 EStG
  FIFO within one custody account          § 20 Abs. 4 Satz 7 EStG
  stock losses only against stock gains    § 20 Abs. 6 Satz 4 EStG
  saver's allowance 1,000 / 2,000 EUR      § 20 Abs. 9 EStG
  fund exemption 30 / 15 / 60 / 80 %       § 20 InvStG (also applied to fund losses: BFH 25.11.2025, VIII R 31/23)
  solidarity surcharge 5.5 % of the tax    § 4 SolZG
Published example to check the formula: 1,000 EUR gain, 9 % church tax -> 27.99 % in total.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

st = load("trader-tax", "steuer")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = st.main(argv)
    return code, out.getvalue(), err.getvalue()


def sale(gain, kind="stock", fund_type=None, date="2026-06-01"):
    r = {"date": date, "ticker": "X", "shares": 1, "proceeds": 100 + gain, "cost": 100, "fees": 0, "kind": kind,
         "gain": gain}
    if fund_type:
        r["fund_type"] = fund_type
    return r


def state_with(realized=(), income=(), carry=None, **settings):
    s = {"settings": dict({"status": "single", "church_tax_rate": 0}, **settings), "realized": list(realized),
         "income": list(income), "carry_in": carry or {}}
    return s


class TaxFormula(unittest.TestCase):
    def test_published_example_with_9_percent_church_tax(self):
        t = st.compute_tax(1000, church_rate=0.09, soli_rate=0.055)
        self.assertAlmostEqual(t["income_tax"], 244.4988, places=3)
        self.assertAlmostEqual(t["church_tax"], 22.0049, places=3)
        self.assertAlmostEqual(t["soli"], 13.4474, places=3)
        self.assertAlmostEqual(t["total"] / 1000 * 100, 27.99, delta=0.01)  # published table adds rounded parts

    def test_8_percent_church_tax(self):
        t = st.compute_tax(1000, church_rate=0.08, soli_rate=0.055)
        self.assertAlmostEqual(t["total"] / 1000 * 100, 27.82, places=2)

    def test_no_church_tax(self):
        t = st.compute_tax(1000, church_rate=0, soli_rate=0.055)
        self.assertAlmostEqual(t["income_tax"], 250.0)
        self.assertAlmostEqual(t["soli"], 13.75)
        self.assertAlmostEqual(t["total"], 263.75)

    def test_foreign_tax_is_credited_inside_the_formula(self):
        t = st.compute_tax(1000, church_rate=0, soli_rate=0.055, foreign_tax=15)
        self.assertAlmostEqual(t["income_tax"], 235.0)  # (1000 - 4*15) / 4

    def test_tax_is_never_negative(self):
        t = st.compute_tax(100, church_rate=0, soli_rate=0.055, foreign_tax=500)
        self.assertEqual(t["total"], 0)

    def test_bad_inputs_are_refused(self):
        for kw in ({"church_rate": 0.5}, {"church_rate": -0.01}, {"soli_rate": -1}, {"foreign_tax": -1}):
            with self.assertRaises(st.TaxError):
                st.compute_tax(1000, **dict({"church_rate": 0, "soli_rate": 0.055}, **kw))
        with self.assertRaises(st.TaxError):
            st.compute_tax(float("nan"), church_rate=0, soli_rate=0.055)


class FundExemption(unittest.TestCase):
    def test_rates(self):
        self.assertEqual([st.teilfreistellung(t) for t in ("equity", "mixed", "real_estate", "foreign_real_estate", "other")],
                         [0.30, 0.15, 0.60, 0.80, 0.0])

    def test_unknown_fund_type_is_refused(self):
        with self.assertRaises(st.TaxError):
            st.teilfreistellung("crypto")

    def test_the_exemption_cuts_gains_and_losses(self):
        self.assertAlmostEqual(st.taxable_amount(sale(1000, "fund", "equity")), 700)
        self.assertAlmostEqual(st.taxable_amount(sale(-1000, "fund", "equity")), -700)
        self.assertAlmostEqual(st.taxable_amount(sale(1000)), 1000)


class LossPots(unittest.TestCase):
    def year(self, **kw):
        return st.year_summary(state_with(**kw), 2026)

    def test_allowance_single_and_joint(self):
        y = self.year(realized=[sale(1500)])
        self.assertEqual(y["allowance"], 1000)
        self.assertAlmostEqual(y["taxable"], 500)
        self.assertAlmostEqual(y["tax"]["total"], 125 * 1.055)
        y = self.year(realized=[sale(1500)], status="joint")
        self.assertEqual(y["allowance"], 2000)
        self.assertEqual(y["taxable"], 0)

    def test_stock_losses_offset_stock_gains(self):
        y = self.year(realized=[sale(1000), sale(-400)])
        self.assertAlmostEqual(y["taxable_before_allowance"], 600)

    def test_stock_losses_do_not_offset_dividends(self):
        y = self.year(realized=[sale(-500)], income=[{"date": "2026-05-01", "type": "dividend", "gross": 3000}])
        self.assertAlmostEqual(y["taxable_before_allowance"], 3000)
        self.assertAlmostEqual(y["carry_out"]["stock"], 500)

    def test_stock_losses_do_not_offset_fund_gains(self):
        y = self.year(realized=[sale(-500), sale(1000, "fund", "equity")])
        self.assertAlmostEqual(y["taxable_before_allowance"], 700)
        self.assertAlmostEqual(y["carry_out"]["stock"], 500)

    def test_fund_losses_do_offset_stock_gains(self):
        y = self.year(realized=[sale(2000), sale(-1000, "fund", "equity")])
        self.assertAlmostEqual(y["taxable_before_allowance"], 1300)  # 2000 - 700

    def test_other_gains_are_not_offset_by_stock_losses_but_by_other_losses(self):
        y = self.year(realized=[sale(1000, "fund", "other"), sale(-300, "fund", "other")])
        self.assertAlmostEqual(y["taxable_before_allowance"], 700)

    def test_carry_in_reduces_the_same_pot(self):
        y = self.year(realized=[sale(1000)], carry={"2026": {"stock": 300, "other": 0}})
        self.assertAlmostEqual(y["taxable_before_allowance"], 700)
        y = self.year(realized=[sale(-100)], carry={"2026": {"stock": 300, "other": 50}})
        self.assertAlmostEqual(y["carry_out"]["stock"], 400)
        self.assertAlmostEqual(y["carry_out"]["other"], 50)

    def test_only_the_chosen_year_counts(self):
        y = self.year(realized=[sale(1000, date="2025-12-31"), sale(200, date="2026-01-02")])
        self.assertAlmostEqual(y["taxable_before_allowance"], 200)

    def test_foreign_tax_on_dividends_is_credited(self):
        y = self.year(income=[{"date": "2026-05-01", "type": "dividend", "gross": 2000, "foreign_tax": 100}])
        # taxable 2000 - 1000 = 1000; (1000 - 4*100) / 4 = 150
        self.assertAlmostEqual(y["tax"]["income_tax"], 150)

    def test_church_tax_setting_flows_through(self):
        y = self.year(realized=[sale(2000)], church_tax_rate=0.09)
        self.assertAlmostEqual(y["tax"]["total"] / 1000 * 100, 27.99, delta=0.01)

    def test_allowance_override_for_a_partly_used_allowance(self):
        y = self.year(realized=[sale(1500)], allowance=400)
        self.assertEqual(y["allowance"], 400)
        self.assertAlmostEqual(y["taxable"], 1100)


class Fifo(unittest.TestCase):
    LOTS = [{"id": "A-2", "date": "2026-03-01", "shares": 10, "cost": 1500.0},
            {"id": "A-1", "date": "2026-01-05", "shares": 10, "cost": 1000.0}]

    def test_oldest_first_across_lots(self):
        r = st.simulate_sale(self.LOTS, 15, price=150.0, fees=0)
        self.assertEqual([(u["id"], u["shares"]) for u in r["used"]], [("A-1", 10), ("A-2", 5)])
        self.assertAlmostEqual(r["proceeds"], 2250)
        self.assertAlmostEqual(r["cost"], 1750)
        self.assertAlmostEqual(r["gain"], 500)

    def test_fees_reduce_the_gain(self):
        r = st.simulate_sale(self.LOTS, 10, price=150.0, fees=4.9)
        self.assertAlmostEqual(r["gain"], 1500 - 4.9 - 1000)

    def test_cannot_sell_more_than_held(self):
        with self.assertRaises(st.TaxError):
            st.simulate_sale(self.LOTS, 20.5, price=1, fees=0)

    def test_lots_in_two_depots_need_a_choice(self):
        lots = [dict(self.LOTS[0], depot="bank-a"), dict(self.LOTS[1], depot="bank-b")]
        with self.assertRaises(st.TaxError) as cm:
            st.simulate_sale(lots, 5, price=1, fees=0)
        self.assertIn("depot", str(cm.exception))
        r = st.simulate_sale(lots, 5, price=1, fees=0, depot="bank-a")
        self.assertEqual(r["used"][0]["id"], "A-2")

    def test_bad_numbers_are_refused(self):
        for kw in ({"shares": 0}, {"shares": float("nan")}, {"price": -1}, {"price": float("inf")}, {"fees": -1}):
            args = dict({"shares": 1, "price": 1.0, "fees": 0}, **kw)
            with self.assertRaises(st.TaxError):
                st.simulate_sale(self.LOTS, args["shares"], price=args["price"], fees=args["fees"])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.pf = os.path.join(self.d, "portfolio.json")
        self.prices = os.path.join(self.d, "prices.json")
        self.tax = os.path.join(self.d, "tax.json")
        json.dump({"cash": 0, "positions": [
            {"ticker": "SAP.DE", "shares": 10, "lots": [
                {"id": "SAP.DE-1", "date": "2026-01-05", "shares": 4, "cost": 400.0},
                {"id": "SAP.DE-2", "date": "2026-03-01", "shares": 6, "cost": 900.0}]},
            {"ticker": "EUNL.DE", "fund_type": "equity", "shares": 20, "lots": [
                {"id": "EUNL.DE-1", "date": "2026-02-01", "shares": 20, "cost": 2000.0}]}]}, open(self.pf, "w"))
        json.dump({"currency": "EUR", "prices": {"SAP.DE": 140.0, "EUNL.DE": 90.0}}, open(self.prices, "w"))
        self.base = ["--portfolio", self.pf, "--prices", self.prices, "--tax", self.tax]

    def test_settings_are_saved_and_validated(self):
        code, out, err = run(["settings", "--tax", self.tax, "--status", "joint", "--church", "0.09"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.load(open(self.tax))["settings"], {"status": "joint", "church_tax_rate": 0.09})
        self.assertEqual(run(["settings", "--tax", self.tax, "--church", "0.5"])[0], 1)
        self.assertEqual(run(["settings", "--tax", self.tax, "--status", "married"])[0], 1)

    def test_report_shows_unrealized_by_lot_in_euro(self):
        code, out, _ = run(["report", "--lots"] + self.base)
        self.assertEqual(code, 0)
        self.assertIn("SAP.DE", out)
        self.assertIn("€1,400", out)           # 10 x 140
        self.assertIn("-€200", out)            # 1,400 - 1,300 cost... SAP lots cost 1,300 -> +100; EUNL 1,800 vs 2,000 -> -200
        self.assertIn("EUNL.DE", out)

    def test_sell_previews_gain_pot_and_extra_tax_without_changing_anything(self):
        before = open(self.pf).read()
        code, out, _ = run(["sell", "SAP.DE", "5", "--price", "140", "--date", "2026-06-01"] + self.base)
        self.assertEqual(code, 0)
        # FIFO: 4 sh at 100 + 1 sh at 150 -> cost 550, proceeds 700, gain 150
        self.assertIn("gain €150", out)
        self.assertIn("stock", out.lower())
        self.assertEqual(open(self.pf).read(), before)
        self.assertFalse(os.path.exists(self.tax))

    def test_record_updates_lots_and_the_realized_log(self):
        code, out, err = run(["sell", "SAP.DE", "5", "--price", "140", "--date", "2026-06-01", "--record"] + self.base)
        self.assertEqual(code, 0, err)
        pos = {p["ticker"]: p for p in json.load(open(self.pf))["positions"]}
        self.assertAlmostEqual(pos["SAP.DE"]["shares"], 5)
        self.assertEqual([l["id"] for l in pos["SAP.DE"]["lots"]], ["SAP.DE-2"])
        self.assertAlmostEqual(pos["SAP.DE"]["lots"][0]["shares"], 5)
        self.assertAlmostEqual(pos["SAP.DE"]["lots"][0]["cost"], 750.0)
        rec = json.load(open(self.tax))["realized"]
        self.assertEqual(len(rec), 1)
        self.assertAlmostEqual(rec[0]["gain"], 150)
        self.assertEqual(rec[0]["kind"], "stock")
        self.assertEqual(json.load(open(self.pf))["cash"], 700.0)  # net proceeds come back as cash

    def test_a_fund_sale_is_logged_as_a_fund_with_its_exemption(self):
        code, out, err = run(["sell", "EUNL.DE", "20", "--price", "90", "--date", "2026-06-01", "--record"] + self.base)
        self.assertEqual(code, 0, err)
        rec = json.load(open(self.tax))["realized"][0]
        self.assertEqual((rec["kind"], rec["fund_type"]), ("fund", "equity"))
        self.assertAlmostEqual(rec["gain"], -200)
        code, out, _ = run(["year", "--year", "2026"] + self.base)
        self.assertIn("-€140", out)            # -200 x (1 - 30 %)

    def test_year_report_and_december_reminder(self):
        run(["sell", "SAP.DE", "10", "--price", "200", "--date", "2026-06-01", "--record"] + self.base)
        run(["sell", "EUNL.DE", "20", "--price", "80", "--date", "2026-06-02", "--record"] + self.base)
        code, out, _ = run(["year", "--year", "2026", "--today", "2026-11-20"] + self.base)
        self.assertEqual(code, 0)
        self.assertIn("Sparer-Pauschbetrag", out)
        self.assertIn("15 December", out)
        code, out, _ = run(["year", "--year", "2026", "--today", "2026-03-01"] + self.base)
        self.assertNotIn("15 December", out)

    def test_income_and_carry_are_recorded(self):
        code, _, err = run(["income", "--ticker", "KO", "--type", "dividend", "--gross", "30", "--foreign-tax", "4.5",
                            "--date", "2026-04-15"] + self.base)
        self.assertEqual(code, 0, err)
        code, _, err = run(["carry", "--year", "2026", "--stock", "300"] + self.base)
        self.assertEqual(code, 0, err)
        doc = json.load(open(self.tax))
        self.assertEqual(doc["income"][0]["foreign_tax"], 4.5)
        self.assertEqual(doc["carry_in"]["2026"]["stock"], 300)

    def test_pending_constitutional_court_case_is_mentioned_when_stock_losses_exist(self):
        run(["sell", "SAP.DE", "10", "--price", "100", "--date", "2026-06-01", "--record"] + self.base)
        code, out, _ = run(["year", "--year", "2026"] + self.base)
        self.assertIn("2 BvL 3/21", out)

    def test_missing_files_give_a_clear_error(self):
        code, _, err = run(["report", "--portfolio", os.path.join(self.d, "nope.json"), "--prices", self.prices,
                            "--tax", self.tax])
        self.assertEqual(code, 1)
        self.assertIn("no portfolio", err)

    def test_prices_not_in_euro_are_refused(self):
        json.dump({"currency": "USD", "prices": {"SAP.DE": 140.0}}, open(self.prices, "w"))
        code, _, err = run(["report"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("euro", err)

    def test_harvest_lists_losses_and_what_they_could_offset(self):
        run(["sell", "SAP.DE", "10", "--price", "200", "--date", "2026-06-01", "--record"] + self.base)
        json.dump({"cash": 0, "positions": [
            {"ticker": "BMW.DE", "shares": 10, "lots": [{"id": "B-1", "date": "2026-02-01", "shares": 10, "cost": 1000.0}]}]},
            open(self.pf, "w"))
        json.dump({"currency": "EUR", "prices": {"BMW.DE": 80.0}}, open(self.prices, "w"))
        code, out, _ = run(["harvest", "--min-loss", "100", "--year", "2026"] + self.base)
        self.assertEqual(code, 0)
        self.assertIn("BMW.DE", out)
        self.assertIn("-€200", out)
        self.assertIn("stock gains", out)


if __name__ == "__main__":
    unittest.main()
