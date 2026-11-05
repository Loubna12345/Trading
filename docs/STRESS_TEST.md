# Stress test of the validator on made-up markets

Run 2026-11-05. All data is synthetic (`tests/stress_scenarios.py`, run with `tests/stress_run.py`). 15-minute
bars, 80 days of 34 bars, costs 2 bp spread + 1 bp slippage, flat at each day's end, three settings per rule.
Numbers are verdict counts out of 12 random series per cell (the first run used 20). Nothing here says
how the rules behave on real markets.

## What it found

| Finding | Before | After the fix |
| --- | --- | --- |
| **Bug.** In a rising market with no skill, a trend rule passed because any rule that is often long profits from drift. | trend PASS 20 of 20, vwap PASS 9 of 20 | 0 PASS in all three rules (12 series each) |
| No PASS on markets with no edge (random walk, fat tails, volatility swings, gaps, U-shaped volatility, random regimes, a bad tick, a falling market). | 0 PASS in about 420 runs | 0 PASS in about 290 runs |
| A real momentum edge is still found by the trend rule. | PASS 18 of 20 | PASS 12 of 12 |
| An edge that works in the first half and then stops. | trend PASS 5 of 20 | trend PASS 1 of 12 |

Fix: the deflated Sharpe and a new hard check use the alpha (strategy return minus beta times buy and hold);
two soft checks were added (last third of the data not a loss; no bar with a high more than 20% above its low).
A single bad tick did not produce a PASS in either run, but the new check lists the bar and the data quality
check in trader-data now warns about it too.

## Weaknesses that remain (not fixed, by design or for lack of a better rule)

1. **Rules that trade about once a day cannot reach 100 trades in 80 days.** The opening-range breakout got
   INCONCLUSIVE 12 of 12 on data with a built-in opening-drive edge. It needs about 100 or more trading days.
2. **The "two bars late" check rejects edges that last a single bar.** VWAP reversion on a series with a
   one-bar mean-reversion edge failed 12 of 12 (best net +16%, but a thin break-even cost of about 3 bp and a
   negative result when orders are late). A real reversion that lasts several bars would not fail this way;
   the scenario is the cause as much as the check.
3. **Rules that do not fit the market type fail**, as they should: trend on mean-reverting data lost 40%.
4. **A rule that fits a different market can still pass by chance.** The trend rule passed 3 of 12 on
   "open drive" data. Whether that is skill or luck cannot be told from one series; several periods and
   tickers are needed.
5. **The thresholds are choices** (100 trades, 0.95, 2x costs, 20% bar range, last third). Changing them
   moves the pass rates. A real market needs its own sanity checks.

## Cost

About 0.75 seconds per validation of 80 days at 15-minute bars, so a full run with several settings and walk-forward
folds on 2 years of hourly bars should take a few seconds to a minute.

## End-to-end training run on made-up data (2026-11-05)

Each scenario: 10 made-up stocks, 140 days of 15-minute bars, plan with 3 rules x 3 settings, discovery, then
confirmation, then one holdout test per confirmed rule. Script lived in the scratchpad, not in the repo.

| scenario | candidates after discovery | confirmed | note |
| --- | --- | --- | --- |
| null random walk | none | none | as wanted |
| null volatility clustering | none | none | as wanted |
| bull drift, no skill | none | none | rising market alone did not fool it |
| real momentum edge | trend, orb | trend | final holdout: 9 of 10 stocks hold, p = 0.0107 |
| open-drive edge | none | none | missed; not investigated yet |
| mean-reversion edge | none | none | missed; only the long-only VWAP rule could fit, not investigated yet |

Reading: no false positives in three no-skill cases, one of three real edges found, two missed. The protocol errs
on the side of saying nothing. Why the two edges were missed (rule fit, too few days, or too strict) is open.
