#!/usr/bin/env python3
"""Training: run the strategies over many stocks under rules fixed in advance. Standard library only.

Trying many rules on many stocks and keeping what works is the easiest way to fool yourself. This tool makes
that harder:

  plan      fixes the stocks, which are discovery and which are confirmation, the settings of every rule, the costs,
            the holdout, and the number of rounds. The plan is locked: its hash is in the ledger.
  ledger    every run is appended to a hash-chained file. Editing or deleting a middle line is detected. (Cutting
            off the last lines cannot be detected without an outside copy; keep one.)
  discover  each rule on each discovery stock (bars before the holdout only), plus the same rule on shuffled copies
            of the stock, so the pass rate expected from chance is measured on your own data.
  confirm   rules that pass on more discovery stocks than chance would give are frozen (most common settings) and run
            on confirmation stocks they have never seen.
  final     a confirmed rule gets ONE test on the untouched holdout of every stock.

Judging is by the share of stocks where a rule passes, compared with chance with a binomial tail. The thresholds
(0.05 significance, at least 2 passing stocks, 3 stocks per role, 10 to 50% holdout) are this project's choices.
A result is a reason to try a rule on paper money, never a forecast.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import sys
import time
import zlib
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = _load(os.path.join(ROOT, "trader-validate", "validate.py"), "trader_validate_train")
engine = V.engine
strategies = V.strategies
metrics = engine.metrics

SIGNIFICANCE = 0.05
MIN_PASSES = 2
MIN_PER_ROLE = 3
MIN_DAYS = 30
MIN_HOLD, MAX_HOLD = 0.10, 0.50
CHANCE_FLOOR, CHANCE_CAP = 0.05, 0.95
MIN_HOLDOUT_DAYS_NOTE = 20


# ---------- ledger ----------

def _canon(entry):
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


def _digest(entry):
    return hashlib.sha256(_canon(entry).encode("utf-8")).hexdigest()


def ledger_path(folder):
    return os.path.join(folder, "ledger.jsonl")


def ledger_read(folder):
    path = ledger_path(folder)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def ledger_append(folder, entry):
    os.makedirs(folder, exist_ok=True)
    entries = ledger_read(folder)
    e = dict(entry)
    e["n"] = len(entries) + 1
    e["prev"] = entries[-1]["hash"] if entries else "0" * 64
    e["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    e["hash"] = _digest(e)
    with open(ledger_path(folder), "a", encoding="utf-8") as fh:
        fh.write(_canon(e) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return e


def ledger_verify(folder):
    prev = "0" * 64
    entries = ledger_read(folder)
    for i, e in enumerate(entries, start=1):
        body = {k: v for k, v in e.items() if k != "hash"}
        if e.get("n") != i or e.get("prev") != prev or _digest(body) != e.get("hash"):
            raise ValueError("ledger was changed: entry %d does not match the chain" % i)
        prev = e["hash"]
    return len(entries)


# ---------- maths and data ----------

def binom_tail(n, k, p):
    """P(X >= k) for X ~ Binomial(n, p)."""
    return sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))


def split_holdout(bars, fraction):
    days = V.split_days(bars)
    if len(days) < MIN_DAYS:
        raise ValueError("need at least %d days of bars, have %d" % (MIN_DAYS, len(days)))
    cut = int(len(days) * (1.0 - fraction))
    start = days[cut][1]
    return bars[:start], bars[start:], days[cut][0]


def shuffled_within_days(bars, seed):
    """A null copy: within each day the bar-to-bar returns (with their wick sizes and volumes) are put in random
    order. Each day's end price, volatility and bar count stay, the order of moves inside the day is destroyed."""
    rnd = random.Random(seed)
    out = []
    for _, a, b in V.split_days(bars):
        day = bars[a:b]
        legs = []
        for i, x in enumerate(day):
            prev = day[i - 1]["c"] if i else x["o"]
            r = x["c"] / prev - 1.0
            top = x["h"] / max(x["o"], x["c"])
            bot = x["l"] / min(x["o"], x["c"])
            legs.append((r, top, bot, x["v"], x["o"] / prev))
        rnd.shuffle(legs)
        price = day[0]["o"]
        for i, x in enumerate(day):
            r, top, bot, v, _ = legs[i]
            o = price
            c = o * (1.0 + r)
            out.append({"t": x["t"], "o": o, "h": max(o, c) * top, "l": min(o, c) * bot, "c": c, "v": v})
            price = c
    return out


def load_series(plan, ticker):
    path = os.path.join(plan["store"], "%s_%dm.csv" % (ticker, plan["interval"]))
    if not os.path.exists(path):
        raise ValueError("no stored bars for %s at %s" % (ticker, path))
    return engine.load_bars_csv(path)


# ---------- plan ----------

def plan_hash(plan):
    return hashlib.sha256(_canon(plan).encode("utf-8")).hexdigest()


def _check_grids(grids):
    for name, grid in grids.items():
        if name not in strategies.REGISTRY:
            raise ValueError("unknown strategy %r" % name)
        if not grid or any(not v for v in grid.values()):
            raise ValueError("%s needs at least one value for every parameter" % name)
        V.expand_grid(grid)
        strategies.make(name, **{k: v[0] for k, v in grid.items()})


def make_plan(folder, store, interval, tickers, periods_per_year, costs, grids, flatten_eod, holdout,
              train_days, test_days, null_shuffles, max_rounds, seed, roles=None):
    if os.path.exists(os.path.join(folder, "plan.json")):
        raise ValueError("a plan already exists in %s; it is locked. Use amend to add stocks or rules." % folder)
    if costs is None:
        raise ValueError("a cost model is required")
    costs = engine.make_costs(**costs)
    if not MIN_HOLD <= holdout <= MAX_HOLD:
        raise ValueError("holdout must be between %g and %g of the days" % (MIN_HOLD, MAX_HOLD))
    tickers = sorted(set(tickers))
    if len(tickers) < 2 * MIN_PER_ROLE:
        raise ValueError("need at least %d stocks (%d discovery, %d confirmation); with fewer, results are anecdotes"
                         % (2 * MIN_PER_ROLE, MIN_PER_ROLE, MIN_PER_ROLE))
    _check_grids(grids)
    plan = {"store": store, "interval": interval, "periods_per_year": periods_per_year, "costs": costs,
            "flatten_eod": bool(flatten_eod), "holdout": holdout, "train_days": train_days,
            "test_days": test_days, "null_shuffles": null_shuffles, "max_rounds": max_rounds, "seed": seed,
            "grids": {k: {p: list(v) for p, v in g.items()} for k, g in grids.items()}, "amendments": []}
    for t in tickers:
        split_holdout(load_series(plan, t), holdout)  # must exist and have enough days
    if roles is None:
        order = list(tickers)
        random.Random(seed).shuffle(order)
        k = (len(order) + 1) // 2
        roles = {t: ("discovery" if t in order[:k] else "confirmation") for t in tickers}
    else:
        if sorted(roles) != tickers:
            raise ValueError("roles must cover exactly the stocks given")
    for role in ("discovery", "confirmation"):
        if sum(1 for r in roles.values() if r == role) < MIN_PER_ROLE:
            raise ValueError("need at least %d %s stocks" % (MIN_PER_ROLE, role))
    plan["roles"] = roles
    os.makedirs(folder, exist_ok=True)
    _write_plan(folder, plan)
    ledger_append(folder, {"kind": "plan", "plan_hash": plan_hash(plan)})
    return plan


def _write_plan(folder, plan):
    tmp = os.path.join(folder, "plan.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2, sort_keys=True)
    os.replace(tmp, os.path.join(folder, "plan.json"))


def load_plan(folder):
    path = os.path.join(folder, "plan.json")
    if not os.path.exists(path):
        raise ValueError("no plan in %s: run plan first" % folder)
    with open(path, encoding="utf-8") as fh:
        plan = json.load(fh)
    marks = [e for e in ledger_read(folder) if e["kind"] in ("plan", "amend")]
    if not marks or marks[-1]["plan_hash"] != plan_hash(plan):
        raise ValueError("plan.json does not match the ledger: it was changed outside amend")
    return plan


def amend(folder, add_grids=None, add_tickers=None):
    """Add rules (never change existing ones) or stocks. Every amendment is logged and counts as a new round of
    trials."""
    plan = load_plan(folder)
    add_grids = add_grids or {}
    add_tickers = add_tickers or {}
    for name in add_grids:
        if name in plan["grids"]:
            raise ValueError("%s is already in the plan; its settings cannot be changed" % name)
    _check_grids(add_grids)
    for t, role in add_tickers.items():
        if t in plan["roles"]:
            raise ValueError("%s is already in the plan" % t)
        if role not in ("discovery", "confirmation"):
            raise ValueError("role must be discovery or confirmation")
        split_holdout(load_series(plan, t), plan["holdout"])
    plan["grids"].update({k: {p: list(v) for p, v in g.items()} for k, g in add_grids.items()})
    plan["roles"].update(add_tickers)
    plan["amendments"].append({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                               "grids": sorted(add_grids), "tickers": sorted(add_tickers)})
    _write_plan(folder, plan)
    ledger_append(folder, {"kind": "amend", "plan_hash": plan_hash(plan), "grids": sorted(add_grids),
                           "tickers": sorted(add_tickers)})
    return plan


# ---------- runs ----------

def current_round(folder):
    rounds = [e.get("round", 0) for e in ledger_read(folder) if e["kind"] == "discover"]
    return max(rounds, default=0)


def settings_tried_on(folder, ticker):
    return sum(e.get("settings_tried", 0) for e in ledger_read(folder)
               if e["kind"] in ("discover", "confirm") and e["ticker"] == ticker)


def _entry(kind, ticker, strategy, grid, rep, earlier, round_no=None):
    e = {"kind": kind, "ticker": ticker, "strategy": strategy, "grid": grid, "verdict": rep["verdict"],
         "params": rep["best_params"], "settings_tried": len(V.expand_grid(grid)), "earlier_trials": earlier,
         "net_return": rep["best_net_return"], "trades": rep["best_trades"],
         "deflated_sharpe": rep["deflated_sharpe"], "break_even_extra_bps": rep["break_even_extra_bps"]}
    if round_no is not None:
        e["round"] = round_no
    return e


def run_discovery_one(plan, ticker, strategy, grid, kind, shuffle_seed=None, earlier=0):
    pre, _, _ = split_holdout(load_series(plan, ticker), plan["holdout"])
    if kind == "null":
        pre = shuffled_within_days(pre, shuffle_seed)
    return V.validate(pre, strategy, grid, plan["costs"], plan["periods_per_year"], flatten_eod=plan["flatten_eod"],
                      train_days=plan["train_days"], test_days=plan["test_days"], earlier_trials=earlier)


def _call_one(plan, ticker, strategy, grid, kind, seed, earlier):
    """run_discovery_one may be replaced in tests with a function that does not take `earlier`."""
    try:
        return run_discovery_one(plan, ticker, strategy, grid, kind, shuffle_seed=seed, earlier=earlier)
    except TypeError:
        return run_discovery_one(plan, ticker, strategy, grid, kind, shuffle_seed=seed)


def discover(folder):
    plan = load_plan(folder)
    done = {(e["ticker"], e["strategy"], _canon(e["grid"])) for e in ledger_read(folder) if e["kind"] == "discover"}
    todo = [(t, s) for t in sorted(plan["roles"]) if plan["roles"][t] == "discovery"
            for s in sorted(plan["grids"]) if (t, s, _canon(plan["grids"][s])) not in done]
    rnd = current_round(folder)
    if not todo:
        return {"round": rnd, "ran": 0}
    rnd += 1
    if rnd > plan["max_rounds"]:
        raise ValueError("round limit reached (%d): this data set is spent; new data is needed" % plan["max_rounds"])
    for ticker, strategy in todo:
        grid = plan["grids"][strategy]
        earlier = settings_tried_on(folder, ticker)
        rep = _call_one(plan, ticker, strategy, grid, "discover", None, earlier)
        ledger_append(folder, _entry("discover", ticker, strategy, grid, rep, earlier, rnd))
        for k in range(plan["null_shuffles"]):
            seed = (zlib.crc32(ticker.encode()) ^ (plan["seed"] * 7919)) + k
            rep = _call_one(plan, ticker, strategy, grid, "null", seed, earlier)
            ledger_append(folder, _entry("null", ticker, strategy, grid, rep, earlier, rnd))
    return {"round": rnd, "ran": len(todo)}


def chance_rate(folder, strategy):
    nulls = [e for e in ledger_read(folder) if e["kind"] == "null" and e["strategy"] == strategy]
    if not nulls:
        return CHANCE_FLOOR
    rate = sum(1 for e in nulls if e["verdict"] == "PASS") / float(len(nulls))
    return min(max(rate, CHANCE_FLOOR), CHANCE_CAP)


def candidates(folder):
    runs = [e for e in ledger_read(folder) if e["kind"] == "discover"]
    out = []
    for strategy in sorted({e["strategy"] for e in runs}):
        mine = [e for e in runs if e["strategy"] == strategy]
        passed = [e for e in mine if e["verdict"] == "PASS"]
        p0 = chance_rate(folder, strategy)
        p = binom_tail(len(mine), len(passed), p0) if passed else 1.0
        if len(passed) >= MIN_PASSES and p < SIGNIFICANCE:
            common = Counter(_canon(e["params"]) for e in passed)
            best = sorted(common.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            out.append({"strategy": strategy, "params": json.loads(best), "passes": len(passed), "n": len(mine),
                        "chance_rate": p0, "p_value": p})
    return out


def confirm(folder):
    plan = load_plan(folder)
    done = {e["strategy"]: e for e in ledger_read(folder) if e["kind"] == "confirm_result"}
    confirmed = []
    for c in candidates(folder):
        s = c["strategy"]
        if s in done:
            if done[s]["confirmed"]:
                confirmed.append(s)
            continue
        grid = {k: [v] for k, v in c["params"].items()}
        conf = sorted(t for t, r in plan["roles"].items() if r == "confirmation")
        passes = 0
        for t in conf:
            pre, _, _ = split_holdout(load_series(plan, t), plan["holdout"])
            earlier = settings_tried_on(folder, t)
            rep = V.validate(pre, s, grid, plan["costs"], plan["periods_per_year"], flatten_eod=plan["flatten_eod"],
                             train_days=plan["train_days"], test_days=plan["test_days"], earlier_trials=earlier)
            ledger_append(folder, _entry("confirm", t, s, grid, rep, earlier))
            passes += rep["verdict"] == "PASS"
        p0 = chance_rate(folder, s)
        p = binom_tail(len(conf), passes, p0) if passes else 1.0
        ok = passes >= MIN_PASSES and p < SIGNIFICANCE
        ledger_append(folder, {"kind": "confirm_result", "strategy": s, "params": c["params"], "confirmed": ok,
                               "passes": passes, "n": len(conf), "p_value": p, "chance_rate": p0})
        if ok:
            confirmed.append(s)
    return {"confirmed": confirmed}


def holdout_check(bars, name, params, plan):
    costs = plan["costs"]
    kw = {"flatten_eod": plan["flatten_eod"]}
    make = lambda: strategies.make(name, **params)
    base = engine.backtest(bars, make(), costs, **kw)
    stressed = engine.backtest(bars, make(), V.scale_costs(costs, V.STRESS), **kw)
    late = engine.backtest(bars, make(), costs, delay=2, **kw)
    bench = engine.buy_and_hold(bars, costs)
    alpha, _ = V.alpha_returns(metrics.period_returns(base["equity"]), metrics.period_returns(bench["equity"]))
    return {"net_return": metrics.total_return(base["equity"]),
            "stressed_return": metrics.total_return(stressed["equity"]),
            "delay2_return": metrics.total_return(late["equity"]), "alpha_return": sum(alpha),
            "n_trades": len(base["trades"])}


def final(folder, strategy):
    plan = load_plan(folder)
    entries = ledger_read(folder)
    conf = [e for e in entries if e["kind"] == "confirm_result" and e["strategy"] == strategy and e["confirmed"]]
    if not conf:
        raise ValueError("%s is not confirmed on unseen stocks; the holdout test is only for confirmed rules" % strategy)
    if any(e["kind"] == "final_result" and e["strategy"] == strategy for e in entries):
        raise ValueError("%s already had its one holdout test; running it again would make it a training set"
                         % strategy)
    params = conf[-1]["params"]
    rows, holds = [], 0
    for t in sorted(plan["roles"]):
        _, hold, _ = split_holdout(load_series(plan, t), plan["holdout"])
        m = holdout_check(hold, strategy, params, plan)
        ok = all(m[k] > 0 for k in ("net_return", "stressed_return", "delay2_return", "alpha_return"))
        holds += ok
        rows.append(dict(m, ticker=t, holds=ok, holdout_days=len(V.split_days(hold))))
    n = len(rows)
    p = binom_tail(n, holds, 0.5) if holds else 1.0
    verdict = "HOLDS" if p < SIGNIFICANCE else "DOES NOT HOLD"
    res = {"strategy": strategy, "params": params, "verdict": verdict, "stocks_holding": holds, "stocks": n,
           "p_value": p, "per_stock": rows}
    if min(r["holdout_days"] for r in rows) < MIN_HOLDOUT_DAYS_NOTE:
        res["warning"] = "the holdout is shorter than %d days on some stocks: weak evidence" % MIN_HOLDOUT_DAYS_NOTE
    ledger_append(folder, {"kind": "final_result", "strategy": strategy, "params": params, "verdict": verdict,
                           "stocks_holding": holds, "stocks": n, "p_value": p})
    return res


def status(folder):
    plan = load_plan(folder)
    entries = ledger_read(folder)
    return {"round": current_round(folder), "rounds_left": plan["max_rounds"] - current_round(folder),
            "ledger_entries": len(entries), "ledger_ok": ledger_verify(folder) == len(entries),
            "roles": plan["roles"], "grids": plan["grids"], "candidates": candidates(folder),
            "confirmed": [e["strategy"] for e in entries if e["kind"] == "confirm_result" and e["confirmed"]],
            "final": [{"strategy": e["strategy"], "verdict": e["verdict"]} for e in entries
                      if e["kind"] == "final_result"]}


# ---------- command line ----------

def _grids(items):
    grids = {}
    for item in items or []:
        try:
            name, rest = item.split(":", 1)
            param, vals = rest.split("=", 1)
            values = [int(v) if v.lstrip("-").isdigit() else float(v) for v in vals.split(",")]
        except ValueError:
            raise ValueError("--grid %r must look like strategy:param=v1,v2" % item)
        grids.setdefault(name, {})[param] = values
    return grids


def main(argv=None):
    ap = argparse.ArgumentParser(description="Training under fixed rules. Not financial advice.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default="trader/training", help="folder for the plan and the ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", parents=[common])
    p.add_argument("--store", required=True)
    p.add_argument("--interval", type=int, required=True)
    p.add_argument("--tickers", required=True, help="comma separated")
    p.add_argument("--discovery", help="optional explicit comma separated list")
    p.add_argument("--confirmation", help="optional explicit comma separated list")
    p.add_argument("--periods-per-year", type=int, required=True)
    p.add_argument("--grid", action="append", help="strategy:param=v1,v2 (repeat)")
    p.add_argument("--flatten-eod", action="store_true")
    p.add_argument("--holdout", type=float, default=0.2)
    p.add_argument("--train-days", type=int, default=20)
    p.add_argument("--test-days", type=int, default=5)
    p.add_argument("--null-shuffles", type=int, default=5)
    p.add_argument("--max-rounds", type=int, default=3)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--commission-bps", type=float)
    p.add_argument("--spread-bps", type=float)
    p.add_argument("--slippage-bps", type=float)
    p.add_argument("--fixed-fee", type=float, default=0.0)
    sub.add_parser("discover", parents=[common])
    sub.add_parser("confirm", parents=[common])
    f = sub.add_parser("final", parents=[common])
    f.add_argument("--strategy", required=True)
    am = sub.add_parser("amend", parents=[common])
    am.add_argument("--add-grid", action="append")
    am.add_argument("--add-ticker", action="append", help="role:SYMBOL, role is discovery or confirmation")
    sub.add_parser("status", parents=[common])
    sub.add_parser("verify", parents=[common])
    a = ap.parse_args(argv)
    try:
        if a.cmd == "plan":
            if None in (a.commission_bps, a.spread_bps, a.slippage_bps):
                raise ValueError("a cost model is required: give --commission-bps, --spread-bps and "
                                 "--slippage-bps (0 is allowed if that is really your cost)")
            roles = None
            if a.discovery or a.confirmation:
                roles = {t: "discovery" for t in (a.discovery or "").split(",") if t}
                roles.update({t: "confirmation" for t in (a.confirmation or "").split(",") if t})
            out = make_plan(a.dir, a.store, a.interval, [t for t in a.tickers.split(",") if t], a.periods_per_year,
                            {"commission_bps": a.commission_bps, "spread_bps": a.spread_bps,
                             "slippage_bps": a.slippage_bps, "fixed_fee": a.fixed_fee},
                            _grids(a.grid), a.flatten_eod, a.holdout, a.train_days, a.test_days, a.null_shuffles,
                            a.max_rounds, a.seed, roles=roles)
        elif a.cmd == "discover":
            out = discover(a.dir)
        elif a.cmd == "confirm":
            out = confirm(a.dir)
        elif a.cmd == "final":
            out = final(a.dir, a.strategy)
        elif a.cmd == "amend":
            tick = {}
            for item in a.add_ticker or []:
                role, _, sym = item.partition(":")
                tick[sym] = role
            out = amend(a.dir, add_grids=_grids(a.add_grid), add_tickers=tick)
        elif a.cmd == "status":
            out = status(a.dir)
        else:
            out = {"entries_verified": ledger_verify(a.dir)}
    except (ValueError, OSError) as e:
        print("Refused: %s" % e, file=sys.stderr)
        return 2
    out = dict(out) if isinstance(out, dict) else out
    out["note"] = "A test of whether a past result is likely to be luck, not a prediction. Taxes not included."
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
