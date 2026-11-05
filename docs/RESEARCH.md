# Research notes: day trading, per asset class

Written 2026-11-05. Every claim names where it came from. Secondary sources (blogs, summaries, broker
comparison sites) are marked as such. Where I did not read the primary text, the note says so.
Nothing here is financial or tax advice.

## 1. Does day trading make money? The evidence

| Finding | Source | Weight |
| --- | --- | --- |
| Over 80% of day traders lose money after transaction costs. About 1% of individual investors produced 50% of day trading volume. The most active group earned about 31 bp per day, not enough to cover costs; a small group earned about 62 bp and covered them. | Barber, Lee, Liu, Odean, "Do Individual Day Traders Make Money? Evidence from Taiwan" (2004), read through the [CXO Advisory summary](https://www.cxoadvisory.com/individual-investing/do-day-traders-make-money/) | One market, 2004, secondary summary. The paper itself was not read. |
| A published intraday momentum rule on SPY (1-minute data, 2007-2024) reports Sharpe 1.24 to 1.33 net of costs against 0.45 for buy and hold. | Zarattini, Aziz, Barbon, "Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)", 2025 version, [PDF](https://alexandria.unisg.ch/server/api/core/bitstreams/a99aba00-f967-49b3-aceb-f544dc386e0b/content) | Backtest only, no out-of-sample test shown, authors sell trading tools, final version uses up to 4x leverage, edge reported to fade after 2013 in earlier work, costs assume a US institutional-grade broker. |

What this means for the project:

1. The base rate is bad. A tool that reports "reliable predictions" for day trading would be lying. The
   goal is a tool that tells you honestly, with numbers, whether a rule survives costs and new data.
2. Costs decide outcomes. Both sources turn on transaction costs. Every backtest here charges costs, and
   a strategy only counts if it survives a stress test with higher costs (roadmap step 4).
3. A published backtest is a hypothesis to test on your own data, not a result to rely on.

## 2. Backtesting pitfalls and how to catch them

Source: [dev.to, "Why your backtest is lying to you"](https://dev.to/eedgee/why-your-backtest-is-lying-to-you-3-tests-that-catch-lookahead-bias-overfitting-and-fantasy-2bnc)
(a blog by a vendor of a paid validation kit, so I use the ideas, not the product). The ideas themselves
(deflated Sharpe ratio, probability of backtest overfitting, walk-forward testing) come from published
quantitative finance research that I have not read in the original.

- **Look-ahead bias.** Test: delay execution by 0, 1, 2, 3 bars. A real edge fades gradually; a leak
  collapses after delay 0. Rule adopted: execution delay is at least 1 bar, enforced in code.
- **Overfitting by repeated trials.** The more settings you try, the better the best one looks by luck
  alone. The source's example: a Sharpe-based score of 0.97 for one trial becomes 0.01 for the best of 300.
  Rule adopted: every strategy run records how many settings were tried (roadmap step 4).
- **Unrealistic fills.** Test: find the per-trade cost at which net Sharpe reaches zero (break-even
  cost) and compare it with your real cost. Rule adopted: report break-even cost for each strategy.
- **Walk-forward.** Pick settings on one window, score on the next unseen window. Rule adopted.
- **Trade counts.** The source gives no minimum. I use 100 trades as a flag for "too few to trust". That
  number is my own rule of thumb, not a sourced threshold.

## 3. Costs

- Trading 212 Invest: 0% commission on stocks and ETFs; about 0.15% FX conversion each way when the
  instrument's currency differs from your account currency; spreads about 0.01-0.05% for major index
  ETFs and 0.20-0.50%+ for niche ones, wider at open and close. Source: [quantroutine.com fee
  summary](https://quantroutine.com/brokers/trading-212-fees-explained/), a third-party page whose own
  dates disagree. Check Trading 212's official price list before using any figure.
- Revolut X (crypto): fees were not read. Look them up before backtesting crypto.
- Your costs are set in `trader/config.json`, not assumed by the code. The backtester refuses to run
  without a cost model.

## 4. Per asset class

### Stocks and ETFs (cash, no leverage)
- US stocks trade 15:30-22:00 Berlin time; Xetra 9:00-17:30 (see EUROPE.md for the clocks).
- Both the strongest evidence in section 1 (SPY intraday momentum) and the weakest (retail day trading
  in general) apply here. ETFs have wider spreads at the open and close; this was in the search results
  but I did not read a primary source, so treat it as a reason to model time-of-day spreads, not a fact.
- German tax: 25% flat plus solidarity surcharge (the pack's steuer.py), stock losses only against stock
  gains, fund exemptions for ETFs. Day trading creates many taxable events and many small losses that sit
  in the stock-loss pot.

### Crypto
- Tax: private sales within one year are taxed at your personal income tax rate under § 23 EStG; below
  EUR 1,000 total private-sale gain per year they are tax free, at or above it the whole gain is taxable
  (a Freigrenze, not an allowance); FIFO is accepted by the tax authority per wallet; losses only offset
  other § 23 gains. Source: [Blockpit guide](https://www.blockpit.io/de-de/steuer-guides/krypto-steuer-deutschland),
  a secondary source. I did not read § 23 EStG or the BMF letter. A government reform was said to be
  planned for 2027 with no draft published as of October 2026.
- Consequence: day-trading crypto is the most heavily taxed route here (personal rate up to 42-45%, and
  losses cannot offset salary or stock gains). The tax module for crypto does not exist yet.
- Markets trade 24/7, so there is no session end; "flat at the close" does not apply.
- Revolut X has an API but no paper mode (see the earlier research in this project). Crypto paper trading
  will have to be simulated by our own backtester and a simulated fill model.

### FX and CFDs
- ESMA's retail CFD measures (March 2018 press release; I could not confirm they are unchanged today)
  cap leverage at 30:1 on major currency pairs, 20:1 gold and major indices, 10:1 other commodities, 5:1
  shares, 2:1 crypto, with close-out at 50% of minimum margin and negative balance protection. Source:
  [ESMA](https://www.esma.europa.eu/node/84933).
- I did not research FX tax treatment, CFD loss rules, or any FX API available to you. Revolut and N26
  expose no FX trading API that I found. FX is therefore out of scope until those are researched.

## 5. Metrics that the tool must report

Chosen because each answers "would I have survived and is it real":

- Total and annualised return, against the buy-and-hold benchmark in the same instrument and costs.
- Maximum drawdown (worst peak to trough fall).
- Sharpe ratio (risk-free rate taken as zero, stated in the output); Sortino is a later addition.
- Number of trades, win rate, average win, average loss, expectancy per trade, profit factor.
- Exposure (share of time in the market).
- Total costs paid, as an amount and as a share of gross profit.
- Break-even cost per trade; delay scan; out-of-sample result; number of settings tried.

## 6. What is not known yet

- Where to get free intraday price history for German and European stocks and for crypto, and its
  quality. Daily data is covered by the existing sources. This is roadmap step 2.
- Whether Trading 212's demo API offers the instruments and order types we need.
- Crypto fees at Revolut X, FX tax, CFD tax. Not researched.
