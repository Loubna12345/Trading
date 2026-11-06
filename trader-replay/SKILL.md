---
name: trader-replay
description: >-
  The Replay. Plays saved price data back one week at a time as if each week were the future: the stock and the
  dates are hidden, a rule for the next week must be written down before that week is shown, the week is then
  revealed and the rule scored with costs, and the last third of the weeks stays sealed for one final test per
  rule. Use when the user says "replay the data", "week by week", "use the past as live training", "test my
  assumptions on week 1 then week 2", or wants practice predictions that are logged before the answer.
argument-hint: "[start|show|predict|reveal|final|status]"
---

# trader-replay

Past data is only practice if you do not know what comes next. This skill enforces the order and keeps a
tamper-evident record. It places no orders.

```bash
python3 "${CLAUDE_SKILL_DIR}/replay.py" start trader/history/SAP.DE_60m.csv --dir trader/replay/a \
    --commission-bps 0 --spread-bps 2 --slippage-bps 1 --fixed-fee 0 --periods-per-year 2268
python3 "${CLAUDE_SKILL_DIR}/replay.py" show --dir trader/replay/a --week 1
python3 "${CLAUDE_SKILL_DIR}/replay.py" predict --dir trader/replay/a --week 2 --strategy trend --param window=6 --note "week 1 trended"
python3 "${CLAUDE_SKILL_DIR}/replay.py" reveal --dir trader/replay/a --week 2
python3 "${CLAUDE_SKILL_DIR}/replay.py" final --dir trader/replay/a --strategy trend --param window=6
python3 "${CLAUDE_SKILL_DIR}/replay.py" status --dir trader/replay/a
```

## What it does

- `start` hides the stock and the dates (first open becomes 100, volume is divided by its median, dates become
  consecutive made-up weekdays), cuts the days into weeks of 5, drops left-over days, seals the last third of the
  weeks (default 0.34) and writes a plan whose hash goes into the ledger. Costs are required. The folder holds no
  prices; the data is re-read from the source file every time, and a changed source is refused.
- `show` prints a week that has already been revealed (week 1 is free). `predict` writes the rule for the next
  week into the ledger (strategy from /trader-strategies, or `none` to stay out); one prediction per week, never
  rewritten. `reveal` shows the week and scores the rule: same backtester, costs, one-bar delay and end-of-day
  flatten as the rest of the project; the result carries buy-and-hold for the same week.
- Sealed weeks cannot be read or predicted. `final` runs one rule on all sealed weeks, once per rule, only for
  a rule that was written down for some week, and reports how many different rules were tried.

## Limits (read before trusting a result)

- The order is enforced by this tool, not by the file system: the source file sits on the same computer and
  anybody who opens it can look ahead. This is practice, not proof.
- A person or model that already knows how the real period went may remember it despite the hidden names and
  dates. Only data after that knowledge ends is truly unseen.
- One week of one stock is a handful of trades. Only the total over many weeks and stocks means anything, and
  each rule tried counts as a trial.
- Predictions here are rules from the registry. An agent that decides bar by bar is a later step.
