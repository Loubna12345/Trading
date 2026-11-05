#!/usr/bin/env python3
"""Run a strategy over a bars file with the backtester and print the report. Standard library only.

    python3 run.py list
    python3 run.py run --strategy orb --param range_bars=6 --bars trader/history/SAP.DE_5m.csv \
        --periods-per-year 25704 --flatten-eod --commission-bps 0 --spread-bps 2 --slippage-bps 1

No price data and no default costs are assumed: bars come from a file, costs from you.
"""

import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


strategies = _load(os.path.join(HERE, "strategies.py"), "trader_strategies")
engine = _load(os.path.join(ROOT, "trader-backtest", "engine.py"), "trader_backtest_engine_run")


def parse_params(items):
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("--param %r must look like name=value" % item)
        k, v = item.split("=", 1)
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                raise ValueError("--param %s=%s: the value must be a number" % (k, v))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a day-trading rule over saved bars. Not financial advice.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show the strategies and why each might work")
    r = sub.add_parser("run")
    r.add_argument("--strategy", required=True)
    r.add_argument("--param", action="append", help="name=value, repeat for more")
    r.add_argument("--bars", required=True)
    r.add_argument("--periods-per-year", type=int, required=True,
                   help="bars per year: bars per day times trading days (252 for stocks, 365 for crypto)")
    r.add_argument("--capital", type=float, default=10000.0)
    r.add_argument("--delay", type=int, default=1)
    r.add_argument("--flatten-eod", action="store_true", help="close every position at the end of each day")
    r.add_argument("--commission-bps", type=float)
    r.add_argument("--spread-bps", type=float)
    r.add_argument("--slippage-bps", type=float)
    r.add_argument("--fixed-fee", type=float, default=0.0)
    a = ap.parse_args(argv)

    if a.cmd == "list":
        for name, info in sorted(strategies.REGISTRY.items()):
            print("%s  (%s)\n    why: %s\n    parameters: %s" % (
                name, ", ".join(info["asset_classes"]), info["reason"],
                "; ".join("%s = %s" % kv for kv in info["params"].items())))
        return 0

    if None in (a.commission_bps, a.spread_bps, a.slippage_bps):
        print("A cost model is required: give --commission-bps, --spread-bps and --slippage-bps "
              "(0 is allowed if that is really your cost).", file=sys.stderr)
        return 2
    try:
        params = parse_params(a.param)
        strat = strategies.make(a.strategy, **params)
        costs = engine.make_costs(commission_bps=a.commission_bps, spread_bps=a.spread_bps,
                                  slippage_bps=a.slippage_bps, fixed_fee=a.fixed_fee)
        bars = engine.load_bars_csv(a.bars)
        rep = engine.report(bars, strat, costs, a.periods_per_year, capital=a.capital, delay=a.delay,
                            flatten_eod=a.flatten_eod)
    except ValueError as e:
        print("Refused: %s" % e, file=sys.stderr)
        return 2
    rep.pop("trades")
    rep.pop("equity")
    rep.update({"strategy_name": a.strategy, "params": params, "bars": len(bars),
                "reason": strategies.REGISTRY[a.strategy]["reason"], "flatten_eod": a.flatten_eod,
                "note": "A replay on past data after the stated costs, not a prediction. Taxes not included."})
    print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
