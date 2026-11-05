---
name: trader-data
description: >-
  The Price History Keeper. Downloads intraday price bars (Yahoo for stocks and ETFs, Kraken for crypto),
  refuses bad data, appends them to a store you own so the history grows with every run, and reports missing
  bars. Feeds the backtester. Use when the user says "get intraday data", "download 5-minute bars", "build a
  price history", "is my data complete", "check the gaps", or before backtesting a day-trading rule.
argument-hint: "[source] [symbol] [--interval minutes]"
---

# trader-data

Backtests are only as good as their prices. Free intraday history is short, so this skill keeps what each
download returns in a store on the user's machine and the history grows over time. It never places an order.

Tool in this folder (standard library Python):

```bash
python3 "${CLAUDE_SKILL_DIR}/history.py" fetch yahoo SAP.DE --interval 5 --days 30 --store trader/history
python3 "${CLAUDE_SKILL_DIR}/history.py" fetch kraken XBTEUR --interval 60 --store trader/history
python3 "${CLAUDE_SKILL_DIR}/history.py" import yahoo saved.json --symbol SAP.DE --interval 5 --store trader/history
python3 "${CLAUDE_SKILL_DIR}/history.py" watch --spec yahoo:SAP.DE:5 --spec kraken:XBTEUR:60 --store trader/history
python3 "${CLAUDE_SKILL_DIR}/history.py" check --symbol SAP.DE --interval 5 --store trader/history
```

The store is one CSV per symbol and interval (`SAP.DE_5m.csv`) in the format the backtester reads
(`t,o,h,l,c,v`, times in UTC, each time is the start of the bar).

## Keeping it current

`watch` pulls what is new every 60 seconds (never faster than every 30) until you press Ctrl-C, for up to
10 symbols. Each round it asks only for what the store is missing: from the last stored day for Yahoo (one
day of overlap, so the forming bar is refreshed) and from the last stored bar for Kraken. A failed pull is
logged and tried again next round; a symbol that fails 10 times in a row is dropped. A new bar only exists
once per interval, so a 5-minute series gains a bar every 5 minutes however often it is asked. Pulling every
minute for stocks while the exchange is closed returns nothing new. It must keep running on a computer that
stays on; it stops when the window closes.

## What it refuses

- Anything that fails the backtester's own bar check: non-positive or missing prices, a high or low that does
  not contain the open and close, times that do not strictly increase.
- Bars spaced closer than the interval asked for (the wrong interval was requested).
- A Yahoo error answer, an empty answer, or a Kraken error. Nothing is written; the old store stays as it was.
- A symbol that is not safe as a file name. Files are written whole and swapped in, so a crash cannot leave a
  half-written store.

## What to tell the user

- Yahoo's intraday history is short. Third-party documentation reports about 30 days of 1-minute bars, 60 days
  of 5 to 30-minute bars and 730 days of hourly bars; Yahoo does not publish this, so check it. Kraken returns
  at most 720 bars per call and offers no bulk history. Both are free, unofficial for this use, and can change
  without notice. See docs/DATA.md.
- The quotes are for research, never for placing orders. Prices stay in the instrument's own currency.
- The last Yahoo bar may still be forming when the market is open; a later download revises it and the
  report counts revised bars. Kraken's unfinished last bar is dropped.
- UK stocks are quoted in pence (GBp); the tool says so, and the value must be divided by 100.
- `check` finds missing bars inside a day. If more than 2% are missing, or there are fewer than 10 bars, the
  verdict is WARN: do not trust a backtest on that series. The 2% and 10 are this project's choices.
- The download commands could not be tested against the live services from the build environment. The parsers
  are tested on synthetic answers shaped like the real ones. The first real download should be checked by eye.

## Safety

- Education and tooling, not financial advice. Nothing here predicts prices or promises returns.
- This skill places no orders and needs no keys.
