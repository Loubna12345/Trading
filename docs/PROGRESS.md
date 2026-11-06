# Progress and handoff

Last updated 2026-11-06. Read this first in a new session. It says what exists, what was decided, what is
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
| B3 | `trader-agent` | built (2026-11-06): a model as a rule via the Claude API, judged by code with criteria locked in the ledger; never run against a real model yet (no API key set) |
| B2 | `trader-replay` | built: weekly replay with hidden stock and dates, forecast and rule logged before each reveal, sealed last third, one final test per rule, HTML report; first run on SAP done (see below) |
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

## Agent-versus-rules comparison (built 2026-11-06, `trader-agent`)

Two arms judged from outside: a named rule, and a model called through code. `agent.py`: once per trading day,
after the day's first two bars, the model gets only anonymized basis-point moves (last 5 days plus today so far),
the cost per trade side and a fixed instruction, and answers JSON `{"position": 0|1, "reason": ...}`; held to the
close. No ticker, dates or times. Every answer goes to a hash-chained ledger in `<replay>/agent/` keyed by the
prompt hash, so reruns are free and reproducible; an unusable answer is retried once then counts as flat; a hard
budget (dollars, from reported token usage) stops the run and a rerun continues. The key is read only from the
environment variable `ANTHROPIC_API_KEY` on her computer, never printed or logged. `judge.py`: `lock` writes
window (open or sealed), rule, model, prompt hash, budget, seed and verdict criteria into the replay ledger before
any run; `run` scores agent, rule, flat and the stock on the same backtester and costs; `status`. Verdict: at
least 20 weeks, paired bootstrap p below 0.05 on weekly differences, total gap at least 1 point, winner profitable
after costs; else NO CLEAR DIFFERENCE or INCONCLUSIVE. Open window can be rerun free from cache; sealed window
runs once and uses up that folder's final test. Rehearse with Haiku in a separate scratch replay folder.
Models and prices (USD per million tokens in/out, from the pricing page on 2026-11-06): Haiku 5.5 0.10/0.50,
Sonnet 5.5 2/10, Opus 4/20, Fable 5.1 10/50. Limit: a model that remembers the period may recognise it despite
hidden names and dates; only data after its knowledge and paper trading going forward count cleanly.

## Real-data findings (2026-11-06)

First live download worked on her Mac (SAP.DE, 60-minute bars, 4349 bars). It exposed three things now fixed:
Python on the Mac lacked certificates (fix: install certifi and set SSL_CERT_FILE), asking for exactly 730 days
returned HTTP 422 (700 worked), and Yahoo adds a live-price row inside the forming bar (now dropped). She asked
that Claude not read the downloaded results until the replay tool exists; reading goes through the replay tool.

## First replay run on SAP (2026-11-06, dry run, placeholder costs 0 / 2 / 1 bps)

Played by Claude by hand, weeks 2 to 64 of 97 (33 sealed weeks untouched, no final test run). Folder
`replay-dryrun` and report `sap-replay-report.html` sit next to the repo on her laptop (Documents/trading), plus
the helper `play_week.py`. Result: rules -6.1% after costs, holding the stock -31.3% (true move, weeks 2 to 64),
staying out 0. Trend (window 6) about break-even after costs (-0.3%, 132 trades, about 5 points of costs), VWAP
(25 bps, warmup 4) -5.8%. Assumptions: A (momentum after a rising week) direction right 11 of 22, B (rebound after
a fall) 6 of 20 and the stock kept falling, C (stay out after a flat week) neutral, D (stay out after a drop of 5%
or more, added after seeing the week-13 loss) missed a +24.8% rebound over its 6 weeks. Return forecasts no better
than always zero; direction right 29 of 63 against 31 for always-up. Cost sensitivity on those weeks: no costs +1.9%, run costs -6.1%, double costs -13.4%, with Trading 212's 0.15% FX fee -49.1%. Trading 212 is commission-free but charges 0.15% when the instrument currency differs from the account currency; its spread is not published, so spread and slippage stay assumptions. Overnight gaps -15.2% and intraday -19.0% over
the stretch, so the early guess that overnight dominated did not hold. No edge found; a falling stock favours
anything that is often out of the market. Per-week buy-and-hold in the ledger skips each week's first bar and the
gap before it; the report uses the true move. SAP's open weeks are now seen by Claude, so use other stocks for a
clean replay. Costs must be confirmed from her broker's price list before a real replay.

The replay folder was made in a sandbox and stored that sandbox's data path; on her Mac it would not open. It was
moved with `replay.py relocate --dir replay-dryrun --source history/SAP.DE_60m.csv` (2026-11-06): same SHA-256,
logged in the ledger, plan untouched. `play_week.py` now finds the replay next to itself.

## Fixes on 2026-11-06 (after reading the code)

- `trader-validate`: with one setting tried, the deflated Sharpe ignored `--earlier-trials` (no spread of Sharpe
  ratios across trials, so the luck level was 0). It now uses the sampling variance of a zero-edge Sharpe ratio,
  1/(n-1), and reports `trial_variance_source`. Still open: with several similar settings the measured spread is
  small and understates luck; changing that would move existing pass rates.
- `trader-replay`: new `relocate` command (above).
- `trader-agent` (never run against a real model yet):
  - `judge.py lock` crashed on the command line (read a budget option that did not exist); fixed and tested.
  - All listed models think before answering and thinking counts against `max_tokens`; the old limit of 300
    could cut answers off, and a cut answer counted as "stay out". Now `max_tokens` 16000 and effort `medium`
    set explicitly (the default differs by model); both are in the locked hash and the cache key
    (PROMPT_VERSION 2); the stop reason of every attempt is logged. Model IDs and prices were checked against
    the claude-api reference on 2026-11-06 and match.
  - The budget is checked before every call against the most that call could cost, so it is a hard cap.
  - The sealed window now takes only a rule frozen in the replay and shares the replay's one final test with
    it, in both directions.
  - Not changed: the `hold` arm's weekly returns skip the gap between weeks while its total includes it (not
    used in the verdict); no refusal fallback to another model (the arm must be the locked model).

## Runs on 2026-11-06 (Haiku 5.5, €500 per run, costs 0 / 2 / 1 bps, weeks 2-64)

Reports in `~/Documents/trading/reports/` (never overwritten; `index.html` lists them).

| Stock | Who | Mode | End | Saved as |
| --- | --- | --- | --- | --- |
| SAP | rules by hand | day-only | €469.60 | rules-run-01 |
| SAP | agent v1 | day-only | €483.95 | agent-run-01 |
| SAP | agent v1 | overnight | €450.92 | agent-run-04 |
| SAP | just holding | | about €343.50 | |
| Siemens | rules by hand | day-only | €491.58 | rules-run-02 |
| Siemens | agent v1 | day-only | €499.10 | agent-run-03 (02 is a partial save, marked) |
| Siemens | agent v1 | overnight | €528.15 | agent-run-05 |
| Siemens | just holding | | about €566.50 | |

Every comparison: NO CLEAR DIFFERENCE. Reading the runs: the agent froze into cash ("paused until paper wins"),
its instructions said cash costs nothing, it copied raw data into its notebook, it did not use overnight holding
(one-night trades on SAP, 5 trading days in 63 weeks on Siemens), and forecast exactly zero in most weeks. Version 2
of the agent and overnight rules were built from this. Claude has seen every SAP and Siemens week in the chat, so
its own hand play on those stocks is no longer clean; fresh helpers with no memory play the rules from now on.

## Unverified or open

- Live downloads from Yahoo and Kraken, Yahoo's real intraday limits, and data delays: untested.
- Cost figures for Trading 212 and Revolut X are third-party or missing; she must confirm from her broker's price list.
- Crypto tax comes from a secondary source (Blockpit); § 23 EStG and the BMF letter were not read.
- FX and CFD tax, and any FX API: not researched.
- The deflated Sharpe formula follows a library implementation; the original paper was not read.
- All validation so far is on made-up data. All thresholds (100 trades, 0.95, 2x costs, 20% bar range, last third,
  significance 0.05) are this project's choices.
- Merge of `europe` / `learning-agent` into `main`: her decision.
- The training holdout test needs every stock to hold when there are 6 stocks (binomial at 0.5, p < 0.05); 5 of 6
  is DOES NOT HOLD. Use more stocks or accept that.
- The ledgers detect edits, not a rewrite with recomputed hashes; keep an outside copy.

## Next

1. Her side: `git pull` in `~/Documents/trading/repo`; create an Anthropic API key with a monthly limit and set
   `ANTHROPIC_API_KEY` in her own Terminal (never in chat).
2. Round 2: SAP, SIE, ALV, BAS, DTE with agent v2 overnight and overnight rules played by fresh helpers;
   then a Sonnet run if she decides on it.
3. Confirm Trading 212 spread and slippage so the placeholder costs (0 / 2 / 1 bps) can be replaced.
4. Haiku rehearsal of the agent comparison in a scratch replay folder, then a locked Sonnet run: open window first,
   sealed window once, on a stock Claude has not seen.
5. Training protocol on the multi-stock data; report honestly including FAIL.
6. Step 5 asset profiles, then the crypto tax module; steps 6 to 8.
7. Parked: script-gate redesign, daily loss limit and kill switch, untrusted-news handling, Trading 212 adapter;
   merge of `europe` / `learning-agent` into `main` is her decision.

## Working rules used throughout

Test first, one logical change per commit, commits authored `Claude <noreply@anthropic.com>` with the footers
`Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: <session url>`; hygiene tests ban
US-only leftovers in skill files; nothing places an order without trader-risk and her typed yes.
