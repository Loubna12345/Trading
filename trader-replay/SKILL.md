---
name: trader-replay
description: >-
  The Replay. Plays saved price data back one week at a time as if each week were the future: the stock and the
  dates are hidden, a rule for the next week must be written down before that week is shown, the week is then
  revealed and the rule scored with costs, and the last third of the weeks stays sealed for one final test per
  rule. Use when the user says "replay the data", "week by week", "use the past as live training", "test my
  assumptions on week 1 then week 2", or wants practice predictions that are logged before the answer.
argument-hint: "[start|show|predict|reveal|final|status|relocate]"
---

# trader-replay

Past data is only practice if you do not know what comes next. This skill enforces the order and keeps a
tamper-evident record. It places no orders.

```bash
python3 "${CLAUDE_SKILL_DIR}/replay.py" start trader/history/SAP.DE_60m.csv --dir trader/replay/a \
    --commission-bps 0 --spread-bps 2 --slippage-bps 1 --fixed-fee 0 --periods-per-year 2268
python3 "${CLAUDE_SKILL_DIR}/replay.py" show --dir trader/replay/a --week 1
python3 "${CLAUDE_SKILL_DIR}/replay.py" predict --dir trader/replay/a --week 2 --strategy trend --param window=6 \
    --expect-return 0.004 --expect-market up --note "week 1 trended"
python3 "${CLAUDE_SKILL_DIR}/replay.py" reveal --dir trader/replay/a --week 2
python3 "${CLAUDE_SKILL_DIR}/replay.py" final --dir trader/replay/a --strategy trend --param window=6
python3 "${CLAUDE_SKILL_DIR}/replay.py" status --dir trader/replay/a
python3 "${CLAUDE_SKILL_DIR}/replay.py" relocate --dir trader/replay/a --source trader/history/SAP.DE_60m.csv
    # only when the same data file moved (another computer, a renamed folder); checked by SHA-256, logged
python3 "${CLAUDE_SKILL_DIR}/report.py" --dir trader/replay/a --out report.html --label "Dry run, placeholder costs"
```

## What it does

- `start` hides the stock and the dates (first open becomes 100, volume is divided by its median, dates become
  consecutive made-up weekdays), cuts the days into weeks of 5, drops left-over days, seals the last third of the
  weeks (default 0.34) and writes a plan whose hash goes into the ledger. Costs are required. The folder holds no
  prices; the data is re-read from the source file every time, and a changed source is refused.
- `show` prints a week that has already been revealed (week 1 is free). `predict` writes the rule for the next
  week into the ledger (strategy from /trader-strategies, or `none` to stay out); one prediction per week, never
  rewritten. Every prediction also carries two forecasts, required: the net return you expect from the rule that
  week (`--expect-return 0.004` is +0.4%) and where the stock itself ends the week (`--expect-market up|down`).
  `reveal` shows the week and scores the rule: same backtester, costs, one-bar delay and end-of-day
  flatten as the rest of the project; the result carries buy-and-hold for the same week. `hold` is a rule too
  (always in the stock).
- `start --overnight` makes a replay without the end-of-day sale: positions stay open overnight and from week to
  week. Each week runs its own rule, so a week starts with what the previous week's rule held at its last close;
  the weekend gap counts. The option is written in the plan (older plans have no key and stay day-only).
- Sealed weeks cannot be read or predicted. `final` runs one rule on all sealed weeks, once per rule, only for
  a rule that was written down for some week, and reports how many different rules were tried.

- Each result stores the difference between forecast and actual (`error`, and whether the direction was right).
  `status` sums it up: mean error, mean absolute error next to the error of always forecasting zero, direction
  hits next to always-saying-up and a coin. A forecaster is only worth listening to if it beats those.

## The report page

`report.py` writes one self-contained HTML page (no downloads, light and dark): the rules against holding the
stock and against staying out, a cumulative chart, the result of each week, when the stock moved (overnight or
inside the day), the assumptions behind the rule choices (a note starting with a letter, such as `A: ...`, groups
the weeks), a table per rule, forecast scorecard, limits, and every week in a table. It checks the ledger chain,
uses prices only for weeks already revealed, and never shows the stock's name, the dates or file paths. The
"holding" figure is the true move of the stock from the start of the first scored week to the end of the last,
not the sum of the per-week buy-and-hold results (which skip each week's first bar and the gap before it).

## Limits (read before trusting a result)

- The order is enforced by this tool, not by the file system: the source file sits on the same computer and
  anybody who opens it can look ahead. This is practice, not proof.
- A person or model that already knows how the real period went may remember it despite the hidden names and
  dates. Only data after that knowledge ends is truly unseen.
- One week of one stock is a handful of trades. Only the total over many weeks and stocks means anything, and
  each rule tried counts as a trial.
- Predictions here are rules from the registry. An agent that decides bar by bar is a later step.
