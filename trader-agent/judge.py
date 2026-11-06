#!/usr/bin/env python3
"""Judge an agent against a rule from the outside. Standard library only.

The agent (see agent.py) and one named rule are run through the same backtester, the same costs, the same
one-bar delay and the same end-of-day flatten, on the same anonymized weeks. A flat arm (never trade) and the
stock itself (before costs) are shown beside them. The judge is this code, not Claude and not the agent, and its
criteria are written to the replay's tamper-evident ledger BEFORE anything is run:

  lock    window (open weeks or sealed weeks), the rule and its parameters, the model, the prompt hash, the
          budget, the random seed and the verdict criteria. One lock per window.
  run     asks the agent (budget-capped, answers cached in its own ledger), scores the arms and logs the result.
          The open window may be rerun for free from the cache; the sealed window runs ONCE.
  status  shows the locks and results.

Verdict (criteria locked in advance): needs at least min_weeks weeks; the difference between the two arms'
weekly returns must be better than luck by a paired bootstrap (one-sided p below alpha), the total-return gap
must be at least min_total_gap, and the winner must itself have made money after costs. Anything else is
reported as NO CLEAR DIFFERENCE, and too few weeks as INCONCLUSIVE.

The sealed window is the replay's final test. Running it here uses it up for the agent and the chosen rule
together, so freeze the rule first and run the open window before the sealed one.
"""

import argparse
import importlib.util
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


replay = _load(os.path.join(ROOT, "trader-replay", "replay.py"), "trader_replay_judge")
agent = _load(os.path.join(HERE, "agent.py"), "trader_agent_judge")
engine = replay.engine
T = replay.T

DEFAULT_CRITERIA = {"min_weeks": 20, "alpha": 0.05, "min_total_gap": 0.01}
WINDOWS = ("open", "sealed")


# ---------- statistics ----------

def paired_p_better(a, b, seed, resamples=2000):
    """One-sided p-value that arm a is better than arm b: the share of bootstrap resamples of the weekly
    differences whose mean is not above zero. Small means a is better than luck explains."""
    if len(a) != len(b) or not a:
        raise ValueError("the two arms need the same, non-zero number of weeks")
    diff = [x - y for x, y in zip(a, b)]
    rnd = random.Random(seed)
    n = len(diff)
    bad = 0
    for _ in range(resamples):
        if sum(diff[rnd.randrange(n)] for _ in range(n)) <= 1e-15:
            bad += 1
    return bad / resamples


def verdict(s, crit):
    """(label, reason) from weeks, agent_total, rule_total, p_agent_better, p_rule_better."""
    if s["weeks"] < crit["min_weeks"]:
        return "INCONCLUSIVE", "only %d weeks; the criteria need at least %d" % (s["weeks"], crit["min_weeks"])
    gap = s["agent_total"] - s["rule_total"]
    if (s["p_agent_better"] < crit["alpha"] and gap >= crit["min_total_gap"] and s["agent_total"] > 0):
        return "AGENT BETTER", "ahead by %.2f points with p=%.3f and a profit after costs" % (gap * 100, s["p_agent_better"])
    if (s["p_rule_better"] < crit["alpha"] and -gap >= crit["min_total_gap"] and s["rule_total"] > 0):
        return "RULE BETTER", "ahead by %.2f points with p=%.3f and a profit after costs" % (-gap * 100, s["p_rule_better"])
    return "NO CLEAR DIFFERENCE", ("gap %.2f points, p(agent better)=%.3f, p(rule better)=%.3f; not enough to tell "
                                   "them apart, or the leader did not make money after costs"
                                   % (gap * 100, s["p_agent_better"], s["p_rule_better"]))


# ---------- the lock ----------

def _locks(entries):
    return [e for e in entries if e["kind"] == "comparison_lock"]


def _results(entries):
    return [e for e in entries if e["kind"] == "comparison_result"]


def _check_sealed_allowed(entries, rule_key):
    """The sealed weeks are the replay's one final test: only for a rule frozen in the replay, and only if that
    rule has not had its replay final yet (the replay in turn refuses a final after a sealed comparison)."""
    if not any(e["kind"] == "prediction" and e["rule"] == rule_key for e in entries):
        raise ValueError("%s was never written down for a week in this replay; the sealed window is for a frozen "
                         "rule only" % rule_key)
    if any(e["kind"] == "final" and e["rule"] == rule_key for e in entries):
        raise ValueError("%s already had its replay final test on the sealed weeks" % rule_key)


def lock(folder, window, rule, rule_params, model, budget, first_week=None, last_week=None, seed=1,
         criteria=None):
    plan, blind, weeks, entries = replay._open(folder)
    if window not in WINDOWS:
        raise ValueError("window must be 'open' or 'sealed'")
    if any(e["window"] == window for e in _locks(entries)):
        raise ValueError("the %s window is already locked; criteria are not rewritten" % window)
    agent.price_for(model)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        raise ValueError("budget must be a positive number of US dollars")
    replay._check_params(rule_params)
    replay._fn(rule, rule_params)
    n_open = plan["weeks"] - plan["sealed_weeks"]
    if window == "open":
        first = 2 if first_week is None else first_week
        last = n_open if last_week is None else last_week
        if not (isinstance(first, int) and isinstance(last, int) and 1 <= first <= last <= n_open):
            raise ValueError("the open window must lie inside weeks 1..%d" % n_open)
    else:
        _check_sealed_allowed(entries, replay._rule_key(rule, rule_params))
        first, last = n_open + 1, plan["weeks"]
        if (first_week not in (None, first)) or (last_week not in (None, last)):
            raise ValueError("the sealed window is always weeks %d..%d" % (first, last))
    crit = dict(DEFAULT_CRITERIA)
    crit.update(criteria or {})
    if sorted(crit) != sorted(DEFAULT_CRITERIA):
        raise ValueError("criteria keys are %s" % sorted(DEFAULT_CRITERIA))
    entry = {"kind": "comparison_lock", "window": window, "first_week": first, "last_week": last,
             "rule": rule, "rule_params": rule_params, "rule_key": replay._rule_key(rule, rule_params),
             "model": model, "prompt_version": agent.PROMPT_VERSION, "instructions_hash": agent.instructions_hash(),
             "request": agent.request_settings(),
             "decision_bar": agent.DECISION_BAR, "lookback_days": agent.LOOKBACK_DAYS,
             "budget": float(budget), "seed": seed, "criteria": crit}
    return T.ledger_append(folder, entry)


# ---------- the run ----------

def _arm(eq, blind, weeks, first, last, trades):
    a, b = weeks[first - 1][0], weeks[last - 1][1]
    base = eq[a - 1] if a > 0 else replay.CAPITAL
    wk = []
    for w in range(first, last + 1):
        s, e = weeks[w - 1]
        start = eq[s - 1] if s > 0 else replay.CAPITAL
        wk.append(eq[e - 1] / start - 1.0)
    return {"total_return": eq[b - 1] / base - 1.0, "weekly": wk, "trades": trades}


def _trades_since(run, t0):
    return sum(1 for t in run["trades"] if t["exit_t"] >= t0)


def run(folder, client, window=None):
    plan, blind, weeks, entries = replay._open(folder)
    locks = {e["window"]: e for e in _locks(entries)}
    if not locks:
        raise ValueError("no comparison is locked; run `lock` first so the criteria are fixed before any run")
    if window is None:
        if len(locks) > 1:
            raise ValueError("both windows are locked; say which one to run (open or sealed)")
        window = next(iter(locks))
    if window not in locks:
        raise ValueError("the %s window is not locked" % window)
    lk = locks[window]
    if window == "sealed" and any(e["window"] == "sealed" for e in _results(entries)):
        raise ValueError("the sealed window runs once; its result is already in the ledger")
    if window == "sealed":
        _check_sealed_allowed(entries, lk["rule_key"])
    if lk["instructions_hash"] != agent.instructions_hash() or lk["prompt_version"] != agent.PROMPT_VERSION:
        raise ValueError("the agent's instructions changed after the lock; start a new replay folder to change them")
    first, last = lk["first_week"], lk["last_week"]
    a, b = weeks[first - 1][0], weeks[last - 1][1]
    costs = plan["costs"]
    strat = agent.make_agent(folder, client, lk["model"], costs, budget=lk["budget"], active_from=a)
    ag_run = engine.backtest(blind[:b], strat, costs, capital=replay.CAPITAL, flatten_eod=True)
    rule_run = engine.backtest(blind[:b], replay._fn(lk["rule"], lk["rule_params"]), costs, capital=replay.CAPITAL,
                               flatten_eod=True)
    t0 = blind[a]["t"]
    arms = {"agent": _arm(ag_run["equity"], blind, weeks, first, last, _trades_since(ag_run, t0)),
            "rule": _arm(rule_run["equity"], blind, weeks, first, last, _trades_since(rule_run, t0))}
    n = last - first + 1
    arms["flat"] = {"total_return": 0.0, "weekly": [0.0] * n, "trades": 0}
    hold_weekly = []
    for w in range(first, last + 1):
        s, e = weeks[w - 1]
        hold_weekly.append(blind[e - 1]["c"] / blind[s]["o"] - 1.0)
    arms["hold"] = {"total_return": blind[b - 1]["c"] / blind[a]["o"] - 1.0, "weekly": hold_weekly, "trades": 1,
                    "note": "the stock itself, before any cost"}
    ag, ru = arms["agent"]["weekly"], arms["rule"]["weekly"]
    stats = {"weeks": n, "agent_total": arms["agent"]["total_return"], "rule_total": arms["rule"]["total_return"],
             "p_agent_better": paired_p_better(ag, ru, lk["seed"]),
             "p_rule_better": paired_p_better(ru, ag, lk["seed"])}
    label, why = verdict(stats, lk["criteria"])
    res = {"kind": "comparison_result", "window": window, "first_week": first, "last_week": last, "weeks": n,
           "model": lk["model"], "rule_key": lk["rule_key"], "costs": costs, "criteria": lk["criteria"],
           "agent": arms["agent"], "rule": arms["rule"], "flat": arms["flat"], "hold": arms["hold"],
           "p_agent_better": stats["p_agent_better"], "p_rule_better": stats["p_rule_better"],
           "verdict": label, "reason": why, "agent_stats": dict(strat.stats)}
    T.ledger_append(folder, res)
    out = dict(res)
    del out["kind"]
    return out


def status(folder):
    plan, blind, weeks, entries = replay._open(folder)
    ent_ok = T.ledger_verify(os.path.join(folder, "agent")) if os.path.exists(
        T.ledger_path(os.path.join(folder, "agent"))) else 0
    return {"locks": [{k: e[k] for k in ("window", "first_week", "last_week", "rule_key", "model", "budget",
                                         "criteria")} for e in _locks(entries)],
            "results": [{k: e[k] for k in ("window", "weeks", "verdict", "reason")} for e in _results(entries)],
            "agent_decisions_logged": ent_ok, "ledger_ok": True}


# ---------- command line ----------

def main(argv=None):
    p = argparse.ArgumentParser(description="Judge a model against a rule on the same anonymized weeks.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("lock")
    s.add_argument("--dir", required=True)
    s.add_argument("--window", required=True, choices=WINDOWS)
    s.add_argument("--rule", required=True)
    s.add_argument("--param", action="append")
    s.add_argument("--model", required=True)
    s.add_argument("--budget-usd", dest="budget", type=float, required=True,
                   help="hard cap in US dollars")
    s.add_argument("--first-week", type=int)
    s.add_argument("--last-week", type=int)
    s.add_argument("--seed", type=int, default=1)
    r = sub.add_parser("run")
    r.add_argument("--dir", required=True)
    r.add_argument("--window", choices=WINDOWS)
    st = sub.add_parser("status")
    st.add_argument("--dir", required=True)
    args = p.parse_args(argv)
    try:
        if args.cmd == "lock":
            e = lock(args.dir, args.window, args.rule, replay._params(args.param), args.model, args.budget,
                     first_week=args.first_week, last_week=args.last_week, seed=args.seed)
            out = {k: e[k] for k in ("window", "first_week", "last_week", "rule_key", "model", "budget",
                                     "criteria", "instructions_hash")}
        elif args.cmd == "run":
            plan, _, _, entries = replay._open(args.dir)
            wins = {e["window"]: e for e in _locks(entries)}
            w = args.window or (next(iter(wins)) if len(wins) == 1 else None)
            if w not in wins:
                raise ValueError("lock a window first, or say which one to run")
            out = run(args.dir, agent.AnthropicClient(wins[w]["model"]), window=w)
            for arm in ("agent", "rule", "flat", "hold"):
                out[arm] = {k: v for k, v in out[arm].items() if k != "weekly"}
        else:
            out = status(args.dir)
    except agent.BudgetExceeded as e:
        print("Stopped: %s" % e, file=sys.stderr)
        return 3
    except ValueError as e:
        print("Refused: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
