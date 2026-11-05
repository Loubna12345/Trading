#!/usr/bin/env python3
"""Intraday price history for the backtester: fetch, check, and keep a growing local store.

Standard library only. Free sources keep only a short window of intraday bars (see docs/DATA.md), so this
tool appends what each fetch returns to a store you own, and the history grows with every run.

Conventions:
  * Every bar time `t` is UTC, written like 2026-01-05T08:00:00Z, and is the START of the bar.
  * A bar is trusted only if it passes the backtester's own bar check (positive prices, high and low
    contain open and close, times strictly increase). Anything else is refused, never repaired.
  * Prices stay in the instrument's own currency; the conversion to euro is trader-alerts' job.

    python3 history.py fetch yahoo SAP.DE --interval 5 --days 30 --store trader/history
    python3 history.py fetch kraken XBTEUR --interval 60 --store trader/history
    python3 history.py import yahoo saved.json --symbol SAP.DE --interval 5 --store trader/history
    python3 history.py check --symbol SAP.DE --interval 5 --store trader/history
    python3 history.py watch --spec yahoo:SAP.DE:5 --spec kraken:XBTEUR:60 --store trader/history
                                          # pulls what is new every 60 seconds until you press Ctrl-C
"""

import argparse
import csv
import datetime
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; trading-local/1.0)"

# How far back Yahoo keeps each intraday interval, in days, as reported by third-party documentation
# (getbruin ingestr docs and a yfinance issue, read 2026-11-05). Yahoo does not document this itself; check it.
YAHOO_INTERVALS = {1: "1m", 2: "2m", 5: "5m", 15: "15m", 30: "30m", 60: "60m", 90: "90m"}
YAHOO_MAX_DAYS = {1: 30, 2: 60, 5: 60, 15: 60, 30: 60, 60: 730, 90: 60}
KRAKEN_INTERVALS = (1, 5, 15, 30, 60, 240, 1440)  # minutes; at most 720 bars per call (Kraken docs)

MIN_BARS = 10                 # fewer bars than this is not worth testing on: this project's choice
MAX_MISSING_SHARE = 0.02      # more than 2% of expected bars missing gives a WARN: this project's choice


def _load_engine():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "trader-backtest", "engine.py")
    spec = importlib.util.spec_from_file_location("trader_backtest_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


engine = _load_engine()


def iso(epoch):
    return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_of(t):
    return datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ---------- parsing ----------

def parse_yahoo(doc, interval_min):
    """Yahoo chart JSON -> (bars, meta, notes)."""
    try:
        chart = doc["chart"]
        if chart.get("error"):
            raise ValueError("Yahoo returned an error: %s" % json.dumps(chart["error"]))
        res = chart["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        o, h, l, c = q["open"], q["high"], q["low"], q["close"]
        v = q.get("volume") or [None] * len(ts)
        meta = res.get("meta", {})
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ValueError("this is not a Yahoo intraday chart answer")
    if not all(len(a) == len(ts) for a in (o, h, l, c, v)):
        raise ValueError("Yahoo arrays have different lengths")
    bars, empty = [], 0
    for i, t in enumerate(ts):
        if not all(_num(x) for x in (o[i], h[i], l[i], c[i])):
            empty += 1
            continue
        bars.append({"t": iso(t), "o": float(o[i]), "h": float(h[i]), "l": float(l[i]), "c": float(c[i]),
                     "v": float(v[i]) if _num(v[i]) else 0.0})
    if not bars:
        raise ValueError("Yahoo gave no usable bars")
    for a, b in zip(bars, bars[1:]):
        if epoch_of(b["t"]) - epoch_of(a["t"]) < interval_min * 60:
            raise ValueError("bars %s and %s are closer than %d minutes: wrong interval?"
                             % (a["t"], b["t"], interval_min))
    engine.check_bars(bars)
    notes = []
    if empty:
        notes.append("%d empty row(s) dropped (Yahoo had no trade in that bar)." % empty)
    if meta.get("currency") == "GBp":
        notes.append("Prices are in pence (GBp), not pounds; divide by 100 before any euro conversion.")
    notes.append("The last bar may still be forming if the market is open; a later fetch will revise it.")
    return bars, {"symbol": meta.get("symbol"), "currency": meta.get("currency"),
                  "exchange_tz": meta.get("exchangeTimezoneName")}, notes


def parse_kraken(doc, interval_min):
    """Kraken OHLC JSON -> (bars, notes). The last row is the bar still forming, so it is dropped."""
    if not isinstance(doc, dict):
        raise ValueError("this is not a Kraken answer")
    if doc.get("error"):
        raise ValueError("Kraken returned an error: %s" % "; ".join(map(str, doc["error"])))
    result = doc.get("result")
    keys = [k for k in (result or {}) if k != "last"]
    if not keys:
        raise ValueError("Kraken answer has no pair data")
    rows = result[keys[0]]
    bars = []
    for r in rows[:-1]:
        try:
            bars.append({"t": iso(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                         "c": float(r[4]), "v": float(r[6])})
        except (ValueError, IndexError, TypeError):
            raise ValueError("Kraken row is malformed: %r" % (r,))
    engine.check_bars(bars)
    return bars, ["The last bar was dropped because it is unfinished (still forming)."]


# ---------- urls and fetching ----------

def yahoo_url(symbol, interval_min, days, now=None):
    if interval_min not in YAHOO_INTERVALS:
        raise ValueError("interval must be one of %s minutes" % sorted(YAHOO_INTERVALS))
    limit = YAHOO_MAX_DAYS[interval_min]
    if not isinstance(days, int) or not 1 <= days <= limit:
        raise ValueError("Yahoo keeps about %d days of %d-minute bars; asked for %r" % (limit, interval_min, days))
    now = int(time.time() if now is None else now)
    return ("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=%s&period1=%d&period2=%d"
            % (urllib.parse.quote(symbol, safe=""), YAHOO_INTERVALS[interval_min], now - days * 86400, now))


def kraken_url(pair, interval_min, since=None):
    if interval_min not in KRAKEN_INTERVALS:
        raise ValueError("Kraken interval must be one of %s minutes" % list(KRAKEN_INTERVALS))
    u = "https://api.kraken.com/0/public/OHLC?pair=%s&interval=%d" % (urllib.parse.quote(pair, safe=""), interval_min)
    if since is not None:
        u += "&since=%d" % int(since)
    return u


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise ValueError("could not fetch %s: %s" % (url.split("?")[0], e))


# ---------- the store ----------

NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$")


def store_path(folder, symbol, interval_min):
    if not NAME_OK.match(symbol) or ".." in symbol:
        raise ValueError("symbol %r is not safe to use as a file name" % symbol)
    return os.path.join(folder, "%s_%dm.csv" % (symbol, interval_min))


def read_store(path):
    if not os.path.exists(path):
        return []
    bars = []
    with open(path, newline="", encoding="utf-8") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):
            try:
                bars.append({"t": row["t"], "o": float(row["o"]), "h": float(row["h"]), "l": float(row["l"]),
                             "c": float(row["c"]), "v": float(row.get("v") or 0.0)})
            except (ValueError, KeyError, TypeError):
                raise ValueError("%s line %d is damaged" % (path, n))
    if len(bars) >= 2:
        engine.check_bars(bars)
    return bars


def _same(a, b):
    return all(a[k] == b[k] for k in ("o", "h", "l", "c", "v"))


def merge_store(path, new_bars):
    engine.check_bars(new_bars)
    have = {b["t"]: b for b in read_store(path)}
    added = revised = 0
    for b in new_bars:
        if b["t"] not in have:
            added += 1
        elif not _same(have[b["t"]], b):
            revised += 1
        have[b["t"]] = dict(b)
    merged = [have[t] for t in sorted(have)]
    engine.check_bars(merged)
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["t", "o", "h", "l", "c", "v"])
            for b in merged:
                w.writerow([b["t"], repr(b["o"]), repr(b["h"]), repr(b["l"]), repr(b["c"]), repr(b["v"])])
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return {"added": added, "revised": revised, "total": len(merged)}


# ---------- quality ----------

def quality(bars, interval_min, always_open=False):
    """Find missing bars inside a trading day. Gaps across the night or a weekend are not counted unless the
    market is always open (crypto)."""
    gaps, missing = [], 0
    step = interval_min * 60
    for a, b in zip(bars, bars[1:]):
        dt = epoch_of(b["t"]) - epoch_of(a["t"])
        if dt <= step:
            continue
        if not always_open and a["t"][:10] != b["t"][:10]:
            continue
        m = int(round(dt / step)) - 1
        if m > 0:
            missing += m
            gaps.append({"after": a["t"], "before": b["t"], "missing": m})
    expected = len(bars) + missing
    share = missing / expected if expected else 0.0
    notes = []
    verdict = "OK"
    wild = engine.suspicious_bars(bars) if bars else []
    if wild:
        verdict = "WARN"
        notes.append("%d bar(s) have a high more than 20%% above the low, often a bad tick; first: %s. "
                     "Look at them before testing on this series." % (len(wild), wild[0][0]))
    if len(bars) < MIN_BARS:
        verdict = "WARN"
        notes.append("Only %d bars (fewer than %d): too little to test on." % (len(bars), MIN_BARS))
    if share > MAX_MISSING_SHARE:
        verdict = "WARN"
        notes.append("%.1f%% of the expected bars are missing (limit %.0f%%)."
                     % (share * 100, MAX_MISSING_SHARE * 100))
    return {"verdict": verdict, "n_bars": len(bars), "first": bars[0]["t"] if bars else None,
            "last": bars[-1]["t"] if bars else None, "missing_bars": missing,
            "missing_share": share, "gaps": gaps[:20], "suspicious_bars": wild[:20], "notes": notes}


# ---------- keeping the store current ----------

MIN_EVERY = 30        # seconds between rounds: be polite to free, unofficial endpoints
MAX_SPECS = 10        # symbols per watcher
MAX_FAILURES = 10     # failed pulls in a row before one symbol is dropped (probably blocked or wrong symbol)


def plan_days(stored, interval_min, now):
    """How many days back to ask Yahoo for: everything it keeps if the store is empty, otherwise from the
    last stored day (one day of overlap so the last bar is refreshed). Returns (days, lost): lost is True
    when the store is older than Yahoo keeps, so some bars are gone for good."""
    limit = YAHOO_MAX_DAYS[interval_min]
    if not stored:
        return limit, False
    age = now - epoch_of(stored[-1]["t"])
    days = int(math.ceil(max(age, 0) / 86400.0)) + 1
    if days > limit:
        return limit, True
    return max(days, 1), False


def parse_spec(text):
    parts = text.split(":")
    if len(parts) != 3:
        raise ValueError("spec %r must look like yahoo:SAP.DE:5 (source:symbol:minutes)" % text)
    source, symbol, minutes = parts
    if source not in ("yahoo", "kraken"):
        raise ValueError("spec %r: source must be yahoo or kraken" % text)
    if not NAME_OK.match(symbol) or ".." in symbol:
        raise ValueError("spec %r: symbol is not safe as a file name" % text)
    try:
        interval = int(minutes)
    except ValueError:
        raise ValueError("spec %r: minutes must be a whole number" % text)
    if source == "yahoo" and interval not in YAHOO_INTERVALS:
        raise ValueError("spec %r: Yahoo interval must be one of %s" % (text, sorted(YAHOO_INTERVALS)))
    if source == "kraken" and interval not in KRAKEN_INTERVALS:
        raise ValueError("spec %r: Kraken interval must be one of %s" % (text, list(KRAKEN_INTERVALS)))
    return {"source": source, "symbol": symbol, "interval": interval}


def tick(spec, folder, now, fetcher):
    """One pull for one symbol, asking only for what the store is missing."""
    path = store_path(folder, spec["symbol"], spec["interval"])
    stored = read_store(path)
    notes = []
    if spec["source"] == "yahoo":
        days, lost = plan_days(stored, spec["interval"], now)
        if lost:
            notes.append("store is older than Yahoo keeps; some bars are gone for good")
        bars, _, _ = parse_yahoo(fetcher(yahoo_url(spec["symbol"], spec["interval"], days, now=now)),
                                 spec["interval"])
    else:
        since = int(epoch_of(stored[-1]["t"])) if stored else None
        bars, _ = parse_kraken(fetcher(kraken_url(spec["symbol"], spec["interval"], since=since)),
                               spec["interval"])
    result = merge_store(path, bars)
    result.update({"status": "ok", "spec": "%s:%s:%d" % (spec["source"], spec["symbol"], spec["interval"]),
                   "notes": notes})
    return result


def watch(specs, folder, every=60, max_ticks=None, fetcher=None, sleep=time.sleep, now_fn=time.time,
          log=print):
    """Pull every `every` seconds until Ctrl-C (or max_ticks rounds). Returns one list of results per round."""
    if every < MIN_EVERY:
        raise ValueError("every must be at least %d seconds (free endpoints are not for hammering)" % MIN_EVERY)
    if not specs or len(specs) > MAX_SPECS:
        raise ValueError("give between 1 and %d symbols" % MAX_SPECS)
    fetcher = fetcher or fetch_json
    failures = {i: 0 for i in range(len(specs))}
    dropped = set()
    rounds = []
    n = 0
    try:
        while max_ticks is None or n < max_ticks:
            if n:
                sleep(every)
            n += 1
            results = []
            for i, spec in enumerate(specs):
                label = "%s:%s:%d" % (spec["source"], spec["symbol"], spec["interval"])
                if i in dropped:
                    continue
                try:
                    r = tick(spec, folder, int(now_fn()), fetcher)
                    failures[i] = 0
                    log("%s  %s  +%d new, %d revised, %d stored" % (iso(now_fn()), label, r["added"],
                                                                      r["revised"], r["total"]))
                except (ValueError, OSError) as e:
                    failures[i] += 1
                    r = {"status": "error", "spec": label, "message": str(e)}
                    log("%s  %s  FAILED (%d in a row): %s" % (iso(now_fn()), label, failures[i], e))
                    if failures[i] >= MAX_FAILURES:
                        dropped.add(i)
                        r = {"status": "stopped", "spec": label, "message": str(e)}
                        log("%s  %s  stopped after %d failures in a row" % (iso(now_fn()), label, MAX_FAILURES))
                results.append(r)
            if results:
                rounds.append(results)
            if len(dropped) == len(specs):
                break
    except KeyboardInterrupt:
        log("stopped by you")
    return rounds


# ---------- command line ----------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Intraday history store. Not financial advice.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download from the network and add to the store")
    f.add_argument("source", choices=["yahoo", "kraken"])
    f.add_argument("symbol")
    f.add_argument("--interval", type=int, required=True, help="minutes per bar")
    f.add_argument("--days", type=int, default=None, help="Yahoo only: how many days back")
    f.add_argument("--store", required=True)

    i = sub.add_parser("import", help="add a saved answer file to the store (no network)")
    i.add_argument("source", choices=["yahoo", "kraken"])
    i.add_argument("file")
    i.add_argument("--symbol", required=True)
    i.add_argument("--interval", type=int, required=True)
    i.add_argument("--store", required=True)

    c = sub.add_parser("check", help="report missing bars in a stored series")
    c.add_argument("--symbol", required=True)
    c.add_argument("--interval", type=int, required=True)
    c.add_argument("--store", required=True)
    c.add_argument("--always-open", action="store_true", help="24/7 market such as crypto")

    w = sub.add_parser("watch", help="pull what is new every N seconds until Ctrl-C")
    w.add_argument("--spec", action="append", required=True,
                   help="source:symbol:minutes, e.g. yahoo:SAP.DE:5 or kraken:XBTEUR:60 (repeat for more)")
    w.add_argument("--store", required=True)
    w.add_argument("--every", type=int, default=60, help="seconds between rounds (minimum 30)")
    w.add_argument("--max-ticks", type=int, default=None, help="stop after this many rounds")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "watch":
            watch([parse_spec(t) for t in a.spec], a.store, every=a.every, max_ticks=a.max_ticks)
            return 0
        path = store_path(a.store, a.symbol if a.cmd != "fetch" else a.symbol, a.interval)
        if a.cmd == "check":
            bars = read_store(path)
            if not bars:
                raise ValueError("nothing stored at %s" % path)
            print(json.dumps(quality(bars, a.interval, always_open=a.always_open), indent=2))
            return 0
        if a.cmd == "fetch":
            if a.source == "yahoo":
                doc = fetch_json(yahoo_url(a.symbol, a.interval, a.days or YAHOO_MAX_DAYS.get(a.interval, 0)))
            else:
                doc = fetch_json(kraken_url(a.symbol, a.interval))
        else:
            with open(a.file, encoding="utf-8") as fh:
                doc = json.load(fh)
        if a.source == "yahoo":
            bars, meta, notes = parse_yahoo(doc, a.interval)
        else:
            bars, notes = parse_kraken(doc, a.interval)
            meta = {}
        result = merge_store(path, bars)
        result.update({"store": path, "meta": meta, "notes": notes})
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError) as e:
        print("Refused: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
