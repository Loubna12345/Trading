---
name: trader-backtest
description: >-
  The Backtester. Replays a trading rule over saved price bars and reports what it would have done after
  costs: return, drawdown, Sharpe, trade count, win rate, expectancy, costs paid, next to plain buy and hold.
  It cannot see the future, always charges costs, and warns when there are too few trades to trust. Use when
  the user says "backtest this", "would this rule have worked", "test my strategy", "compare with buy and
  hold", "how much do costs eat", or before any day-trading rule is tried on paper money.
argument-hint: "[bars.csv] [--periods-per-year N]"
---

# trader-backtest

Most day traders lose money after costs (see docs/RESEARCH.md). This skill exists to find out cheaply,
before any order, whether a rule survives costs. It reports; it never places an order.

Tools in this folder (standard library Python, no network):

```bash
python3 "${CLAUDE_SKILL_DIR}/engine.py" benchmark --bars bars.csv --periods-per-year 252 \
    --commission-bps 0 --spread-bps 2 --slippage-bps 1
```

`metrics.py` holds the numbers (return, drawdown, Sharpe, trade statistics); `engine.py` holds the replay.
This is step 1 of docs/ROADMAP.md: the command line only runs buy and hold. Strategies arrive in step 3.

## Rules the engine enforces

- Bars come from a CSV with columns `t,o,h,l,c` and optional `v`. Times are ISO 8601 and must strictly
  increase. Prices are plain numbers with a decimal point and above zero. Anything else is refused, not
  repaired.
- The strategy is called after each bar closes and sees only the bars up to that one, read-only.
- The order fills at the open of a later bar. The delay is at least 1 bar; 0 is refused because it would
  trade at the price the signal was computed from.
- Costs are required. Each fill pays half the spread plus slippage against you, plus commission and a fixed
  fee. The command line refuses to run until you state commission, spread and slippage in basis points.
  Taking the figures from your broker's price list is your job; the code assumes none.
- `flatten_eod` closes the position at the last bar of each day and carries nothing overnight.
- Sharpe uses a risk-free rate of zero, and needs `--periods-per-year` (252 for daily bars) because the
  annual figure depends on how often the bars come. The skill will not guess it.
- Fewer than 100 trades produces a warning. The 100 is this project's rule of thumb, not a sourced number.

## What to tell the user

- Say the result is a replay on past data, not a prediction. Paper and backtest results hide real-world
  costs and flatter the rule.
- Always show the buy-and-hold result next to the strategy. A rule that does not beat it after costs is
  not worth running.
- If an open position remains at the end, say so: it is marked at the last close and its exit costs are not
  charged.
- Taxes are not in these numbers. Say so; use /trader-tax for stocks and ETFs. Crypto has no tax module yet.

## Safety

- Education and tooling, not financial advice. Nothing here predicts prices or promises returns.
- This skill places no orders and needs no keys. Anything it suggests still goes through /trader-risk and
  needs the user's explicit yes in /trader-broker.
