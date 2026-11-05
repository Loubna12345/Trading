# European edition: what was changed, and what each rule rests on

Everything below was read on 2026-11-05. "Read" means I opened the source; "not verified" means I did not.
When a source is a tax-law or news site rather than the statute, it says so. Nothing here is advice.

## Currency
- Base currency is euro. Settings are `*_eur`. `trader/prices.json` is in euro and says so (`"currency": "EUR"`);
  budget, rebalance, risk and tax refuse a prices file that does not.
- Quotes in other currencies are converted once, in `trader-alerts`, with a rate you give and date
  (`--fx USD=1.08 --fx-date 2026-11-05`; one euro buys 1.08 dollars, the ECB's way). A rate older than 3 days, in
  the future, or missing drops that quote. London pence are divided by 100. The original price, currency, rate
  and date stay in `prices.json` under `detail`.
- The ECB rate is not your broker's rate. Your broker converts at its own rate plus a fee; expect a small gap.

## Markets and hours
| market | hours | closed days | source |
| --- | --- | --- | --- |
| Xetra | Mon-Fri 9:00-17:30 Berlin | 2026: Jan 1, Apr 3, Apr 6, May 1, Dec 24, 25, 31. 2027: Jan 1, Mar 26, Mar 29, Dec 24, 31 | Deutsche Boerse trading calendar (read) |
| Euronext | 9:00-17:30 is the usual session; the page read does not state hours (not verified) | 2026: Jan 1, Apr 3, Apr 6, May 1, Dec 25. Dec 24 and 31 are half days with closing time "to be announced" (the tool treats them as closed). 2027 not published | Euronext holiday calendar (read) |
| NYSE | 9:30-16:00 New York | as before (2026, 2027) | nyse.com (read 2026-10-13, upstream) |
No clock for London, Swiss, Nordic, Spanish, Irish or Oslo tickers; the tool says so.

## Assets
- European tickers keep their exchange suffix (SAP.DE, MC.PA). US share classes use a dash (BRK-B).
- A buy of a well-known US-domiciled ETF is blocked. Reason (justETF, read): PRIIPs requires a Key Information
  Document that US funds mostly do not publish, so many EU brokers do not offer them; holders can keep or sell.
  The tool's list is partial; your broker decides. Individual US stocks: the source does not say they are
  restricted, and this tool does not restrict them.
- Leveraged and inverse funds are blocked unless you set `allow_leveraged`.

## German tax (trader-tax)
| rule | source |
| --- | --- |
| 25 % tax; church-tax formula `(e - 4q) / (4 + k)` | § 32d Abs. 1 EStG, gesetze-im-internet.de (read) |
| gain = proceeds - sale costs - purchase cost; foreign currency converted at the sale date and the purchase date | § 20 Abs. 4 Satz 1 EStG (read) |
| FIFO for same-kind securities in one custody account | § 20 Abs. 4 Satz 7 EStG (read) |
| stock losses offset only stock gains; fund/ETF, dividends, interest are the other pot | § 20 Abs. 6 Satz 4 EStG (read); the pot assignment of funds and dividends is from two tax-info sites (finanzfluss, pandotax), not the statute |
| saver's allowance 1,000 / 2,000 euro, actual costs not deductible | § 20 Abs. 9 EStG (read) |
| fund exemption 30 % / 15 % / 60 % / 80 % | § 20 InvStG (read) |
| the exemption also reduces fund sale losses, except the part from pre-2018 fictitious cost | BFH 25.11.2025, VIII R 31/23, via a Haufe summary (read), not the judgment |
| bank nets losses per calendar year; loss certificate request by 15 December | § 43a Abs. 3 EStG (read) |
| solidarity surcharge 5.5 % of the tax | Raisin tax guide (read); § 4 SolZG was not fetched |
| church tax 8 % (Bavaria, Baden-Wuerttemberg) or 9 % | Raisin tax guide (read) |
| whole-formula check: 1,000 euro gain with 9 % church tax = 27.99 % | Raisin table (read); the tool reproduces it |

Unsettled: whether the stock-loss restriction is constitutional is before the Federal Constitutional Court
(2 BvL 3/21); the latest source I found is April 2026 and says a decision is expected in 2026. I found no
decision. The tool names the case whenever you have a stock loss.

Not modelled: Guenstigerpruefung (§ 32d Abs. 6), derivatives, bonds, crypto, pre-2018 fund holdings, treaty
limits on creditable foreign tax, gift/inheritance cost carry-over, and the Vorabpauschale amount itself (enter
the bank's figure as income). I did not read a wash-sale-like rule in § 20 EStG; that is not the same as a
finding that selling and rebuying is always safe, and the tool only lists losses.

## Directors' dealings (trader-insiders)
- MAR Article 19: managers notify the company and the regulator within three business days (Euronext glossary,
  read). Germany's reporting threshold rose from 20,000 to 50,000 euro per calendar year on 2026-01-01 (Heuking,
  read). The default under MAR is 20,000 and countries may set 10,000 to 50,000.
- BaFin's database: keeps 12 months, updated daily (BaFin page, read). The page does not describe an export, and
  the portal's robots rules disallow automated access, so the tool reads a table you save. The real export format
  was never seen: the reader is tolerant, and unreadable rows are reported, not guessed.

## What did not change
13F copy-trading, company facts, screening and the SEC news feed still read US filings and cover US-listed
companies only (each SKILL.md now says so).
