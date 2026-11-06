# Progress and handoff

Last updated 2026-11-05. Read this first in a new session. It says what exists, what was decided, what is
unverified, and what comes next. Everything below is in this repo; nothing depends on a chat.

## Who and what

Loubna lives in Berlin, is single with no church tax, and wants to trade with real money eventually, from
Germany, in euro. She started from the `Jakeschincariol/trader-skill` pack (11 Claude trading skills), audited
it, copied it to this repo, hardened it, made it European, and is now building a day-trading research tool
that tests rules on past data under strict checks. She wants to start on paper money only, never to put keys,
passwords or tokens in chat, and no reliance on a tax adviser: the tax logic comes from the law, with limits
documented.

## Branches (github.com/Loubna12345/Trading)

| branch | what |
| --- | --- |
| `main` | the hardened copy of the upstream pack (audit fixes), untouched since |
| `hardening` | the audit fix work |
| `europe` | European edition: euro, German tax (`trader-tax/steuer.py`), directors' dealings (`trader-insiders`), Xetra/Euronext clocks, hygiene tests; not merged into main (her decision pending) |
| `learning-agent` | everything below, branched from `europe`; this is the working branch |

## What is built (learning-agent), with the roadmap in docs/ROADMAP.md

| step | skill | state |
| --- | --- | --- |
| 1 | `trader-backtest` | done: bar-based backtester, costs required, orders fill at a later bar's open (delay at least 1), end-of-day flatten, buy-and-hold benchmark, metrics |
| 2 | `trader-data` | done: Yahoo and Kraken parsers, growing local store, gap and bad-bar check, `watch` loop. Live downloads NEVER tested (the build environment cannot reach those hosts) |
| 3 | `trader-strategies` | done: trend, opening-range breakout, VWAP reversion, long-only, no default parameters |
| 4 | `trader-validate` | done: cost stress, delay scan, walk-forward, deflated Sharpe on the alpha, trade count, recent stretch, bad-bar check; verdict PASS/FAIL/INCONCLUSIVE |
| B | `trader-training` | built (see below); only tested on made-up data |
| B2 | `trader-replay` | built: weekly replay with hidden stock and dates, rule logged before each reveal, sealed last third, one final test per rule; made-up data only so far |
| 5 | asset profiles | not started: hours, costs, tax and limits per stocks, ETFs, crypto; crypto needs its own tax module |
| 6 | journal and paper broker | not started: Trading 212 demo adapter behind the risk check and approval |
| 7 | weekly review loop | not started |
| 8 | go or no-go gate for real money | not started |

Test suite: `python3 -m unittest discover -s tests` (run from the repo root; no network needed). Slow stress run:
`python3 tests/stress_run.py 12` (about 6 minutes). Findings are in docs/STRESS_TEST.md.

## The training step (trader-training)

Protocol (written in docs/ROADMAP.md): fixed plan, tamper-evident ledger of every run, discovery and confirmation
stocks, an untouched holdout, a chance rate measured on shuffled copies of the same stocks, a binomial test on the
share of stocks that pass, one final holdout test per rule, a round limit. Commands: `plan`, `discover`, `confirm`,
`final`, `amend`, `status`, `verify` in `trader-training/train.py`. She will provide real data; the intended use is
hourly bars on 5 to 10 stocks first.

## Decisions and constraints from her

- Euro base currency; US and European stocks; German tax from the law (Abgeltungsteuer, stock-loss pot, fund exemption).
- Real broker with an API and paper money, usable from Germany. Findings: Revolut X (crypto) has an API and an
  official AI connector but no paper mode and the MCP part is read-only; Revolut and N26 stocks, ETFs and FX have
  no public API (Upvest runs them behind the app); Trading 212 has a beta REST API with a demo environment, euro
  accounts only, no multi-currency; Alpaca is US only and USD-funded; Interactive Brokers (Ireland) has TWS paper.
- Keep the "Claude gate" order flow (script checks limits, Claude shows the order, she types yes). A redesign where
  Claude cannot place orders is parked.
- Day trading first. Per-asset-class knowledge wanted (stocks, ETFs, crypto). No fine-tuning of the model; the
  tool learns by a journal and tests, not by changing weights.
- She wants honest results: most day traders lose money after costs (docs/RESEARCH.md); PASS is only permission to
  try a rule on paper money.

## Agent-versus-rules comparison (agreed 2026-11-06, not yet built beyond the replay)

She wants two arms judged from outside: fixed rules, and an agent called through code with only the allowed
context (anonymous bars up to the decision, fixed answer format, answers logged before scoring, notes file
logged). A script judges with a comparison locked in advance (net result after costs vs the rule arm and vs
buy-and-hold, plus a significance test), not Claude. Plan: replay tool (done), agent runner using the Claude API
(needs her own API key kept on her computer, never in chat; hard budget cap), judge script. Suggested models:
Haiku 5.5 for rehearsal, Sonnet 5.5 for the judged run, Fable 5.1 only as a later challenger. Costs quoted from the
pricing page on 2026-11-06 (USD per million tokens, in/out): Haiku 0.10/0.50, Sonnet 2/10, Opus 4/20, Fable 10/50.
A replay on past data cannot cleanly test an agent that may remember the period; only data after its knowledge
ends, and paper trading going forward, count.

## Real-data findings (2026-11-06)

First live download worked on her Mac (SAP.DE, 60-minute bars, 4349 bars). It exposed three things now fixed:
Python on the Mac lacked certificates (fix: install certifi and set SSL_CERT_FILE), asking for exactly 730 days
returned HTTP 422 (700 worked), and Yahoo adds a live-price row inside the forming bar (now dropped). She asked
that Claude not read the downloaded results until the replay tool exists; reading goes through the replay tool.

## Unverified or open

- Live downloads from Yahoo and Kraken, Yahoo's real intraday limits, and data delays: untested.
- Cost figures for Trading 212 and Revolut X are third-party or missing; she must confirm from her broker's price list.
- Crypto tax comes from a secondary source (Blockpit); § 23 EStG and the BMF letter were not read.
- FX and CFD tax, and any FX API: not researched.
- The deflated Sharpe formula follows a library implementation; the original paper was not read.
- All validation so far is on made-up data. All thresholds (100 trades, 0.95, 2x costs, 20% bar range, last third,
  significance 0.05) are this project's choices.
- Merge of `europe` / `learning-agent` into `main`: her decision.

## Next

1. Her real data (hourly bars, several stocks): run the training protocol and report honestly, including FAIL.
2. Step 5 asset profiles, then crypto tax module.
3. Steps 6 to 8.
4. Parked: script-gate redesign, daily loss limit and kill switch, untrusted-news handling, Trading 212 adapter.

To get the data she runs (on her computer):
`python3 trader-data/history.py fetch yahoo SAP.DE --interval 60 --days 730 --store trader/history`
and sends the CSV files from `trader/history/`.

## Working rules used throughout

Test first, one logical change per commit, commits authored `Claude <noreply@anthropic.com>` with the footers
`Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: <session url>`; hygiene tests ban
US-only leftovers in skill files; nothing places an order without trader-risk and her typed yes.
