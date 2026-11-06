import json
import os
import re
import tempfile
import unittest

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
        replay.predict(self.dir, k, strategy, params if params is not None else {"window": 3}, note="t")
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
            replay.predict(self.dir, 3, "trend", {"window": 3}, note="")
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 1, "trend", {"window": 3}, note="")

    def test_a_week_cannot_be_predicted_twice(self):
        replay.predict(self.dir, 2, "trend", {"window": 3}, note="")
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 2, "orb", {"range_bars": 2}, note="")

    def test_after_reveal_the_week_can_be_read_again_and_the_next_is_open_for_prediction(self):
        self.play_week(2)
        self.assertEqual(len(replay.show(self.dir, 2)), len(replay.show(self.dir, 1)))
        replay.predict(self.dir, 3, "trend", {"window": 3}, note="")

    def test_unknown_strategy_or_missing_params_are_refused_and_not_logged(self):
        before = len(replay.T.ledger_read(self.dir))
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 2, "crystal-ball", {}, note="")
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 2, "trend", {}, note="")
        self.assertEqual(len(replay.T.ledger_read(self.dir)), before)

    def test_sealed_weeks_cannot_be_read_or_predicted(self):
        for k in range(2, 17):
            self.play_week(k)
        with self.assertRaises(ValueError):
            replay.predict(self.dir, 17, "trend", {"window": 3}, note="")
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
                                      "--param", "window=3", "--note", "momentum")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("reveal", "--dir", self.dir, "--week", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("strategy_return", out)
        code, out, err = self.run_cli("status", "--dir", self.dir)
        self.assertEqual(code, 0, err)

    def test_start_without_costs_is_refused_by_the_command_line(self):
        code, out, err = self.run_cli("start", self.csv, "--dir", self.dir, "--periods-per-year", "5000")
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
