#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json) and
prices (trader/prices.json). Each order gets PASS or BLOCK with the reasons.
Orders are checked in file order as if each PASS had filled, so five buys
that are fine one by one but break the cash buffer together are caught.

    python3 check.py                       # the trader/ files, writes trader/state/risk.json
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write            # check without recording the verdicts

What blocks an order:

  format        missing symbol or side, both or neither of qty / notional,
                notional on anything but a market day order (Alpaca's rule),
                a limit order without a limit price, unknown type
  asset         options (OCC symbols) unless "allow_options": true; crypto
                and anything that is not a stock or ETF; US-domiciled ETFs
                (a BUY; EU retail brokers can't sell them under PRIIPs) unless
                "allow_us_etfs": true; leveraged and inverse funds unless
                "allow_leveraged": true; the lists are partial, not complete
  currency      a share order sized from a price that prices.json does not
                mark as euro; a limit price whose currency is unknown
  blocked       a ticker on your "blocked" list
  size          under min_order_eur, over max_order_eur
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  short         selling more than you hold, unless "allow_short": true
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   more than max_orders_per_day orders placed today

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_eur": 2000,
    "min_order_eur": 5, "max_orders_per_day": 10, "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "allow_short": False,
    "max_price_age_hours": 72, "currency": "EUR",
}
ALPACA_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
TYPES = {"market", "limit", "stop", "stop_limit", "trailing_stop"}
TIFS = {"day", "gtc", "opg", "cls", "ioc", "fok"}
OCC = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
# US tickers: 1-5 letters, optionally a one-letter share class (BRK.B, BF-B).
# European tickers: Yahoo-style, the exchange suffix is part of the ticker (SAP.DE, 1COV.DE, MC.PA, NOVO-B.CO).
# The suffixes are the same list as trader-alerts (each skill folder stands alone, so it is repeated here).
EXCHANGE_SUFFIXES = ("DE", "PA", "AS", "BR", "LS", "MI", "MC", "HE", "VI", "IR", "L", "SW", "ST", "CO", "OL")
TICKER = re.compile(r"^(?:[A-Z]{1,5}(?:[.\-][A-Z])?|[A-Z0-9]{1,6}(?:-[A-Z])?\.(?:%s))$" % "|".join(EXCHANGE_SUFFIXES))

# Well-known US-domiciled ETFs. EU retail clients generally cannot BUY these: the PRIIPs rules need a Key
# Information Document (KID) that US fund companies do not publish, so brokers stopped offering them
# (https://www.justetf.com/en/news/etf/us-domiciled-etfs.html: you can hold or sell, but not buy more).
# This list is NOT complete: it catches the common ones; your broker is the authority.
US_ETFS = {
    "SPY", "IVV", "VOO", "VTI", "QQQ", "DIA", "IWM", "IWF", "IWD", "VEA", "VWO", "VNQ", "VUG", "VTV", "VIG", "VYM",
    "SCHD", "SCHX", "SCHB", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC", "ARKK",
    "GLD", "SLV", "TLT", "IEF", "SHY", "LQD", "HYG", "AGG", "BND", "EEM", "EFA", "IEFA", "IEMG", "SOXX", "SMH",
    "VGT", "VXUS", "VT", "SPYG", "SPYV", "MTUM", "QUAL", "USMV", "QQQM", "SPLG", "ITOT", "IJH", "IJR", "VB", "VO",
}
# Leveraged and inverse ETFs/ETNs: they reset daily and can lose most of their value while the index is flat.
LEVERAGED = {
    "TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SPXU", "SOXL", "SOXS", "TNA", "TZA", "UVXY", "SVXY", "VXX", "UVIX",
    "LABU", "LABD", "FAS", "FAZ", "TECL", "TECS", "NUGT", "DUST", "JNUG", "JDST", "BOIL", "KOLD", "UCO", "SCO",
    "YINN", "YANG", "TMF", "TMV", "QLD", "SSO", "SDS", "QID", "SH", "PSQ", "DOG", "SPXS", "FNGU", "FNGD",
}
CENT = 0.005


class RiskError(Exception):
    pass


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked."""
    core = {k: order[k] for k in ALPACA_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    """A finite float, or None. NaN and infinity count as "not a number" so every check fails closed."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _parse_time(value):
    """ISO 8601 ("...Z" or offset) to an aware UTC datetime, or None."""
    try:
        d = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone(datetime.timezone.utc)


def money(x):
    return "€" + "{:,.2f}".format(float(x)).replace(".00", "")


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and str(rec.get("date") or str(rec.get("at", ""))[:10]) == today:
            out.append(rec)
    return out


def native_to_base(meta, base):
    """Multiply a price in the stock's own currency by this to get the base currency.
    None when prices.json does not say (fail closed)."""
    if not isinstance(meta, dict) or meta.get("price_currency") != base:
        return None
    rate = _f(meta.get("fx_rate"))
    if rate is None or rate <= 0:
        return None
    return (0.01 if meta.get("native_currency") in ("GBp", "GBX") else 1.0) / rate


def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, price_times=None, now=None,
          price_meta=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    today = today or datetime.date.today().isoformat()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    price_times = price_times or {}
    price_meta = {str(k).upper(): v for k, v in (price_meta or {}).items()}
    base = str(c.get("currency") or "EUR").upper()
    max_age = float(c.get("max_price_age_hours") or 0)
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    placed_keys = {(str(r.get("symbol", "")).upper(), r.get("side")) for r in done_today}
    seen = set()
    results = []
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")

    for i, o in enumerate(orders, 1):
        reasons, warns = [], []
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        qty, notional = o.get("qty"), o.get("notional")
        value = None

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if (qty is None) == (notional is None):
            reasons.append("format: give exactly one of qty or notional")
        for k in ("qty", "notional", "limit_price", "stop_price", "trail_price", "trail_percent"):
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif not isinstance(o.get(k), str):
                    warns.append("send %s as a string (\"%s\"), the way place_stock_order types it"
                                 % (k, o.get(k)))
        if typ not in TYPES:
            reasons.append("format: unknown order type %r" % typ)
        if tif not in TIFS:
            reasons.append("format: unknown time_in_force %r" % tif)
        if notional is not None and (typ != "market" or tif != "day"):
            reasons.append("format: a notional (amount in euro) order must be a market order with time_in_force day")
        if typ in ("limit", "stop_limit") and o.get("limit_price") is None:
            reasons.append("format: a %s order needs a limit_price" % typ)
        if typ in ("stop", "stop_limit") and o.get("stop_price") is None:
            reasons.append("format: a %s order needs a stop_price" % typ)
        if typ == "trailing_stop" and (o.get("trail_price") is None) == (o.get("trail_percent") is None):
            reasons.append("format: a trailing_stop needs exactly one of trail_price or trail_percent")
        if o.get("check_ticker"):
            reasons.append("ticker: %s is an unverified ticker guess (check_ticker); confirm it against the "
                           "filing, then fix it in trader/tickers.json or with --figi" % (sym or "?"))
        if o.get("extended_hours") and not (typ == "limit" and tif == "day"):
            reasons.append("format: extended hours needs a limit order with time_in_force day")
        if o.get("order_class") or o.get("advanced_instructions"):
            warns.append("advanced order (%s): check every leg before you say yes"
                         % (o.get("order_class") or "advanced_instructions"))

        # asset
        if sym and OCC.match(sym.replace(" ", "")):
            if not c.get("allow_options"):
                reasons.append("asset: %s is an option contract; options are off "
                               "(\"allow_options\": false)" % sym)
            else:
                warns.append("option order: options can lose 100%% of what you pay")
        elif sym and ("/" in sym or not TICKER.match(sym)):
            reasons.append("asset: %s does not look like a stock or ETF ticker (US: KO, BRK.B; Europe: SAP.DE, MC.PA); "
                           "this pack trades stocks and ETFs only" % sym)
        elif sym in LEVERAGED:
            if not c.get("allow_leveraged"):
                reasons.append("asset: %s is a leveraged or inverse fund; it resets daily and can lose most of its "
                               "value while the market goes nowhere (\"allow_leveraged\": false)" % sym)
            else:
                warns.append("%s is a leveraged or inverse fund: it can lose most of what you pay" % sym)
        elif sym in US_ETFS and side == "buy":
            if not c.get("allow_us_etfs"):
                reasons.append("asset: %s is a US-domiciled ETF. EU retail brokers generally cannot sell you one "
                               "(PRIIPs: no Key Information Document). Buy the UCITS version of the same index "
                               "instead (search the index name on justETF); set \"allow_us_etfs\": true only if "
                               "your broker really lets you buy it" % sym)
            else:
                warns.append("%s is a US-domiciled ETF: check your broker really allows it, and the tax treatment" % sym)

        # blocked list
        if sym in blocked:
            reasons.append("blocked: %s is on your blocked list" % sym)

        # value of the order in euro
        ref = None
        if typ in ("limit", "stop_limit"):
            ref = _f(o.get("limit_price"))
        elif typ == "stop":
            ref = _f(o.get("stop_price"))
        price_used = False
        if ref is not None and notional is None and qty is not None:
            # a limit or stop price is in the stock's own currency: bring it to the base currency
            factor = native_to_base(price_meta.get(sym), base)
            if factor is None:
                reasons.append("currency: prices.json does not say what currency the %s price is in, so the limit "
                               "price cannot be converted to %s; refresh it with a stated rate "
                               "(trader-alerts: alerts.py quote %s --write --fx USD=RATE --fx-date DATE)"
                               % (sym, base, sym))
                ref = None
            else:
                ref = ref * factor
        elif ref is None:
            ref = prices.get(sym)
            price_used = ref is not None and notional is None  # only orders SIZED from a price need it fresh
            if price_used and (price_meta.get(sym) or {}).get("price_currency") != base:
                reasons.append("currency: the %s price is not in %s (prices.json says %s); refresh it with a stated "
                               "rate (trader-alerts: alerts.py quote %s --write --fx USD=RATE --fx-date DATE)"
                               % (sym, base, (price_meta.get(sym) or {}).get("price_currency") or "nothing", sym))
        if price_used and max_age:
            stamp = _parse_time(price_times.get(sym))
            if stamp is None:
                reasons.append("price: no timestamp for the %s price, so its age is unknown; refresh it "
                               "(trader-alerts: alerts.py quote %s --write)" % (sym, sym))
            else:
                age_h = (now - stamp).total_seconds() / 3600.0
                if age_h > max_age:
                    reasons.append("price: the %s price is %.0f hours old (max %s); refresh it"
                                   % (sym, age_h, _pct(max_age)))
        if notional is not None and _f(notional):
            value = _f(notional)
        elif qty is not None and _f(qty):
            if ref:
                value = _f(qty) * ref
            else:
                reasons.append("price: no price for %s to size a %s-share order; add it to "
                               "trader/prices.json (trader-alerts: alerts.py quote %s --write)"
                               % (sym, qty, sym))

        if value is not None:
            if value < float(c.get("min_order_eur") or 0) - CENT:
                reasons.append("size: %s is under your %s minimum order"
                               % (money(value), money(c["min_order_eur"])))
            if c.get("max_order_eur") and value > float(c["max_order_eur"]) + CENT:
                reasons.append("size: %s is over your %s max order"
                               % (money(value), money(c["max_order_eur"])))

        # account effects, simulated in order
        cash = acct["cash"]
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        total = cash + sum(acct["values"].values())
        buffer_eur = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy":
            after_cash = cash - value
            if after_cash < -CENT and not c.get("allow_margin"):
                reasons.append("cash: needs %s but only %s cash; that would be margin (borrowed money)"
                               % (money(value), money(cash)))
            elif after_cash < buffer_eur - CENT:
                reasons.append("cash: leaves %s cash, under your %s%% buffer of %s"
                               % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_eur)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                reasons.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                               % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                  _pct(c["max_position_pct"])))
        if side == "sell" and sym:
            if qty is not None and _f(qty):
                over = _f(qty) > held + 1e-9
            elif value is not None:
                over = value > held_value + CENT
            else:
                over = held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off"
                               % (sym, _num(held)))

        # duplicates and the daily limit
        key = (sym, side)
        if key in seen:
            reasons.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            reasons.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            reasons.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                           "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS" and value is not None:
            if side == "buy":
                acct["cash"] -= value
                acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if ref:
                    acct["shares"][sym] = held + value / ref
            else:
                acct["cash"] += value
                acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if qty is not None:
                    acct["shares"][sym] = held - _f(qty)
                elif ref:
                    acct["shares"][sym] = held - value / ref
        results.append({
            "index": i, "symbol": sym, "side": side,
            "amount": money(_f(notional) or 0) if notional is not None else ("%s shares" % qty),
            "value_eur": round(value, 2) if value is not None else None,
            "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns,
        })

    return {
        "date": today,
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
    }


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"]))
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], money(res["cash_after"]), "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        doc = read_json(a.orders, "orders file (run trader-budget or trader-rebalance first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices = pdoc.get("prices", pdoc) if isinstance(pdoc, dict) else {}
        state = read_json(a.state, "broker state", required=False)
        detail = pdoc.get("detail") if isinstance(pdoc.get("detail"), dict) else {}
        times = {str(t).upper(): (detail.get(t) or {}).get("fetched_at") or pdoc.get("as_of") for t in prices}
        res = check(orders, cfg, portfolio, prices, state, a.today, price_times=times, price_meta=detail)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now().replace(microsecond=0).isoformat()
            rec["orders_file"] = a.orders
            with open(a.out, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
