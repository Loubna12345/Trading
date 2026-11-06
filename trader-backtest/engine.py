#!/usr/bin/env python3
"""A bar-based backtester that cannot see the future and always charges costs.

Standard library only, no network. Bars are dicts with ISO 8601 time `t` and prices `o h l c` (and an
optional volume `v`).

How a trade happens:
  * After the close of bar i the strategy is called with the bars up to and including i (read-only) and the
    position it holds. It returns the position it wants: 1 (long), 0 (flat), or -1 (short, only when
    allow_short is set).
  * The order fills at the OPEN of bar i + delay. delay is at least 1, so a signal can never trade at the
    price it was computed from.
  * Each fill pays half the spread plus slippage against you, plus commission and a fixed fee.
  * With flatten_eod the position is closed at the close of the last bar of each day (a market-on-close
    order) and nothing is carried to the next day.
An open position at the end is marked at the last close, and exit costs are not charged for it; the same
rule applies to the buy-and-hold benchmark, and the report says there is an open position.
"""

import argparse
import csv
import json
import math
import sys
from types import MappingProxyType

import importlib.util
import os


def _load_metrics():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.py")
    spec = importlib.util.spec_from_file_location("trader_backtest_metrics", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


metrics = _load_metrics()


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def check_bars(bars):
    """Refuse bad data instead of guessing: fail closed."""
    if len(bars) < 2:
        raise ValueError("need at least 2 bars")
    last_t = None
    for i, b in enumerate(bars):
        for k in ("t", "o", "h", "l", "c"):
            if k not in b:
                raise ValueError("bar %d is missing %r" % (i, k))
        if not isinstance(b["t"], str) or not b["t"]:
            raise ValueError("bar %d has no time" % i)
        for k in ("o", "h", "l", "c"):
            if not _finite(b[k]) or b[k] <= 0:
                raise ValueError("bar %d (%s): %s must be a positive number, got %r" % (i, b["t"], k, b[k]))
        if b["h"] < max(b["o"], b["c"], b["l"]) or b["l"] > min(b["o"], b["c"], b["h"]):
            raise ValueError("bar %d (%s): high and low do not contain open and close" % (i, b["t"]))
        if last_t is not None and b["t"] <= last_t:
            raise ValueError("bar %d (%s): times must strictly increase (previous %s)" % (i, b["t"], last_t))
        last_t = b["t"]


def suspicious_bars(bars, limit=0.20):
    """Bars whose high is more than `limit` above their low: often a bad tick, not a real move. Returns
    [(time, range)]. Such a bar can create a profit that never existed, so look at each one by eye."""
    return [(b["t"], b["h"] / b["l"] - 1.0) for b in bars if b["h"] / b["l"] - 1.0 > limit]


def make_costs(*, commission_bps, spread_bps, slippage_bps, fixed_fee):
    c = {"commission_bps": commission_bps, "spread_bps": spread_bps,
         "slippage_bps": slippage_bps, "fixed_fee": fixed_fee}
    for k, v in c.items():
        if not _finite(v) or v < 0:
            raise ValueError("%s must be a number of at least 0, got %r" % (k, v))
    return c


def fill_price(price, side, costs):
    """side +1 buys (pays up), -1 sells (receives less). Half the spread plus slippage."""
    adverse = (costs["spread_bps"] / 2.0 + costs["slippage_bps"]) / 1e4
    return price * (1.0 + side * adverse)


def commission(notional, costs):
    return abs(notional) * costs["commission_bps"] / 1e4 + costs["fixed_fee"]


class History:
    """Read-only view of the bars up to now. Slices and indexes beyond now do not exist."""

    def __init__(self, frozen, limit):
        self._f = frozen
        self._n = limit

    def __len__(self):
        return self._n

    def __getitem__(self, i):
        if isinstance(i, slice):
            return tuple(self._f[j] for j in range(*i.indices(self._n)))
        if i < 0:
            i += self._n
        if not 0 <= i < self._n:
            raise IndexError("no bar at %r yet" % i)
        return self._f[i]

    def __iter__(self):
        return (self._f[j] for j in range(self._n))


HALF_SIZES = (0, 0.5, 1)


def _signal(value, allow_short, fractional=False):
    ok = HALF_SIZES if fractional else ((-1, 0, 1) if allow_short else (0, 1))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value not in ok:
        raise ValueError("strategy must return one of %s, got %r" % (list(ok), value))
    return float(value) if fractional else int(value)


def backtest(bars, strategy, costs, capital=10000.0, delay=1, allow_short=False, flatten_eod=False,
             size_fraction=1.0, fractional=False):
    """fractional=True: the strategy returns 0, 0.5 or 1, the share of the account to hold in the asset (long
    only). A change buys or sells only the difference, at the next bar's open, with the usual costs on the amount
    traded. A trade runs from the first buy out of cash to the sale back to cash; its pnl is the change in the
    account over that time and its ret is that pnl as a share of the account when the trade began."""
    if fractional and (allow_short or size_fraction != 1.0):
        raise ValueError("fractional positions are long only and size themselves; leave allow_short and "
                         "size_fraction at their defaults")
    if costs is None:
        raise ValueError("a cost model is required: use make_costs(...)")
    if not isinstance(delay, int) or delay < 1:
        raise ValueError("delay must be a whole number of at least 1 bar (0 would trade on the same price)")
    if not (0 < size_fraction <= 1):
        raise ValueError("size_fraction must be above 0 and at most 1")
    if not _finite(capital) or capital <= 0:
        raise ValueError("capital must be positive")
    check_bars(bars)

    n = len(bars)
    frozen = tuple(MappingProxyType(dict(b)) for b in bars)
    day = [b["t"][:10] for b in bars]

    cash = float(capital)
    qty = 0.0
    scheduled = {}
    fills, trades, equity = [], [], []
    costs_paid = 0.0
    open_trade = None

    def do_fill(i, ref_price, side, amount_qty, label):
        """Trade amount_qty (positive) units on `side` at ref_price. Returns the fill record."""
        nonlocal cash, qty, costs_paid
        px = fill_price(ref_price, side, costs)
        notional = amount_qty * px
        fee = commission(notional, costs)
        cash += -side * notional - fee
        qty += side * amount_qty
        costs_paid += fee + amount_qty * abs(px - ref_price)
        rec = {"t": bars[i]["t"], "side": side, "qty": amount_qty, "price": px, "cost": fee, "why": label}
        fills.append(rec)
        return rec

    def close_position(i, ref_price, label):
        nonlocal open_trade, qty
        if abs(qty) < 1e-12:
            return
        side = -1 if qty > 0 else 1
        held = abs(qty)
        entry = open_trade
        rec = do_fill(i, ref_price, side, held, label)
        qty = 0.0
        gross = (rec["price"] - entry["entry_price"]) * entry["qty"]
        pnl = gross - entry["entry_fee"] - rec["cost"]
        basis = abs(entry["qty"]) * entry["entry_price"]
        trades.append({"entry_t": entry["entry_t"], "exit_t": bars[i]["t"],
                       "side": 1 if entry["qty"] > 0 else -1, "qty": abs(entry["qty"]),
                       "entry_price": entry["entry_price"], "exit_price": rec["price"],
                       "pnl": pnl, "ret": pnl / basis, "bars_held": i - entry["entry_i"]})
        open_trade = None

    def open_position(i, ref_price, want):
        nonlocal open_trade
        px_probe = fill_price(ref_price, want, costs)
        budget = size_fraction * cash if want > 0 else size_fraction * cash
        denom = px_probe * (1.0 + costs["commission_bps"] / 1e4)
        units = (budget - costs["fixed_fee"]) / denom
        if units <= 1e-12 or budget <= 0:
            return
        rec = do_fill(i, ref_price, want, units, "entry")
        open_trade = {"entry_t": bars[i]["t"], "entry_i": i, "entry_price": rec["price"],
                      "qty": rec["qty"] * want, "entry_fee": rec["cost"]}

    target = 0.0          # fractional mode: the share of the account last asked for

    def frac_close(i, ref_price, label):
        nonlocal open_trade
        if abs(qty) < 1e-12:
            return
        rec = do_fill(i, ref_price, -1, qty, label)
        entry = open_trade
        pnl = cash - entry["cash_before"]
        trades.append({"entry_t": entry["entry_t"], "exit_t": bars[i]["t"], "side": 1, "qty": entry["qty"],
                       "entry_price": entry["entry_price"], "exit_price": rec["price"], "pnl": pnl,
                       "ret": pnl / entry["cash_before"], "bars_held": i - entry["entry_i"]})
        open_trade = None

    def frac_rebalance(i, ref_price, want):
        nonlocal open_trade
        if want == 0:
            frac_close(i, ref_price, "exit")
            return
        eq = cash + qty * ref_price
        if want * eq > qty * ref_price:
            px = fill_price(ref_price, 1, costs)
            denom = px * (1.0 + costs["commission_bps"] / 1e4)
            gap = (want * eq - qty * ref_price) / denom
            room = (cash - costs["fixed_fee"]) / denom
            units = min(gap, room)
            if units <= 1e-12:
                return
            before = cash
            rec = do_fill(i, ref_price, 1, units, "entry" if open_trade is None else "add")
            if open_trade is None:
                open_trade = {"entry_t": bars[i]["t"], "entry_i": i, "entry_price": rec["price"], "qty": rec["qty"],
                              "entry_fee": rec["cost"], "cash_before": before}
        else:
            units = qty - want * eq / ref_price
            if units > 1e-12:
                do_fill(i, ref_price, -1, units, "reduce")

    for i in range(n):
        if i in scheduled:
            want = scheduled.pop(i)
            if fractional:
                if want != target:
                    frac_rebalance(i, bars[i]["o"], want)
                    target = want
            else:
                current = 0 if abs(qty) < 1e-12 else (1 if qty > 0 else -1)
                if want != current:
                    close_position(i, bars[i]["o"], "exit")
                    if want != 0:
                        open_position(i, bars[i]["o"], want)
        last_of_day = i == n - 1 or day[i + 1] != day[i]
        if flatten_eod and last_of_day:
            if fractional:
                frac_close(i, bars[i]["c"], "end of day")
                target = 0.0
            else:
                close_position(i, bars[i]["c"], "end of day")
            scheduled.clear()
        equity.append(cash + qty * bars[i]["c"])
        if flatten_eod and last_of_day:
            continue
        if fractional:
            current = target
        else:
            current = 0 if abs(qty) < 1e-12 else (1 if qty > 0 else -1)
        sig = _signal(strategy(History(frozen, i + 1), current), allow_short, fractional)
        if i + delay < n:
            scheduled[i + delay] = sig

    return {"equity": equity, "trades": trades, "fills": fills, "costs_paid": costs_paid,
            "cash_end": cash, "open_position": abs(qty) > 1e-12, "delay": delay, "capital": capital}


def buy_and_hold(bars, costs, capital=10000.0):
    return backtest(bars, lambda h, p: 1, costs, capital=capital)


def report(bars, strategy, costs, periods_per_year, capital=10000.0, **kw):
    res = backtest(bars, strategy, costs, capital=capital, **kw)
    bench = buy_and_hold(bars, costs, capital=capital)
    strat = metrics.summarize(res["equity"], [t["pnl"] for t in res["trades"]], periods_per_year)
    base = metrics.summarize(bench["equity"], [], periods_per_year, min_trades=0)
    warnings = list(strat["warnings"])
    if res["open_position"]:
        warnings.append("A position was still open at the end; it is marked at the last close and its exit "
                        "costs are not charged.")
    gross = sum(t["pnl"] for t in res["trades"]) + res["costs_paid"]
    return {"strategy": strat, "benchmark": base, "costs_paid": res["costs_paid"],
            "costs_share_of_gross_profit": (res["costs_paid"] / gross) if gross > 0 else None,
            "warnings": warnings, "delay": res["delay"], "capital": capital,
            "trades": res["trades"], "equity": res["equity"]}


def load_bars_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        cols = reader.fieldnames or []
        for k in ("t", "o", "h", "l", "c"):
            if k not in cols:
                raise ValueError("%s: missing column %r (need t,o,h,l,c and optional v)" % (path, k))
        bars = []
        for n, row in enumerate(reader, start=2):
            b = {"t": (row["t"] or "").strip()}
            for k in ("o", "h", "l", "c"):
                try:
                    b[k] = float((row[k] or "").strip())
                except ValueError:
                    raise ValueError("%s line %d: %s is not a number with a decimal point: %r"
                                     % (path, n, k, row[k]))
            if row.get("v") not in (None, ""):
                try:
                    b["v"] = float(row["v"])
                except ValueError:
                    raise ValueError("%s line %d: v is not a number: %r" % (path, n, row["v"]))
            bars.append(b)
    check_bars(bars)
    return bars


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest tools. Not financial advice.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("benchmark", help="buy and hold on a bars CSV, with costs")
    b.add_argument("--bars", required=True)
    b.add_argument("--periods-per-year", type=int, required=True,
                   help="bars per year, e.g. 252 for daily bars; needed to annualise")
    b.add_argument("--capital", type=float, default=10000.0)
    b.add_argument("--commission-bps", type=float)
    b.add_argument("--spread-bps", type=float)
    b.add_argument("--slippage-bps", type=float)
    b.add_argument("--fixed-fee", type=float, default=0.0)
    args = ap.parse_args(argv)

    if None in (args.commission_bps, args.spread_bps, args.slippage_bps):
        print("A cost model is required: give --commission-bps, --spread-bps and --slippage-bps "
              "(0 is allowed if that is really your cost).", file=sys.stderr)
        return 2
    try:
        costs = make_costs(commission_bps=args.commission_bps, spread_bps=args.spread_bps,
                           slippage_bps=args.slippage_bps, fixed_fee=args.fixed_fee)
        bars = load_bars_csv(args.bars)
        res = buy_and_hold(bars, costs, capital=args.capital)
        out = {"benchmark": metrics.summarize(res["equity"], [], args.periods_per_year, min_trades=0),
               "bars": len(bars), "costs_paid": res["costs_paid"], "capital": args.capital,
               "note": "Buy and hold; marked at the last close; exit costs not charged."}
    except ValueError as e:
        print("Refused: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
