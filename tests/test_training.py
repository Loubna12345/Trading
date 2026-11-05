import io
import json
import math
import os
import random
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load

train = load("trader-training", "train")
engine = load("trader-backtest", "engine")

FREE = {"commission_bps": 0.0, "spread_bps": 1.0, "slippage_bps": 0.5, "fixed_fee": 0.0}


def mkbar(day, i, o, h, l, c, v=1000.0):
    minutes = 8 * 60 + 15 * i
    return {"t": "%sT%02d:%02d:00Z" % (day, minutes // 60, minutes % 60), "o": o, "h": h, "l": l, "c": c, "v": v}


def series(seed, n_days=40, per_day=20):
    rnd = random.Random(seed)
    bars, price = [], 100.0
    for d in range(n_days):
        day = "2026-%02d-%02d" % (1 + d // 28, 1 + d % 28)
        for i in range(per_day):
            o = price
            c = o * (1 + rnd.gauss(0, 0.002))
            bars.append(mkbar(day, i, o, max(o, c) * 1.0005, min(o, c) * 0.9995, c, 500 + rnd.random() * 500))
            price = c
    return bars


def write_store(folder, ticker, bars, interval=15):
    path = os.path.join(folder, "%s_%dm.csv" % (ticker, interval))
    with open(path, "w") as fh:
        fh.write("t,o,h,l,c,v\n")
        for b in bars:
            fh.write("%s,%r,%r,%r,%r,%r\n" % (b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]))
    return path


class Env(unittest.TestCase):
    TICKERS = ["T1", "T2", "T3", "T4", "T5", "T6"]

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.store = os.path.join(self.root, "history")
        self.tdir = os.path.join(self.root, "training")
        os.makedirs(self.store)
        for i, t in enumerate(self.TICKERS):
            write_store(self.store, t, series(i))

    def make_plan(self, **over):
        kw = dict(store=self.store, interval=15, tickers=self.TICKERS, periods_per_year=252 * 20,
                  costs=FREE, grids={"trend": {"window": [3, 6]}}, flatten_eod=True, holdout=0.2,
                  train_days=10, test_days=4, null_shuffles=2, max_rounds=3, seed=1)
        kw.update(over)
        return train.make_plan(self.tdir, **kw)


class Ledger(Env):
    def test_append_and_verify(self):
        for k in range(3):
            train.ledger_append(self.tdir, {"kind": "x", "k": k})
        self.assertEqual(train.ledger_verify(self.tdir), 3)
        self.assertEqual([e["k"] for e in train.ledger_read(self.tdir)], [0, 1, 2])

    def test_editing_a_line_is_detected(self):
        for k in range(3):
            train.ledger_append(self.tdir, {"kind": "x", "k": k})
        path = os.path.join(self.tdir, "ledger.jsonl")
        text = open(path).read()
        self.assertIn('"k":1', text)
        text = text.replace('"k":1', '"k":9')
        open(path, "w").write(text)
        with self.assertRaises(ValueError):
            train.ledger_verify(self.tdir)

    def test_deleting_a_middle_line_is_detected(self):
        for k in range(4):
            train.ledger_append(self.tdir, {"kind": "x", "k": k})
        path = os.path.join(self.tdir, "ledger.jsonl")
        lines = open(path).read().splitlines()
        open(path, "w").write("\n".join(lines[:1] + lines[2:]) + "\n")
        with self.assertRaises(ValueError):
            train.ledger_verify(self.tdir)

    def test_an_empty_ledger_verifies_as_zero(self):
        self.assertEqual(train.ledger_verify(self.tdir), 0)


class Maths(unittest.TestCase):
    def test_binomial_tail(self):
        self.assertAlmostEqual(train.binom_tail(10, 9, 0.5), 11 / 1024.0)
        self.assertAlmostEqual(train.binom_tail(5, 0, 0.3), 1.0)
        self.assertAlmostEqual(train.binom_tail(3, 3, 0.05), 0.05 ** 3)

    def test_shuffle_keeps_each_days_end_and_count_but_breaks_order(self):
        bars = series(7, n_days=6, per_day=20)
        sh = train.shuffled_within_days(bars, seed=1)
        self.assertEqual(len(sh), len(bars))
        engine.check_bars(sh)
        days = {}
        for b in bars:
            days.setdefault(b["t"][:10], []).append(b)
        days2 = {}
        for b in sh:
            days2.setdefault(b["t"][:10], []).append(b)
        self.assertEqual(sorted(days), sorted(days2))
        for d in days:
            self.assertEqual(len(days[d]), len(days2[d]))
            self.assertAlmostEqual(days[d][-1]["c"], days2[d][-1]["c"], places=6)
        self.assertNotEqual([b["c"] for b in bars], [b["c"] for b in sh])
        self.assertEqual([b["t"] for b in bars], [b["t"] for b in sh])

    def test_shuffle_is_deterministic_per_seed(self):
        bars = series(7, n_days=4)
        a = train.shuffled_within_days(bars, seed=3)
        b = train.shuffled_within_days(bars, seed=3)
        c = train.shuffled_within_days(bars, seed=4)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_holdout_split_by_days(self):
        bars = series(1, n_days=40, per_day=5)
        pre, hold, cutoff = train.split_holdout(bars, 0.2)
        self.assertEqual(len(pre) + len(hold), len(bars))
        self.assertEqual(len(hold), 8 * 5)
        self.assertEqual(hold[0]["t"][:10], cutoff)
        self.assertLess(pre[-1]["t"], hold[0]["t"])

    def test_holdout_needs_enough_days(self):
        with self.assertRaises(ValueError):
            train.split_holdout(series(1, n_days=10, per_day=5), 0.2)


class Plan(Env):
    def test_creates_a_locked_plan_and_logs_it(self):
        plan = self.make_plan()
        self.assertTrue(os.path.exists(os.path.join(self.tdir, "plan.json")))
        self.assertEqual(sorted(plan["roles"]), sorted(self.TICKERS))
        self.assertEqual(sum(1 for r in plan["roles"].values() if r == "discovery"), 3)
        self.assertEqual(sum(1 for r in plan["roles"].values() if r == "confirmation"), 3)
        self.assertEqual(train.ledger_read(self.tdir)[0]["kind"], "plan")
        self.assertEqual(train.load_plan(self.tdir)["roles"], plan["roles"])

    def test_roles_are_deterministic_for_a_seed(self):
        a = self.make_plan()["roles"]
        os.remove(os.path.join(self.tdir, "plan.json"))
        os.remove(os.path.join(self.tdir, "ledger.jsonl"))
        b = self.make_plan()["roles"]
        self.assertEqual(a, b)

    def test_a_second_plan_is_refused(self):
        self.make_plan()
        with self.assertRaises(ValueError):
            self.make_plan()

    def test_editing_the_plan_file_is_refused(self):
        self.make_plan()
        path = os.path.join(self.tdir, "plan.json")
        plan = json.load(open(path))
        plan["holdout"] = 0.05
        json.dump(plan, open(path, "w"))
        with self.assertRaises(ValueError):
            train.load_plan(self.tdir)

    def test_needs_three_tickers_per_role(self):
        with self.assertRaises(ValueError):
            self.make_plan(tickers=self.TICKERS[:5])

    def test_unknown_strategy_or_parameter_is_refused(self):
        with self.assertRaises(ValueError):
            self.make_plan(grids={"magic": {"x": [1]}})
        with self.assertRaises(ValueError):
            self.make_plan(grids={"trend": {"windw": [3]}})

    def test_missing_store_file_is_refused(self):
        os.remove(os.path.join(self.store, "T3_15m.csv"))
        with self.assertRaises(ValueError):
            self.make_plan()

    def test_holdout_fraction_is_limited(self):
        for bad in (0.0, 0.05, 0.6):
            with self.assertRaises(ValueError, msg=bad):
                self.make_plan(holdout=bad)

    def test_costs_are_required(self):
        with self.assertRaises(ValueError):
            self.make_plan(costs=None)


def fake_report(verdict="FAIL", params=None, trades=120):
    return {"verdict": verdict, "best_params": params or {"window": 3}, "n_trials": 2,
            "settings_tried_here": 2, "best_net_return": 0.01, "best_trades": trades,
            "deflated_sharpe": 0.5, "reasons": [], "break_even_extra_bps": 3.0}


class Discover(Env):
    def test_runs_every_discovery_ticker_and_null_and_logs_them(self):
        plan = self.make_plan()
        out = train.discover(self.tdir)
        disc = [t for t, r in plan["roles"].items() if r == "discovery"]
        entries = train.ledger_read(self.tdir)
        kinds = [e["kind"] for e in entries]
        self.assertEqual(kinds.count("discover"), len(disc))
        self.assertEqual(kinds.count("null"), len(disc) * 2)
        self.assertEqual(out["round"], 1)
        self.assertEqual(train.ledger_verify(self.tdir), len(entries))

    def test_never_looks_at_the_holdout(self):
        self.make_plan()
        seen = []

        def fake(bars, *a, **k):
            seen.append(bars[-1]["t"])
            return fake_report()

        with mock.patch.object(train.V, "validate", fake):
            train.discover(self.tdir)
        cutoff = train.split_holdout(series(0), 0.2)[2]
        self.assertTrue(seen)
        self.assertTrue(all(t[:10] < cutoff for t in seen))

    def test_a_second_discover_runs_nothing_and_uses_no_round(self):
        self.make_plan()
        with mock.patch.object(train.V, "validate", lambda *a, **k: fake_report()):
            train.discover(self.tdir)
            n = len(train.ledger_read(self.tdir))
            out = train.discover(self.tdir)
        self.assertEqual(out["ran"], 0)
        self.assertEqual(len(train.ledger_read(self.tdir)), n)
        self.assertEqual(train.current_round(self.tdir), 1)

    def test_amending_adds_only_the_new_combinations_in_a_new_round(self):
        write_store(self.store, "T7", series(7))
        plan = self.make_plan(tickers=self.TICKERS + ["T7"])
        with mock.patch.object(train.V, "validate", lambda *a, **k: fake_report()):
            train.discover(self.tdir)
            before = len(train.ledger_read(self.tdir))
            train.amend(self.tdir, add_grids={"orb": {"range_bars": [2, 4]}})
            out = train.discover(self.tdir)
        disc = [t for t, r in train.load_plan(self.tdir)["roles"].items() if r == "discovery"]
        self.assertEqual(out["round"], 2)
        self.assertEqual(out["ran"], len(disc))
        added = train.ledger_read(self.tdir)[before:]
        self.assertTrue(any(e["kind"] == "amend" for e in added))
        self.assertTrue(all(e["strategy"] == "orb" for e in added if e["kind"] in ("discover", "null")))

    def test_the_round_limit_is_enforced(self):
        self.make_plan(max_rounds=1)
        with mock.patch.object(train.V, "validate", lambda *a, **k: fake_report()):
            train.discover(self.tdir)
            train.amend(self.tdir, add_grids={"orb": {"range_bars": [2]}})
            with self.assertRaises(ValueError):
                train.discover(self.tdir)

    def test_earlier_runs_count_as_trials_for_the_same_ticker(self):
        self.make_plan()
        trials = []

        def fake(bars, name, grid, costs, ppy, **k):
            trials.append((name, k.get("earlier_trials")))
            return fake_report()

        with mock.patch.object(train.V, "validate", fake):
            train.discover(self.tdir)
            train.amend(self.tdir, add_grids={"orb": {"range_bars": [2, 4]}})
            train.discover(self.tdir)
        first = [e for n, e in trials if n == "trend"]
        second = [e for n, e in trials if n == "orb"]
        self.assertTrue(all(e == 0 for e in first))
        self.assertTrue(second and all(e == 2 for e in second))

    def test_amend_cannot_change_holdout_or_costs_or_existing_roles(self):
        self.make_plan()
        with self.assertRaises(TypeError):
            train.amend(self.tdir, holdout=0.1)
        with self.assertRaises(ValueError):
            train.amend(self.tdir, add_grids={"trend": {"window": [9]}})  # already in the plan


class Judging(Env):
    def run_discover_with(self, verdicts):
        """verdicts: ticker -> (verdict, params) for real runs; null runs always FAIL."""
        plan = self.make_plan()

        disc = sorted(t for t, r in plan["roles"].items() if r == "discovery")

        def patched(plan_, ticker, strategy, grid, kind, shuffle_seed=None):
            if kind == "null":
                return fake_report("FAIL")
            v, p = verdicts.get(ticker, ("FAIL", {"window": 3}))
            return fake_report(v, p)

        with mock.patch.object(train, "run_discovery_one", patched):
            train.discover(self.tdir)
        return plan, disc

    def test_candidate_needs_passes_beyond_chance(self):
        plan, disc = self.run_discover_with({})
        self.assertEqual(train.candidates(self.tdir), [])
        os.remove(os.path.join(self.tdir, "plan.json"))
        os.remove(os.path.join(self.tdir, "ledger.jsonl"))
        plan, disc = self.run_discover_with({disc[0]: ("PASS", {"window": 6}), disc[1]: ("PASS", {"window": 6}),
                                             disc[2]: ("PASS", {"window": 3})})
        c = train.candidates(self.tdir)
        self.assertEqual([x["strategy"] for x in c], ["trend"])
        self.assertEqual(c[0]["params"], {"window": 6})  # most common among the passing runs
        self.assertEqual(c[0]["passes"], 3)

    def test_one_pass_among_three_is_not_a_candidate(self):
        plan, disc = self.run_discover_with({"T1": ("PASS", {"window": 3})})
        self.assertEqual(train.candidates(self.tdir), [])

    def test_null_pass_rate_raises_the_bar(self):
        plan = self.make_plan()
        disc = sorted(t for t, r in plan["roles"].items() if r == "discovery")

        def patched(plan_, ticker, strategy, grid, kind, shuffle_seed=None):
            return fake_report("PASS" if kind == "null" or ticker in disc[:2] else "FAIL", {"window": 3})

        with mock.patch.object(train, "run_discovery_one", patched):
            train.discover(self.tdir)
        self.assertEqual(train.candidates(self.tdir), [])  # chance passes just as often


class ConfirmAndFinal(Env):
    def prepare(self, discovery_pass=True):
        plan = self.make_plan()
        disc = sorted(t for t, r in plan["roles"].items() if r == "discovery")

        def patched(plan_, ticker, strategy, grid, kind, shuffle_seed=None):
            ok = kind == "discover" and discovery_pass
            return fake_report("PASS" if ok else "FAIL", {"window": 6})

        with mock.patch.object(train, "run_discovery_one", patched):
            train.discover(self.tdir)
        return plan

    def test_confirm_uses_frozen_settings_on_confirmation_tickers_only(self):
        plan = self.prepare()
        seen = []

        def fake(bars, name, grid, costs, ppy, **k):
            seen.append((name, grid, k.get("earlier_trials")))
            return fake_report("PASS", {"window": 6})

        with mock.patch.object(train.V, "validate", fake):
            out = train.confirm(self.tdir)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(g == {"window": [6]} for _, g, _ in seen))
        self.assertEqual(out["confirmed"], ["trend"])

    def test_confirm_fails_when_the_unseen_stocks_do_not_pass(self):
        self.prepare()
        with mock.patch.object(train.V, "validate", lambda *a, **k: fake_report("FAIL")):
            out = train.confirm(self.tdir)
        self.assertEqual(out["confirmed"], [])

    def test_confirm_with_no_candidates_runs_nothing(self):
        self.prepare(discovery_pass=False)
        with mock.patch.object(train.V, "validate", lambda *a, **k: self.fail("should not run")):
            out = train.confirm(self.tdir)
        self.assertEqual(out["confirmed"], [])

    def test_final_needs_a_confirmed_rule_and_runs_once(self):
        self.prepare()
        with self.assertRaises(ValueError):
            train.final(self.tdir, "trend")
        with mock.patch.object(train.V, "validate", lambda *a, **k: fake_report("PASS", {"window": 6})):
            train.confirm(self.tdir)
        good = {"net_return": 0.02, "stressed_return": 0.01, "delay2_return": 0.01, "alpha_return": 0.01,
                "n_trades": 30}
        with mock.patch.object(train, "holdout_check", lambda *a, **k: dict(good)):
            out = train.final(self.tdir, "trend")
            self.assertEqual(out["verdict"], "HOLDS")
            with self.assertRaises(ValueError):
                train.final(self.tdir, "trend")

    def test_final_fails_when_holdout_does_not_hold(self):
        self.prepare()
        with mock.patch.object(train.V, "validate", lambda *a, **k: fake_report("PASS", {"window": 6})):
            train.confirm(self.tdir)
        bad = {"net_return": -0.02, "stressed_return": -0.03, "delay2_return": -0.01, "alpha_return": -0.01,
               "n_trades": 30}
        with mock.patch.object(train, "holdout_check", lambda *a, **k: dict(bad)):
            out = train.final(self.tdir, "trend")
        self.assertEqual(out["verdict"], "DOES NOT HOLD")

    def test_holdout_check_runs_for_real_on_the_holdout_only(self):
        plan = self.make_plan()
        bars = series(0)
        pre, hold, cutoff = train.split_holdout(bars, 0.2)
        m = train.holdout_check(hold, "trend", {"window": 3}, plan)
        for k in ("net_return", "stressed_return", "delay2_return", "alpha_return", "n_trades"):
            self.assertIn(k, m)


class Cli(Env):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = train.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    PLAN = ["plan", "--interval", "15", "--periods-per-year", "5040", "--grid", "trend:window=3,6",
            "--commission-bps", "0", "--spread-bps", "1", "--slippage-bps", "0.5", "--flatten-eod",
            "--train-days", "10", "--test-days", "4", "--null-shuffles", "1", "--max-rounds", "3"]

    def base(self):
        return ["--dir", self.tdir]

    def test_plan_discover_status_verify(self):
        code, out, err = self.run_cli(*self.PLAN, *self.base(), "--store", self.store,
                                      "--tickers", ",".join(self.TICKERS))
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("discover", *self.base())
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("status", *self.base())
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["round"], 1)
        self.assertGreater(data["ledger_entries"], 0)
        code, out, err = self.run_cli("verify", *self.base())
        self.assertEqual(code, 0, err)

    def test_costs_are_required(self):
        args = [a for a in self.PLAN if a not in ("--commission-bps", "0")]
        code, out, err = self.run_cli(*args, *self.base(), "--store", self.store,
                                      "--tickers", ",".join(self.TICKERS))
        self.assertNotEqual(code, 0)

    def test_refuses_with_a_message(self):
        code, out, err = self.run_cli("discover", *self.base())
        self.assertNotEqual(code, 0)
        self.assertIn("Refused", err)


if __name__ == "__main__":
    unittest.main()
