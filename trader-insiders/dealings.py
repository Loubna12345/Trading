#!/usr/bin/env python3
"""Directors' dealings reader for trader-insiders. Standard library only, no network.

Managers of a listed company (and people close to them) must report their own trades in the company's shares
under Article 19 of the EU Market Abuse Regulation (MAR): to the company and the national regulator within three
business days of the trade, and the company publishes it. This tool reads a table you saved from such a
database (BaFin's "Eigengeschaefte von Fuehrungskraeften", or another country's) and turns it into clean rows.

    python3 dealings.py list dealings.csv --side buy --min-value 50000
    python3 dealings.py new dealings.csv            # only rows not shown before (trader/state/insiders.json)
    python3 dealings.py list dealings.csv --portfolio trader/portfolio.json --isin-map trader/isin.json

It does NOT download anything. BaFin's database page describes no export function and its portal's robots rules
block automated access, so you save the table (copy it into a spreadsheet and export CSV, or use an export button
if the page has one) and give the file to this tool. The reader is tolerant on purpose because the real export
format was not available when this was written:
  - separator ; , or tab; UTF-8 or Windows-1252
  - German or English column names (Emittent/Issuer, Meldepflichtiger/Notifying person, Art des Geschaefts/
    Transaction type, Datum des Geschaefts/Date of transaction, Datum der Mitteilung/Notification date, ISIN,
    Position/Status, Durchschnittspreis/Price, Aggregiertes Volumen/Total volume, Waehrung/Currency, Stueckzahl)
  - numbers like 61.700,00 and 1,234.56. A number that could mean two things (1.234) is REFUSED unless you say
    --decimal comma or --decimal dot. Dates: 2026-11-05, 05.11.2026, 05/11/2026 (day first).
A row it cannot read is reported with its line number and skipped; it is never guessed and never marked as seen.

What it is not: these are public filings read after the fact, not inside information. A trade is reported up to
three business days later and the database shows it a day or two after that. Germany's reporting threshold is
EUR 50,000 per calendar year since 2026-01-01 (EUR 20,000 before), so a small trade may never appear. Many
insider purchases are routine. A cluster (two or more insiders of one company buying within 30 days) is the only
pattern this tool points out, and it is a reason to read the company's news, not a reason to buy.
"""

import argparse
import csv
import datetime
import hashlib
import io
import json
import math
import os
import re
import sys
import unicodedata

LATE_BUSINESS_DAYS = 3
CLUSTER_DAYS = 30

ALIASES = {
    "issuer": ["emittent", "issuer", "issuername", "unternehmen", "company"],
    "isin": ["isin"],
    "person": ["meldepflichtiger", "meldepflichtige", "person", "name", "notifyingperson", "personnotifying",
               "personundmeldepflichtiger", "pdmr"],
    "role": ["positionstatus", "position", "funktion", "role", "status"],
    "type": ["artdesgeschafts", "artdergeschafts", "geschaftsart", "transactiontype", "typeoftransaction",
             "natureoftransaction", "type", "art"],
    "price": ["durchschnittspreis", "preis", "price", "averageprice", "aggregatedprice"],
    "value": ["aggregiertesvolumen", "gesamtvolumen", "volumen", "aggregatedvolume", "totalvolume", "volume"],
    "quantity": ["stuckzahl", "anzahl", "quantity", "numberofshares"],
    "currency": ["wahrung", "currency", "ccy"],
    "traded": ["datumdesgeschafts", "datumdergeschafts", "transaktionsdatum", "geschaftsdatum",
               "dateoftransaction", "transactiondate", "tradedate"],
    "notified": ["datumdermitteilung", "mitteilungsdatum", "meldedatum", "notificationdate", "dateofnotification",
                 "veroffentlichungsdatum", "publicationdate", "published"],
}
REQUIRED = ("issuer", "person", "type", "traded", "notified")
BUY_WORDS = ("kauf", "erwerb", "acquisition", "purchase", "buy", "bought")
SELL_WORDS = ("verkauf", "veraus", "veraeuss", "disposal", "sale", "sell", "sold")


class DealingsError(Exception):
    pass


# ---------------------------------------------------------------- parsing

def _norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def parse_number(text, decimal="auto"):
    """'61.700,00' -> 61700.0, '1,234.56' -> 1234.56. Ambiguous input ('1.234') is refused unless `decimal` says."""
    t = str(text).strip().replace(" ", " ").replace(" ", "")
    if not t or not re.fullmatch(r"[+-]?[0-9.,]+", t) or t.count("-") + t.count("+") > 1:
        raise DealingsError("not a number: %r" % text)
    sign = -1.0 if t.startswith("-") else 1.0
    t = t.lstrip("+-")
    if decimal == "comma":
        t = t.replace(".", "").replace(",", ".")
    elif decimal == "dot":
        t = t.replace(",", "")
    else:
        has_dot, has_comma = "." in t, "," in t
        if has_dot and has_comma:
            dec = "," if t.rfind(",") > t.rfind(".") else "."
            thou = "." if dec == "," else ","
            if t.count(dec) != 1 or t.rfind(thou) > t.rfind(dec):
                raise DealingsError("not a number: %r" % text)
            t = t.replace(thou, "").replace(dec, ".")
        elif has_dot or has_comma:
            sep = "." if has_dot else ","
            if t.count(sep) > 1:
                parts = t.split(sep)
                if not all(len(p) == 3 for p in parts[1:]):
                    raise DealingsError("not a number: %r" % text)
                t = "".join(parts)  # 1.015.000 can only be thousands
            else:
                after = t.split(sep)[1]
                if len(after) == 3:
                    raise DealingsError("ambiguous number %r (thousands or decimals?): use --decimal comma or dot" % text)
                t = t.replace(sep, ".")
    try:
        v = float(t) * sign
    except ValueError:
        raise DealingsError("not a number: %r" % text)
    if not math.isfinite(v):
        raise DealingsError("not a number: %r" % text)
    return v


def parse_date(text):
    t = str(text).strip()[:10]
    for pattern, order in ((r"(\d{4})-(\d{1,2})-(\d{1,2})", "ymd"), (r"(\d{1,2})\.(\d{1,2})\.(\d{4})", "dmy"),
                           (r"(\d{1,2})/(\d{1,2})/(\d{4})", "dmy")):
        m = re.fullmatch(pattern, t)
        if m:
            a, b, c = (int(x) for x in m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            try:
                return datetime.date(y, mo, d)
            except ValueError:
                break
    raise DealingsError("not a date: %r (use 2026-11-05 or 05.11.2026)" % text)


def business_days_between(start, end):
    """Weekdays after `start` up to and including `end`. Public holidays are not counted as non-working."""
    if end < start:
        return 0
    n, d = 0, start
    while d < end:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def classify(text):
    t = _norm(text)
    if any(w in t for w in SELL_WORDS):
        return "sell"
    if any(t.startswith(w) or w == t for w in BUY_WORDS) or t in ("kauf", "acquisition", "purchase", "buy"):
        return "buy"
    if any(w in t for w in BUY_WORDS) and not any(w in t for w in ("option", "ausub", "exercise", "grant", "gewahr")):
        return "buy"
    return "other"


def _open(path):
    if not path or not os.path.exists(path):
        raise DealingsError("no file at %s: save the dealings table from the database as CSV first" % path)
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read_table(path, decimal="auto"):
    """(rows, errors). Rows are dicts; errors are 'line N: reason' strings for rows that were skipped."""
    text = _open(path)
    first = text.splitlines()[0] if text.strip() else ""
    delim = max((";", ",", "\t"), key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        header = next(reader)
    except StopIteration:
        raise DealingsError("%s is empty" % path)
    cols = {}
    for i, h in enumerate(header):
        n = _norm(h)
        for field, names in ALIASES.items():
            if n in names and field not in cols:
                cols[field] = i
    missing = [f for f in REQUIRED if f not in cols]
    if missing:
        raise DealingsError("cannot find the column(s) for: %s. The header row has: %s" % (
            ", ".join(missing), "; ".join(h.strip() for h in header)))
    rows, errors = [], []
    for lineno, rec in enumerate(reader, 2):
        if not any(c.strip() for c in rec):
            continue

        def get(f):
            i = cols.get(f)
            return rec[i].strip() if i is not None and i < len(rec) else ""
        try:
            traded, notified = parse_date(get("traded")), parse_date(get("notified"))
            price = parse_number(get("price"), decimal) if get("price") else None
            value = parse_number(get("value"), decimal) if get("value") else None
            qty = parse_number(get("quantity"), decimal) if get("quantity") else None
            if not get("issuer") or not get("person"):
                raise DealingsError("issuer or person is empty")
            if (value is None or value == 0) and price and qty:
                value = price * qty
        except DealingsError as e:
            errors.append("line %d: %s" % (lineno, e))
            continue
        delay = business_days_between(traded, notified)
        key = hashlib.sha1("|".join([get("issuer"), get("isin"), get("person"), get("type"), traded.isoformat(),
                                     notified.isoformat(), str(value), str(price)]).encode()).hexdigest()[:16]
        rows.append({"issuer": get("issuer"), "isin": get("isin").upper(), "person": get("person"),
                     "role": get("role"), "type": get("type"), "side": classify(get("type")), "price": price,
                     "value": value, "currency": (get("currency") or "EUR").upper(), "traded": traded.isoformat(),
                     "notified": notified.isoformat(), "delay_days": delay, "late": delay > LATE_BUSINESS_DAYS,
                     "key": key, "line": lineno})
    return rows, errors


# ---------------------------------------------------------------- signals

def clusters(rows):
    """Two or more different people buying the same company within 30 days of each other."""
    by = {}
    for r in rows:
        if r["side"] == "buy":
            by.setdefault(r.get("isin") or r["issuer"], []).append(r)
    found = []
    for group in by.values():
        group = sorted(group, key=lambda r: r["traded"])
        best = None
        for i, first in enumerate(group):
            start = datetime.date.fromisoformat(first["traded"])
            window = [r for r in group[i:] if (datetime.date.fromisoformat(r["traded"]) - start).days <= CLUSTER_DAYS]
            people = {r["person"] for r in window}
            if len(people) >= 2 and (best is None or len(people) > best[0]):
                best = (len(people), window)
        if best:
            w = best[1]
            found.append({"issuer": w[0]["issuer"], "isin": w[0].get("isin"), "people": best[0],
                          "value": sum(r["value"] or 0 for r in w), "from": w[0]["traded"], "to": w[-1]["traded"]})
    return found


def flag_holdings(rows, portfolio, isin_map):
    """Add 'held': the ticker you hold in that company, matched by ISIN, else None."""
    held = {}
    for p in (portfolio or {}).get("positions", []):
        if p.get("isin"):
            held[str(p["isin"]).upper()] = p["ticker"]
    tickers = {str(p.get("ticker", "")).upper() for p in (portfolio or {}).get("positions", [])}
    for isin, t in (isin_map or {}).items():
        if str(t).upper() in tickers:
            held.setdefault(str(isin).upper(), str(t).upper() if str(t).upper() in tickers else t)
    out = []
    for r in rows:
        r = dict(r)
        r["held"] = held.get(r.get("isin"))
        out.append(r)
    return out


# ---------------------------------------------------------------- output

def fmt_money(x, cur):
    if x is None:
        return "n/a"
    s = "{:,.0f}".format(x) if abs(x) >= 100 else "{:,.2f}".format(x)
    return ("€" + s) if cur == "EUR" else "%s %s" % (s, cur)


def show(rows):
    for r in rows:
        price = (" at %g" % r["price"]) if r.get("price") else ""
        late = "  LATE" if r["late"] else ""
        held = ("  [YOU HOLD %s]" % r["held"]) if r.get("held") else ""
        print("%s %-5s %s%s, %s (%s): %s, %s%s; notified %s, %d business day%s%s%s" % (
            r["traded"], r["side"].upper(), r["issuer"], (" (%s)" % r["isin"]) if r["isin"] else "", r["person"],
            r["role"] or "role n/a", r["type"], fmt_money(r["value"], r["currency"]), price, r["notified"],
            r["delay_days"], "" if r["delay_days"] == 1 else "s", late, held))


def caveat():
    print("These are public filings read after the fact: not inside information. A trade is reported up to %d "
          "business days later and shows in the database a day or two after that. Germany's threshold is €50,000 a "
          "calendar year since 2026-01-01 (€20,000 before), so smaller trades may not appear." % LATE_BUSINESS_DAYS)
    print("LATE = notified more than %d weekdays after the trade (public holidays are not subtracted, so check it)."
          % LATE_BUSINESS_DAYS)


def _filter(rows, a):
    out = rows
    if a.side:
        out = [r for r in out if r["side"] == a.side]
    if a.min_value is not None:
        out = [r for r in out if (r["value"] or 0) >= a.min_value]
    if a.since:
        since = parse_date(a.since).isoformat()
        out = [r for r in out if r["traded"] >= since]
    return out


def _load(a):
    rows, errors = read_table(a.file, a.decimal)
    pf = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = json.load(open(a.portfolio, encoding="utf-8"))
    isin_map = json.load(open(a.isin_map, encoding="utf-8")) if a.isin_map and os.path.exists(a.isin_map) else {}
    return flag_holdings(rows, pf, isin_map), errors


def _report(rows, errors, as_json=False):
    if as_json:
        print(json.dumps({"rows": rows, "clusters": clusters(rows), "errors": errors}, indent=2))
        return
    show(rows)
    for c in clusters(rows):
        print("CLUSTER: %d insiders bought %s between %s and %s, %s together." % (
            c["people"], c["issuer"], c["from"], c["to"], fmt_money(c["value"], "EUR")))
    for e in errors:
        print("error: %s (skipped, not marked as seen)" % e)
    caveat()


def cmd_list(a):
    rows, errors = _load(a)
    rows = _filter(rows, a)
    _report(rows, errors, a.json)
    return 0 if rows else 1


def cmd_new(a):
    rows, errors = _load(a)
    state = {"seen": []}
    if os.path.exists(a.state):
        try:
            state = json.load(open(a.state, encoding="utf-8"))
        except (ValueError, OSError):
            raise DealingsError("%s is not valid JSON; fix or delete it" % a.state)
    seen = set(state.get("seen", []))
    fresh = _filter([r for r in rows if r["key"] not in seen], a)
    if fresh:
        print("%d new row(s):" % len(fresh))
    else:
        print("Nothing new since the last check.")
    _report(fresh, errors, a.json) if fresh or errors else None
    state["seen"] = sorted(seen | {r["key"] for r in fresh})
    d = os.path.dirname(a.state)
    if d:
        os.makedirs(d, exist_ok=True)
    json.dump(state, open(a.state, "w", encoding="utf-8"), indent=2)
    return 0 if fresh else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Directors' dealings reader (trader-insiders). Never downloads, never trades.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("file", help="the dealings table saved as CSV")
        p.add_argument("--decimal", choices=["auto", "comma", "dot"], default="auto")
        p.add_argument("--side", choices=["buy", "sell", "other"])
        p.add_argument("--min-value", type=float, help="EUR; hide smaller trades")
        p.add_argument("--since", help="trade date from (YYYY-MM-DD or DD.MM.YYYY)")
        p.add_argument("--portfolio", default="trader/portfolio.json", help="flag companies you hold (positions with an isin)")
        p.add_argument("--isin-map", default="trader/isin.json", help='optional {"ISIN": "TICKER"} file')
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="every row, cleaned")
    common(p)
    p = sub.add_parser("new", help="only rows not shown before")
    common(p)
    p.add_argument("--state", default="trader/state/insiders.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"list": cmd_list, "new": cmd_new}[a.cmd](a)
    except DealingsError as e:
        print("dealings.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
