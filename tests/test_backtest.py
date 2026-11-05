import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

metrics = load("trader-backtest", "metrics")
engine = load("trader-backtest", "engine")

NO_COST = dict(commission_bps=0.0, spread_bps=0.0, slippage_bps=0.0, fixed_fee=0.0)


def bar(t, o, h=None, l=None, c=None, v=1000):
    c = o if c is None else c
    h = max(o, c) if h is None else h
    l = min(o, c) if l is None else l
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def day_bars(day, closes, open_first=None):
    out = []
    prev = open_first if open_first is not None else closes[0]
    for i, c in enumerate(closes):
        out.append(bar("%sT%02d:00:00" % (day, 9 + i), prev, c=c))
        prev = c
    return out


class Metrics(unittest.TestCase):
    def test_total_return_and_drawdown(self):
        eq = [100, 110, 99, 120]
        self.assertAlmostEqual(metrics.total_return(eq), 0.20)
        self.assertAlmostEqual(metrics.max_drawdown(eq), 0.10)

    def test_drawdown_of_rising_curve_is_zero(self):
        self.assertEqual(metrics.max_drawdown([1, 2, 3]), 0.0)

    def test_sharpe_needs_periods_per_year(self):
        with self.assertRaises(ValueError):
            metrics.sharpe([100, 101, 102, 101], None)

    def test_sharpe_none_when_flat_or_too_short(self):
        self.assertIsNone(metrics.sharpe([100, 100, 100, 100], 252))
        self.assertIsNone(metrics.sharpe([100, 101], 252))

    def test_sharpe_value(self):
        eq = [100.0, 101.0, 100.0, 102.0, 101.0]
        r = [eq[i + 1] / eq[i] - 1 for i in range(4)]
        mean = sum(r) / 4
        sd = (sum((x - mean) ** 2 for x in r) / 3) ** 0.5
        self.assertAlmostEqual(metrics.sharpe(eq, 252), mean / sd * 252 ** 0.5)

    def test_trade_stats(self):
        pnls = [10.0, -5.0, 20.0, -5.0]
        s = metrics.trade_stats(pnls)
        self.assertEqual(s["n_trades"], 4)
        self.assertAlmostEqual(s["win_rate"], 0.5)
        self.assertAlmostEqual(s["avg_win"], 15.0)
        self.assertAlmostEqual(s["avg_loss"], -5.0)
        self.assertAlmostEqual(s["expectancy"], 5.0)
        self.assertAlmostEqual(s["profit_factor"], 3.0)

    def test_trade_stats_no_losses_has_no_profit_factor(self):
        s = metrics.trade_stats([1.0, 2.0])
        self.assertIsNone(s["profit_factor"])

    def test_trade_stats_empty(self):
        s = metrics.trade_stats([])
        self.assertEqual(s["n_trades"], 0)
        self.assertIsNone(s["win_rate"])

    def test_too_few_trades_warning(self):
        s = metrics.summarize([100, 101, 102], [1.0, 1.0], 252)
        self.assertTrue(any("too few" in w.lower() for w in s["warnings"]))

    def test_annualised_return(self):
        eq = [100.0] + [100.0] * 251 + [110.0]
        self.assertAlmostEqual(metrics.annualised_return(eq, 252), 0.10, places=6)


class BarChecks(unittest.TestCase):
    def test_good_bars_pass(self):
        engine.check_bars(day_bars("2026-01-05", [10, 11, 12]))

    def test_rejects_empty_and_short(self):
        with self.assertRaises(ValueError):
            engine.check_bars([])
        with self.assertRaises(ValueError):
            engine.check_bars(day_bars("2026-01-05", [10]))

    def test_rejects_out_of_order(self):
        b = day_bars("2026-01-05", [10, 11, 12])
        b[1], b[2] = b[2], b[1]
        with self.assertRaises(ValueError):
            engine.check_bars(b)

    def test_rejects_duplicate_time(self):
        b = day_bars("2026-01-05", [10, 11, 12])
        b[2]["t"] = b[1]["t"]
        with self.assertRaises(ValueError):
            engine.check_bars(b)

    def test_rejects_nonpositive_nan_and_inconsistent_prices(self):
        for field, value in (("o", 0), ("c", -1), ("c", float("nan")), ("h", 1.0), ("l", 99.0)):
            b = day_bars("2026-01-05", [10, 11, 12])
            b[1][field] = value
            with self.assertRaises(ValueError, msg=(field, value)):
                engine.check_bars(b)

    def test_rejects_missing_field(self):
        b = day_bars("2026-01-05", [10, 11, 12])
        del b[1]["o"]
        with self.assertRaises(ValueError):
            engine.check_bars(b)


class Costs(unittest.TestCase):
    def test_backtest_refuses_without_a_cost_model(self):
        with self.assertRaises(ValueError):
            engine.backtest(day_bars("2026-01-05", [10, 11, 12]), lambda h, p: 1, costs=None)

    def test_rejects_negative_costs(self):
        with self.assertRaises(ValueError):
            engine.make_costs(commission_bps=-1, spread_bps=0, slippage_bps=0, fixed_fee=0)

    def test_buy_pays_up_and_sell_receives_less(self):
        c = engine.make_costs(commission_bps=0, spread_bps=10, slippage_bps=10, fixed_fee=0)
        self.assertAlmostEqual(engine.fill_price(100.0, +1, c), 100.0 * (1 + 0.0015))
        self.assertAlmostEqual(engine.fill_price(100.0, -1, c), 100.0 * (1 - 0.0015))

    def test_commission(self):
        c = engine.make_costs(commission_bps=10, spread_bps=0, slippage_bps=0, fixed_fee=1.0)
        self.assertAlmostEqual(engine.commission(1000.0, c), 1.0 + 1.0)


class Backtest(unittest.TestCase):
    def test_delay_below_one_is_refused(self):
        with self.assertRaises(ValueError):
            engine.backtest(day_bars("2026-01-05", [10, 11, 12]), lambda h, p: 1,
                            costs=engine.make_costs(**NO_COST), delay=0)

    def test_strategy_sees_only_bars_up_to_now(self):
        bars = day_bars("2026-01-05", [10, 11, 12, 13, 14])
        seen = []

        def strat(hist, pos):
            seen.append(len(hist))
            return 0

        engine.backtest(bars, strat, costs=engine.make_costs(**NO_COST))
        self.assertEqual(seen, [1, 2, 3, 4, 5])

    def test_strategy_cannot_change_the_data(self):
        bars = day_bars("2026-01-05", [10, 11, 12])
        before = json.dumps(bars)

        def evil(hist, pos):
            try:
                hist[-1]["c"] = 1000
            except TypeError:
                pass
            return 0

        engine.backtest(bars, evil, costs=engine.make_costs(**NO_COST))
        self.assertEqual(json.dumps(bars), before)

    def test_signal_fills_at_next_open_not_current_close(self):
        # signal at bar 0 close; fills at bar 1 open (11.0), exit signal at bar 2 fills bar 3 open
        bars = [bar("2026-01-05T09:00:00", 10, c=10), bar("2026-01-05T10:00:00", 11, c=12),
                bar("2026-01-05T11:00:00", 12, c=13), bar("2026-01-05T12:00:00", 14, c=14)]
        sig = {0: 1, 1: 1, 2: 0, 3: 0}
        res = engine.backtest(bars, lambda h, p: sig[len(h) - 1], costs=engine.make_costs(**NO_COST),
                              capital=1000.0)
        self.assertEqual(len(res["trades"]), 1)
        t = res["trades"][0]
        self.assertAlmostEqual(t["entry_price"], 11.0)
        self.assertAlmostEqual(t["exit_price"], 14.0)
        self.assertAlmostEqual(t["pnl"], 1000.0 / 11.0 * 3.0)

    def test_larger_delay_fills_later(self):
        bars = day_bars("2026-01-05", [10, 20, 30, 40, 50])
        sig = {0: 1}
        res = engine.backtest(bars, lambda h, p: sig.get(len(h) - 1, 1 if p else 0),
                              costs=engine.make_costs(**NO_COST), capital=1000.0, delay=2)
        # signal at bar 0, fill at open of bar 2
        self.assertAlmostEqual(res["fills"][0]["price"], bars[2]["o"])

    def test_costs_reduce_result(self):
        bars = day_bars("2026-01-05", [10, 11, 12, 13, 14, 15])
        sig = {0: 1, 1: 1, 2: 1, 3: 1, 4: 0, 5: 0}

        def strat(h, p):
            return sig[len(h) - 1]

        free = engine.backtest(bars, strat, costs=engine.make_costs(**NO_COST), capital=1000.0)
        paid = engine.backtest(bars, strat, capital=1000.0,
                               costs=engine.make_costs(commission_bps=10, spread_bps=10, slippage_bps=10, fixed_fee=0.5))
        self.assertLess(paid["equity"][-1], free["equity"][-1])
        self.assertGreater(paid["costs_paid"], 0.0)
        self.assertEqual(free["costs_paid"], 0.0)

    def test_equity_never_negative_and_starts_at_capital(self):
        bars = day_bars("2026-01-05", [10, 5, 2, 1, 1])
        res = engine.backtest(bars, lambda h, p: 1, costs=engine.make_costs(**NO_COST), capital=500.0)
        self.assertAlmostEqual(res["equity"][0], 500.0)
        self.assertTrue(all(e >= 0 for e in res["equity"]))

    def test_flat_strategy_keeps_capital(self):
        bars = day_bars("2026-01-05", [10, 11, 12, 13])
        res = engine.backtest(bars, lambda h, p: 0, costs=engine.make_costs(**NO_COST), capital=700.0)
        self.assertEqual(res["trades"], [])
        self.assertAlmostEqual(res["equity"][-1], 700.0)

    def test_short_signal_refused_unless_allowed(self):
        bars = day_bars("2026-01-05", [10, 11, 12, 13])
        with self.assertRaises(ValueError):
            engine.backtest(bars, lambda h, p: -1, costs=engine.make_costs(**NO_COST))

    def test_short_profits_when_price_falls(self):
        bars = day_bars("2026-01-05", [10, 9, 8, 7, 6])
        res = engine.backtest(bars, lambda h, p: -1, costs=engine.make_costs(**NO_COST), capital=1000.0,
                              allow_short=True)
        self.assertAlmostEqual(res["equity"][-1], 1400.0)

    def test_bad_signal_value_refused(self):
        bars = day_bars("2026-01-05", [10, 11, 12, 13])
        for bad in (2, None, "long", float("nan")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                engine.backtest(bars, lambda h, p, b=bad: b, costs=engine.make_costs(**NO_COST))

    def test_flatten_at_day_end(self):
        d1 = day_bars("2026-01-05", [10, 11, 12])
        d2 = day_bars("2026-01-06", [20, 21, 22], open_first=20)
        bars = d1 + d2
        res = engine.backtest(bars, lambda h, p: 1, costs=engine.make_costs(**NO_COST), capital=1000.0,
                              flatten_eod=True)
        exits = [t["exit_t"] for t in res["trades"]]
        self.assertTrue(all(x[:10] == t["entry_t"][:10] for x, t in zip(exits, res["trades"])))
        # flat at the end of each day: last bar of each day closed the position
        self.assertEqual(len(res["trades"]), 2)

    def test_open_position_is_marked_and_reported_not_hidden(self):
        bars = day_bars("2026-01-05", [10, 11, 12, 13])
        res = engine.backtest(bars, lambda h, p: 1, costs=engine.make_costs(**NO_COST), capital=1000.0)
        self.assertEqual(res["trades"], [])
        self.assertTrue(res["open_position"])
        self.assertGreater(res["equity"][-1], 1000.0)

    def test_fractional_units_use_all_capital(self):
        bars = day_bars("2026-01-05", [10, 10, 10, 10])
        res = engine.backtest(bars, lambda h, p: 1, costs=engine.make_costs(**NO_COST), capital=1000.0)
        self.assertAlmostEqual(res["fills"][0]["qty"], 100.0)

    def test_position_size_fraction(self):
        bars = day_bars("2026-01-05", [10, 10, 10, 10])
        res = engine.backtest(bars, lambda h, p: 1, costs=engine.make_costs(**NO_COST), capital=1000.0,
                              size_fraction=0.5)
        self.assertAlmostEqual(res["fills"][0]["qty"], 50.0)

    def test_size_fraction_range(self):
        bars = day_bars("2026-01-05", [10, 10, 10])
        for bad in (0, -1, 1.5):
            with self.assertRaises(ValueError):
                engine.backtest(bars, lambda h, p: 1, costs=engine.make_costs(**NO_COST), size_fraction=bad)

    def test_cash_never_goes_negative_with_costs(self):
        bars = day_bars("2026-01-05", [10, 10, 10, 10])
        res = engine.backtest(bars, lambda h, p: 1, capital=1000.0,
                              costs=engine.make_costs(commission_bps=50, spread_bps=20, slippage_bps=20, fixed_fee=1))
        self.assertGreaterEqual(res["cash_end"], -1e-9)  # float rounding only
        self.assertLessEqual(res["fills"][0]["qty"] * res["fills"][0]["price"] + res["fills"][0]["cost"],
                             1000.0 + 1e-9)


class Benchmark(unittest.TestCase):
    def test_buy_and_hold_matches_price_move_without_costs(self):
        bars = day_bars("2026-01-05", [10, 11, 12, 15])
        res = engine.buy_and_hold(bars, engine.make_costs(**NO_COST), capital=1000.0)
        # buys at bar 1 open (10.0 -> bars[1].o is 10), ends at last close 15
        self.assertAlmostEqual(res["equity"][-1], 1000.0 / bars[1]["o"] * 15.0)

    def test_buy_and_hold_pays_costs(self):
        bars = day_bars("2026-01-05", [10, 11, 12, 15])
        free = engine.buy_and_hold(bars, engine.make_costs(**NO_COST), capital=1000.0)
        paid = engine.buy_and_hold(bars, engine.make_costs(commission_bps=10, spread_bps=10,
                                                           slippage_bps=10, fixed_fee=1), capital=1000.0)
        self.assertLess(paid["equity"][-1], free["equity"][-1])


class Report(unittest.TestCase):
    def test_report_has_strategy_benchmark_and_costs(self):
        bars = day_bars("2026-01-05", [10, 11, 12, 13, 12, 14])
        sig = {0: 1, 1: 1, 2: 1, 3: 0, 4: 0, 5: 0}
        rep = engine.report(bars, lambda h, p: sig[len(h) - 1], engine.make_costs(**NO_COST),
                            periods_per_year=252, capital=1000.0)
        for key in ("strategy", "benchmark", "costs_paid", "warnings", "delay", "capital"):
            self.assertIn(key, rep)
        self.assertIn("total_return", rep["strategy"])
        self.assertIn("max_drawdown", rep["benchmark"])
        self.assertEqual(rep["delay"], 1)


class Csv(unittest.TestCase):
    def write(self, text):
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        fh.write(text)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_reads_bars(self):
        p = self.write("t,o,h,l,c,v\n2026-01-05T09:00:00,10,11,9,10.5,100\n2026-01-05T10:00:00,10.5,12,10,11,120\n")
        bars = engine.load_bars_csv(p)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[1]["c"], 11.0)

    def test_non_numeric_value_refused(self):
        p = self.write("t,o,h,l,c,v\n2026-01-05T09:00:00,1,2,0.5,abc,3\n2026-01-05T10:00:00,1,2,0.5,1,3\n")
        with self.assertRaises(ValueError):
            engine.load_bars_csv(p)

    def test_decimal_comma_refused(self):
        p = self.write('t,o,h,l,c,v\n2026-01-05T09:00:00,1,2,0.5,"1,5",3\n2026-01-05T10:00:00,1,2,0.5,1,3\n')
        with self.assertRaises(ValueError):
            engine.load_bars_csv(p)

    def test_missing_column_refused(self):
        p = self.write("t,o,l,c,v\n2026-01-05T09:00:00,10,9,10.5,1\n2026-01-05T10:00:00,1,1,1,1\n")
        with self.assertRaises(ValueError):
            engine.load_bars_csv(p)

    def test_cli_benchmark_prints_json(self):
        p = self.write("t,o,h,l,c,v\n" + "".join(
            "2026-01-%02dT09:00:00,%s,%s,%s,%s,100\n" % (d, 10 + d, 12 + d, 9 + d, 11 + d) for d in range(5, 12)))
        out = io.StringIO()
        with redirect_stdout(out):
            code = engine.main(["benchmark", "--bars", p, "--periods-per-year", "252",
                                "--commission-bps", "0", "--spread-bps", "0", "--slippage-bps", "0"])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertIn("total_return", data["benchmark"])

    def test_cli_refuses_missing_costs(self):
        p = self.write("t,o,h,l,c,v\n2026-01-05T09:00:00,10,11,9,10.5,100\n2026-01-06T09:00:00,10.5,12,10,11,120\n")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = engine.main(["benchmark", "--bars", p, "--periods-per-year", "252"])
        self.assertNotEqual(code, 0)
        self.assertIn("cost", err.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
