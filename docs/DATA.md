# Intraday data: what is available, free

Read 2026-11-05. Secondary sources are marked. Nothing here was tested against the live services, because the
build environment cannot reach them; the first real download should be checked by eye.

| Source | Covers | Free history | Notes |
| --- | --- | --- | --- |
| Yahoo chart JSON (unofficial) | US and European stocks and ETFs (suffixes such as .DE, .PA) | about 30 days of 1-minute, 60 days of 2 to 90-minute, 730 days of hourly | Limits from [ingestr docs](https://getbruin.com/docs/ingestr/supported-sources/yfinance.html) and a [yfinance issue](https://github.com/ranaroussi/yfinance/issues/2451), not from Yahoo. Delayed by exchange. Intraday bars are not adjusted after splits. Unofficial: can break or block. |
| Kraken public OHLC | crypto (for example XBTEUR) | at most 720 bars per call; "no bulk historical data dump" | [Kraken docs](https://docs.kraken.com/exchange/guides/general/historical-data). Intervals 1, 5, 15, 30, 60, 240, 1440 minutes. |
| Revolut X public candles | crypto traded on Revolut X | not documented | [Revolut X docs](https://developer.revolut.com/docs/x-api/public-market-data): candles endpoint without a stated depth, 1 request per second. Not used yet. |

Consequences:

1. History has to be collected over time. `trader-data` appends every download to a store you own, so a daily
   fetch builds months of history. Until then, backtests rest on weeks of data.
2. A few weeks of bars cannot prove a strategy. The validation step (roadmap step 4) is built to say so.
3. For longer history, a paid data vendor or the broker's own history would be needed. Not researched.
4. Alpaca offers US historical bars with a free account; not used here because it needs a key and covers US
   stocks only.
