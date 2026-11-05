---
name: trader-insiders
description: >-
  Directors' Dealings Tracker. Reads the table of managers' own share trades
  that European listed companies must report under Article 19 of the Market
  Abuse Regulation (BaFin's database for German issuers, other countries'
  regulators for theirs), cleans it into rows (who, role, company, ISIN, buy
  or sell, price, value, trade date, notification date, business days of delay),
  shows only what is new since the last check, flags companies you hold, and
  points out clusters of several insiders buying within 30 days. Not financial
  advice. Use when the user says "what are the directors buying", "insider
  buying", "Directors' Dealings", "Eigengeschaefte von Fuehrungskraeften",
  "managers' transactions", "did the CEO buy", or "run the insiders skill".
argument-hint: "[file] [--side buy] [--min-value 50000]"
---

# trader-insiders

Board members and top managers of a listed company, and people close to them,
have to report their own trades in the company's shares. The company publishes
the report and the national regulator keeps a database. This skill turns a
table from that database into clean rows and notices what is new.

It is public information read after the fact. It is not inside information
and not a tip. Say that every time, and say it is not financial advice. A
trade is reported up to three business days after it happens, and the
database shows it a day or two after that. Many insider purchases are routine
(share programmes, a signal of nothing). One buy by one director means little;
the tool points out only one pattern, a cluster.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/dealings.py" list dealings.csv --side buy --min-value 50000
python3 "${CLAUDE_SKILL_DIR}/dealings.py" new dealings.csv        # only rows not shown before
python3 "${CLAUDE_SKILL_DIR}/dealings.py" list dealings.csv --portfolio trader/portfolio.json
```

## Where the data comes from

The tool **does not download anything**. BaFin's database page does not
describe an export function, and the portal's robots rules disallow
automated access, so Claude does not scrape it. The user opens the database
(`bafin.de` → "Eigengeschaefte von Fuehrungskraeften", the database link on
that page), searches by company or date, and saves the result table: select it,
paste it into a spreadsheet, export CSV. If the page offers an export button,
use that. Other countries have their own regulator databases (AMF in France,
AFM in the Netherlands, CONSOB in Italy and so on); the same reader works on
their tables if the columns can be recognised.

The reader was written without a sample of the real export, so it is tolerant:

- separators `;` `,` or tab; UTF-8 or Windows-1252;
- German or English column names (list at the top of `dealings.py`);
- numbers like `61.700,00` and `1,234.56`. A number that could mean two things
  (`1.234`) is refused until the user says `--decimal comma` or `--decimal dot`;
- dates `2026-11-05`, `05.11.2026`, `05/11/2026` (day first).

A row it cannot read is printed with its line number and **skipped**, never
guessed and never marked as seen. If the header is not recognised it lists the
columns it found: show them to the user and, if needed, rename the headers in
the CSV to the English names (`Issuer`, `ISIN`, `Notifying person`, `Position`,
`Transaction type`, `Price`, `Total volume`, `Currency`, `Date of transaction`,
`Notification date`).

## What it reads and writes

- Reads the CSV the user gives it; `trader/portfolio.json` (positions with an
  `"isin"` field are flagged `[YOU HOLD ...]`); optionally `trader/isin.json`
  (`{"DE0007164600": "SAP.DE"}`) to match companies by ISIN to tickers you hold.
- Writes only `trader/state/insiders.json` (fingerprints of rows already shown),
  and only for rows it could read.

## The rules

- **Quote the filing.** Every row comes from the regulator's table. Names,
  dates and amounts are theirs; never round a range into a precise number.
- **Say the delay.** Each row shows business days between the trade and the
  notification. `LATE` is more than three weekdays; public holidays are not
  subtracted, so check before saying anyone broke a rule.
- **Buy, sell, other.** Option exercises, grants, gifts and pay in shares are
  `other`; they are not a decision to buy at the market. Do not call them buys.
- **Threshold.** Germany's reporting threshold rose from €20,000 to €50,000 per
  calendar year on 2026-01-01, so small trades may never show. Other countries
  set their own between €10,000 and €50,000.
- **Cluster** means two or more different insiders of one company bought
  within 30 days. It is a reason to read the company's news, not a reason to buy.
- **Not an order.** To act on anything, the normal path runs: size
  (`trader-budget`), check (`trader-risk`), and the user's own typed yes
  (`trader-broker`). This skill never writes an order.

## Steps

1. Ask the user for the saved table, or help them get it as above. Do not try
   to fetch the database yourself.
2. `new FILE` for what changed, or `list FILE --side buy --min-value 50000` for
   a view. Add `--portfolio trader/portfolio.json` to flag holdings.
3. Report in a few lines: the cluster first if there is one, then the biggest
   buys, then sells over €500,000 (sells are common and often plain tax or
   diversification), with the delay shown. Skip `other` rows unless asked.
4. Close with one sentence: public filing, read after the fact, not advice.
