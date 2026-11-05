---
name: trader-strategies
description: >-
  The Strategy Library. Three rule-based day-trading ideas (trend filter, opening-range breakout, VWAP
  reversion), each with its stated reason and the markets it applies to, run through the backtester on saved
  price bars with your costs. Use when the user says "test the opening range breakout", "try a trend rule on
  SAP", "run VWAP reversion on bitcoin", "which strategies do we have", or "does this rule work".
argument-hint: "[strategy] [--param name=value] [bars.csv]"
---

# trader-strategies

Each strategy is a small, readable rule with a written reason. They are candidates to test, not recommendations:
most day-trading rules lose money after costs (docs/RESEARCH.md). This skill runs them; it places no orders.

```bash
python3 "${CLAUDE_SKILL_DIR}/run.py" list
python3 "${CLAUDE_SKILL_DIR}/run.py" run --strategy orb --param range_bars=6 \
    --bars trader/history/SAP.DE_5m.csv --periods-per-year 25704 --flatten-eod \
    --commission-bps 0 --spread-bps 2 --slippage-bps 1
```

Bars come from /trader-data (or any CSV with `t,o,h,l,c,v`). Costs must be stated; none are assumed.

## The strategies

| name | rule | markets | parameters |
| --- | --- | --- | --- |
| trend | long while the close is above the average of the last N bars | stocks, ETFs, crypto | `window` |
| orb | the first N bars of the day set a range; long on a close above it, out on a close below it | stocks, ETFs | `range_bars` |
| vwap | buy when the close is more than X basis points below the day's VWAP, sell when it returns to it | stocks, ETFs, crypto | `band_bps`, `warmup` |

All three are long-only (shorting is blocked by /trader-risk) and see only the bars up to now. No parameter has
a default: a missing parameter is refused, because a default would be a guess presented as a setting.

## Reading the result

- `--periods-per-year` is bars per day times trading days a year (252 for stocks, 365 for crypto). A 5-minute
  series on Xetra has about 102 bars a day, so about 25,700.
- `--flatten-eod` closes every position at the end of each day, which is what day trading means. Without it a
  position can be held overnight.
- The report puts the strategy next to buy and hold and lists the costs paid. Fewer than 100 trades gives a
  warning, and a result from so few trades is mostly luck.
- "Today" is the UTC calendar day. That fits Xetra, Euronext and the NYSE; for crypto it is only a daily reset.

## What to tell the user

- This is a replay on past data, not a prediction, and it leaves out tax. A single run on a few weeks of
  bars proves almost nothing; the validation step (roadmap step 4) is what decides whether a rule is worth
  trying on paper money.
- Trying many parameter values and keeping the best one inflates the result. Say how many were tried.
- Never present a strategy as reliable. If it does not beat buy and hold after costs, say so plainly.

## Safety

- Education and tooling, not financial advice. Nothing here predicts prices or promises returns.
- This skill places no orders and needs no keys. Anything traded still goes through /trader-risk and needs the
  user's explicit yes in /trader-broker.
