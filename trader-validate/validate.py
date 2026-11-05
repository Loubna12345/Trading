#!/usr/bin/env python3
"""Decide whether a backtest result is worth believing. Standard library only.

A strategy run through the backtester is not evidence by itself: try enough settings and one will look good by
luck. This tool takes a strategy and a grid of settings and runs the checks that catch that:

  1. cost stress      net result with every cost doubled must stay positive; also reports the extra cost per
                      side at which the result reaches zero (break-even cost)
  2. delay scan       the same rule with orders one, two, three bars late; a real edge fades gradually
  3. walk-forward     choose settings on a training window, score them on the next unseen window, repeat
  4. deflated Sharpe  the Sharpe ratio is discounted for how many settings were tried and for the shape of the
                      returns (formula of Bailey and Lopez de Prado, as implemented in the endgame library;
                      the paper itself was not read)
  5. trade count      too few trades is luck
  6. alpha            the result after removing what simply being in a rising market would have paid
                      (beta times buy and hold); without this, any long rule "passes" in a bull market
  7. recent stretch   the last third of the data must not be a loss (an edge that stopped is not an edge)
  8. wild bars        a bar with a huge range is probably a bad tick and can fake a profit

The verdict is PASS, FAIL or INCONCLUSIVE. FAIL is the expected answer for most ideas. The thresholds below are
this project's choices, not sourced numbers; each is printed next to its result.
"""

import argparse
import importlib.util
import itertools
import json
import math
import os
import sys
from statistics import NormalDist

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


engine = _load(os.path.join(ROOT, "trader-backtest", "engine.py"), "trader_backtest_engine_val")
metrics = engine.metrics
strategies = _load(os.path.join(ROOT, "trader-strategies", "strategies.py"), "trader_strategies_val")

MIN_TRADES = 100          # fewer trades than this is luck (project rule of thumb)
MIN_DSR = 0.95            # deflated Sharpe probability needed (project choice)
MAX_BAR_RANGE = 0.20      # a bar whose high is 20% above its low is suspicious
STRESS = 2.0              # costs multiplied by this for the stress test
MAX_GRID = 200            # most settings combinations allowed in one run
EULER_GAMMA = 0.5772156649015329
ND = NormalDist()


# ---------- small helpers ----------

def scale_costs(costs, k):
    if not isinstance(k, (int, float)) or k <= 0:
        raise ValueError("cost scale must be above 0")
    return {key: v * k for key, v in costs.items()}


def split_days(bars):
    """[(day, start, end_exclusive)] using the UTC date in each bar's time."""
    out, start = [], 0
    for i in range(1, len(bars) + 1):
        if i == len(bars) or bars[i]["t"][:10] != bars[start]["t"][:10]:
            out.append((bars[start]["t"][:10], start, i))
            start = i
    return out


def sharpe_pp(returns):
    """Per-period Sharpe (mean over sample standard deviation), risk-free zero. None if undefined."""
    if len(returns) < 2:
        return None
    m = sum(returns) / len(returns)
    var = sum((x - m) ** 2 for x in returns) / (len(returns) - 1)
    if var <= 1e-18:
        return None
    return m / math.sqrt(var)


def moments(returns):
    """(skewness, raw kurtosis) from population moments."""
    n = len(returns)
    m = sum(returns) / n
    m2 = sum((x - m) ** 2 for x in returns) / n
    if m2 <= 1e-18:
        return 0.0, 3.0
    m3 = sum((x - m) ** 3 for x in returns) / n
    m4 = sum((x - m) ** 4 for x in returns) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2


def expected_max_sharpe(n_trials, var_sr):
    """Largest per-period Sharpe expected among n_trials tries of a rule with no edge."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * ND.inv_cdf(1 - 1.0 / n_trials)
                                + EULER_GAMMA * ND.inv_cdf(1 - 1.0 / (n_trials * math.e)))


def deflated_sharpe(sr, n_obs, skew, kurt, n_trials, var_sr):
    """Probability that the true Sharpe is above what luck alone would give across n_trials tries.
    sr and var_sr are per-period (not annualised); kurt is raw kurtosis (3 for a normal)."""
    if n_obs < 3:
        raise ValueError("need at least 3 return observations")
    sr0 = expected_max_sharpe(n_trials, var_sr)
    var = (1 - skew * sr + (kurt - 1) / 4.0 * sr ** 2) / (n_obs - 1)
    if var <= 0:
        return 1.0 if sr > sr0 else 0.0
    return ND.cdf((sr - sr0) / math.sqrt(var))


def alpha_returns(strategy_returns, bench_returns):
    """Per-bar strategy return minus beta times the buy-and-hold return; returns (alpha_series, beta)."""
    if len(strategy_returns) != len(bench_returns):
        raise ValueError("return series differ in length")
    n = len(bench_returns)
    mb = sum(bench_returns) / n
    ms = sum(strategy_returns) / n
    var = sum((b - mb) ** 2 for b in bench_returns)
    beta = 0.0 if var <= 1e-18 else sum((s - ms) * (b - mb) for s, b in zip(strategy_returns, bench_returns)) / var
    return [s - beta * b for s, b in zip(strategy_returns, bench_returns)], beta


def expand_grid(grid):
    names = sorted(grid)
    combos = [dict(zip(names, vals)) for vals in itertools.product(*(grid[n] for n in names))]
    if not combos:
        raise ValueError("the grid is empty")
    if len(combos) > MAX_GRID:
        raise ValueError("%d combinations is too many (limit %d): every extra setting tried makes a good "
                         "result more likely by luck" % (len(combos), MAX_GRID))
    return combos


# ---------- the checks ----------

def _run(bars, make_strategy, costs, engine_kw, capital=10000.0):
    return engine.backtest(bars, make_strategy(), costs, capital=capital, **engine_kw)


def delay_scan(bars, make_strategy, costs, periods_per_year, engine_kw):
    out = []
    for d in (1, 2, 3):
        kw = dict(engine_kw, delay=d)
        res = _run(bars, make_strategy, costs, kw)
        out.append({"delay": d, "total_return": metrics.total_return(res["equity"]),
                    "sharpe": metrics.sharpe(res["equity"], periods_per_year)})
    return out


def break_even_extra_bps(bars, make_strategy, costs, engine_kw, cap_bps=500.0):
    """Extra slippage per side, in basis points, at which the total return reaches zero.
    0.0 if it is not positive already; None if it is still positive at cap_bps."""
    def ret(extra):
        c = dict(costs, slippage_bps=costs["slippage_bps"] + extra)
        return metrics.total_return(_run(bars, make_strategy, c, engine_kw)["equity"])

    if ret(0.0) <= 0:
        return 0.0
    if ret(cap_bps) > 0:
        return None
    lo, hi = 0.0, cap_bps
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if ret(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def walk_forward(bars, name, grid, costs, periods_per_year, train_days, test_days, engine_kw):
    days = split_days(bars)
    if len(days) < train_days + test_days:
        raise ValueError("walk-forward needs %d days of bars (%d train + %d test); there are %d"
                         % (train_days + test_days, train_days, test_days, len(days)))
    folds, oos_returns, growth, trades = [], [], 1.0, 0
    s = 0
    while s + train_days + test_days <= len(days):
        a = days[s][1]
        train_end = days[s + train_days - 1][2] - a
        piece = bars[a: days[s + train_days + test_days - 1][2]]
        best = None
        for params in grid:
            res = engine.backtest(piece, strategies.make(name, **params), costs, **engine_kw)
            tr = metrics.period_returns(res["equity"][:train_end]) if train_end >= 2 else []
            sr = sharpe_pp(tr)
            key = sr if sr is not None else float("-inf")
            if best is None or key > best[0]:
                best = (key, params, res, sr)
        _, params, res, train_sr = best
        eq = res["equity"][train_end - 1:]
        test_ret = eq[-1] / eq[0] - 1.0
        oos_returns.extend(metrics.period_returns(eq))
        growth *= 1.0 + test_ret
        cut = piece[train_end]["t"]
        trades += sum(1 for t in res["trades"] if t["entry_t"] >= cut)
        folds.append({"params": params, "train_days": [d[0] for d in days[s: s + train_days]],
                      "test_days": [d[0] for d in days[s + train_days: s + train_days + test_days]],
                      "train_sharpe": None if train_sr is None else train_sr * math.sqrt(periods_per_year),
                      "test_return": test_ret})
        s += test_days
    sr = sharpe_pp(oos_returns)
    trained = [f["train_sharpe"] for f in folds if f["train_sharpe"] is not None]
    return {"folds": folds, "oos_total_return": growth - 1.0,
            "oos_sharpe": None if sr is None else sr * math.sqrt(periods_per_year),
            "oos_trades": trades,
            "mean_train_sharpe": sum(trained) / len(trained) if trained else None}


def decide(v):
    """Turn the numbers into a verdict. Hard failures give FAIL; missing numbers, too few trades or a weak
    deflated Sharpe give INCONCLUSIVE; PASS needs every check to hold."""
    rules = [
        ("trades", "n_trades", lambda x: x >= MIN_TRADES, "at least %d trades" % MIN_TRADES, False),
        ("net result after costs", "net_return", lambda x: x > 0, "above 0", True),
        ("cost stress (all costs x%g)" % STRESS, "stressed_return", lambda x: x > 0, "above 0", True),
        ("orders two bars late", "delay2_return", lambda x: x > 0, "above 0", True),
        ("walk-forward result on unseen days", "oos_return", lambda x: x > 0, "above 0", True),
        ("walk-forward Sharpe", "oos_sharpe", lambda x: x > 0, "above 0", True),
        ("alpha after removing market exposure", "alpha_return", lambda x: x > 0, "above 0", True),
        ("deflated Sharpe probability (of the alpha)", "dsr", lambda x: x >= MIN_DSR, "at least %g" % MIN_DSR, False),
        ("last third of the data", "recent_return", lambda x: x > 0, "above 0", False),
        ("largest single-bar range", "max_bar_range", lambda x: x <= MAX_BAR_RANGE,
         "at most %g%% (otherwise look for a bad tick)" % (MAX_BAR_RANGE * 100), False),
    ]
    checks, hard_fail, soft = [], False, False
    for label, key, ok, rule, hard in rules:
        val = v.get(key)
        passed = None if val is None else bool(ok(val))
        checks.append({"name": label, "value": val, "rule": rule, "passed": bool(passed) if passed is not None else False,
                       "computed": passed is not None})
        if passed is None:
            soft = True
        elif not passed:
            if hard:
                hard_fail = True
            else:
                soft = True
    verdict = "FAIL" if hard_fail else ("INCONCLUSIVE" if soft else "PASS")
    reasons = []
    for c in checks:
        if not c["computed"]:
            reasons.append("%s: not computed" % c["name"])
        elif not c["passed"]:
            reasons.append("%s: %s, needs %s" % (c["name"], _fmt(c["value"]), c["rule"]))
    return {"verdict": verdict, "checks": checks, "reasons": reasons}


def _fmt(x):
    return ("%.4f" % x) if isinstance(x, float) else str(x)


# ---------- the whole run ----------

def validate(bars, name, grid, costs, periods_per_year, flatten_eod=False, train_days=20, test_days=5,
             earlier_trials=0, capital=10000.0):
    combos = expand_grid({k: list(v) for k, v in grid.items()})
    kw = {"flatten_eod": flatten_eod}
    bench = engine.buy_and_hold(bars, costs, capital=capital)
    bench_rets = metrics.period_returns(bench["equity"])
    results = []
    for params in combos:
        res = _run(bars, lambda p=params: strategies.make(name, **p), costs, kw, capital)
        alpha, beta = alpha_returns(metrics.period_returns(res["equity"]), bench_rets)
        results.append((sharpe_pp(alpha), params, res, alpha, beta))
    trial_srs = [r[0] for r in results if r[0] is not None]
    best = max(results, key=lambda r: r[0] if r[0] is not None else float("-inf"))
    best_sr, best_params, best_res, best_alpha, best_beta = best
    n_trials = len(combos) + int(earlier_trials)
    mean_sr = sum(trial_srs) / len(trial_srs) if len(trial_srs) > 1 else 0.0
    var_sr = (sum((x - mean_sr) ** 2 for x in trial_srs) / (len(trial_srs) - 1)) if len(trial_srs) > 1 else 0.0

    make_best = lambda: strategies.make(name, **best_params)
    stressed = _run(bars, make_best, scale_costs(costs, STRESS), kw, capital)
    scan = delay_scan(bars, make_best, costs, periods_per_year, kw)
    be = break_even_extra_bps(bars, make_best, costs, kw)
    notes = []
    try:
        wf = walk_forward(bars, name, combos, costs, periods_per_year, train_days, test_days, kw)
    except ValueError as e:
        wf = None
        notes.append("walk-forward not computed: %s" % e)

    dsr = None
    if best_sr is not None and len(best_alpha) >= 3:
        skew, kurt = moments(best_alpha)
        dsr = deflated_sharpe(best_sr, len(best_alpha), skew, kurt, n_trials, var_sr)

    eq = best_res["equity"]
    third = eq[2 * len(eq) // 3:]
    wild = engine.suspicious_bars(bars, MAX_BAR_RANGE)
    values = {"n_trades": len(best_res["trades"]),
              "net_return": metrics.total_return(eq),
              "stressed_return": metrics.total_return(stressed["equity"]),
              "delay2_return": scan[1]["total_return"],
              "oos_return": wf["oos_total_return"] if wf else None,
              "oos_sharpe": wf["oos_sharpe"] if wf else None,
              "alpha_return": sum(best_alpha),
              "dsr": dsr,
              "recent_return": (third[-1] / third[0] - 1.0) if len(third) >= 2 else None,
              "max_bar_range": max([r for _, r in wild], default=max(b["h"] / b["l"] - 1.0 for b in bars))}
    verdict = decide(values)
    verdict["reasons"] = notes + verdict["reasons"]
    return {"verdict": verdict["verdict"], "reasons": verdict["reasons"], "checks": verdict["checks"],
            "best_params": best_params, "beta_to_buy_and_hold": best_beta, "suspicious_bars": wild[:20],
            "n_trials": n_trials, "settings_tried_here": len(combos),
            "break_even_extra_bps": be, "delay_scan": scan, "walk_forward": wf, "deflated_sharpe": dsr,
            "best_net_return": values["net_return"], "best_trades": values["n_trades"],
            "benchmark": metrics.summarize(bench["equity"], [], periods_per_year, min_trades=0),
            "strategy_report": metrics.summarize(best_res["equity"], [t["pnl"] for t in best_res["trades"]],
                                                 periods_per_year),
            "bars": len(bars), "flatten_eod": flatten_eod}


# ---------- command line ----------

def parse_grid(items):
    grid = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("--grid %r must look like name=v1,v2,v3" % item)
        k, vs = item.split("=", 1)
        vals = []
        for v in vs.split(","):
            try:
                vals.append(int(v))
            except ValueError:
                try:
                    vals.append(float(v))
                except ValueError:
                    raise ValueError("--grid %s: %r is not a number" % (k, v))
        grid[k] = vals
    if not grid:
        raise ValueError("give at least one --grid name=values")
    return grid


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate a day-trading rule. Not financial advice.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--strategy", required=True)
    r.add_argument("--grid", action="append", help="name=v1,v2,... every setting you try; repeat per parameter")
    r.add_argument("--bars", required=True)
    r.add_argument("--periods-per-year", type=int, required=True)
    r.add_argument("--capital", type=float, default=10000.0)
    r.add_argument("--flatten-eod", action="store_true")
    r.add_argument("--train-days", type=int, default=20)
    r.add_argument("--test-days", type=int, default=5)
    r.add_argument("--earlier-trials", type=int, default=0,
                   help="settings you already tried on this data in other runs (they count against the result)")
    r.add_argument("--commission-bps", type=float)
    r.add_argument("--spread-bps", type=float)
    r.add_argument("--slippage-bps", type=float)
    r.add_argument("--fixed-fee", type=float, default=0.0)
    a = ap.parse_args(argv)
    if None in (a.commission_bps, a.spread_bps, a.slippage_bps):
        print("A cost model is required: give --commission-bps, --spread-bps and --slippage-bps "
              "(0 is allowed if that is really your cost).", file=sys.stderr)
        return 2
    try:
        costs = engine.make_costs(commission_bps=a.commission_bps, spread_bps=a.spread_bps,
                                  slippage_bps=a.slippage_bps, fixed_fee=a.fixed_fee)
        bars = engine.load_bars_csv(a.bars)
        rep = validate(bars, a.strategy, parse_grid(a.grid), costs, a.periods_per_year,
                       flatten_eod=a.flatten_eod, train_days=a.train_days, test_days=a.test_days,
                       earlier_trials=a.earlier_trials, capital=a.capital)
    except ValueError as e:
        print("Refused: %s" % e, file=sys.stderr)
        return 2
    rep["note"] = ("Not a prediction: a test of whether a past result is likely to be luck. Taxes are not "
                   "included. Thresholds are this project's choices.")
    print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
