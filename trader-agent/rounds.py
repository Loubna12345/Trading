#!/usr/bin/env python3
"""A round: several assets, each traded by the agent and by hand-picked rules over the same weeks. Standard library.

    python3 rounds.py setup --base ~/Documents/trading --name crypto1 --market crypto \
        --asset BTC-EUR=history/BTC-EUR_60m.csv --asset ETH-EUR=history/ETH-EUR_60m.csv \
        --commission-bps 9 --spread-bps 4 --slippage-bps 2 --periods-per-year 8760 --days-per-week 7 \
        --model claude-haiku-5-5 --budget-usd 1
    python3 rounds.py status --base ~/Documents/trading --name crypto1
    python3 rounds.py save   --base ~/Documents/trading --name crypto1 --reports reports --start-money 500

`setup` makes, per asset, a rules replay (positions kept overnight and from week to week) and an agent replay with
a locked agent (version 2; overnight for stocks, around the clock for crypto). The rules folders get neutral names
(s1, s2, ...) in an order shuffled by --seed, and their data is copied under those names, so whoever plays the
rules by hand does not learn which asset it is; the key is in `<round>/KEY-do-not-show-helpers.txt`. The agent
never sees folder names. The agents are then run with `weekly.py run --dir <round>/agent-<asset>`.

`save` saves every finished report of the round through archive.py (numbered, never overwritten, grouped under
the round's name); an asset whose agent or rules have not reached the last week is skipped and named.
"""

import argparse
import json
import os
import random
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import archive  # noqa: E402  (loaded from this folder)

weekly = archive.weekly
replay = weekly.replay
T = weekly.T

CONFIG = "round.json"
KEY = "KEY-do-not-show-helpers.txt"


def _slug(symbol):
    return re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-")


def setup(base, name, assets, market, costs, periods_per_year, days_per_week, model, budget, holdout=0.34,
          seed=1, first_week=2):
    """assets: [(symbol, csv path)]. Returns the round's config."""
    if market not in weekly.MARKETS:
        raise ValueError("market must be one of %s" % (weekly.MARKETS,))
    if not assets or len(set(s for s, _ in assets)) != len(assets):
        raise ValueError("give each asset once")
    root = os.path.join(base, name)
    if os.path.exists(root):
        raise ValueError("%s exists; a round is never set up twice (choose a new name)" % root)
    data = os.path.join(base, name + "-data")
    order = list(range(len(assets)))
    random.Random(seed).shuffle(order)
    os.makedirs(root)
    os.makedirs(data, exist_ok=True)
    cfg = {"name": name, "market": market, "costs": costs, "periods_per_year": periods_per_year,
           "days_per_week": days_per_week, "model": model, "budget": budget, "holdout": holdout, "assets": []}
    for k, idx in enumerate(order, 1):
        symbol, src = assets[idx]
        neutral = "s%d" % k
        copy = os.path.join(data, neutral + ".csv")
        shutil.copyfile(src, copy)
        os.chmod(copy, 0o600)
        rules = os.path.join(root, "rules-" + neutral)
        agent_dir = os.path.join(root, "agent-" + _slug(symbol))
        replay.start(source=copy, folder=rules, costs=costs, periods_per_year=periods_per_year,
                     days_per_week=days_per_week, holdout=holdout, overnight=True)
        replay.start(source=src, folder=agent_dir, costs=costs, periods_per_year=periods_per_year,
                     days_per_week=days_per_week, holdout=holdout)
        lk = weekly.lock(folder=agent_dir, model=model, budget=budget, first_week=first_week, overnight=True,
                         market=market)
        cfg["assets"].append({"symbol": symbol, "rules": os.path.basename(rules), "agent": os.path.basename(agent_dir),
                              "first_week": lk["first_week"], "last_week": lk["last_week"]})
    cfg["assets"].sort(key=lambda a: a["rules"])
    with open(os.path.join(root, CONFIG), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=1, sort_keys=True)
    with open(os.path.join(root, KEY), "w", encoding="utf-8") as fh:
        fh.write("".join("%s = %s\n" % (a["rules"][len("rules-"):], a["symbol"]) for a in cfg["assets"]))
    return cfg


def _config(base, name):
    with open(os.path.join(base, name, CONFIG), encoding="utf-8") as fh:
        return json.load(fh)


def _rules_weeks(folder):
    return sum(1 for e in T.ledger_read(folder) if e["kind"] == "result")


def status(base, name):
    cfg = _config(base, name)
    out = []
    for a in cfg["assets"]:
        root = os.path.join(base, name)
        out.append({"symbol": a["symbol"], "rules_folder": a["rules"],
                    "rules_weeks": _rules_weeks(os.path.join(root, a["rules"])),
                    "agent_weeks": weekly.status(os.path.join(root, a["agent"]))["weeks_done"],
                    "planned_weeks": a["last_week"] - a["first_week"] + 1})
    return out


def save(base, name, reports, start_money):
    cfg = _config(base, name)
    root = os.path.join(base, name)
    saved, skipped = [], []
    label_round = name
    for a in cfg["assets"]:
        rules = os.path.join(root, a["rules"])
        agent_dir = os.path.join(root, a["agent"])
        planned = a["last_week"] - a["first_week"] + 1
        rules_weeks = _rules_weeks(rules)
        agent_weeks = weekly.status(agent_dir)["weeks_done"]
        if rules_weeks < planned or agent_weeks < planned:
            skipped.append("%s (rules %d, agent %d of %d weeks)" % (a["symbol"], rules_weeks, agent_weeks, planned))
            continue
        kind = "crypto" if cfg["market"] == "crypto" else "overnight"
        saved.append(archive.save_rules(reports, rules, start_money, "%s, %s rules, played by a fresh helper "
                                        "(folder %s)" % (a["symbol"], kind, a["rules"]), round_name=label_round))
        saved.append(archive.save_agent(reports, agent_dir, start_money, "%s, agent v%s %s, %s" % (
            a["symbol"], weekly.WEEK_VERSION, kind, cfg["model"]), round_name=label_round))
        saved.append(archive.save_comparison(reports, agent_dir, rules, start_money, "%s: agent vs helper rules"
                                             % a["symbol"], round_name=label_round))
    return saved, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description="Set up and save a round of agent and rules runs.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--base", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--market", choices=weekly.MARKETS, required=True)
    s.add_argument("--asset", action="append", required=True, help="SYMBOL=path/to/bars.csv (relative to --base)")
    for k in ("commission-bps", "spread-bps", "slippage-bps", "periods-per-year"):
        s.add_argument("--" + k, type=float, required=True)
    s.add_argument("--fixed-fee", type=float, default=0.0)
    s.add_argument("--days-per-week", type=int, required=True)
    s.add_argument("--model", required=True)
    s.add_argument("--budget-usd", dest="budget", type=float, required=True)
    s.add_argument("--seed", type=int, default=1)
    for c in ("status", "save"):
        x = sub.add_parser(c)
        x.add_argument("--base", required=True)
        x.add_argument("--name", required=True)
        if c == "save":
            x.add_argument("--reports", required=True)
            x.add_argument("--start-money", type=float, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "setup":
            assets = []
            for spec in a.asset:
                if "=" not in spec:
                    raise ValueError("--asset takes SYMBOL=path")
                sym, path = spec.split("=", 1)
                assets.append((sym, path if os.path.isabs(path) else os.path.join(a.base, path)))
            costs = {"commission_bps": a.commission_bps, "spread_bps": a.spread_bps,
                     "slippage_bps": a.slippage_bps, "fixed_fee": a.fixed_fee}
            cfg = setup(a.base, a.name, assets, a.market, costs, a.periods_per_year, a.days_per_week, a.model,
                        a.budget, seed=a.seed)
            print(json.dumps({"round": cfg["name"], "assets": [x["agent"] for x in cfg["assets"]],
                              "rules_folders": [x["rules"] for x in cfg["assets"]]}, indent=1))
        elif a.cmd == "status":
            print(json.dumps(status(a.base, a.name), indent=1))
        else:
            saved, skipped = save(a.base, a.name, a.reports, a.start_money)
            print(json.dumps({"saved": [e["file"] for e in saved], "skipped": skipped}, indent=1))
    except (ValueError, OSError) as err:
        print("Refused: %s" % err, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
