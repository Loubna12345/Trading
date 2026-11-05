---
name: trader-tax
description: >-
  German Tax Tracker. Keeps cost-basis lots in euro, shows unrealized gains
  and losses, previews what a sale realizes (FIFO, the only method German law
  allows inside one custody account) and what it adds to this year's tax, logs
  sales, dividends and carried-in losses, and works out the year under the
  Abgeltungsteuer: separate stock-loss pot, other pot for funds and dividends,
  fund partial exemption, Sparer-Pauschbetrag, 25 % plus solidarity surcharge
  and church tax. Not tax advice. Use when the user says "what are my gains",
  "cost basis", "what would selling cost me in tax", "Steuer", "Abgeltungsteuer",
  "Verlusttopf", "Freistellungsauftrag", "tax-loss harvesting", "Sparer-Pauschbetrag",
  or before selling anything.
argument-hint: "[ticker]"
---

# trader-tax

The Tax Tracker does the arithmetic on the user's lots so nobody sells blind.
It follows German rules for a private investor. It is a calculator, not advice
and not a filing: say "this is not financial advice or a tax return; compare it
with your broker's Steuerbescheinigung" once, early, in every answer that uses
this skill. Where the law is unsettled the tool says so, and so should you.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/steuer.py" settings --status single --church 0.09   # once
python3 "${CLAUDE_SKILL_DIR}/steuer.py" report --lots                    # unrealized, by lot, in euro
python3 "${CLAUDE_SKILL_DIR}/steuer.py" sell SAP.DE 3 --price 140 --fees 1.5   # preview, changes nothing
python3 "${CLAUDE_SKILL_DIR}/steuer.py" sell SAP.DE 3 --price 140 --fees 1.5 --record   # it filled
python3 "${CLAUDE_SKILL_DIR}/steuer.py" income --ticker KO --type dividend --gross 30 --foreign-tax 4.5
python3 "${CLAUDE_SKILL_DIR}/steuer.py" carry --year 2026 --stock 300 --other 0
python3 "${CLAUDE_SKILL_DIR}/steuer.py" year --year 2026
python3 "${CLAUDE_SKILL_DIR}/steuer.py" harvest --min-loss 100
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/portfolio.json` | positions with lots `{"id", "date", "shares", "cost"[, "depot"]}`; `cost` is the total purchase cost **in euro, fees included**. A fund/ETF position has `"fund_type"`. Shape: `portfolio.example.json` here |
| `trader/prices.json` | current prices **in euro** (trader-alerts: `alerts.py quote --holdings --fx USD=RATE --fx-date DATE --write`). The tool refuses a prices file that does not say it is in euro |
| `trader/tax.json` | your settings, every recorded sale, dividends and carried-in losses |

Buys are recorded with `ticket.py fill` (it keeps the cost in euro: a dollar
price needs `--currency USD --fx RATE`, the rate of that day). **Sales are
recorded only here** (`sell --record`), because that is what logs the taxable
gain; `ticket.py fill` refuses sells. If a position has shares but no lots, ask
for the purchase dates and euro costs from the broker statement. Never invent a
cost basis.

## The rules, and where they come from

Read from the statute texts on gesetze-im-internet.de on 2026-11-05 (the BFH
ruling and the loss-pot overview from court and tax-law sites the same day).

- **Gain** (§ 20 Abs. 4 Satz 1 EStG): proceeds minus the costs of the sale
  minus the purchase cost. For a foreign-currency trade, proceeds are converted
  at the sale date and the cost at the purchase date, so a currency move is
  part of the gain. A US stock bought at 100 USD and sold at 100 USD can still
  be taxable if the dollar fell against the euro.
- **FIFO** (§ 20 Abs. 4 Satz 7 EStG): shares of the same kind in one custody
  account are sold oldest first. There is nothing to choose, so the tool has no
  other method. Lots in two depots: the tool asks which depot (`--depot`).
- **Two loss pots** (§ 20 Abs. 6 EStG): a loss from selling **stocks** offsets
  only gains from selling stocks. Everything else (fund/ETF sales, dividends,
  interest) is the **other pot**; a loss there offsets any capital income, stock
  gains included, but a stock loss cannot offset a fund gain or a dividend.
  What is left carries forward in its own pot.
- **Allowance** (§ 20 Abs. 9 EStG): 1,000 euro, 2,000 euro for a joint
  assessment. Actual costs cannot be deducted on top (the fees are already in
  the gain). If part of your allowance sits at another bank
  (Freistellungsauftrag), set the amount left with `settings --allowance`.
- **Fund exemption** (§ 20 InvStG): of the income from an equity fund 30 % is
  tax-free, mixed fund 15 %, real-estate fund 60 %, foreign real-estate fund
  80 %. It applies to gains, distributions and sale **losses** alike (BFH
  25.11.2025, VIII R 31/23). Set `fund_type`: `equity`, `mixed`, `real_estate`,
  `foreign_real_estate` or `other`. Most world-index ETFs are `equity`; your
  fund's factsheet or KID states its classification.
- **Tax** (§ 32d Abs. 1 EStG): 25 %. With church tax rate k the tax is
  `(e - 4q) / (4 + k)`, where q is credited foreign tax. Solidarity surcharge
  is 5.5 % of that tax, church tax is k times it (k = 0.08 in Bavaria and
  Baden-Wuerttemberg, 0.09 elsewhere). Check: 1,000 euro gain with 9 % church tax
  is 27.99 % in total; with none, 26.375 %.

## Open points: tell the user when they apply

- **Constitutional Court.** Whether stock losses may only offset stock gains
  is before the Federal Constitutional Court (2 BvL 3/21). No decision in any
  source read on 2026-11-05. Advice from tax-law sites: keep loss certificates
  and tax assessments. Re-check the status before relying on either outcome.
- **Banks net per account.** The bank withholds during the year, per custody
  account. This tool nets everything the user entered, as the assessment does.
  A loss left at one bank can be used against gains at another bank only through
  a loss certificate requested by **15 December** (§ 43a Abs. 3 Satz 4-5 EStG).
  The tool reminds in October to December.
- **Assessment option.** With a personal tax rate under 25 % the assessment
  can be cheaper (§ 32d Abs. 6 EStG, Guenstigerpruefung). Not modelled.
- **Not modelled:** derivatives, bonds, crypto, pre-2018 holdings of funds,
  treaty limits on creditable foreign tax (the user gives the creditable
  amount), Vorabpauschale of accumulating funds (enter it as income of type
  `fund_distribution` with its `fund_type`; the bank's statement has the amount),
  cost carry-over for gifts and inheritance.
- **No wash-sale rule** is in what was read of § 20 EStG. That is the absence
  of a rule in the text read, not a finding that selling and rebuying is
  always safe: the tax office can question arrangements that exist only to save
  tax. `harvest` therefore only lists losses and what they could offset.

## Steps

1. First use: `settings` (single or joint, church tax rate). Ask; do not guess.
2. Refresh prices in euro, then `report --lots`. Show gains and losses by
   position, funds with their exemption.
3. Before any sale, `sell TICKER SHARES --price P`. Show the gain, which pot it
   lands in, and the change in this year's tax, in one or two lines. A sale
   inside the allowance costs nothing; a stock loss with no stock gains this
   year is carried forward, not lost.
4. After the user's yes (trader-broker) and the fill, `sell ... --record`. Not
   before. Buys go in with `ticket.py fill`.
5. Dividends and fund distributions go in with `income`. Last year's loss
   certificate goes in with `carry`.
6. `year` any time, and in December. `harvest` lists unrealized losses and
   what they could offset: share losses only against stock gains.

## Rules

- Never sell, and never write an order. Anything that becomes a trade goes
  through sizing, trader-risk and the user's own yes.
- Never invent a cost, a date or a rate. Missing lots mean ask.
- Say the figures are an estimate: the broker's tax certificate decides.
