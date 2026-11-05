import io
import json
import os
import random
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

strategies = load("trader-strategies", "strategies")
run = load("trader-strategies", "run")
engine = load("trader-backtest", "engine")

FREE = engine.make_costs(commission_bps=0, spread_bps=0, slippage_bps=0, fixed_fee=0)


def mkbar(day, i, o, h, l, c, v=1000.0):
    minutes = 8 * 60 + 5 * i
    return {"t": "%sT%02d:%02d:00Z" % (day, minutes // 60, minutes % 60), "o": o, "h": h, "l": l, "c": c, "v": v}


def day_from_closes(day, closes, spread=1.0, start_price=None, v=1000.0):
    out, prev = [], closes[0] if start_price is None else start_price
    for i, c in enumerate(closes):
        out.append(mkbar(day, i, prev, max(prev, c) + spread, min(prev, c) - spread, c, v))
        prev = c
    return out


def signals(strategy, bars, pos_rule=None):
    """The strategy's answer after each bar; pos follows the answer like a held position would."""
    out, pos = [], 0
    for i in range(len(bars)):
        s = strategy(bars[: i + 1], pos)
        out.append(s)
        pos = s
    return out


class Trend(unittest.TestCase):
    def test_flat_until_enough_bars(self):
        bars = day_from_closes("2026-01-05", [100 + i for i in range(10)])
        s = signals(strategies.make("trend", window=5), bars)
        self.assertEqual(s[:4], [0, 0, 0, 0])

    def test_long_above_average_flat_below(self):
        up = day_from_closes("2026-01-05", [100 + i for i in range(10)])
        self.assertEqual(signals(strategies.make("trend", window=5), up)[-1], 1)
        down = day_from_closes("2026-01-05", [100 - i for i in range(10)])
        self.assertEqual(signals(strategies.make("trend", window=5), down)[-1], 0)

    def test_window_must_be_a_positive_whole_number(self):
        for bad in (0, -3, 2.5, "5", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                strategies.make("trend", window=bad)


class OpeningRange(unittest.TestCase):
    def day(self, closes):
        return day_from_closes("2026-01-05", closes, spread=1.0)

    def test_no_trade_while_the_range_forms(self):
        s = signals(strategies.make("orb", range_bars=3), self.day([100, 100, 100, 110, 110]))
        self.assertEqual(s[:3], [0, 0, 0])

    def test_breakout_above_the_range_goes_long(self):
        s = signals(strategies.make("orb", range_bars=3), self.day([100, 100, 100, 103, 104]))
        self.assertEqual(s[3], 1)
        self.assertEqual(s[4], 1)

    def test_falling_back_below_the_range_exits(self):
        s = signals(strategies.make("orb", range_bars=3), self.day([100, 100, 100, 103, 97]))
        self.assertEqual(s[-1], 0)

    def test_inside_the_range_keeps_the_position(self):
        # range high is 101 (close 100 + spread 1), low 99
        s = signals(strategies.make("orb", range_bars=3), self.day([100, 100, 100, 103, 100]))
        self.assertEqual(s[-1], 1)
        s = signals(strategies.make("orb", range_bars=3), self.day([100, 100, 100, 100.5, 100]))
        self.assertEqual(s[-1], 0)

    def test_the_range_resets_each_day(self):
        d1 = self.day([100, 100, 100, 103])
        d2 = day_from_closes("2026-01-06", [200, 200, 200, 201], spread=1.0)
        s = signals(strategies.make("orb", range_bars=3), d1 + d2)
        # first bars of day 2 are range-forming again, not a breakout of day 1's range
        self.assertEqual(s[4:7], [0, 0, 0])

    def test_range_bars_validated(self):
        for bad in (0, -1, 1.5, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                strategies.make("orb", range_bars=bad)


class VwapReversion(unittest.TestCase):
    def test_buys_a_dip_below_vwap_and_sells_the_return(self):
        closes = [100] * 8 + [97, 97, 100.5]
        bars = day_from_closes("2026-01-05", closes, spread=0.2)
        s = signals(strategies.make("vwap", band_bps=100, warmup=5), bars)
        self.assertEqual(s[8], 1)
        self.assertEqual(s[9], 1)
        self.assertEqual(s[10], 0)

    def test_no_trade_before_warmup(self):
        bars = day_from_closes("2026-01-05", [100, 90, 100, 100, 100, 100], spread=0.2)
        s = signals(strategies.make("vwap", band_bps=100, warmup=5), bars)
        self.assertEqual(s[:4], [0, 0, 0, 0])

    def test_zero_volume_is_refused(self):
        bars = day_from_closes("2026-01-05", [100] * 8, spread=0.2, v=0.0)
        with self.assertRaises(ValueError):
            signals(strategies.make("vwap", band_bps=100, warmup=3), bars)

    def test_vwap_resets_each_day(self):
        d1 = day_from_closes("2026-01-05", [100] * 8, spread=0.2)
        d2 = day_from_closes("2026-01-06", [150] * 8, spread=0.2)
        s = signals(strategies.make("vwap", band_bps=100, warmup=3), d1 + d2)
        self.assertEqual(s[8:11], [0, 0, 0])

    def test_params_validated(self):
        for kw in ({"band_bps": -1, "warmup": 5}, {"band_bps": 100, "warmup": 0}, {"band_bps": "x", "warmup": 5}):
            with self.assertRaises(ValueError, msg=repr(kw)):
                strategies.make("vwap", **kw)


class Registry(unittest.TestCase):
    def test_every_strategy_states_its_reason_and_markets(self):
        self.assertEqual(sorted(strategies.REGISTRY), ["orb", "trend", "vwap"])
        for name, info in strategies.REGISTRY.items():
            self.assertTrue(info["reason"].strip(), name)
            self.assertTrue(info["asset_classes"], name)
            self.assertTrue(info["params"], name)

    def test_unknown_strategy_and_unknown_parameter_are_refused(self):
        with self.assertRaises(ValueError):
            strategies.make("magic")
        with self.assertRaises(ValueError):
            strategies.make("trend", windw=5)

    def test_missing_parameter_is_refused_not_defaulted(self):
        with self.assertRaises(ValueError):
            strategies.make("trend")


def random_days(n_days=6, per_day=30, seed=3):
    rnd = random.Random(seed)
    bars, price = [], 100.0
    for d in range(n_days):
        day = "2026-01-%02d" % (5 + d)
        for i in range(per_day):
            o = price
            c = o * (1 + rnd.gauss(0, 0.002))
            h = max(o, c) * 1.001
            l = min(o, c) * 0.999
            bars.append(mkbar(day, i, o, h, l, c, v=500 + rnd.random() * 500))
            price = c
    return bars


PARAMS = {"trend": {"window": 10}, "orb": {"range_bars": 6}, "vwap": {"band_bps": 10, "warmup": 6}}


class WithTheBacktester(unittest.TestCase):
    def test_every_strategy_gives_a_report(self):
        bars = random_days()
        for name, params in PARAMS.items():
            rep = engine.report(bars, strategies.make(name, **params), FREE, 252 * 30, flatten_eod=True)
            self.assertIn("strategy", rep, name)
            self.assertTrue(all(e > 0 for e in rep["equity"]), name)

    def test_day_strategies_hold_nothing_overnight_when_flattened(self):
        bars = random_days()
        for name, params in PARAMS.items():
            res = engine.backtest(bars, strategies.make(name, **params), FREE, flatten_eod=True)
            for t in res["trades"]:
                self.assertEqual(t["entry_t"][:10], t["exit_t"][:10], name)


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "bars.csv")
        with open(self.path, "w") as fh:
            fh.write("t,o,h,l,c,v\n")
            for b in random_days():
                fh.write("%s,%r,%r,%r,%r,%r\n" % (b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    COSTS = ["--commission-bps", "0", "--spread-bps", "2", "--slippage-bps", "1"]

    def test_run_prints_strategy_benchmark_and_warnings(self):
        code, out, err = self.run_cli("run", "--strategy", "orb", "--param", "range_bars=6", "--bars", self.path,
                                      "--periods-per-year", "7560", "--flatten-eod", *self.COSTS)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        for key in ("strategy", "benchmark", "costs_paid", "warnings", "reason", "params", "delay"):
            self.assertIn(key, data)
        self.assertEqual(data["params"], {"range_bars": 6})
        self.assertTrue(any("Too few trades" in w for w in data["warnings"]))

    def test_run_refuses_without_costs(self):
        code, out, err = self.run_cli("run", "--strategy", "trend", "--param", "window=10", "--bars", self.path,
                                      "--periods-per-year", "7560")
        self.assertNotEqual(code, 0)
        self.assertIn("cost", err.lower())

    def test_run_refuses_unknown_strategy_and_bad_param(self):
        code, _, err = self.run_cli("run", "--strategy", "magic", "--bars", self.path,
                                    "--periods-per-year", "7560", *self.COSTS)
        self.assertNotEqual(code, 0)
        code, _, err = self.run_cli("run", "--strategy", "trend", "--param", "window=abc", "--bars", self.path,
                                    "--periods-per-year", "7560", *self.COSTS)
        self.assertNotEqual(code, 0)
        self.assertIn("Refused", err)

    def test_list_shows_every_strategy_with_its_reason(self):
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0)
        for name in ("trend", "orb", "vwap"):
            self.assertIn(name, out)


if __name__ == "__main__":
    unittest.main()
