---
name: trader-training
description: >-
  The Trainer. Runs the day-trading rules over many stocks of real saved price data under rules fixed in advance:
  a locked plan, a tamper-evident ledger of every run, discovery and confirmation stocks, an untouched holdout, a
  chance rate measured on shuffled copies of the same stocks, and one final test per rule. Use when the user says
  "train on this data", "run the strategies on these stocks", "which rules hold up across stocks", "find edges",
  "another round", or hands over real price files to learn from.
argument-hint: "[plan|discover|confirm|final|amend|status|verify]"
---

# trader-training

Trying many rules on many stocks and keeping what works is the easiest way to fool yourself. This skill makes
that harder, and reports honestly when nothing survives. It places no orders.

```bash
python3 "${CLAUDE_SKILL_DIR}/train.py" plan --store trader/history --interval 60 \
    --tickers SAP.DE,SIE.DE,ALV.DE,BAS.DE,DTE.DE,MUV2.DE --periods-per-year 2268 --flatten-eod \
    --grid trend:window=10,20,40 --grid orb:range_bars=1,2 --commission-bps 0 --spread-bps 2 --slippage-bps 1
python3 "${CLAUDE_SKILL_DIR}/train.py" discover
python3 "${CLAUDE_SKILL_DIR}/train.py" confirm
python3 "${CLAUDE_SKILL_DIR}/train.py" final --strategy trend
python3 "${CLAUDE_SKILL_DIR}/train.py" status
python3 "${CLAUDE_SKILL_DIR}/train.py" verify
```

Bars come from /trader-data (one CSV per stock and interval). Costs are required. Files go in `trader/training/`
(`plan.json`, `ledger.jsonl`).

## The protocol

1. **plan** fixes everything before any run: the stocks, which are discovery and which confirmation (random but
   seeded, or listed by the user), every setting of every rule, the costs, the holdout fraction (10 to 50% of the
   newest days of every stock), and the number of rounds. It needs at least 3 stocks per role. The plan is locked:
   its hash is written to the ledger, and a plan file edited by hand is refused.
2. **discover** runs each rule's settings on each discovery stock, using only bars before the holdout, and the same
   on shuffled copies of the stock (the order of moves inside each day is randomised). The share of shuffled copies
   that "pass" is the chance rate on this data.
3. **confirm** takes rules that pass on more discovery stocks than chance would give (binomial tail below 0.05, at
   least 2 passing stocks), freezes their most common settings, and runs them once on the confirmation stocks.
4. **final** gives a confirmed rule ONE test on the untouched holdout of every stock. It holds only if it is
   profitable after costs, with doubled costs, with orders two bars late, and beyond the market's own drift, on
   a share of stocks that chance could not give. A second final test of the same rule is refused.
5. **amend** adds a new rule or stock. It is logged, never changes existing rules, and the next `discover` is a new
   round. Rounds are limited; when they are used up the data set is spent and new data is needed.
6. **ledger**: every run is appended with a hash chain, `verify` detects an edited or deleted middle line. Settings
   already tried on a stock count as earlier trials in the next validation of that stock, so repeating runs makes
   the bar higher. Cutting off the last lines cannot be detected; keep a copy of the ledger elsewhere.

The thresholds (0.05, 2 passing stocks, 3 stocks per role, 10 to 50% holdout) are this project's choices.

## How to run it honestly

- Say before starting that FAIL on every rule is the likely and acceptable result.
- Do not add rules after seeing which worked; if one is added, use `amend`, say why, and report the round count.
- Do not look at the holdout or run `final` early. Do not delete and recreate the plan to get a second try; say
  that this destroys the protocol.
- Use as many stocks as the data allows. With 6 stocks the evidence is weak, and a pass on all 3 confirmation
  stocks is still only 3 observations.
- Report: round, stocks per role, pass share against the chance rate, p-values, and the final verdict with the
  number of stocks that held.

## What to tell the user

- A HOLDS verdict means a rule survived costs, unseen stocks and an untouched period on this data. It is
  permission to try the rule on paper money, not a forecast, and not tax-adjusted.
- The shuffle null removes order inside a day. It measures the pass rate for rules without intraday structure; a rule
  built on drift or on gaps between days may pass the null for reasons unrelated to chance. Say so if relevant.
- Tested only on made-up data so far (see docs/PROGRESS.md).

## Safety

- Education and tooling, not financial advice. Nothing here predicts prices or promises returns.
- This skill places no orders and needs no keys. Anything traded still goes through /trader-risk and needs the
  user's explicit yes in /trader-broker.
