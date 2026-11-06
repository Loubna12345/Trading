#!/usr/bin/env python3
"""The agent trades a replay week by week, day by day, and learns only through its own notebook.

Standard library only. The agent replaces the rules: no rule from the library is run. The order is the same as in
a hand-played replay, and this code enforces it:

  1. Before week k (k >= 2), the model gets the weeks already seen (anonymized moves in basis points), its own
     record so far (forecasts against results, after costs), last week's daily choices with their reasons and
     outcomes, and its notebook. It answers with a forecast for the week (its own net return and where the stock
     ends) and a rewritten notebook. That answer is written to the replay's tamper-evident ledger BEFORE any bar
     of week k reaches it.
  2. During week k, once per day after the day's first DECISION_BAR bars, it decides: hold to the close (1) or
     stay in cash (0). In an --overnight run it decides instead hold (1) or cash (0) until its next decision, the
     next trading day after DECISION_BAR bars, so a held position stays open overnight and over weekends. It sees its notebook, its weekly forecast and the bars up to that moment, nothing later.
     The order fills at the next bar's open; costs are charged like for every rule; the position is closed at the
     day's close.
  3. After week k the week is scored with the same backtester and costs as the hand-played rules, and the result
     (with every day's choice, its reason and whether it was right) is logged. Then week k+1 starts at step 1.

Nothing is lost when the run ends: every answer is in `<folder>/agent/ledger.jsonl`, every forecast and result in
the replay ledger (both hash-chained), and readable copies are written to `<folder>/memory/` after every week:
notebook.md (the latest notebook), notebook-history.md (every version), journal.md and journal.jsonl (every
forecast, plan, daily choice with its reason, and how right or wrong it was). A later run can start from a saved
notebook with `lock --notebook-from`.

A rerun reads every answer from the ledger and pays nothing, so an interrupted run (budget, network) continues
where it stopped. The comparison criteria are written in the lock, before anything runs.

    python3 weekly.py lock    --dir D --model claude-haiku-5-5 --budget-usd 2 [--first-week 2 --last-week 64]
                              [--notebook-from OLD/memory/notebook.md] [--overnight]
    python3 weekly.py run     --dir D
    python3 weekly.py status  --dir D
    python3 weekly.py memory  --dir D                  # rewrite the files in D/memory from the ledgers
    python3 weekly.py report  --dir D --out agent-report.html [--label TEXT]
    python3 weekly.py compare --dir D --rules-dir HAND --out comparison.html [--label TEXT]
"""

import argparse
import hashlib
import html
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


judge = _load(os.path.join(HERE, "judge.py"), "trader_judge_weekly")
agent = judge.agent
replay = judge.replay
report = _load(os.path.join(ROOT, "trader-replay", "report.py"), "trader_report_weekly")
engine = replay.engine
T = replay.T

WEEK_VERSION = "1"
NOTEBOOK_MAX = 3000
PLAN_MAX = 600
RECORD_WEEKS = 12          # past weeks shown one by one in the weekly prompt (totals cover all of them)

WEEK_INSTRUCTIONS = (
    "You are trading one stock, day by day, to learn whether you can make money after costs. The data is "
    "anonymized: you are not told which stock or which dates, and prices are shown only as moves in basis points "
    "(1 bp = 0.01%). Each trading day, after the day's first two hourly bars, you will decide whether to hold the "
    "stock until that day's close or stay in cash. You cannot short. Every trade side costs the amount stated below "
    "and every position is closed at the close. Before each week you write a forecast and rewrite your notebook. "
    "The notebook is your only memory from week to week: keep in it what you learned, which kinds of choices worked "
    "and which failed, and the rules you want to follow; drop what turned out wrong. Staying in cash is a valid "
    "choice and costs nothing."
)

DAY_INSTRUCTIONS = (
    "You are trading one anonymized stock, day by day, to learn whether you can make money after costs. Decide for "
    "today only: hold the stock from the next bar's open until today's close (1), or stay in cash (0). You cannot "
    "short. Use your notebook and your forecast for this week, written by you before the week began."
)


WEEK_INSTRUCTIONS_OVERNIGHT = (
    "You are trading one stock, day by day, to learn whether you can make money after costs. The data is "
    "anonymized: you are not told which stock or which dates, and prices are shown only as moves in basis points "
    "(1 bp = 0.01%). Each trading day, after the day's first two hourly bars, you decide whether to hold the stock "
    "or be in cash until your next decision, which comes the next trading day after its first two bars. Holding "
    "means you keep the stock overnight (and over the weekend after the last day of a week), so you get the moves "
    "between one day's close and the next day's open as well. You cannot short. Buying and selling each cost the "
    "amount stated below; keeping what you already have costs nothing. Before each week you write a forecast and "
    "rewrite your notebook. The notebook is your only memory from week to week: keep in it what you learned, which "
    "kinds of choices worked and which failed, and the rules you want to follow; drop what turned out wrong. Being "
    "in cash is a valid choice."
)

DAY_INSTRUCTIONS_OVERNIGHT = (
    "You are trading one anonymized stock, day by day, to learn whether you can make money after costs. Decide what "
    "to have until your next decision (the next trading day, after its first two bars): hold the stock (1), kept "
    "overnight, or be in cash (0). Buying or selling costs one trade side each; keeping what you have costs "
    "nothing. You cannot short. Use your notebook and your forecast for this week, written by you before the week "
    "began."
)


def _texts(overnight):
    return ((WEEK_INSTRUCTIONS_OVERNIGHT, DAY_INSTRUCTIONS_OVERNIGHT) if overnight
            else (WEEK_INSTRUCTIONS, DAY_INSTRUCTIONS))


def _overnight(lk):
    return bool(lk and lk.get("overnight"))


def context_hash(overnight=False):
    """Everything that shapes the agent's answers besides the model and the bars."""
    text = "\n".join([WEEK_VERSION, WEEK_INSTRUCTIONS, DAY_INSTRUCTIONS, agent.instructions_hash(),
                      str(agent.DECISION_BAR), str(NOTEBOOK_MAX), str(RECORD_WEEKS)])
    if overnight:
        text += "\novernight\n" + WEEK_INSTRUCTIONS_OVERNIGHT + "\n" + DAY_INSTRUCTIONS_OVERNIGHT
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- small helpers ----------

def _bps(x, ref):
    return (x / ref - 1.0) * 1e4


def _days(bars, a, b):
    """[(start, end_exclusive)] of the days in bars[a:b]."""
    out, s = [], a
    for i in range(a + 1, b + 1):
        if i == b or bars[i]["t"][:10] != bars[s]["t"][:10]:
            out.append((s, i))
            s = i
    return out


def _first_json(text):
    if not isinstance(text, str):
        raise ValueError("no text in the reply")
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text[i:])
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            return obj
        i = text.find("{", i + 1)
    raise ValueError("no JSON object in the reply")


def parse_week(text):
    obj = _first_json(text)
    r = obj.get("expect_return")
    if isinstance(r, bool) or not isinstance(r, (int, float)) or not math.isfinite(r) or abs(r) > 1.0:
        raise ValueError("expect_return must be a fraction between -1 and 1, got %r" % (r,))
    m = obj.get("expect_market")
    if m not in ("up", "down"):
        raise ValueError("expect_market must be 'up' or 'down', got %r" % (m,))
    nb = obj.get("notebook")
    if not isinstance(nb, str):
        raise ValueError("notebook must be text")
    return {"expect_return": float(r), "expect_market": m, "notebook": nb[:NOTEBOOK_MAX],
            "reason": str(obj.get("plan", ""))[:PLAN_MAX]}


def parse_day(text):
    pos, reason = agent.parse_answer(text)
    return {"position": pos, "reason": reason}


def _pct(x, d=2):
    return "%+.*f%%" % (d, x * 100)


# ---------- what the model is shown ----------

def week_prompt(plan, blind, weeks, week, notebook, results, overnight=False):
    side = agent.per_side_bps(plan["costs"])
    lines = [_texts(overnight)[0], "", "Cost of one trade side: %.1f bps." % side, "",
             "The stock so far, one line per week (week 1 is the first week of the data):"]
    for k in range(1, week):
        a, b = weeks[k - 1]
        ref = blind[a - 1]["c"] if a > 0 else blind[a]["o"]
        days = _days(blind, a, b)
        up = sum(1 for s, e in days if blind[e - 1]["c"] > blind[s]["o"])
        hi = max(x["h"] for x in blind[a:b])
        lo = min(x["l"] for x in blind[a:b])
        vol = sum(x["v"] for x in blind[a:b]) / (b - a)
        lines.append("Week %d: %+.0f bps from the previous close (opening gap %+.0f), range %.0f bps, %d of %d days "
                     "closed above their open, mean volume %.2f"
                     % (k, _bps(blind[b - 1]["c"], ref), _bps(blind[a]["o"], ref), _bps(hi, lo), up, len(days), vol))
    a, b = weeks[week - 2]
    lines += ["", "Last week (week %d) day by day:" % (week - 1)]
    for n, (s, e) in enumerate(_days(blind, a, b), 1):
        ref = blind[s - 1]["c"] if s > 0 else blind[s]["o"]
        steps, prev = [], ref
        for j in range(s, e):
            steps.append("%+.0f" % _bps(blind[j]["c"], prev))
            prev = blind[j]["c"]
        lines.append("Day %d: gap %+.0f bps, open to close %+.0f bps; bar-to-bar close moves (bps): %s"
                     % (n, _bps(blind[s]["o"], ref), _bps(blind[e - 1]["c"], blind[s]["o"]), " ".join(steps)))
    lines += ["", "Your record so far:"]
    done = [results[k] for k in sorted(results) if k < week]
    if not done:
        lines.append("(none yet: this is your first week)")
    else:
        tot, stock, hits = 1.0, 1.0, 0
        for r in done:
            tot *= 1.0 + r["strategy_return"]
            stock *= 1.0 + r["stock_move"]
            hits += 1 if r["direction_hit"] else 0
        miss = sum(abs(r["error"]) for r in done) / len(done)
        zero = sum(abs(r["strategy_return"]) for r in done) / len(done)
        lines.append("%d weeks: your total %s after costs; the stock %s; stock direction right %d of %d; average "
                     "miss of your return forecast %.2f%% (always forecasting zero would miss by %.2f%%)."
                     % (len(done), _pct(tot - 1.0), _pct(stock - 1.0), hits, len(done), miss * 100, zero * 100))
        for r in done[-RECORD_WEEKS:]:
            lines.append("Week %d: you forecast %s and the stock %s; you got %s after costs (%d trades, costs %.1f), "
                         "the stock moved %s (%s); days held %d of %d."
                         % (r["week"], _pct(r["expected_return"]), r["market_expected"], _pct(r["strategy_return"]),
                            r["trades"], r["costs_paid"], _pct(r["stock_move"]),
                            "direction right" if r["direction_hit"] else "direction wrong", r["days_held"],
                            len(r["days"])))
        last = done[-1]
        if last["week"] == week - 1:
            lines += ["", "Your choices last week, with your reasons and what happened:"]
            for d in last["days"]:
                if d.get("position") is None:
                    lines.append("Day %d: no decision (the day was too short)." % d["day"])
                    continue
                lines.append("Day %d: %s. Your reason: %s. From your entry price to %s the stock moved %+.0f "
                             "bps; %s"
                             % (d["day"], "held" if d["position"] else "stayed in cash", d["reason"] or "-",
                                "your next decision" if overnight else "the close",
                                d["move_bps"], ("your result %+.0f bps after costs, %s." %
                                                (d["result_bps"], "right" if d["right"] else "wrong")) if d["position"]
                                else ("holding would have given %+.0f bps after costs, so staying out was %s."
                                      % (d["hold_would_bps"], "right" if d["right"] else "wrong"))))
    lines += ["", "Your notebook now:", notebook if notebook.strip() else "(empty)", "",
              "Now write your forecast for week %d and your rewritten notebook (at most %d characters)." % (week,
                                                                                                        NOTEBOOK_MAX),
              'Answer with one JSON object and nothing else: {"expect_return": your net return for the week after '
              'costs as a fraction (0.004 = +0.4%), "expect_market": "up" or "down" (where the stock ends the '
              'week), "plan": "one or two sentences: how you will trade this week and why", "notebook": "..."}']
    return "\n".join(lines)


def day_prompt(hist, side, forecast, overnight=False, holding=False):
    body = agent.prompt_for(hist, side).split("\n", 1)[1]
    if overnight:
        body = ("You hold the stock now (kept from your last decision)." if holding
                else "You are in cash now.") + "\n" + body
    return "\n".join([_texts(overnight)[1], "", "Your notebook:", forecast["notebook"] or "(empty)", "",
                      "Your forecast for this week: your net return %s, the stock %s. Your plan: %s"
                      % (_pct(forecast["expect_return"]), forecast["expect_market"], forecast["plan"] or "-"),
                      body])


# ---------- the lock ----------

def _lock(entries):
    locks = [e for e in entries if e["kind"] == "agent_week_lock"]
    return locks[0] if locks else None


def lock(folder, model, budget, first_week=2, last_week=None, notebook_from=None, seed=1, criteria=None,
         overnight=False):
    plan, blind, weeks, entries = replay._open(folder)
    if _lock(entries):
        raise ValueError("this folder already has an agent run locked; use a new replay folder for a new run")
    if any(e["kind"] in ("prediction", "result", "final", "comparison_lock") for e in entries):
        raise ValueError("this replay folder was used for rules; start a fresh replay folder for the agent run")
    agent.price_for(model)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        raise ValueError("budget must be a positive number of US dollars")
    n_open = plan["weeks"] - plan["sealed_weeks"]
    last = n_open if last_week is None else last_week
    if not (isinstance(first_week, int) and isinstance(last, int) and 2 <= first_week <= last <= n_open):
        raise ValueError("weeks must lie inside 2..%d (week 1 is the free first look; sealed weeks stay sealed)"
                         % n_open)
    notebook = ""
    if notebook_from:
        with open(notebook_from, encoding="utf-8") as fh:
            notebook = _strip_notebook_header(fh.read())[:NOTEBOOK_MAX]
    crit = dict(judge.DEFAULT_CRITERIA)
    crit.update(criteria or {})
    if sorted(crit) != sorted(judge.DEFAULT_CRITERIA):
        raise ValueError("criteria keys are %s" % sorted(judge.DEFAULT_CRITERIA))
    return T.ledger_append(folder, {
        "kind": "agent_week_lock", "model": model, "budget": float(budget), "first_week": first_week,
        "last_week": last, "context_hash": context_hash(overnight), "overnight": bool(overnight),
        "request": agent.request_settings(),
        "decision_bar": agent.DECISION_BAR, "notebook_start": notebook,
        "notebook_start_from": os.path.basename(notebook_from) if notebook_from else None,
        "seed": seed, "criteria": crit})


def raise_budget(folder, budget):
    """A higher cap for a run that stopped at its budget. The budget changes no answer, so raising it is allowed;
    every change is logged. Lowering is refused."""
    plan, blind, weeks, entries = replay._open(folder)
    lk = _lock(entries)
    if not lk:
        raise ValueError("no agent run is locked here")
    now = _budget(entries)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= now:
        raise ValueError("the new budget must be above the current %.4f USD" % now)
    return T.ledger_append(folder, {"kind": "agent_week_budget", "budget": float(budget), "previous": now})


def _budget(entries):
    b = _lock(entries)["budget"]
    for e in entries:
        if e["kind"] == "agent_week_budget":
            b = e["budget"]
    return b


NOTEBOOK_HEADER = "<!-- agent notebook; the text below this line is what the agent wrote -->\n"


def _strip_notebook_header(text):
    if NOTEBOOK_HEADER in text:
        return text.split(NOTEBOOK_HEADER, 1)[1].strip()
    return text.strip()


# ---------- the run ----------

def _week_index(weeks, n):
    out = [0] * n
    for w, (a, b) in enumerate(weeks, 1):
        for i in range(a, b):
            out[i] = w
    return out


def _day_strategy(asker, side, week_of, forecasts, record, overnight=False):
    state = {"day": None, "pos": 0}

    def strategy(hist, position):
        n = len(hist)
        i = n - 1
        w = week_of[i]
        if w not in forecasts:
            return 0
        day = hist[i]["t"][:10]
        k = 1
        while k < n and hist[i - k]["t"][:10] == day:
            k += 1
        if day != state["day"]:
            state["day"] = day
            if not overnight:
                state["pos"] = 0
        if k < agent.DECISION_BAR:
            return state["pos"] if overnight else 0
        if k == agent.DECISION_BAR:
            e = asker.ask(day_prompt(hist, side, forecasts[w], overnight, state["pos"] == 1), "day_decision",
                          parse_day, {"position": state["pos"] if overnight else 0})
            state["pos"] = e["position"]
            record[i - agent.DECISION_BAR + 1] = (i, e)
        return state["pos"]

    return strategy


def _score_week(plan, blind, weeks, w, strategy, record, overnight=False):
    a, b = weeks[w - 1]
    costs = plan["costs"]
    head = replay._score_fn(plan, blind, a, b, strategy, flatten_eod=not overnight)
    side = agent.per_side_bps(costs)
    if overnight:
        return head, _overnight_days(blind, a, b, record, side)
    run = engine.backtest(blind[:b], strategy, costs, capital=replay.CAPITAL, flatten_eod=True)
    by_day = {t["entry_t"][:10]: t for t in run["trades"] if t["entry_t"] >= blind[a]["t"]}
    days = []
    for n, (s, e) in enumerate(_days(blind, a, b), 1):
        rec = record.get(s)
        if rec is None:
            days.append({"day": n, "position": None, "reason": "", "failed": False})
            continue
        i_dec, ans = rec
        entry = i_dec + 1
        move = _bps(blind[e - 1]["c"], blind[entry]["o"]) if entry < e else 0.0
        hold_would = move - 2 * side
        t = by_day.get(blind[s]["t"][:10])
        result = t["ret"] * 1e4 if (ans["position"] and t) else 0.0
        right = (result > 0) if ans["position"] else (hold_would <= 0)
        days.append({"day": n, "position": ans["position"], "reason": ans.get("reason", ""), "failed": ans["failed"],
                     "decided_at": i_dec, "move_bps": move, "hold_would_bps": hold_would, "result_bps": result,
                     "right": right})
    return head, days


HOLD_TEXT = {False: "held to the close",
             True: "holds or stays in cash until its next decision, so it can keep the stock overnight"}


def _overnight_days(blind, a, b, record, side):
    """Per-day detail when positions are kept overnight. A day's span runs from its entry (the bar after the
    decision) to the next decision's entry, or to the week's last close. Costs are charged only when the position
    changes. The week's result itself always comes from the backtester; this is the readable breakdown."""
    before = [k for k in record if k < a]
    prev = record[max(before)][1]["position"] if before else 0
    spans = _days(blind, a, b)
    entries = [(n, s, record.get(s)) for n, (s, e) in enumerate(spans, 1)]
    out = []
    for j, (n, s, rec) in enumerate(entries):
        if rec is None:
            out.append({"day": n, "position": None, "reason": "", "failed": False})
            continue
        i_dec, ans = rec
        entry = min(i_dec + 1, b - 1)
        nxt = next((r[0] + 1 for _, _, r in entries[j + 1:] if r is not None), None)
        end = blind[nxt]["o"] if nxt is not None and nxt < b else blind[b - 1]["c"]
        move = _bps(end, blind[entry]["o"])
        pos = ans["position"]
        hold_would = move - (side if prev == 0 else 0.0)
        result = (move - (side if prev == 0 else 0.0)) if pos else (-side if prev == 1 else 0.0)
        right = (result > 0) if pos else (hold_would <= 0)
        out.append({"day": n, "position": pos, "reason": ans.get("reason", ""), "failed": ans["failed"],
                    "decided_at": i_dec, "move_bps": move, "hold_would_bps": hold_would, "result_bps": result,
                    "right": right, "kept_from_before": prev})
        prev = pos
    return out


def run(folder, client, log=None):
    plan, blind, weeks, entries = replay._open(folder)
    lk = _lock(entries)
    if not lk:
        raise ValueError("no agent run is locked here; run `lock` first so the model, weeks, budget and criteria "
                         "are fixed before anything runs")
    overnight = _overnight(lk)
    if lk["context_hash"] != context_hash(overnight):
        raise ValueError("the agent's instructions or request settings changed after the lock; use a new folder")
    side = agent.per_side_bps(plan["costs"])
    asker = agent.Asker(os.path.join(folder, "agent"), client, lk["model"], _budget(entries))
    week_of = _week_index(weeks, len(blind))
    forecasts = {e["week"]: e for e in entries if e["kind"] == "agent_week_forecast"}
    results = {e["week"]: e for e in entries if e["kind"] == "agent_week_result"}
    for w in range(lk["first_week"], lk["last_week"] + 1):
        if w not in forecasts:
            prev = [forecasts[k] for k in sorted(forecasts) if k < w]
            notebook = prev[-1]["notebook"] if prev else lk["notebook_start"]
            ans = asker.ask(week_prompt(plan, blind, weeks, w, notebook, results, overnight), "week_forecast",
                            parse_week,
                            {"expect_return": 0.0, "expect_market": "up", "notebook": notebook})
            forecasts[w] = T.ledger_append(folder, {
                "kind": "agent_week_forecast", "week": w, "expect_return": ans["expect_return"],
                "expect_market": ans["expect_market"], "plan": ans["reason"], "notebook": ans["notebook"],
                "failed": ans["failed"], "answer_key": ans["key"]})
        if w not in results:
            record = {}
            head, days = _score_week(plan, blind, weeks, w,
                                     _day_strategy(asker, side, week_of, forecasts, record, overnight), record,
                                     overnight)
            a, b = weeks[w - 1]
            stock_move = blind[b - 1]["c"] / blind[a - 1]["c"] - 1.0
            f = forecasts[w]
            actual = "up" if head["buy_and_hold_return"] > 0 else "down"
            results[w] = T.ledger_append(folder, {
                "kind": "agent_week_result", "week": w, "strategy_return": head["strategy_return"],
                "buy_and_hold_return": head["buy_and_hold_return"], "stock_move": stock_move,
                "trades": head["trades"], "costs_paid": head["costs_paid"], "expected_return": f["expect_return"],
                "error": head["strategy_return"] - f["expect_return"], "market_expected": f["expect_market"],
                "market_actual": actual, "direction_hit": f["expect_market"] == actual,
                "days_held": sum(1 for d in days if d.get("position")), "days": days})
            write_memory(folder)
            if log:
                r = results[w]
                log("week %d: forecast %s, got %s after costs, stock %s, held %d of %d days, API spent so far "
                    "%.4f USD" % (w, _pct(r["expected_return"]), _pct(r["strategy_return"]), _pct(r["stock_move"]),
                                  r["days_held"], len(days), asker.stats["ledger_spent"]))
    write_memory(folder)
    return status(folder)


def status(folder):
    plan, blind, weeks, entries = replay._open(folder)
    lk = _lock(entries)
    if not lk:
        raise ValueError("no agent run is locked here")
    res = sorted((e for e in entries if e["kind"] == "agent_week_result"), key=lambda e: e["week"])
    tot, stock = 1.0, 1.0
    for r in res:
        tot *= 1.0 + r["strategy_return"]
        stock *= 1.0 + r["stock_move"]
    spent = sum(e.get("cost", 0.0) for e in T.ledger_read(os.path.join(folder, "agent")))
    return {"model": lk["model"], "weeks": [lk["first_week"], lk["last_week"]], "weeks_done": len(res),
            "agent_total_return": tot - 1.0, "stock_total_return": stock - 1.0,
            "direction_hits": sum(1 for r in res if r["direction_hit"]), "api_spent_dollars": spent,
            "budget_dollars": _budget(entries), "ledger_entries": len(entries)}


# ---------- memory files: nothing is lost when the run ends ----------

def write_memory(folder):
    """Readable copies of everything the agent wrote and how it turned out, rebuilt from the ledgers."""
    entries = T.ledger_read(folder)
    lk = _lock(entries)
    fc = {e["week"]: e for e in entries if e["kind"] == "agent_week_forecast"}
    rs = {e["week"]: e for e in entries if e["kind"] == "agent_week_result"}
    mem = os.path.join(folder, "memory")
    os.makedirs(mem, exist_ok=True)
    latest = fc[max(fc)]["notebook"] if fc else (lk or {}).get("notebook_start", "")
    with open(os.path.join(mem, "notebook.md"), "w", encoding="utf-8") as fh:
        fh.write("# Agent notebook\n\nLatest version, written before week %s. Start a later run from it with "
                 "`weekly.py lock --notebook-from <this file>`.\n\n%s%s\n" % (max(fc) if fc else "-", NOTEBOOK_HEADER,
                                                                             latest))
    with open(os.path.join(mem, "notebook-history.md"), "w", encoding="utf-8") as fh:
        fh.write("# Every version of the agent's notebook\n\n")
        if lk and lk.get("notebook_start"):
            fh.write("## Start (from %s)\n\n%s\n\n" % (lk.get("notebook_start_from"), lk["notebook_start"]))
        for w in sorted(fc):
            fh.write("## Before week %d\n\n%s\n\n" % (w, fc[w]["notebook"] or "(empty)"))
    lines = ["# Agent journal", "",
             "Every weekly forecast and plan, every daily choice with its reason, and how right or wrong it was. "
             "Results are after costs. Rebuilt from the hash-chained ledgers after every week.", ""]
    rows = []
    for w in sorted(fc):
        f, r = fc[w], rs.get(w)
        lines += ["## Week %d" % w, "",
                  "- Forecast: own result %s, stock %s%s" % (_pct(f["expect_return"]), f["expect_market"],
                                                             " (unusable answer, defaults used)" if f["failed"] else ""),
                  "- Plan: %s" % (f["plan"] or "-")]
        if r:
            lines.append("- Result: %s after costs (%d trades, costs %.1f); stock %s; forecast missed by %s; "
                         "direction %s" % (_pct(r["strategy_return"]), r["trades"], r["costs_paid"],
                                           _pct(r["stock_move"]), _pct(r["error"]),
                                           "right" if r["direction_hit"] else "wrong"))
            lines += ["", "| Day | Choice | Reason | Stock from entry to close | Outcome | Right? |",
                      "| --- | --- | --- | --- | --- | --- |"]
            for d in r["days"]:
                if d.get("position") is None:
                    lines.append("| %d | no decision | day too short | | | |" % d["day"])
                    continue
                outcome = ("%+.0f bps after costs" % d["result_bps"]) if d["position"] else (
                    "holding would have given %+.0f bps" % d["hold_would_bps"])
                lines.append("| %d | %s | %s | %+.0f bps | %s | %s |" % (
                    d["day"], "hold" if d["position"] else "cash",
                    (d["reason"] or "-").replace("|", "/").replace("\n", " "), d["move_bps"], outcome,
                    "right" if d["right"] else "wrong"))
        lines.append("")
        rows.append({"week": w, "forecast": {k: f[k] for k in ("expect_return", "expect_market", "plan", "failed")},
                     "result": None if r is None else {k: r[k] for k in (
                         "strategy_return", "stock_move", "trades", "costs_paid", "error", "direction_hit",
                         "days_held", "days")}})
    with open(os.path.join(mem, "journal.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    with open(os.path.join(mem, "journal.jsonl"), "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    return mem


# ---------- numbers for the pages ----------

def _positions_strategy(days_by_index, overnight=False):
    """Replays the logged daily choices without asking the model: {decision bar index: position}. Make a new one
    for every backtest: it keeps state."""
    state = {"day": None, "pos": 0}

    def strategy(hist, position):
        i = len(hist) - 1
        day = hist[i]["t"][:10]
        if day != state["day"]:
            state["day"] = day
            if not overnight:
                state["pos"] = 0
        if i in days_by_index:
            state["pos"] = days_by_index[i]
        return state["pos"]

    return strategy


def _decisions(rows):
    return {d["decided_at"]: d["position"] for r in rows for d in r["days"] if d.get("position") is not None}


def _fn_for_row(rows, overnight):
    """Decision function for scoring one week again. Overnight, earlier weeks' choices matter (a position can be
    carried into the week), so every logged choice is replayed; otherwise only that week's."""
    if overnight:
        every = _decisions(rows)
        return lambda r: _positions_strategy(every, True)
    return lambda r: _positions_strategy(_decisions([r]))


def _rescore_agent(plan, blind, weeks, rows, costs, overnight=False):
    p = dict(plan, costs=costs)
    total = 1.0
    fn_for = _fn_for_row(rows, overnight)
    for r in rows:
        a, b = weeks[r["week"] - 1]
        total *= 1.0 + replay._score_fn(p, blind, a, b, fn_for(r), flatten_eod=not overnight)["strategy_return"]
    return total - 1.0


def agent_money(folder, d, start):
    """Money for the agent run and for holding, starting from `start` (euro), along the real path."""
    report.check_start_money(d["costs"], start)
    plan, blind, weeks, _ = replay._open(folder)
    overnight = _overnight(d["lock"])
    end, paid = report.money_path(plan, blind, weeks, d["rows"], start, _fn_for_row(d["rows"], overnight),
                                  flatten_eod=not overnight)
    return {"start": float(start), "agent_end": end, "agent_costs": paid,
            "stock_end": start * (1.0 + d["stock_total"]),
            "hold_costs": start * 2 * report.per_side_bps(d["costs"]) / 1e4}


def build(folder):
    plan, blind, weeks, entries = replay._open(folder)
    lk = _lock(entries)
    fc = {e["week"]: e for e in entries if e["kind"] == "agent_week_forecast"}
    rows = sorted((e for e in entries if e["kind"] == "agent_week_result"), key=lambda e: e["week"])
    if not rows:
        raise ValueError("no week has been played by the agent yet")
    cum, total, stock = [], 1.0, 1.0
    cum_stock = []
    for r in rows:
        total *= 1.0 + r["strategy_return"]
        stock *= 1.0 + r["stock_move"]
        cum.append(total - 1.0)
        cum_stock.append(stock - 1.0)
    n = len(rows)
    held = [d for r in rows for d in r["days"] if d.get("position") == 1]
    out = [d for r in rows for d in r["days"] if d.get("position") == 0]
    spent = sum(e.get("cost", 0.0) for e in T.ledger_read(os.path.join(folder, "agent")))
    sens = [{"label": text, "per_side_bps": report.per_side_bps(c),
             "total_return": _rescore_agent(plan, blind, weeks, rows, c, _overnight(lk))} for _, text, c in report.scenarios(plan["costs"])]
    return {
        "rows": rows, "forecasts": fc, "lock": lk, "costs": plan["costs"], "cum": cum, "cum_stock": cum_stock,
        "agent_total": cum[-1], "stock_total": cum_stock[-1], "n": n, "spent": spent,
        "weeks_open": plan["weeks"] - plan["sealed_weeks"], "weeks_sealed": plan["sealed_weeks"],
        "held_days": len(held), "out_days": len(out),
        "held_right": sum(1 for d in held if d["right"]), "out_right": sum(1 for d in out if d["right"]),
        "held_avg_bps": sum(d["result_bps"] for d in held) / len(held) if held else 0.0,
        "out_avg_bps": sum(d["hold_would_bps"] for d in out) / len(out) if out else 0.0,
        "failed": sum(1 for f in fc.values() if f["failed"]) + sum(1 for r in rows for d in r["days"] if d.get("failed")),
        "forecast": _forecast_stats(rows), "sensitivity": sens,
        "total_costs": sum(r["costs_paid"] for r in rows), "total_trades": sum(r["trades"] for r in rows),
        "notebook": fc[max(fc)]["notebook"] if fc else ""}


def _forecast_stats(rows):
    n = len(rows)
    hits = sum(1 for r in rows if r["direction_hit"])
    return {"weeks": n, "mean_abs_error": sum(abs(r["error"]) for r in rows) / n,
            "mean_abs_error_if_always_zero": sum(abs(r["strategy_return"]) for r in rows) / n,
            "direction_hits": hits, "always_up_hits": sum(1 for r in rows if r["market_actual"] == "up"),
            "direction_p_value_vs_coin": T.binom_tail(n, hits, 0.5)}


# ---------- drawing ----------

EXTRA_CSS = """
:root{--c-agent:#7b5cd6}
@media (prefers-color-scheme: dark){:root:where(:not([data-theme="light"])){--c-agent:#9a80ea}}
:root[data-theme="dark"]{--c-agent:#9a80ea}
.s-agent{stroke:var(--c-agent)}.d-agent{fill:var(--c-agent)}.b-agent{fill:var(--c-agent)}
.nb{white-space:pre-wrap;background:var(--panel);padding:12px 14px;border-radius:8px;color:var(--ink);font-size:.9rem}
.good{color:var(--c-trend)}.bad{color:var(--c-rules)}
"""

LINES_JS = """
(function(){try{
var svg=document.getElementById('cum');if(!svg)return;
var d=JSON.parse(document.getElementById('cumdata').textContent);
var hit=document.getElementById('hit'),cross=document.getElementById('cross'),tip=document.getElementById('tip');
var vb=svg.viewBox.baseVal;
hit.addEventListener('mousemove',function(e){
 var r=svg.getBoundingClientRect(),x=(e.clientX-r.left)*vb.width/r.width;
 var n=d.weeks.length,i=Math.round((x-d.ml)/(d.pw/Math.max(1,n-1)));i=Math.max(0,Math.min(n-1,i));
 var cx=d.ml+(n>1?d.pw*i/(n-1):d.pw/2);cross.setAttribute('x1',cx);cross.setAttribute('x2',cx);cross.style.display='';
 tip.style.display='block';tip.style.left=(e.clientX+14)+'px';tip.style.top=(e.clientY+10)+'px';
 tip.textContent='Week '+d.weeks[i]+': '+d.series.map(function(s){return s.name+' '+s.vals[i];}).join(', ');});
hit.addEventListener('mouseleave',function(){cross.style.display='none';tip.style.display='none';});
}catch(err){}})();
"""


def lines_chart(week_nums, series):
    """series: [(name, css name, values)]; one line each, labelled at its end."""
    W, H, ML, MR, MT, MB = report.W, report.H, report.ML, report.MR, report.MT, report.MB
    n = len(week_nums)
    allv = [v for _, _, vals in series for v in vals]
    ticks = report.nice_ticks(min(0.0, min(allv)), max(0.0, max(allv)))
    lo, hi = min(min(allv), ticks[0], 0.0), max(max(allv), ticks[-1], 0.0)
    pw, ph = W - ML - MR, H - MT - MB

    def X(i):
        return ML + (pw * i / (n - 1) if n > 1 else pw / 2)

    def Y(v):
        return MT + ph * (1 - (v - lo) / (hi - lo))

    out = ['<svg class="chart" id="cum" viewBox="0 0 %d %d" role="img" aria-label="Cumulative result by week">' % (W, H)]
    for t in ticks:
        out.append('<line class="grid" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (ML, W - MR, Y(t), Y(t)))
        out.append('<text class="tick" x="%d" y="%.1f" text-anchor="end">%s</text>' % (ML - 8, Y(t) + 4,
                                                                                     report.pct(t, False, 0)))
    out.append('<line class="zero" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (ML, W - MR, Y(0), Y(0)))
    step = 10 if n > 25 else 5 if n > 12 else 1
    for i, w in enumerate(week_nums):
        if (w - week_nums[0]) % step == 0:
            out.append('<text class="tick" x="%.1f" y="%d" text-anchor="middle">%d</text>' % (X(i), H - 10, w))
    ends = []
    for name, cls, vals in series:
        out.append('<polyline class="line s-%s" points="%s"/>' % (cls, " ".join("%.1f,%.1f" % (X(i), Y(v))
                                                                               for i, v in enumerate(vals))))
        out.append('<circle class="dot d-%s" cx="%.1f" cy="%.1f" r="4"/>' % (cls, X(n - 1), Y(vals[-1])))
        ends.append([Y(vals[-1]), name, vals[-1]])
    ends.sort()
    for k in range(1, len(ends)):
        if ends[k][0] - ends[k - 1][0] < 15:
            ends[k][0] = ends[k - 1][0] + 15
    for y, name, v in ends:
        out.append('<text class="lbl" x="%.1f" y="%.1f">%s %s</text>' % (X(n - 1) + 10, y + 4, html.escape(name),
                                                                        report.pct(v)))
    out.append('<line id="cross" class="cross" x1="0" x2="0" y1="%d" y2="%d" style="display:none"/>' % (MT, H - MB))
    out.append('<rect id="hit" x="%d" y="%d" width="%d" height="%d" fill="transparent"/>' % (ML, MT, pw, ph))
    out.append("</svg>")
    data = {"weeks": week_nums, "ml": ML, "pw": pw,
            "series": [{"name": name, "vals": [report.pct(v) for v in vals]} for name, _, vals in series]}
    return "".join(out) + '<div id="tip"></div><script type="application/json" id="cumdata">%s</script>' % json.dumps(data)


def week_bars(rows, key, cls):
    W = report.W
    h, ML, MT, MB = 230, report.ML, report.MT, report.MB
    vals = [r[key] for r in rows]
    ticks = report.nice_ticks(min(0.0, min(vals)), max(0.0, max(vals)), 4)
    lo, hi = min(min(vals), ticks[0], 0.0), max(max(vals), ticks[-1], 0.0)
    pw, ph = W - ML - 16, h - MT - MB
    slot = pw / len(rows)
    bw = max(2.0, min(24.0, slot - 2.0))

    def Y(v):
        return MT + ph * (1 - (v - lo) / (hi - lo))

    out = ['<svg class="chart" viewBox="0 0 %d %d" role="img" aria-label="Result in each week">' % (W, h)]
    for t in ticks:
        out.append('<line class="grid" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (ML, W - 16, Y(t), Y(t)))
        out.append('<text class="tick" x="%d" y="%.1f" text-anchor="end">%s</text>' % (ML - 8, Y(t) + 4,
                                                                                     report.pct(t, False, 0)))
    out.append('<line class="zero" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (ML, W - 16, Y(0), Y(0)))
    for i, r in enumerate(rows):
        x = ML + slot * i + (slot - bw) / 2
        tip = "Week %d: %s (stock %s)" % (r["week"], report.pct(r[key]), report.pct(r["stock_move"]))
        path = report._bar_path(x, Y(0), bw, Y(r[key]), 4)
        if path:
            out.append('<path class="%s" d="%s"><title>%s</title></path>' % (cls, path, html.escape(tip)))
        else:
            out.append('<rect class="b-none" x="%.1f" y="%.1f" width="%.1f" height="1.5"><title>%s</title></rect>'
                       % (x, Y(0) - 0.75, bw, html.escape(tip)))
        if (r["week"] - rows[0]["week"]) % 10 == 0:
            out.append('<text class="tick" x="%.1f" y="%d" text-anchor="middle">%d</text>' % (x + bw / 2, h - 10,
                                                                                              r["week"]))
    out.append("</svg>")
    return "".join(out)


def _head(title):
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\"><title>%s</title><style>%s%s</style></head><body><main>"
            % (html.escape(title), report.CSS, EXTRA_CSS))


def _tile(value, text, color=None):
    sw = '<i class="sw" style="background:var(%s)"></i>' % color if color else ""
    return '<div class="tile"><b>%s</b><span>%s%s</span></div>' % (value, sw, html.escape(text))


def agent_sentences(d):
    a, h = d["agent_total"], d["stock_total"]
    out = ["Over %d weeks the agent returned %s after costs, deciding each day itself. Holding the stock returned %s. "
           "Staying out returns 0%%." % (d["n"], report.pct(a), report.pct(h))]
    if a <= 0:
        out.append("The agent lost money after costs." + (" It lost less than holding, which a stock that falls "
                                                           "gives to anything that is often in cash; that is not an "
                                                           "edge." if h < a else ""))
    elif a < h:
        out.append("The agent made money but less than holding the stock.")
    else:
        out.append("The agent did better than holding and than staying out. One stock and one stretch are not "
                   "proof; the comparison page tests whether the gap is larger than luck.")
    if d["held_days"]:
        out.append("It held on %d days and was right on %d of them (made money after costs); average %+.0f bps per "
                   "held day." % (d["held_days"], d["held_right"], d["held_avg_bps"]))
    if d["out_days"]:
        out.append("It stayed in cash on %d days; that was right on %d of them (holding would have lost after "
                   "costs)." % (d["out_days"], d["out_right"]))
    f = d["forecast"]
    if f["mean_abs_error"] < 0.9 * f["mean_abs_error_if_always_zero"]:
        out.append("Its return forecasts were somewhat closer than always guessing zero.")
    else:
        out.append("Its return forecasts were no better than always guessing zero (average miss %s against %s)."
                   % (report.pct(f["mean_abs_error"], False, 2), report.pct(f["mean_abs_error_if_always_zero"], False, 2)))
    out.append("The stock's direction was right in %d of %d weeks (always saying up: %d; a coin gives p = %.2f)."
               % (f["direction_hits"], f["weeks"], f["always_up_hits"], f["direction_p_value_vs_coin"]))
    out.append("This is practice on past data, not proof: the model may remember the real period.")
    return out


def render_agent(d, label="", start_money=None):
    lk, rows = d["lock"], d["rows"]
    p = [_head("Agent replay"), "<h1>Agent replay</h1>",
         "<p class=\"sub\">The agent traded weeks %d to %d one day at a time, stock and dates hidden, learning only "
         "through its own notebook. No rule from the library was used. %d sealed weeks untouched.</p>"
         % (rows[0]["week"], rows[-1]["week"], d["weeks_sealed"])]
    if label:
        p.append("<div class=\"banner\">%s</div>" % html.escape(label))
    p.append("<div class=\"tiles\">%s%s%s%s</div>" % (
        _tile(report.pct(d["agent_total"]), "The agent, after costs", "--c-agent"),
        _tile(report.pct(d["stock_total"]), "Holding the stock", "--c-stock"),
        _tile("0.0%", "Staying out of the market"),
        _tile("$%.2f" % d["spent"], "API spend (model %s)" % lk["model"])))
    p.append("<h2>What it says</h2><ul>%s</ul>" % "".join("<li>%s</li>" % html.escape(s) for s in agent_sentences(d)))
    if start_money:
        m = start_money
        p.append(report.money_table([("The agent", m["agent_end"], m["agent_costs"], ""),
                                     ("Holding the stock", m["stock_end"], None,
                                      "about %s for one buy and one sell, not counted" % report.eur(m["hold_costs"])),
                                     ("Staying out", m["start"], 0.0, "")], m["start"]))
    p.append("<h2>Cumulative result</h2>%s<p class=\"sub\">Holding is the true move of the stock including overnight "
             "gaps.</p>" % lines_chart([r["week"] for r in rows], [("Agent", "agent", d["cum"]),
                                                                  ("Stock", "stock", d["cum_stock"])]))
    p.append("<h2>Each week</h2>%s" % week_bars(rows, "strategy_return", "b-agent"))
    p.append("<h2>Its daily choices</h2><div class=\"tiles\">%s%s%s</div>" % (
        _tile("%d / %d" % (d["held_right"], d["held_days"]), "Held days that made money after costs"),
        _tile("%d / %d" % (d["out_right"], d["out_days"]), "Cash days where holding would have lost"),
        _tile(str(d["failed"]), "Unusable answers (counted as cash / no change)")))
    p.append("<h2>Were the forecasts any good?</h2><div class=\"tiles\">%s%s%s</div>" % (
        _tile(report.pct(d["forecast"]["mean_abs_error"], False, 2), "Average miss of its return forecast"),
        _tile(report.pct(d["forecast"]["mean_abs_error_if_always_zero"], False, 2), "Average miss if always guessing zero"),
        _tile("%d / %d" % (d["forecast"]["direction_hits"], d["forecast"]["weeks"]),
              "Direction right (always up: %d)" % d["forecast"]["always_up_hits"])))
    p.append("<h2>What costs do to this result</h2><p>The same daily choices re-scored with different costs (the "
             "choices were not changed).</p><div class=\"scroll\"><table><tr><th class=\"l\">Costs</th><th>Per trade "
             "side</th><th>Agent, total</th></tr>")
    for x in d["sensitivity"]:
        p.append("<tr><td class=\"l\">%s</td><td>%.1f bps</td><td>%s</td></tr>" % (
            html.escape(x["label"]), x["per_side_bps"], report.pct(x["total_return"])))
    p.append("</table></div><p class=\"sub\">%d trades, %.1f paid in costs on a 10,000 account.</p>" % (
        d["total_trades"], d["total_costs"]))
    p.append("<h2>Its notebook at the end</h2><div class=\"nb\">%s</div><p class=\"sub\">Every version, the plan of "
             "every week and every daily reason are in the run folder's memory/ files.</p>"
             % html.escape(d["notebook"] or "(empty)"))
    c = d["costs"]
    p.append("<h2>Limits</h2><ul><li>Costs used: commission %.1f bps, spread %.1f bps, slippage %.1f bps, fixed fee "
             "%.2f. Confirm them against the broker's price list.</li><li>One decision per day, after the first two "
             "bars, %s; the rules in the hand run could trade several times a day and always sold by the close.</li><li>The model "
             "may remember the real period despite the hidden names and dates; past data is practice.</li><li>One "
             "stock, one stretch: luck and skill cannot be told apart from this alone.</li><li>The stock's name, "
             "the dates and the source file are not shown.</li></ul>" % (
                 c["commission_bps"], c["spread_bps"], c["slippage_bps"], c["fixed_fee"],
                 HOLD_TEXT[_overnight(d["lock"])]))
    p.append("<h2>All weeks</h2><div class=\"scroll\"><table><tr><th>Week</th><th>Expected</th><th>Result</th>"
             "<th>Miss</th><th>Stock</th><th>Direction</th><th>Days held</th><th class=\"l\">Plan</th></tr>")
    for r in rows:
        p.append("<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%d / %d</td>"
                 "<td class=\"note\">%s</td></tr>" % (
                     r["week"], report.pct(r["expected_return"], True, 2), report.pct(r["strategy_return"], True, 2),
                     report.pct(r["error"], True, 2), report.pct(r["stock_move"]),
                     "yes" if r["direction_hit"] else "no", r["days_held"], len(r["days"]),
                     html.escape(d["forecasts"][r["week"]]["plan"] or "-")))
    p.append("</table></div><script>%s</script></main></body></html>" % LINES_JS)
    return "".join(p)


# ---------- the comparison with the hand-played rules ----------

SAME_KEYS = ("source_sha256", "costs", "days_per_week", "holdout", "weeks", "sealed_weeks")


def compare_build(agent_folder, rules_folder, start_money=None):
    d = build(agent_folder)
    aplan, blind, weeks, _ = replay._open(agent_folder)
    rplan, _, _, rentries = replay._open(rules_folder)
    diff = [k for k in SAME_KEYS if aplan[k] != rplan[k]]
    if diff:
        raise ValueError("the two runs differ in %s; they must use the same data, costs and weeks" % ", ".join(diff))
    rres = {e["week"]: e for e in rentries if e["kind"] == "result"}
    rpred = {e["week"]: e for e in rentries if e["kind"] == "prediction"}
    missing = [r["week"] for r in d["rows"] if r["week"] not in rres]
    if missing:
        raise ValueError("the rules run has no result for week(s) %s" % missing[:10])
    rows = []
    ca, cr, cs = 1.0, 1.0, 1.0
    for r in d["rows"]:
        w = r["week"]
        rr = rres[w]
        ca *= 1.0 + r["strategy_return"]
        cr *= 1.0 + rr["strategy_return"]
        cs *= 1.0 + r["stock_move"]
        rows.append({"week": w, "agent": r["strategy_return"], "rules": rr["strategy_return"],
                     "stock": r["stock_move"], "agent_hit": r["direction_hit"], "rules_hit": rr["direction_hit"],
                     "agent_miss": abs(r["error"]), "rules_miss": abs(rr["error"]),
                     "agent_costs": r["costs_paid"], "rules_costs": rr["costs_paid"],
                     "agent_trades": r["trades"], "rules_trades": rr["trades"],
                     "rule": report.rule_label(rpred[w]["strategy"], rpred[w]["params"]),
                     "cum_agent": ca - 1.0, "cum_rules": cr - 1.0, "cum_stock": cs - 1.0})
    lk = d["lock"]
    crit, seed = lk["criteria"], lk["seed"]
    A = [x["agent"] for x in rows]
    tests = []
    for name, other in (("the hand-played rules", [x["rules"] for x in rows]),
                        ("holding the stock", [x["stock"] for x in rows]),
                        ("staying out", [0.0] * len(rows))):
        tot_a = rows[-1]["cum_agent"]
        tot_o = 1.0
        for v in other:
            tot_o *= 1.0 + v
        tot_o -= 1.0
        s = {"weeks": len(rows), "agent_total": tot_a, "rule_total": tot_o,
             "p_agent_better": judge.paired_p_better(A, other, seed),
             "p_rule_better": judge.paired_p_better(other, A, seed)}
        label, why = judge.verdict(s, crit)
        label = {"AGENT BETTER": "AGENT BETTER", "RULE BETTER": "%s BETTER" % name.upper()}.get(label, label)
        tests.append({"against": name, "agent_total": tot_a, "other_total": tot_o, "verdict": label, "why": why,
                      "p_agent_better": s["p_agent_better"], "p_other_better": s["p_rule_better"]})
    rsens = report.build(rules_folder)["sensitivity"]
    rules_sens = {}
    for x in rsens:
        rules_sens[x["label"]] = x["total_return"]
    n = len(rows)
    money = None
    if start_money:
        am = agent_money(agent_folder, d, start_money)
        rd = report.build(rules_folder)
        wanted = {x["week"] for x in rows}
        r_end, r_paid = report.money_path(rplan, blind, weeks, [w for w in rd["weeks"] if w["week"] in wanted],
                                          start_money, lambda r: replay._fn(r["strategy"], r["params"]))
        money = {"start": float(start_money), "agent_end": am["agent_end"], "agent_costs": am["agent_costs"],
                 "rules_end": r_end, "rules_costs": r_paid, "stock_end": start_money * (1.0 + rows[-1]["cum_stock"]),
                 "hold_costs": am["hold_costs"]}
    return {"rows": rows, "tests": tests, "criteria": crit, "agent": d, "n": n, "money": money,
            "agent_total": rows[-1]["cum_agent"], "rules_total": rows[-1]["cum_rules"],
            "stock_total": rows[-1]["cum_stock"],
            "agent_hits": sum(1 for x in rows if x["agent_hit"]), "rules_hits": sum(1 for x in rows if x["rules_hit"]),
            "agent_miss": sum(x["agent_miss"] for x in rows) / n, "rules_miss": sum(x["rules_miss"] for x in rows) / n,
            "agent_costs": sum(x["agent_costs"] for x in rows), "rules_costs": sum(x["rules_costs"] for x in rows),
            "agent_trades": sum(x["agent_trades"] for x in rows), "rules_trades": sum(x["rules_trades"] for x in rows),
            "agent_wins": sum(1 for x in rows if x["agent"] > x["rules"]),
            "sensitivity": [{"label": x["label"], "per_side_bps": x["per_side_bps"], "agent": x["total_return"],
                             "rules": rules_sens.get(x["label"])} for x in d["sensitivity"]],
            "rules_weeks_note": len(rres)}


def render_compare(c, label=""):
    rows, crit = c["rows"], c["criteria"]
    p = [_head("Agent versus rules"), "<h1>Agent versus rules</h1>",
         "<p class=\"sub\">The same %d weeks, the same hidden stock, the same costs. The rules run: a rule from the "
         "library chosen by hand each week. The agent run: a model deciding each day itself, learning through its "
         "notebook.</p>" % c["n"]]
    if label:
        p.append("<div class=\"banner\">%s</div>" % html.escape(label))
    p.append("<div class=\"tiles\">%s%s%s%s</div>" % (
        _tile(report.pct(c["agent_total"]), "The agent, after costs", "--c-agent"),
        _tile(report.pct(c["rules_total"]), "The rules, after costs", "--c-rules"),
        _tile(report.pct(c["stock_total"]), "Holding the stock", "--c-stock"),
        _tile("0.0%", "Staying out")))
    if c.get("money"):
        m = c["money"]
        p.append(report.money_table([("The agent", m["agent_end"], m["agent_costs"], ""),
                                     ("The rules", m["rules_end"], m["rules_costs"], ""),
                                     ("Holding the stock", m["stock_end"], None,
                                      "about %s for one buy and one sell, not counted" % report.eur(m["hold_costs"])),
                                     ("Staying out", m["start"], 0.0, "")], m["start"]))
    p.append("<h2>The verdict, by criteria locked before the run</h2><p>Each pair is judged on the weekly results: a "
             "paired bootstrap must give p below %g, the gap must be at least %s, the winner must have made money "
             "after costs, and there must be at least %d weeks. Otherwise: no clear difference.</p>"
             "<div class=\"scroll\"><table><tr><th class=\"l\">Agent against</th><th>Agent</th><th>Other</th>"
             "<th>p (agent better)</th><th>p (other better)</th><th class=\"l\">Verdict</th></tr>"
             % (crit["alpha"], report.pct(crit["min_total_gap"], False, 0), crit["min_weeks"]))
    for t in c["tests"]:
        p.append("<tr><td class=\"l\">%s</td><td>%s</td><td>%s</td><td>%.3f</td><td>%.3f</td><td class=\"l\"><b>%s"
                 "</b></td></tr>" % (html.escape(t["against"]), report.pct(t["agent_total"]),
                                     report.pct(t["other_total"]), t["p_agent_better"], t["p_other_better"],
                                     html.escape(t["verdict"])))
    p.append("</table></div>")
    p.append("<h2>Cumulative result</h2>%s" % lines_chart([x["week"] for x in rows], [
        ("Agent", "agent", [x["cum_agent"] for x in rows]), ("Rules", "rules", [x["cum_rules"] for x in rows]),
        ("Stock", "stock", [x["cum_stock"] for x in rows])]))
    p.append("<h2>Side by side</h2><div class=\"scroll\"><table><tr><th class=\"l\"></th><th>Agent</th><th>Rules</th>"
             "</tr>")
    for name, a, r in (("Total after costs", report.pct(c["agent_total"]), report.pct(c["rules_total"])),
                       ("Weeks it did better than the other", str(c["agent_wins"]),
                        str(sum(1 for x in rows if x["rules"] > x["agent"]))),
                       ("Stock direction right", "%d / %d" % (c["agent_hits"], c["n"]),
                        "%d / %d" % (c["rules_hits"], c["n"])),
                       ("Average miss of the weekly return forecast", report.pct(c["agent_miss"], False, 2),
                        report.pct(c["rules_miss"], False, 2)),
                       ("Trades", str(c["agent_trades"]), str(c["rules_trades"])),
                       ("Costs paid (10,000 account)", "%.1f" % c["agent_costs"], "%.1f" % c["rules_costs"])):
        p.append("<tr><td class=\"l\">%s</td><td>%s</td><td>%s</td></tr>" % (html.escape(name), a, r))
    p.append("</table></div>")
    p.append("<h2>What costs do</h2><p>Both runs' choices kept, re-scored with other costs.</p><div class=\"scroll\">"
             "<table><tr><th class=\"l\">Costs</th><th>Per trade side</th><th>Agent</th><th>Rules</th></tr>")
    for x in c["sensitivity"]:
        p.append("<tr><td class=\"l\">%s</td><td>%.1f bps</td><td>%s</td><td>%s</td></tr>" % (
            html.escape(x["label"]), x["per_side_bps"], report.pct(x["agent"]),
            "-" if x["rules"] is None else report.pct(x["rules"])))
    p.append("</table></div>")
    p.append("<h2>Fair-comparison notes</h2><ul><li>The rules were chosen by Claude by hand, the agent run used %s "
             "through the API; both saw the same anonymized weeks in the same order.</li><li>The rules could trade "
             "several times a day and always sold by the close; the agent decides once a day, after the first two "
             "bars, and %s.</li><li>Holding is the true move of the stock with no costs; staying out is 0.</li><li>Past data is "
             "practice: either may remember the real period. Only data after the model's knowledge, and paper "
             "trading, count.</li></ul>" % (html.escape(c["agent"]["lock"]["model"]), HOLD_TEXT[
                 _overnight(c["agent"]["lock"])]))
    p.append("<h2>All weeks</h2><div class=\"scroll\"><table><tr><th>Week</th><th>Agent</th><th>Rules</th>"
             "<th class=\"l\">Rule used</th><th>Stock</th><th>Agent dir.</th><th>Rules dir.</th></tr>")
    for x in rows:
        p.append("<tr><td>%d</td><td>%s</td><td>%s</td><td class=\"l\">%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                 % (x["week"], report.pct(x["agent"], True, 2), report.pct(x["rules"], True, 2),
                    html.escape(x["rule"]), report.pct(x["stock"]), "yes" if x["agent_hit"] else "no",
                    "yes" if x["rules_hit"] else "no"))
    p.append("</table></div><script>%s</script></main></body></html>" % LINES_JS)
    return "".join(p)


# ---------- command line ----------

def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="The agent trades a replay week by week. Not financial advice.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("lock")
    s.add_argument("--dir", required=True)
    s.add_argument("--model", required=True)
    s.add_argument("--budget-usd", dest="budget", type=float, required=True, help="hard cap in US dollars")
    s.add_argument("--first-week", type=int, default=2)
    s.add_argument("--last-week", type=int)
    s.add_argument("--notebook-from", help="a notebook.md saved by an earlier run, to start from its lessons")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--overnight", action="store_true",
                   help="hold or cash until the next decision, so a held position stays open overnight")
    for name in ("run", "status", "memory"):
        x = sub.add_parser(name)
        x.add_argument("--dir", required=True)
    rb = sub.add_parser("raise-budget")
    rb.add_argument("--dir", required=True)
    rb.add_argument("--budget-usd", dest="budget", type=float, required=True)
    r = sub.add_parser("report")
    r.add_argument("--dir", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--label", default="")
    c = sub.add_parser("compare")
    c.add_argument("--dir", required=True)
    c.add_argument("--rules-dir", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--label", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "lock":
            e = lock(a.dir, a.model, a.budget, a.first_week, a.last_week, a.notebook_from, a.seed,
                     overnight=a.overnight)
            out = {k: e[k] for k in ("model", "budget", "first_week", "last_week", "criteria", "context_hash",
                                     "overnight")}
        elif a.cmd == "run":
            plan, _, _, entries = replay._open(a.dir)
            lk = _lock(entries)
            if not lk:
                raise ValueError("no agent run is locked here; run `lock` first")
            out = run(a.dir, agent.AnthropicClient(lk["model"]), log=lambda m: print(m, file=sys.stderr, flush=True))
        elif a.cmd == "status":
            out = status(a.dir)
        elif a.cmd == "raise-budget":
            out = {k: v for k, v in raise_budget(a.dir, a.budget).items() if k in ("budget", "previous")}
        elif a.cmd == "memory":
            out = {"memory": write_memory(a.dir)}
        elif a.cmd == "report":
            out = {"written": _write(a.out, render_agent(build(a.dir), a.label))}
        else:
            out = {"written": _write(a.out, render_compare(compare_build(a.dir, a.rules_dir), a.label))}
    except agent.BudgetExceeded as e:
        print("Stopped: %s" % e, file=sys.stderr)
        return 3
    except (ValueError, OSError) as e:
        print("Refused: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
