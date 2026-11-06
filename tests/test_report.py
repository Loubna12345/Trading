import math
import os
import re
import tempfile
import unittest

from _load import load
from test_replay import Env, COSTS, WEEKS_DAYS

replay = load("trader-replay", "replay")
report = load("trader-replay", "report")


class Played(Env):
    """A replay with a few weeks played with different rules and lettered notes."""

    def setUp(self):
        super().setUp()
        self.start()
        plan = [(2, "trend", {"window": 3}, 0.001, "up", "A: momentum after a rising week"),
                (3, "vwap", {"band_bps": 25, "warmup": 4}, 0.001, "up", "B: rebound <script>alert(1)</script>"),
                (4, "none", {}, 0.0, "down", "C: flat, stay out"),
                (5, "trend", {"window": 3}, 0.001, "up", "A: momentum again"),
                (6, "none", {}, 0.0, "up", "D: after a big drop, stay out")]
        for k, name, params, er, em, note in plan:
            replay.predict(self.dir, k, name, params, expect_return=er, expect_market=em, note=note)
            replay.reveal(self.dir, k)

    def data(self):
        return report.build(self.dir)


class Build(Played):
    def test_counts_and_order(self):
        d = self.data()
        self.assertEqual([w["week"] for w in d["weeks"]], [2, 3, 4, 5, 6])
        self.assertEqual(d["revealed_through"], 6)
        self.assertEqual(d["weeks_sealed"], 9)

    def test_cumulative_rules_is_the_compounded_weekly_results(self):
        d = self.data()
        total = 1.0
        for w in d["weeks"]:
            total *= 1.0 + w["strategy_return"]
        self.assertAlmostEqual(d["rules_total"], total - 1.0, places=12)
        self.assertAlmostEqual(d["cum_rules"][-1], total - 1.0, places=12)
        self.assertEqual(len(d["cum_rules"]), 5)
        self.assertEqual(len(d["cum_stock"]), 5)

    def test_true_stock_move_runs_from_the_first_scored_open_to_the_last_scored_close(self):
        d = self.data()
        blind, weeks = replay.anonymize(self.bars, WEEKS_DAYS)
        expect = blind[weeks[5][1] - 1]["c"] / blind[weeks[1][0]]["o"] - 1.0
        self.assertAlmostEqual(d["stock_total"], expect, places=12)
        self.assertAlmostEqual(d["cum_stock"][-1], expect, places=12)

    def test_overnight_and_intraday_split_adds_up_to_the_whole_move_except_the_first_gap(self):
        d = self.data()
        blind, weeks = replay.anonymize(self.bars, WEEKS_DAYS)
        a, b = weeks[1][0], weeks[5][1]
        whole = math.log(blind[b - 1]["c"] / blind[a]["o"])
        self.assertAlmostEqual(d["overnight_log"] + d["intraday_log"], whole, places=9)

    def test_assumptions_are_grouped_by_the_letter_in_the_note(self):
        d = self.data()
        by = {a["letter"]: a for a in d["assumptions"]}
        self.assertEqual(sorted(by), ["A", "B", "C", "D"])
        self.assertEqual(by["A"]["weeks"], 2)
        self.assertIn("momentum after a rising week", by["A"]["first_note"])

    def test_rule_table_counts_weeks_and_trades(self):
        d = self.data()
        rows = {r["rule"]: r for r in d["rules"]}
        self.assertEqual(rows["trend" + '{"window":3}']["weeks"], 2)
        self.assertEqual(rows["none{}"]["weeks"], 2)
        self.assertEqual(rows["none{}"]["trades"], 0)

    def test_nothing_from_the_sealed_weeks_is_used(self):
        d = self.data()
        self.assertEqual(d["weeks"][-1]["week"], 6)
        self.assertEqual(d["finals_run"], 0)

    def test_a_replay_with_no_scored_week_is_refused(self):
        d2 = os.path.join(self.root, "empty")
        replay.start(source=self.csv, folder=d2, costs=COSTS, periods_per_year=self.PPY,
                     days_per_week=WEEKS_DAYS, holdout=0.34)
        with self.assertRaises(ValueError):
            report.build(d2)

    def test_a_changed_ledger_is_refused(self):
        path = replay.T.ledger_path(self.dir)
        with open(path) as fh:
            text = fh.read()
        with open(path, "w") as fh:
            fh.write(text.replace('"window":3', '"window":4'))
        with self.assertRaises(ValueError):
            report.build(self.dir)


class Verdict(unittest.TestCase):
    def base(self, **over):
        d = {"weeks": [{}] * 63, "rules_total": -0.06, "stock_total": -0.31,
             "forecast": {"weeks": 63, "mean_abs_error": 0.0081, "mean_abs_error_if_always_zero": 0.0081,
                          "direction_hits": 29, "always_up_hits": 31, "direction_p_value_vs_coin": 0.78}}
        d.update(over)
        return d

    def text(self, **over):
        return " ".join(report.verdict(self.base(**over)))

    def test_a_losing_run_says_it_lost_and_does_not_call_beating_a_falling_stock_an_edge(self):
        t = self.text()
        self.assertIn("lost", t)
        self.assertIn("not an edge", t)

    def test_a_winning_run_in_a_rising_stock_is_not_called_proof(self):
        t = self.text(rules_total=0.05, stock_total=0.20)
        self.assertNotIn("not an edge", t)
        self.assertIn("less than holding", t)
        self.assertIn("not proof", t)

    def test_forecast_no_better_than_zero_is_said_plainly(self):
        self.assertIn("no better than", self.text())

    def test_always_up_beating_the_forecaster_is_said(self):
        self.assertIn("always saying up", self.text())


class Render(Played):
    def html(self, **kw):
        return report.render(self.data(), **kw)

    def test_page_is_self_contained(self):
        h = self.html()
        self.assertTrue(h.lstrip().lower().startswith("<!doctype html>"))
        self.assertIn("<svg", h)
        self.assertNotRegex(h, r'(src|href)="https?://')
        self.assertNotIn("@import", h)

    def test_notes_are_escaped(self):
        h = self.html()
        self.assertNotIn("<script>alert(1)</script>", h)
        self.assertIn("&lt;script&gt;", h)

    def test_the_page_does_not_name_the_stock_or_show_dates_or_paths(self):
        h = self.html()
        self.assertNotIn("SECRET", h)
        self.assertNotIn(self.root, h)
        self.assertIsNone(re.search(r"20\d\d-\d\d-\d\d", h))

    def test_label_is_shown_and_escaped(self):
        h = self.html(label="Dry run <b>costs</b>")
        self.assertIn("Dry run &lt;b&gt;costs&lt;/b&gt;", h)

    def test_light_and_dark_themes_and_a_table_are_present(self):
        h = self.html()
        self.assertIn("prefers-color-scheme: dark", h)
        self.assertIn('data-theme="dark"', h)
        self.assertIn("<table", h)

    def test_rules_are_shown_by_name_not_as_raw_keys_and_zero_is_not_negative(self):
        h = self.html()
        self.assertIn("trend (window=3)", h)
        self.assertNotIn('trend{"', h)
        self.assertNotIn(" , rules", h)
        self.assertEqual(report.pct(-1e-12, False, 0), "0%")
        self.assertEqual(report.pct(-0.0, True, 1), "+0.0%")

    def test_write_creates_the_file(self):
        out = os.path.join(self.root, "report.html")
        report.write(self.dir, out, label="x")
        self.assertGreater(os.path.getsize(out), 2000)

    def test_cli(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out = os.path.join(self.root, "r2.html")
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            code = report.main(["--dir", self.dir, "--out", out, "--label", "dry run"])
        self.assertEqual(code, 0, e.getvalue())
        self.assertTrue(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
