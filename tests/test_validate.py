import io
import json
import math
import os
import random
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from statistics import NormalDist

from _load import load

validate = load("trader-validate", "validate")
engine = load("trader-backtest", "engine")
strategies = load("trader-strategies", "strategies")

FREE = engine.make_costs(commission_bps=0, spread_bps=0, slippage_bps=0, fixed_fee=0)


def mkbar(day, i, o, h, l, c, v=1000.0):
    minutes = 8 * 60 + 5 * i
    return {"t": "%sT%02d:%02d:00Z" % (day, minutes // 60, minutes % 60), "o": o, "h": h, "l": l, "c": c, "v": v}


def random_days(n_days=10, per_day=30, seed=3, drift=0.0):
    rnd = random.Random(seed)
    bars, price = [], 100.0
    for d in range(n_days):
        day = "2026-01-%02d" % (5 + d)
        for i in range(per_day):
            o = price
            c = o * (1 + drift + rnd.gauss(0, 0.002))
            bars.append(mkbar(day, i, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 500 + rnd.random() * 500))
            price = c
    return bars


def rising_bars(n=40, step=0.005):
    bars = []
    for i in range(n):
        o = 100.0 * (1 + step) ** i
        c = 100.0 * (1 + step) ** (i + 1)
        bars.append(mkbar("2026-01-05" if i < 96 else "2026-01-06", i % 96, o, c * 1.0001, o * 0.9999, c))
    return bars


def alternating(hist, pos):
    return 1 if len(hist) % 2 == 1 else 0


class Costs(unittest.TestCase):
    def test_scale_costs_scales_every_field(self):
        c = engine.make_costs(commission_bps=1, spread_bps=2, slippage_bps=3, fixed_fee=0.5)
        s = validate.scale_costs(c, 2)
        self.assertEqual(s, {"commission_bps": 2, "spread_bps": 4, "slippage_bps": 6, "fixed_fee": 1.0})

    def test_scale_must_be_positive(self):
        with self.assertRaises(ValueError):
            validate.scale_costs(FREE, 0)


class Days(unittest.TestCase):
    def test_split_days(self):
        bars = random_days(n_days=3, per_day=4)
        d = validate.split_days(bars)
        self.assertEqual([(a, b) for _, a, b in d], [(0, 4), (4, 8), (8, 12)])
        self.assertEqual(d[0][0], "2026-01-05")


class Sharpe(unittest.TestCase):
    def test_sharpe_per_period(self):
        r = [0.01, -0.01, 0.02, 0.0]
        m = sum(r) / 4
        sd = (sum((x - m) ** 2 for x in r) / 3) ** 0.5
        self.assertAlmostEqual(validate.sharpe_pp(r), m / sd)
        self.assertIsNone(validate.sharpe_pp([0.0, 0.0, 0.0]))
        self.assertIsNone(validate.sharpe_pp([0.01]))

    def test_moments_of_symmetric_data(self):
        skew, kurt = validate.moments([-1.0, 1.0, -1.0, 1.0])
        self.assertAlmostEqual(skew, 0.0)
        self.assertAlmostEqual(kurt, 1.0)  # raw kurtosis of a two-point distribution


class Deflated(unittest.TestCase):
    def test_one_trial_is_the_probabilistic_sharpe_ratio(self):
        sr, n, skew, kurt = 0.1, 250, -0.3, 4.0
        se = math.sqrt((1 - skew * sr + (kurt - 1) / 4.0 * sr ** 2) / (n - 1))
        expected = NormalDist().cdf(sr / se)
        self.assertAlmostEqual(validate.deflated_sharpe(sr, n, skew, kurt, 1, 0.0), expected)

    def test_more_trials_lower_the_probability(self):
        a = validate.deflated_sharpe(0.1, 250, 0.0, 3.0, 1, 0.0004)
        b = validate.deflated_sharpe(0.1, 250, 0.0, 3.0, 300, 0.0004)
        self.assertLess(b, a)

    def test_expected_max_sharpe(self):
        self.assertEqual(validate.expected_max_sharpe(1, 0.01), 0.0)
        self.assertLess(validate.expected_max_sharpe(10, 0.01), validate.expected_max_sharpe(1000, 0.01))
        self.assertEqual(validate.expected_max_sharpe(50, 0.0), 0.0)

    def test_a_negative_sharpe_is_never_significant(self):
        self.assertLess(validate.deflated_sharpe(-0.05, 250, 0.0, 3.0, 1, 0.0), 0.5)

    def test_too_few_observations_are_refused(self):
        with self.assertRaises(ValueError):
            validate.deflated_sharpe(0.1, 2, 0.0, 3.0, 1, 0.0)


class BreakEven(unittest.TestCase):
    def test_finds_the_cost_that_removes_the_profit(self):
        bars = rising_bars()
        be = validate.break_even_extra_bps(bars, lambda: alternating, FREE, {})
        self.assertGreater(be, 22.0)
        self.assertLess(be, 26.0)
        for extra, positive in ((be * 0.98, True), (be * 1.02, False)):
            c = engine.make_costs(commission_bps=0, spread_bps=0, slippage_bps=extra, fixed_fee=0)
            ret = engine.report(bars, alternating, c, 252)["strategy"]["total_return"]
            self.assertEqual(ret > 0, positive, (extra, ret))

    def test_zero_when_already_unprofitable(self):
        bars = rising_bars()
        costly = engine.make_costs(commission_bps=0, spread_bps=0, slippage_bps=200, fixed_fee=0)
        self.assertEqual(validate.break_even_extra_bps(bars, lambda: alternating, costly, {}), 0.0)

    def test_none_when_still_profitable_at_the_cap(self):
        bars = rising_bars(step=0.1)
        self.assertIsNone(validate.break_even_extra_bps(bars, lambda: alternating, FREE, {}, cap_bps=50))


class DelayScan(unittest.TestCase):
    def test_reports_delays_one_to_three(self):
        bars = random_days()
        out = validate.delay_scan(bars, lambda: strategies.make("trend", window=5), FREE, 7560, {})
        self.assertEqual([d["delay"] for d in out], [1, 2, 3])
        for d in out:
            self.assertIn("total_return", d)
            self.assertIn("sharpe", d)


class WalkForward(unittest.TestCase):
    GRID = [{"window": 5}, {"window": 10}]

    def test_folds_and_compounding(self):
        bars = random_days(n_days=10, per_day=30)
        wf = validate.walk_forward(bars, "trend", self.GRID, FREE, 7560, train_days=4, test_days=2,
                                   engine_kw={"flatten_eod": True})
        self.assertEqual(len(wf["folds"]), 3)
        growth = 1.0
        for f in wf["folds"]:
            self.assertIn(f["params"], self.GRID)
            growth *= 1 + f["test_return"]
        self.assertAlmostEqual(wf["oos_total_return"], growth - 1.0)
        self.assertIn("oos_sharpe", wf)
        self.assertIn("oos_trades", wf)

    def test_fold_test_windows_do_not_overlap_the_training_window(self):
        bars = random_days(n_days=10, per_day=30)
        wf = validate.walk_forward(bars, "trend", self.GRID, FREE, 7560, train_days=4, test_days=2, engine_kw={})
        days = [d for d, _, _ in validate.split_days(bars)]
        for k, f in enumerate(wf["folds"]):
            self.assertEqual(f["train_days"], days[2 * k: 2 * k + 4])
            self.assertEqual(f["test_days"], days[2 * k + 4: 2 * k + 6])
            self.assertFalse(set(f["train_days"]) & set(f["test_days"]))

    def test_not_enough_days_is_refused(self):
        with self.assertRaises(ValueError):
            validate.walk_forward(random_days(n_days=3), "trend", self.GRID, FREE, 7560,
                                  train_days=4, test_days=2, engine_kw={})


GOOD = {"n_trades": 150, "net_return": 0.10, "stressed_return": 0.05, "delay2_return": 0.04,
        "oos_return": 0.03, "oos_sharpe": 0.8, "dsr": 0.97, "alpha_return": 0.04, "recent_return": 0.01,
        "max_bar_range": 0.02}


class Decide(unittest.TestCase):
    def test_all_good_passes(self):
        self.assertEqual(validate.decide(GOOD)["verdict"], "PASS")

    def test_each_hard_failure_fails(self):
        for key, bad in (("net_return", -0.01), ("stressed_return", -0.01), ("delay2_return", -0.01),
                         ("oos_return", -0.01), ("oos_sharpe", -0.1), ("alpha_return", -0.01)):
            d = dict(GOOD, **{key: bad})
            r = validate.decide(d)
            self.assertEqual(r["verdict"], "FAIL", key)
            self.assertTrue(any(not c["passed"] for c in r["checks"]), key)

    def test_too_few_trades_or_weak_dsr_is_inconclusive_not_pass(self):
        self.assertEqual(validate.decide(dict(GOOD, n_trades=40))["verdict"], "INCONCLUSIVE")
        self.assertEqual(validate.decide(dict(GOOD, dsr=0.6))["verdict"], "INCONCLUSIVE")

    def test_a_dead_recent_stretch_or_a_wild_bar_is_inconclusive(self):
        self.assertEqual(validate.decide(dict(GOOD, recent_return=-0.01))["verdict"], "INCONCLUSIVE")
        self.assertEqual(validate.decide(dict(GOOD, max_bar_range=0.5))["verdict"], "INCONCLUSIVE")

    def test_a_failure_beats_inconclusive(self):
        self.assertEqual(validate.decide(dict(GOOD, n_trades=40, stressed_return=-0.2))["verdict"], "FAIL")

    def test_missing_numbers_are_inconclusive_never_pass(self):
        for key in GOOD:
            d = dict(GOOD)
            d[key] = None
            self.assertEqual(validate.decide(d)["verdict"], "INCONCLUSIVE", key)

    def test_thresholds_are_stated(self):
        r = validate.decide(GOOD)
        self.assertTrue(all(c["rule"] for c in r["checks"]))


class Alpha(unittest.TestCase):
    def test_half_the_market_is_all_beta(self):
        bench = [0.01, -0.02, 0.015, 0.0, 0.01, -0.005]
        strat = [0.5 * x for x in bench]
        alpha, beta = validate.alpha_returns(strat, bench)
        self.assertAlmostEqual(beta, 0.5)
        self.assertTrue(all(abs(a) < 1e-12 for a in alpha))

    def test_market_plus_a_constant_has_that_constant_as_alpha(self):
        bench = [0.01, -0.02, 0.015, 0.0, 0.01, -0.005]
        strat = [x + 0.003 for x in bench]
        alpha, beta = validate.alpha_returns(strat, bench)
        self.assertAlmostEqual(beta, 1.0)
        self.assertTrue(all(abs(a - 0.003) < 1e-12 for a in alpha))

    def test_flat_market_means_beta_zero(self):
        alpha, beta = validate.alpha_returns([0.01, 0.02], [0.0, 0.0])
        self.assertEqual(beta, 0.0)
        self.assertEqual(alpha, [0.01, 0.02])

    def test_lengths_must_match(self):
        with self.assertRaises(ValueError):
            validate.alpha_returns([0.1], [0.1, 0.2])


def drifting(seed, drift, n_days=60, per_day=34, glitch=False):
    rnd = random.Random(seed)
    bars, price = [], 100.0
    for d in range(n_days):
        day = "2026-%02d-%02d" % (1 + d // 28, 1 + d % 28)
        for i in range(per_day):
            o = price
            c = o * (1 + drift + rnd.gauss(0, 0.002))
            bars.append(mkbar(day, i, o, max(o, c) * 1.0005, min(o, c) * 0.9995, c, 500 + rnd.random() * 500))
            price = c
    if glitch:
        b = bars[len(bars) // 2]
        b["c"] *= 3
        b["h"] = b["c"]
    return bars


class WhatTheStressTestFound(unittest.TestCase):
    def test_a_rising_market_without_skill_does_not_pass(self):
        for seed in (0, 1, 2):
            rep = validate.validate(drifting(seed, 0.0003), "trend", {"window": [3, 6, 12]},
                                    engine.make_costs(commission_bps=0, spread_bps=2, slippage_bps=1, fixed_fee=0),
                                    252 * 34, flatten_eod=True, train_days=30, test_days=10)
            self.assertNotEqual(rep["verdict"], "PASS", seed)

    def test_a_wild_bar_is_reported(self):
        rep = validate.validate(drifting(0, 0.0, glitch=True), "trend", {"window": [5]},
                                engine.make_costs(commission_bps=0, spread_bps=2, slippage_bps=1, fixed_fee=0),
                                252 * 34, flatten_eod=True, train_days=30, test_days=10)
        self.assertNotEqual(rep["verdict"], "PASS")
        self.assertTrue(rep["suspicious_bars"])


class Whole(unittest.TestCase):
    def test_noise_does_not_pass(self):
        bars = random_days(n_days=12, per_day=30, seed=11)
        rep = validate.validate(bars, "trend", {"window": [5, 10, 20]}, FREE, 7560, flatten_eod=True,
                                train_days=5, test_days=2, earlier_trials=0)
        self.assertIn(rep["verdict"], ("FAIL", "INCONCLUSIVE"))
        for key in ("best_params", "n_trials", "checks", "delay_scan", "break_even_extra_bps",
                    "walk_forward", "deflated_sharpe", "benchmark", "reasons"):
            self.assertIn(key, rep)
        self.assertEqual(rep["n_trials"], 3)

    def test_earlier_trials_are_counted(self):
        bars = random_days(n_days=12, per_day=30, seed=11)
        rep = validate.validate(bars, "trend", {"window": [5]}, FREE, 7560, flatten_eod=True,
                                train_days=5, test_days=2, earlier_trials=40)
        self.assertEqual(rep["n_trials"], 41)

    def test_grid_is_the_cartesian_product_and_is_capped(self):
        g = validate.expand_grid({"band_bps": [1, 2], "warmup": [5, 6, 7]})
        self.assertEqual(len(g), 6)
        with self.assertRaises(ValueError):
            validate.expand_grid({"a": list(range(30)), "b": list(range(30))})

    def test_a_short_series_still_reports_without_crashing(self):
        bars = random_days(n_days=3, per_day=30)
        rep = validate.validate(bars, "trend", {"window": [5]}, FREE, 7560, flatten_eod=True,
                                train_days=5, test_days=2, earlier_trials=0)
        self.assertIn(rep["verdict"], ("INCONCLUSIVE", "FAIL"))
        self.assertTrue(any("walk-forward" in r.lower() for r in rep["reasons"]))


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "bars.csv")
        with open(self.path, "w") as fh:
            fh.write("t,o,h,l,c,v\n")
            for b in random_days(n_days=12, seed=5):
                fh.write("%s,%r,%r,%r,%r,%r\n" % (b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]))

    COSTS = ["--commission-bps", "0", "--spread-bps", "2", "--slippage-bps", "1"]

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = validate.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_run_prints_a_verdict(self):
        code, out, err = self.run_cli("run", "--strategy", "trend", "--grid", "window=5,10", "--bars", self.path,
                                      "--periods-per-year", "7560", "--flatten-eod", "--train-days", "5",
                                      "--test-days", "2", *self.COSTS)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn(data["verdict"], ("PASS", "FAIL", "INCONCLUSIVE"))
        self.assertEqual(data["n_trials"], 2)
        self.assertIn("not a prediction", data["note"].lower())

    def test_refuses_without_costs(self):
        code, out, err = self.run_cli("run", "--strategy", "trend", "--grid", "window=5", "--bars", self.path,
                                      "--periods-per-year", "7560")
        self.assertNotEqual(code, 0)
        self.assertIn("cost", err.lower())

    def test_refuses_bad_grid_and_unknown_strategy(self):
        for args in (["--strategy", "magic", "--grid", "window=5"],
                     ["--strategy", "trend", "--grid", "window=abc"],
                     ["--strategy", "trend", "--grid", "window"],
                     ["--strategy", "trend", "--grid", "windw=5"]):
            code, out, err = self.run_cli("run", *args, "--bars", self.path, "--periods-per-year", "7560",
                                          *self.COSTS)
            self.assertNotEqual(code, 0, args)
            self.assertIn("Refused", err)


if __name__ == "__main__":
    unittest.main()
