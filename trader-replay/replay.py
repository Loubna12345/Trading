#!/usr/bin/env python3
"""Replay past bars one week at a time, as if each week were the future. Standard library only.

Past data is useful as practice only if the practitioner does not know what comes next. This tool enforces the
order:

  1. `start` hides the stock and the dates: prices are rebased so the first open is 100, volume is divided by its
     median, dates become consecutive made-up weekdays, and the days are cut into weeks of equal length. The
     last third of the weeks is sealed.
  2. Week 1 is free to read. For every later week the rule for that week must be written down first
     (`predict`), which puts it in a tamper-evident ledger. Only then does `reveal` show the week and score the
     rule on it, with the same backtester, costs and delay as everywhere else in this project.
  3. Sealed weeks cannot be read or predicted. `final` runs one frozen rule on them, once per rule, and reports how
     many rules were tried along the way.

Limits that cannot be coded away: the source file stays on the computer, so anybody who opens it can look ahead;
the order is enforced by this tool, not by the file system. A person or model that already knows how the real
period went may remember it despite the hidden names and dates. Prices that move very differently from the
rest of the market can still be recognised. Treat a replay result as practice, not as proof.

Commands:
  start   SOURCE.csv --dir D --commission-bps B --spread-bps S --slippage-bps L --fixed-fee F --periods-per-year N
  show    --dir D --week K
  predict --dir D --week K --strategy NAME --param key=value ... --expect-return R --expect-market up|down\n          [--note TEXT]     (NAME may be `none`)
  reveal  --dir D --week K
  final   --dir D --strategy NAME --param key=value ...
  status  --dir D
  relocate --dir D --source PATH   (the same data file in a new place; checked by SHA-256 and logged)
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _load(os.path.join(ROOT, "trader-training", "train.py"), "trader_train_replay")
engine = T.engine
strategies = T.strategies

CAPITAL = 10000.0
MIN_WEEKS = 12
MIN_OPEN_WEEKS = 4
COST_KEYS = ("commission_bps", "spread_bps", "slippage_bps", "fixed_fee")
PLAN = "plan.json"


# ---------- hiding the stock ----------

def anonymize(bars, days_per_week):
    """(blind bars, [(first, end_exclusive)] per week). Whole weeks only; left-over days are dropped."""
    engine.check_bars(bars)
    if not isinstance(days_per_week, int) or isinstance(days_per_week, bool) or days_per_week < 1:
        raise ValueError("days_per_week must be a whole number of at least 1")
    days = T.V.split_days(bars)
    n_weeks = len(days) // days_per_week
    if n_weeks < 1:
        raise ValueError("not enough days for one week")
    kept = days[:n_weeks * days_per_week]
    end = kept[-1][2]
    scale = 100.0 / bars[0]["o"]
    vols = sorted(b.get("v", 0.0) for b in bars[:end] if b.get("v", 0.0) > 0)
    med = vols[len(vols) // 2] if vols else 1.0
    fake = datetime.date(2001, 1, 1)   # a Monday
    blind, weeks = [], []
    for g in range(n_weeks):
        first = len(blind)
        for _, a, b in kept[g * days_per_week:(g + 1) * days_per_week]:
            for x in bars[a:b]:
                blind.append({"t": fake.isoformat() + x["t"][10:], "o": x["o"] * scale, "h": x["h"] * scale,
                              "l": x["l"] * scale, "c": x["c"] * scale, "v": x.get("v", 0.0) / med})
            fake += datetime.timedelta(days=1)
            while fake.weekday() >= 5:
                fake += datetime.timedelta(days=1)
        weeks.append((first, len(blind)))
    return blind, weeks


# ---------- the plan ----------

def _check_costs(costs):
    if not isinstance(costs, dict) or sorted(costs) != sorted(COST_KEYS):
        raise ValueError("costs must give all of %s; none is guessed" % list(COST_KEYS))
    return engine.make_costs(**costs)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _plan_hash(plan):
    return hashlib.sha256(T._canon(plan).encode("utf-8")).hexdigest()


def start(source, folder, costs, periods_per_year, days_per_week=5, holdout=0.34):
    costs = _check_costs(costs)
    if not isinstance(periods_per_year, (int, float)) or isinstance(periods_per_year, bool) or periods_per_year <= 0:
        raise ValueError("periods_per_year must be a positive number")
    if not isinstance(holdout, (int, float)) or not 0.1 <= holdout <= 0.5:
        raise ValueError("holdout must be between 0.1 and 0.5 of the weeks")
    if os.path.exists(T.ledger_path(folder)):
        raise ValueError("%s already holds a replay; use a new folder (a replay is never restarted)" % folder)
    bars = engine.load_bars_csv(source)
    blind, weeks = anonymize(bars, days_per_week)
    if len(weeks) < MIN_WEEKS:
        raise ValueError("need at least %d full weeks, have %d" % (MIN_WEEKS, len(weeks)))
    sealed = math.ceil(len(weeks) * holdout)
    if len(weeks) - sealed < MIN_OPEN_WEEKS:
        raise ValueError("fewer than %d open weeks would be left" % MIN_OPEN_WEEKS)
    plan = {"source": os.path.abspath(source), "source_sha256": _sha256_file(source), "costs": costs,
            "periods_per_year": periods_per_year, "days_per_week": days_per_week, "holdout": holdout,
            "weeks": len(weeks), "sealed_weeks": sealed}
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, PLAN), "w", encoding="utf-8") as fh:
        json.dump(plan, fh, sort_keys=True, indent=1)
    T.ledger_append(folder, {"kind": "start", "plan_hash": _plan_hash(plan)})
    return {"weeks": len(weeks), "sealed_weeks": sealed, "open_weeks": len(weeks) - sealed,
            "bars_per_week": len(blind) // len(weeks), "days_per_week": days_per_week}


def _checked_plan(folder):
    """(plan, ledger entries) after checking the ledger chain and that the plan is the one logged at start."""
    path = os.path.join(folder, PLAN)
    if not os.path.exists(path) or not os.path.exists(T.ledger_path(folder)):
        raise ValueError("%s holds no replay; run start first" % folder)
    T.ledger_verify(folder)
    entries = T.ledger_read(folder)
    with open(path, encoding="utf-8") as fh:
        plan = json.load(fh)
    if not entries or entries[0].get("kind") != "start" or entries[0].get("plan_hash") != _plan_hash(plan):
        raise ValueError("the plan was changed after start")
    return plan, entries


def _source(plan, entries):
    """Where the data is now: the latest `relocate` in the ledger, else the path given at start."""
    moves = [e["source"] for e in entries if e.get("kind") == "relocate"]
    return moves[-1] if moves else plan["source"]


def relocate(folder, new_source):
    """Point the replay at the same data file in a new place (another computer, a moved folder). The file must
    be byte for byte the one used at start (same SHA-256). The plan is not touched; the move is logged."""
    plan, entries = _checked_plan(folder)
    new_source = os.path.abspath(new_source)
    if not os.path.exists(new_source):
        raise ValueError("no file at %s" % new_source)
    if _sha256_file(new_source) != plan["source_sha256"]:
        raise ValueError("%s is not the same data as at start (its SHA-256 differs); a replay cannot switch data"
                         % new_source)
    T.ledger_append(folder, {"kind": "relocate", "source": new_source})
    return {"source": new_source, "sha256": plan["source_sha256"]}


def _open(folder):
    """(plan, blind bars, weeks, ledger entries) after checking the ledger chain, the plan and the data."""
    plan, entries = _checked_plan(folder)
    source = _source(plan, entries)
    if not os.path.exists(source):
        raise ValueError("the source data is not at %s; if the same file is elsewhere, run relocate --source "
                         "PATH (it must be byte for byte the file used at start)" % source)
    if _sha256_file(source) != plan["source_sha256"]:
        raise ValueError("the source data changed since start")
    blind, weeks = anonymize(engine.load_bars_csv(source), plan["days_per_week"])
    if len(weeks) != plan["weeks"]:
        raise ValueError("the source data no longer cuts into the same weeks")
    return plan, blind, weeks, entries


def _state(plan, entries):
    results = [e for e in entries if e["kind"] == "result"]
    preds = [e for e in entries if e["kind"] == "prediction"]
    return {"revealed": max([e["week"] for e in results] or [1]), "open": plan["weeks"] - plan["sealed_weeks"],
            "predictions": preds, "results": results, "finals": [e for e in entries if e["kind"] == "final"]}


def _fn(name, params):
    if name == "none":
        if params:
            raise ValueError("`none` takes no parameters")
        return lambda hist, pos: 0
    return strategies.make(name, **params)


def _rule_key(name, params):
    return name + json.dumps(params, sort_keys=True, separators=(",", ":"))


def _check_params(params):
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    for k, v in params.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("parameter %s must be a number" % k)


# ---------- reading, predicting, revealing ----------

def show(folder, week):
    plan, blind, weeks, entries = _open(folder)
    st = _state(plan, entries)
    if not isinstance(week, int) or not 1 <= week <= plan["weeks"]:
        raise ValueError("week must be between 1 and %d" % plan["weeks"])
    if week > st["open"]:
        raise ValueError("week %d is sealed; only `final` may touch the sealed weeks" % week)
    if week > st["revealed"]:
        raise ValueError("week %d is not revealed yet: write the rule for it first (predict), then reveal" % week)
    a, b = weeks[week - 1]
    return [dict(x) for x in blind[a:b]]


def _check_forecast(expect_return, expect_market):
    if (isinstance(expect_return, bool) or not isinstance(expect_return, (int, float))
            or not math.isfinite(expect_return) or abs(expect_return) > 1.0):
        raise ValueError("expect_return must be the week's net return you expect for this rule, as a fraction "
                         "(0.004 means +0.4%), between -1 and 1")
    if expect_market not in ("up", "down"):
        raise ValueError("expect_market must be 'up' or 'down': where the stock itself (buy and hold) ends the week")


def predict(folder, week, strategy, params, expect_return, expect_market, note=""):
    plan, blind, weeks, entries = _open(folder)
    st = _state(plan, entries)
    _check_params(params)
    _check_forecast(expect_return, expect_market)
    if week > st["open"] and isinstance(week, int) and week <= plan["weeks"]:
        raise ValueError("week %d is sealed; only `final` may touch the sealed weeks" % week)
    if week != st["revealed"] + 1 or week > st["open"]:
        raise ValueError("the next week to predict is %d (revealed through %d)" % (st["revealed"] + 1, st["revealed"]))
    if any(e["week"] == week for e in st["predictions"]):
        raise ValueError("week %d already has a prediction; it cannot be rewritten" % week)
    _fn(strategy, params)          # refuses unknown rules and missing parameters before anything is logged
    return T.ledger_append(folder, {"kind": "prediction", "week": week, "strategy": strategy, "params": params,
                                    "rule": _rule_key(strategy, params),
                                    "expect_return": expect_return, "expect_market": expect_market,
                                    "note": str(note)[:1000]})


def _score(plan, blind, a, b, strategy, params):
    return _score_fn(plan, blind, a, b, _fn(strategy, params))


def _score_fn(plan, blind, a, b, fn):
    """Score any decision function (hist, position) -> 0/1 on bars a..b the same way a rule is scored."""
    costs = plan["costs"]
    run = engine.backtest(blind[:b], fn, costs, capital=CAPITAL, flatten_eod=True)
    eq = run["equity"]
    start_eq = eq[a - 1] if a > 0 else CAPITAL
    t0 = blind[a]["t"]
    adv = (costs["spread_bps"] / 2.0 + costs["slippage_bps"]) / 1e4
    paid = 0.0
    for f in run["fills"]:
        if f["t"] >= t0:
            ref = f["price"] / (1.0 + f["side"] * adv)
            paid += f["cost"] + f["qty"] * abs(f["price"] - ref)
    bh = engine.buy_and_hold(blind[a:b], costs, capital=CAPITAL)
    return {"strategy_return": eq[b - 1] / start_eq - 1.0, "buy_and_hold_return": bh["equity"][-1] / CAPITAL - 1.0,
            "trades": sum(1 for t in run["trades"] if t["exit_t"] >= t0), "costs_paid": paid, "bars": b - a}


def reveal(folder, week):
    plan, blind, weeks, entries = _open(folder)
    st = _state(plan, entries)
    if week != st["revealed"] + 1 or week > st["open"]:
        raise ValueError("the next week to reveal is %d (revealed through %d)" % (st["revealed"] + 1, st["revealed"]))
    pred = [e for e in st["predictions"] if e["week"] == week]
    if not pred:
        raise ValueError("write the rule for week %d first (predict); the ledger must hold it before the reveal" % week)
    a, b = weeks[week - 1]
    res = _score(plan, blind, a, b, pred[0]["strategy"], pred[0]["params"])
    actual_market = "up" if res["buy_and_hold_return"] > 0 else "down"
    res.update({"week": week, "kind": "result", "rule": pred[0]["rule"],
                "expected_return": pred[0]["expect_return"],
                "error": res["strategy_return"] - pred[0]["expect_return"],
                "market_expected": pred[0]["expect_market"], "market_actual": actual_market,
                "direction_hit": pred[0]["expect_market"] == actual_market})
    T.ledger_append(folder, res)
    return res


def final(folder, strategy, params):
    plan, blind, weeks, entries = _open(folder)
    st = _state(plan, entries)
    _check_params(params)
    key = _rule_key(strategy, params)
    tried = sorted({e["rule"] for e in st["predictions"]})
    if key not in tried:
        raise ValueError("this rule was never written down for a week; the final test is for frozen rules only")
    if any(e["rule"] == key for e in st["finals"]):
        raise ValueError("this rule already had its one final test")
    a = weeks[st["open"]][0]
    res = _score(plan, blind, a, len(blind), strategy, params)
    res.update({"kind": "final", "rule": key, "weeks": plan["sealed_weeks"], "rules_tried": len(tried)})
    T.ledger_append(folder, res)
    return res


def status(folder):
    plan, blind, weeks, entries = _open(folder)     # verifies the ledger; raises if it was changed
    st = _state(plan, entries)
    total, bh = 1.0, 1.0
    for e in st["results"]:
        total *= 1.0 + e["strategy_return"]
        bh *= 1.0 + e["buy_and_hold_return"]
    n = len(st["results"])
    hits = sum(1 for e in st["results"] if e["direction_hit"])
    forecast = {"weeks": n}
    if n:
        forecast.update({
            "mean_error": sum(e["error"] for e in st["results"]) / n,
            "mean_abs_error": sum(abs(e["error"]) for e in st["results"]) / n,
            "mean_abs_error_if_always_zero": sum(abs(e["strategy_return"]) for e in st["results"]) / n,
            "direction_hits": hits,
            "always_up_hits": sum(1 for e in st["results"] if e["market_actual"] == "up"),
            "direction_p_value_vs_coin": T.binom_tail(n, hits, 0.5),
            "note": "A forecast only has value if its error is clearly below the always-zero error and its hits "
                    "clearly beat both a coin and always saying up; with few weeks neither can be told apart."})
    return {"forecast": forecast, "weeks_total": plan["weeks"], "weeks_open": st["open"], "weeks_sealed": plan["sealed_weeks"],
            "revealed_through": st["revealed"], "predictions": len(st["predictions"]),
            "rules_tried": len({e["rule"] for e in st["predictions"]}),
            "weeks_scored": len(st["results"]), "strategy_total_return": total - 1.0,
            "buy_and_hold_total_return": bh - 1.0, "finals_run": len(st["finals"]),
            "ledger_ok": True, "ledger_entries": len(entries)}


# ---------- command line ----------

def _csv(bars):
    lines = ["t,o,h,l,c,v"]
    for b in bars:
        lines.append("%s,%r,%r,%r,%r,%r" % (b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]))
    return "\n".join(lines)


def _params(items):
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("--param must look like name=value, got %r" % item)
        k, v = item.split("=", 1)
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                raise ValueError("--param %s: %r is not a number" % (k, v))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Replay past bars one week at a time.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("source")
    s.add_argument("--dir", required=True)
    for k in ("commission-bps", "spread-bps", "slippage-bps", "fixed-fee"):
        s.add_argument("--" + k, type=float, required=True)
    s.add_argument("--periods-per-year", type=float, required=True)
    s.add_argument("--days-per-week", type=int, default=5)
    s.add_argument("--holdout", type=float, default=0.34)
    for name in ("show", "reveal"):
        x = sub.add_parser(name)
        x.add_argument("--dir", required=True)
        x.add_argument("--week", type=int, required=True)
    x = sub.add_parser("predict")
    x.add_argument("--dir", required=True)
    x.add_argument("--week", type=int, required=True)
    x.add_argument("--strategy", required=True)
    x.add_argument("--param", action="append")
    x.add_argument("--expect-return", type=float, required=True,
                   help="net return you expect from this rule this week, as a fraction (0.004 = +0.4%%)")
    x.add_argument("--expect-market", choices=("up", "down"), required=True,
                   help="where the stock itself ends the week")
    x.add_argument("--note", default="")
    x = sub.add_parser("final")
    x.add_argument("--dir", required=True)
    x.add_argument("--strategy", required=True)
    x.add_argument("--param", action="append")
    x = sub.add_parser("status")
    x.add_argument("--dir", required=True)
    x = sub.add_parser("relocate")
    x.add_argument("--dir", required=True)
    x.add_argument("--source", required=True, help="the same data file, in its new place")
    a = p.parse_args(argv)
    try:
        if a.cmd == "start":
            costs = {"commission_bps": a.commission_bps, "spread_bps": a.spread_bps,
                     "slippage_bps": a.slippage_bps, "fixed_fee": a.fixed_fee}
            out = start(a.source, a.dir, costs, a.periods_per_year, a.days_per_week, a.holdout)
            print(json.dumps(out, indent=2))
        elif a.cmd == "show":
            print(_csv(show(a.dir, a.week)))
        elif a.cmd == "predict":
            e = predict(a.dir, a.week, a.strategy, _params(a.param), a.expect_return, a.expect_market, a.note)
            print(json.dumps({"logged": e["n"], "week": e["week"], "rule": e["rule"]}, indent=2))
        elif a.cmd == "reveal":
            res = reveal(a.dir, a.week)
            print(json.dumps(res, indent=2))
            print()
            print(_csv(show(a.dir, a.week)))
        elif a.cmd == "final":
            print(json.dumps(final(a.dir, a.strategy, _params(a.param)), indent=2))
        elif a.cmd == "relocate":
            print(json.dumps(relocate(a.dir, a.source), indent=2))
        else:
            print(json.dumps(status(a.dir), indent=2))
    except ValueError as e:
        print("Refused: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
