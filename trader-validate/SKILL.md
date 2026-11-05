---
name: trader-validate
description: >-
  The Validator. Takes a day-trading rule and every setting you tried, and says PASS, FAIL or INCONCLUSIVE
  with the reasons: doubled costs, orders arriving late, walk-forward on days the settings never saw, a Sharpe
  ratio discounted for how many settings were tried, and the trade count. Use when the user says "is this
  result real", "validate the strategy", "could this be luck", "does it survive costs", or before any rule is
  tried on paper money.
argument-hint: "[strategy] [--grid name=v1,v2] [bars.csv]"
---

# trader-validate

A good backtest is easy to get: try enough settings and one will look great by luck. This skill is the check
that most ideas should fail. It reports; it places no orders.

```bash
python3 "${CLAUDE_SKILL_DIR}/validate.py" run --strategy orb --grid range_bars=3,6,12 \
    --bars trader/history/SAP.DE_5m.csv --periods-per-year 25704 --flatten-eod \
    --commission-bps 0 --spread-bps 2 --slippage-bps 1 --train-days 20 --test-days 5
```

Strategies come from /trader-strategies, bars from /trader-data, costs from the user. No costs are assumed.

## What it checks

| check | rule for passing | why |
| --- | --- | --- |
| trades | at least 100 | fewer is mostly luck |
| net result after costs | above 0 | the basic claim |
| cost stress | still above 0 with every cost doubled | real costs are usually worse than the guess |
| orders two bars late | still above 0 | a real edge fades gradually; a fragile one collapses |
| walk-forward | return and Sharpe above 0 on days the settings were not chosen on | settings picked on one window are scored on the next |
| alpha | above 0 after removing beta times buy and hold | in a rising market any rule that is often long "wins"; only what beats being in the market counts |
| deflated Sharpe | probability at least 0.95, computed on the alpha | the Sharpe ratio is discounted for how many settings were tried |
| last third of the data | not a loss | an edge that stopped working is not an edge |
| largest single-bar range | high at most 20% above the low | a bad price tick can fake a profit; each flagged bar is listed |

The thresholds (100 trades, 0.95, 2x costs) are this project's choices, not sourced numbers. The deflated
Sharpe formula follows Bailey and Lopez de Prado as implemented in the endgame library; the paper itself was
not read. It also reports the break-even cost: the extra cost per side, in basis points, at which the result
reaches zero.

FAIL means a hard check failed. INCONCLUSIVE means nothing failed outright but the evidence is too thin
(too few trades, a weak deflated Sharpe, or a check that could not be computed, such as walk-forward on too
few days). PASS needs every check to hold. A PASS is permission to try the rule on paper money, not a forecast.

## How to run it honestly

- List every setting you try in `--grid`. Settings tried in earlier runs on the same data go in
  `--earlier-trials`, because they count against the result. Hiding them makes the verdict meaningless.
- `--train-days` and `--test-days` need enough days of bars: at least train plus test, and more for several
  folds. With a few weeks of data expect INCONCLUSIVE; say so and collect more data.
- Use `--flatten-eod` for day trading. `--periods-per-year` is bars per day times trading days.
- Re-running with new settings after a FAIL and keeping the one that passes is the mistake this tool exists to
  stop. Say so if the user does it.

## What to tell the user

- Checked only on made-up price data (14 scenarios, see docs/STRESS_TEST.md). It can tell a series with a
  built-in edge from one without on simple data; it says nothing about real markets.
- A rule that trades about once a day (the opening-range breakout) needs 100 or more trading days to reach
  100 trades, so on a few months of data it stays INCONCLUSIVE however good it is.
- The "two bars late" check also rejects edges that last only one bar. Say so if a fast rule fails only there.
- Taxes are not included. Say that the result is a replay of the past, not a prediction.

## Safety

- Education and tooling, not financial advice. Nothing here predicts prices or promises returns.
- This skill places no orders and needs no keys. Anything traded still goes through /trader-risk and needs the
  user's explicit yes in /trader-broker.
