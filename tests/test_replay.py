import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout

from _load import load
from test_training import series, write_store

replay = load("trader-replay", "replay")
engine = load("trader-backtest", "engine")

COSTS = {"commission_bps": 0.0, "spread_bps": 2.0, "slippage_bps": 1.0, "fixed_fee": 0.0}
WEEKS_DAYS = 5


class Env(unittest.TestCase):
    N_DAYS = 128          # 25 full weeks of 5 days plus 3 left-over days
    PPY = 252 * 20

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.store = os.path.join(self.root, "h")
        os.makedirs(self.store)
        self.bars = series(7, n_days=self.N_DAYS)
        self.csv = write_store(self.store, "SECRET", self.bars)
        self.dir = os.path.join(self.root, "r")

    def start(self, **over):
        kw = dict(source=self.csv, folder=self.dir, costs=COSTS, periods_per_year=self.PPY,
                  days_per_week=WEEKS_DAYS, holdout=0.34)
        kw.update(over)
        return replay.start(**kw)

    def play_week(self, k, strategy="trend", params=None):
        replay.predict(self.dir, k, strategy, params if params is not None else {"window": 3}, expect_return=0.0, expect_market="up", note="t")
        return replay.reveal(self.dir, k)


class Anonymize(Env):
    def test_first_open_is_100_and_returns_are_unchanged(self):
        blind, _ = replay.anonymize(self.bars, WEEKS_DAYS)
        self.assertAlmostEqual(blind[0]["o"], 100.0)
        for a, b in zip(self.bars[:50], blind[:50]):
            self.assertAlmostEqual(a["c"] / a["o"], b["c"] / b["o"], places=9)
            self.assertAlmostEqual(a["h"] / a["l"], b["h"] / b["l"], places=9)

    def test_dates_are_replaced_by_consecutive_weekdays_and_time_of_day_is_kept(self):
        import datetime
        blind, _ = replay.anonymize(self.bars, WEEKS_DAYS)
        days = sorted({b["t"][:10] for b in blind})
        self.assertNotEqual(days[0], self.bars[0]["t"][:10])
        for d in days:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5)
        self.assertEqual(blind[0]["t"][10:], self.bars[0]["t"][10:])
        self.assertNotIn(self.bars[0]["t"][:10], {b["t"][:10] for b in blind})

    def test_volume_is_relative_to_the_median(self):
        blind, _ = replay.anonymize(self.bars, WEEKS_DAYS)
        vols = sorted(b["v"] for b in blind)
        self.assertAlmostEqual(vols[len(vols) // 2], 1.0, delta=0.05)

    def test_leftover_days_are_dropped_so_weeks_are_equal(self):
        blind, weeks = replay.anonymize(self.bars, WEEKS_DAYS)
        self.assertEqual(len(weeks), 25)
        self.assertEqual(len({b["t"][:10] for b in blind}), 125)
        for a, b in weeks:
            self.assertEqual(len({x["t"][:10] for x in blind[a:b]}), WEEKS_DAYS)


class SameJudgment(Env):
    """Hiding the stock must not change how a rule is judged: same trades, same returns."""

    RULES = [("trend", {"window": 3}), ("trend", {"window": 12}), ("orb", {"range_bars": 2}),
             ("orb", {"range_bars": 8}), ("vwap", {"band_bps": 10, "warmup": 6}),
             ("vwap", {"band_bps": 50, "warmup": 6})]

    def test_every_rule_scores_the_same_on_hidden_and_original_bars(self):
        blind, weeks = replay.anonymize(self.bars, WEEKS_DAYS)
        original = self.bars[:len(blind)]
        for name, params in self.RULES:
            fn = replay.strategies.make
            a = engine.backtest(original, fn(name, **params), COSTS, capital=replay.CAPITAL, flatten_eod=True)
            b = engine.backtest(blind, fn(name, **params), COSTS, capital=replay.CAPITAL, flatten_eod=True)
            self.assertEqual(len(a["trades"]), len(b["trades"]), name)
            for x, y in zip(a["equity"], b["equity"]):
                self.assertAlmostEqual(x, y, places=6, msg=name)
            self.assertAlmostEqual(a["costs_paid"], b["costs_paid"], places=6, msg=name)

    def test_a_fixed_fee_costs_the_same_because_the_money_invested_does_not_change(self):
        costs = dict(COSTS, fixed_fee=1.0)
        blind, _ = replay.anonymize(self.bars, WEEKS_DAYS)
        original = self.bars[:len(blind)]
        fn = replay.strategies.make("trend", window=3)
        a = engine.backtest(original, fn, costs, capital=replay.CAPITAL, flatten_eod=True)
        b = engine.backtest(blind, fn, costs, capital=replay.CAPITAL, flatten_eod=True)
        self.assertAlmostEqual(a["equity"][-1], b["equity"][-1], places=6)


class Start(Env):
    def test_start_reports_counts_but_no_dates_or_names(self):
        info = self.start()
        self.assertEqual(info["weeks"], 25)
        self.assertEqual(info["sealed_weeks"], 9)       # ceil(25 * 0.34)
        text = json.dumps(info)
        self.assertNotIn("SECRET", text)
        self.assertIsNone(re.search(r"20\d\d-\d\d-\d\d", text))

    def test_the_folder_holds_no_price_data(self):
        self.start()
        for name in os.listdir(self.dir):
            with open(os.path.join(self.dir, name)) as fh:
                text = fh.read()
            self.assertNotIn(repr(self.bars[5]["c"]), text)

    def test_costs_are_required_and_checked(self):
        with self.assertRaises(ValueError):
            self.start(costs=None)
        with self.assertRaises(ValueError):
            self.start(costs={"commission_bps": 0.0})

    def test_refuses_to_overwrite_a_running_replay(self):
        self.start()
        with self.assertRaises(ValueError):
            self.start()

    def test_too_few_weeks_are_refused(self):
        short = write_store(self.store, "SHORT", series(1, n_days=40))
        with self.assertRaises(ValueError):
            self.start(source=short)

    def test_changed_source_data_is_noticed(self):
        self.start()
        with open(self.csv, "a") as fh:
            fh.write("\n")
        with self.assertRaises(ValueError):
            replay.show(self.dir, 1)


class Relocate(Env):
    """A replay started on one computer must keep working when the same data file sits somewhere else."""

    def setUp(self):
        super().setUp()
        self.start()
        self.play_week(2)
        self.moved = os.path.join(self.root, "elsewhere", "copy.csv")
        os.makedirs(os.path.dirname(self.moved))
        os.replace(self.csv, self.moved)

    def test_a_moved_source_is_refused_with_a_pointer_to_relocate(self):
        with self.assertRaisesRegex(ValueError, "relocate"):
            replay.status(self.dir)

    def test_relocate_to_the_same_data_keeps_the_replay_going(self):
        replay.relocate(self.dir, self.moved)
        st = replay.status(self.dir)
        self.assertEqual(st["weeks_scored"], 1)
        self.play_week(3)
        self.assertEqual(replay.status(self.dir)["weeks_scored"], 2)
        self.assertEqual(replay.T.ledger_verify(self.dir), len(replay.T.ledger_read(self.dir)))

    def test_relocate_is_logged_and_the_plan_is_not_touched(self):
        with open(os.path.join(self.dir, replay.PLAN)) as fh:
            before = fh.read()
        replay.relocate(self.dir, self.moved)
        with open(os.path.join(self.dir, replay.PLAN)) as fh:
            self.assertEqual(fh.read(), before)
        last = replay.T.ledger_read(self.dir)[-1]
        self.assertEqual(last["kind"], "relocate")
        self.assertEqual(last["source"], os.path.abspath(self.moved))

    def test_relocate_to_different_data_is_refused_and_not_logged(self):
        other = write_store(self.store, "OTHER", series(8, n_days=self.N_DAYS))
        n = len(replay.T.ledger_read(self.dir))
        with self.assertRaisesRegex(ValueError, "not the same data"):
            replay.relocate(self.dir, other)
        self.assertEqual(len(replay.T.ledger_read(self.dir)), n)

    def test_the_latest_relocation_wins(self):
        replay.relocate(self.dir, self.moved)
        again = os.path.join(self.root, "third.csv")
        os.replace(self.moved, again)
        replay.relocate(self.dir, again)
        self.assertEqual(replay.status(self.dir)["weeks_scored"], 1)

    def test_relocate_from_the_command_line(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(replay.main(["relocate", "--dir", self.dir, "--source", self.moved]), 0)
        self.assertEqual(json.loads(out.getvalue())["source"], os.path.abspath(self.moved))


class Order(Env):
    def setUp(self):
        super().setUp()
        self.start()

    def test_week_one_is_free_to_read(self):
        bars = replay.show(self.dir, 1)
        self.assertEqual(len({b["t"][:10] for b in bars}), WEEKS_DAYS)

    def test_week_two_cannot_be_read_without_a_prediction(self):
        with self.assertRaises(ValueError):
            replay.show(self.dir, 2)
        with self.assertRaises(ValueError):
            replay.reveal(self.dir, 2)

    def test_prediction_must_be_for_the_next_unrevealed_week(self):
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 3, "trend", {"window": 3}, expect_return=0.0, expect_market="up", note="")
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 1, "trend", {"window": 3}, expect_return=0.0, expect_market="up", note="")

    def test_a_week_cannot_be_predicted_twice(self):
        replay.predict(self.dir, 2, "trend", {"window": 3}, expect_return=0.0, expect_market="up", note="")
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 2, "orb", {"range_bars": 2}, expect_return=0.0, expect_market="up", note="")

    def test_after_reveal_the_week_can_be_read_again_and_the_next_is_open_for_prediction(self):
        self.play_week(2)
        self.assertEqual(len(replay.show(self.dir, 2)), len(replay.show(self.dir, 1)))
        replay.predict(self.dir, 3, "trend", {"window": 3}, expect_return=0.0, expect_market="up", note="")

    def test_unknown_strategy_or_missing_params_are_refused_and_not_logged(self):
        before = len(replay.T.ledger_read(self.dir))
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 2, "crystal-ball", {}, expect_return=0.0, expect_market="up", note="")
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 2, "trend", {}, expect_return=0.0, expect_market="up", note="")
        self.assertEqual(len(replay.T.ledger_read(self.dir)), before)

    def test_sealed_weeks_cannot_be_read_or_predicted(self):
        for k in range(2, 17):
            self.play_week(k)
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 17, "trend", {"window": 3}, expect_return=0.0, expect_market="up", note="")
        with self.assertRaises(ValueError):
            replay.show(self.dir, 17)
        with self.assertRaises(ValueError):
            replay.show(self.dir, 25)

    def test_the_prediction_is_in_the_ledger_before_the_result(self):
        self.play_week(2)
        kinds = [e["kind"] for e in replay.T.ledger_read(self.dir)]
        self.assertLess(kinds.index("prediction"), kinds.index("result"))
        self.assertEqual(replay.T.ledger_verify(self.dir), len(kinds))


class Scoring(Env):
    def setUp(self):
        super().setUp()
        self.start()

    def test_staying_out_scores_zero_with_no_trades(self):
        res = self.play_week(2, "none", {})
        self.assertEqual(res["trades"], 0)
        self.assertAlmostEqual(res["strategy_return"], 0.0)

    def test_score_matches_a_direct_backtest(self):
        res = self.play_week(2, "trend", {"window": 3})
        blind, weeks = replay.anonymize(self.bars, WEEKS_DAYS)
        end = weeks[1][1]
        strat = replay.strategies.make("trend", window=3)
        run = engine.backtest(blind[:end], strat, COSTS, capital=replay.CAPITAL, flatten_eod=True)
        start_eq = run["equity"][weeks[1][0] - 1]
        self.assertAlmostEqual(res["strategy_return"], run["equity"][end - 1] / start_eq - 1.0, places=12)

    def test_result_carries_buy_and_hold_and_costs(self):
        res = self.play_week(2, "trend", {"window": 3})
        for k in ("strategy_return", "buy_and_hold_return", "trades", "costs_paid", "week", "bars"):
            self.assertIn(k, res)

    def test_status_counts_and_running_totals(self):
        self.play_week(2)
        self.play_week(3, "none", {})
        st = replay.status(self.dir)
        self.assertEqual(st["revealed_through"], 3)
        self.assertEqual(st["predictions"], 2)
        self.assertEqual(st["weeks_open"], 16)
        self.assertEqual(st["weeks_sealed"], 9)
        self.assertTrue(st["ledger_ok"])
        self.assertNotIn("SECRET", json.dumps(st))

    def test_tampering_with_the_ledger_is_caught(self):
        self.play_week(2)
        path = replay.T.ledger_path(self.dir)
        with open(path) as fh:
            text = fh.read()
        with open(path, "w") as fh:
            fh.write(text.replace('"window":3', '"window":4'))
        with self.assertRaises(ValueError):
            replay.status(self.dir)


class Forecast(Env):
    def setUp(self):
        super().setUp()
        self.start()

    def predict(self, k, er, em, strategy="trend", params=None):
        return replay.predict(self.dir, k, strategy, params if params is not None else {"window": 3},
                              expect_return=er, expect_market=em, note="")

    def test_both_forecasts_are_required_and_checked_before_anything_is_logged(self):
        before = len(replay.T.ledger_read(self.dir))
        for er, em in ((None, "up"), ("0.01", "up"), (float("nan"), "up"), (2.5, "up"), (True, "up"),
                       (0.01, None), (0.01, "sideways")):
            with self.assertRaises(ValueError, msg=(er, em)):
                self.predict(2, er, em)
        self.assertEqual(len(replay.T.ledger_read(self.dir)), before)

    def test_the_forecast_is_in_the_ledger_before_the_week_is_shown(self):
        e = self.predict(2, 0.004, "down")
        self.assertEqual((e["expect_return"], e["expect_market"]), (0.004, "down"))
        self.assertNotIn("result", [x["kind"] for x in replay.T.ledger_read(self.dir)])

    def test_the_result_stores_the_difference_between_forecast_and_actual(self):
        self.predict(2, 0.004, "up")
        res = replay.reveal(self.dir, 2)
        self.assertEqual(res["expected_return"], 0.004)
        self.assertAlmostEqual(res["error"], res["strategy_return"] - 0.004, places=12)
        self.assertEqual(res["market_expected"], "up")
        self.assertEqual(res["market_actual"], "up" if res["buy_and_hold_return"] > 0 else "down")
        self.assertEqual(res["direction_hit"], res["market_actual"] == "up")
        stored = [e for e in replay.T.ledger_read(self.dir) if e["kind"] == "result"][0]
        self.assertEqual(stored["error"], res["error"])

    def test_status_compares_the_forecasts_with_doing_nothing_and_with_always_up(self):
        for k, (er, em) in zip(range(2, 7), [(0.01, "up"), (-0.01, "down"), (0.0, "up"), (0.02, "down"), (0.0, "up")]):
            self.predict(k, er, em, "none", {})
            replay.reveal(self.dir, k)
        st = replay.status(self.dir)
        f = st["forecast"]
        self.assertEqual(f["weeks"], 5)
        results = [e for e in replay.T.ledger_read(self.dir) if e["kind"] == "result"]
        self.assertAlmostEqual(f["mean_abs_error"], sum(abs(e["error"]) for e in results) / 5, places=12)
        self.assertAlmostEqual(f["mean_abs_error_if_always_zero"],
                               sum(abs(e["strategy_return"]) for e in results) / 5, places=12)
        self.assertAlmostEqual(f["mean_error"], sum(e["error"] for e in results) / 5, places=12)
        self.assertEqual(f["direction_hits"], sum(1 for e in results if e["direction_hit"]))
        self.assertEqual(f["always_up_hits"], sum(1 for e in results if e["market_actual"] == "up"))
        self.assertTrue(0.0 < f["direction_p_value_vs_coin"] <= 1.0)

    def test_status_with_no_scored_week_says_so(self):
        self.assertEqual(replay.status(self.dir)["forecast"]["weeks"], 0)


class Final(Env):
    def setUp(self):
        super().setUp()
        self.start()
        for k in range(2, 6):
            self.play_week(k)

    def test_final_needs_a_rule_that_was_predicted_before(self):
        with self.assertRaises(ValueError):
            replay.final(self.dir, "trend", {"window": 9})

    def test_final_runs_once_per_rule_on_the_sealed_weeks_and_says_how_many_rules_were_tried(self):
        res = replay.final(self.dir, "trend", {"window": 3})
        self.assertEqual(res["weeks"], 9)
        self.assertEqual(res["rules_tried"], 1)
        self.assertIn("buy_and_hold_return", res)
        with self.assertRaises(ValueError):
            replay.final(self.dir, "trend", {"window": 3})
        self.assertEqual(replay.T.ledger_verify(self.dir), len(replay.T.ledger_read(self.dir)))

    def test_final_is_not_possible_before_a_week_was_played(self):
        d2 = os.path.join(self.root, "r2")
        replay.start(source=self.csv, folder=d2, costs=COSTS, periods_per_year=self.PPY,
                     days_per_week=WEEKS_DAYS, holdout=0.34)
        with self.assertRaises(ValueError):
            replay.final(d2, "trend", {"window": 3})


class Cli(Env):
    def run_cli(self, *argv):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = replay.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_a_full_round_trip_through_the_command_line(self):
        code, out, err = self.run_cli("start", self.csv, "--dir", self.dir, "--commission-bps", "0",
                                      "--spread-bps", "2", "--slippage-bps", "1", "--fixed-fee", "0",
                                      "--periods-per-year", str(self.PPY))
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("show", "--dir", self.dir, "--week", "1")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("t,o,h,l,c,v"))
        code, out, err = self.run_cli("predict", "--dir", self.dir, "--week", "2", "--strategy", "trend",
                                      "--param", "window=3", "--expect-return", "0.004", "--expect-market", "up",
                                      "--note", "momentum")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("reveal", "--dir", self.dir, "--week", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("strategy_return", out)
        code, out, err = self.run_cli("status", "--dir", self.dir)
        self.assertEqual(code, 0, err)

    def test_start_without_costs_is_refused_by_the_command_line(self):
        code, out, err = self.run_cli("start", self.csv, "--dir", self.dir, "--periods-per-year", "5000")
        self.assertNotEqual(code, 0)


class OvernightRules(Env):
    def test_the_option_is_in_the_plan_and_old_plans_have_no_key(self):
        self.start(overnight=True)
        plan, _, _, _ = replay._open(self.dir)
        self.assertTrue(replay.overnight(plan))
        other = os.path.join(self.root, "day")
        self.start(folder=other)
        with open(os.path.join(other, replay.PLAN)) as fh:
            self.assertNotIn("overnight", json.load(fh))

    def test_hold_keeps_the_stock_through_nights_and_weekends(self):
        self.start(overnight=True)
        r2 = self.play_week(2, "hold", {})
        r3 = self.play_week(3, "hold", {})
        plan, blind, weeks, _ = replay._open(self.dir)
        a, b = weeks[2]
        self.assertEqual(r3["costs_paid"], 0.0)                       # kept from week 2, nothing traded
        self.assertAlmostEqual(r3["strategy_return"], blind[b - 1]["c"] / blind[a - 1]["c"] - 1.0, places=9)
        self.assertGreater(r2["costs_paid"], 0.0)

    def test_a_week_starts_with_what_the_last_weeks_rule_held(self):
        self.start(overnight=True)
        self.play_week(2, "hold", {})
        r3 = self.play_week(3, "none", {})
        self.assertGreater(r3["costs_paid"], 0.0)                     # it sells what week 2 left open
        self.assertEqual(r3["trades"], 1)

    def test_day_only_replays_are_unchanged_by_hold(self):
        self.start()
        r = self.play_week(2, "hold", {})
        self.assertEqual(r["trades"], WEEKS_DAYS)                     # bought and sold every day

    def test_report_money_and_rescoring_follow_the_overnight_path(self):
        report = load("trader-replay", "report")
        self.start(overnight=True)
        for w, (name, params) in zip((2, 3, 4, 5), (("hold", {}), ("trend", {"window": 3}), ("none", {}),
                                                    ("hold", {}))):
            self.play_week(w, name, params)
        d = report.build(self.dir)
        plan, blind, weeks, _ = replay._open(self.dir)
        self.assertAlmostEqual(report._rescore(plan, blind, weeks, d["weeks"], plan["costs"]), d["rules_total"],
                               places=12)
        m = report.money(self.dir, d, 500.0)
        self.assertAlmostEqual(m["rules_end"], 500.0 * (1.0 + d["rules_total"]), places=9)

    def test_the_stock_move_counts_from_the_previous_close_like_holding(self):
        self.start(overnight=True)
        self.play_week(2, "hold", {})
        r3 = self.play_week(3, "hold", {})
        self.assertAlmostEqual(r3["strategy_return"], r3["stock_move"], places=9)
        self.assertEqual(r3["market_actual"], "up" if r3["stock_move"] > 0 else "down")
        s = replay.status(self.dir)
        self.assertIn("stock_total_return", s)

    def test_hold_takes_no_parameters(self):
        self.start()
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 2, "hold", {"x": 1}, 0.0, "up")


if __name__ == "__main__":
    unittest.main()
