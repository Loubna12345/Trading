# Roadmap: a day-trading research agent on paper money

Goal: an agent that proposes and tests short-window trades on paper money, keeps a journal, and tells you
honestly whether a rule works. It does not promise predictions. The research (docs/RESEARCH.md) shows
most day traders lose after costs, so the tool's first job is to say "this rule does not survive costs"
quickly and cheaply.

Rules for every step: tests first, one logical change per commit, no network in tests, nothing places an
order without the existing risk check and your yes, paper keys only.

| Step | What | Done when | Status |
| --- | --- | --- | --- |
| 1 | `trader-backtest`: metrics and a bar-based backtester (costs, execution delay of at least 1 bar, end-of-day flatten, buy-and-hold benchmark) | tests pass; a benchmark run on a CSV prints metrics | done |
| 2 | Price history loader: intraday bars for US stocks, European stocks and ETFs, crypto; each source checked for gaps, time zones, splits | each source has a documented limit and a validity check; bad data is refused | done for Yahoo and Kraken; live download not yet tried |
| 3 | Strategy library as rule files, one per idea (opening range breakout, VWAP reversion, trend filter, momentum), each with its reason and its asset class | each strategy runs in the backtester and has tests on synthetic bars | done for trend, orb, vwap; momentum not yet; not run on real bars |
| 4 | Validation harness: delay scan, break-even cost, walk-forward, count of settings tried, deflated Sharpe | a strategy gets PASS or FAIL with the reasons; FAIL is the expected result for most ideas | done; checked on made-up data only |
| 5 | Asset profiles: stocks, ETFs, crypto each with hours, costs, tax treatment and risk limits (crypto needs its own tax module; FX is out of scope until researched) | a profile is required to run a backtest or a paper trade | not started |
| 6 | Journal and paper broker: every signal, order, fill and exit logged; Trading 212 demo adapter behind the existing risk check and approval | one full day of paper trades logged and reconciled with the broker | not started |
| 7 | Review loop: scheduled weekly review that computes the numbers, Claude reads the journal and proposes rule changes, each change is re-validated before use | a rule change only goes live if it passes step 4 on data it was not tuned on | not started |
| 8 | Go or no-go gate for real money: minimum months of paper results, minimum trades, beats the buy-and-hold baseline after costs and German tax | written criteria, checked by a script, you decide | not started |
| B | **Bonus: training.** Repeated runs of every strategy on real data and many stocks, under a fixed protocol, to find edges that hold up across stocks and time (see the protocol below) | a ledger of every run, a verdict per rule across stocks, and one final untouched test per surviving rule | not started; needs your real data |

## What "learning" means here

The agent does not change its own weights. It improves by evidence: journal, numbers, a proposed change,
a test on unseen data, then adoption or rejection. Versions of each strategy are kept so version 3 can be
compared with version 2.

## Bonus: the training protocol

Running many rules on many stocks and keeping what works is the easiest way to fool yourself, so the training
has fixed rules written before any run:

1. **Write the idea first.** Every strategy has a reason stated before it is run. No rule is added after seeing
   which ones worked.
2. **Split the stocks.** Pick tickers for discovery and keep others untouched for confirmation. A rule that works
   only on the stocks it was found on is rejected.
3. **Keep the last months untouched.** The most recent part of every series is not used until the final test, and
   each rule gets that one test once.
4. **Keep a ledger.** Every run (rule, settings, stock, period, result) is appended to a file that cannot be edited
   by the tools. The number of runs in the ledger feeds the deflated Sharpe automatically, so repeating runs
   makes the bar higher, not lower.
5. **Judge across stocks, not on the best one.** A rule is only interesting if it passes on a clear share of the
   stocks, and that share is compared with what chance would give (a rule with no edge will pass on some stocks
   by luck).
6. **Limit the rounds.** A fixed number of rounds per data set; when it is used up the data set is spent and new
   data is needed.
7. **Forward test last.** What survives goes to paper money (step 6) as the final out-of-sample test.

## Known limits

- Paper trading hides costs and slippage. Backtests and paper results will look better than real trading.
- Crypto in Germany is taxed at your personal rate inside one year, with losses not usable against salary
  or stock gains; a government reform for 2027 was reported as planned with no draft yet.
- No Revolut X paper mode exists; no Revolut or N26 API for stocks, ETFs or FX was found.
- Day trading on a small account may not beat a world index fund. Step 8 exists to check this.

## Bonus: the replay (trader-replay)

Weekly replay with the stock and dates hidden. For every week after the first, a rule is written to the ledger
before the week is shown; then the week is revealed and the rule scored with costs. The last third of the weeks
is sealed; one final test per rule. Each rule tried counts as a trial. Limits are in trader-replay/SKILL.md: the
source file can be opened by anyone with access, and a model that knows the real period may remember it.
