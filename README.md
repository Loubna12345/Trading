# Trading (European edition)

A hardened, personal derivative of an MIT-licensed set of Claude trading skills (see NOTICE.md), rebuilt for a
trader in Germany/Europe. It reads public data, sizes positions in euro, checks orders against limits and tracks
German capital-gains tax. It does not place orders by itself, and it is paper-only until the open items in
docs/FINDINGS.md are closed.

- Not financial advice. Nothing here is a recommendation. You can lose money.
- Install locally only: copy the `trader-*` folders into your own skills directory. There is no plugin manifest
  on purpose, so nothing can auto-update from anywhere.
- Run the tests before every change: `python3 -m unittest discover -s tests -v`
- Keys never go in chat. SEC requires a name and email in `sec_user_agent` (trader/config.json).
- What was changed for Europe, and the source behind each rule: docs/EUROPE.md
- Status of every audit finding (fixed and open): docs/FINDINGS.md

## The skills

| skill | what it does |
| --- | --- |
| trader-alerts | Prices in euro (you state the exchange rate and its date), alerts, Xetra / Euronext / NYSE clocks |
| trader-budget | Sizes a budget in euro across target weights with caps, cash buffer and minimum order |
| trader-rebalance | Brings a portfolio back to target weights, in euro |
| trader-risk | Blocks orders over your limits; accepts European tickers; blocks US-domiciled and leveraged ETFs |
| trader-broker | Order tickets and your typed yes (Alpaca-shaped or IBKR-shaped); records buys with a euro cost basis |
| trader-tax | German tax tracker: FIFO lots, stock-loss pot, fund exemption, allowance, Abgeltungsteuer |
| trader-insiders | Directors' dealings (MAR Art. 19) from a table you save from a regulator's database |
| trader-backtest | Replays a rule over saved price bars after costs, next to buy and hold; no look-ahead, costs required (step 1 of docs/ROADMAP.md) |
| trader-data | Downloads intraday bars (Yahoo, Kraken), refuses bad data, keeps a growing local store and reports gaps (step 2 of docs/ROADMAP.md) |
| trader-strategies | Three day-trading rules (trend, opening-range breakout, VWAP reversion), each with a stated reason, run through the backtester (step 3 of docs/ROADMAP.md) |
| trader-copycat | Rebuilds a US fund's 13F portfolio as targets (US-listed stocks only) |
| trader-analyst, trader-screener, trader-news | Read US company filings (SEC); US-listed companies only |

## The money flow, in euro

1. `alerts.py quote --holdings --fx USD=<rate> --fx-date <day> --write` writes `trader/prices.json` in euro.
   Read the rate from the ECB's euro reference rates the same day.
2. `size.py` or `rebalance.py` turn targets into orders in euro.
3. `check.py` (trader-risk) blocks anything over your limits or in the wrong currency.
4. `ticket.py` shows the order; you type a plain "yes"; the broker's own approval is the last gate.
5. Buys: `ticket.py fill --currency USD --fx <rate>` keeps the cost in euro. Sales: `steuer.py sell ... --record`
   (it logs the taxable gain). `steuer.py year` shows the year's tax.

## Choosing a broker from Germany

Alpaca's live trading is US-only. Look at Interactive Brokers (Ireland), Trading 212 (demo API, beta) or others
that offer a paper account and an API; docs/FINDINGS.md lists what is still to decide. Paper first.
