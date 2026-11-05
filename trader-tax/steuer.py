#!/usr/bin/env python3
"""German capital-gains tax tracker for trader-tax. Standard library only, no network.

It does arithmetic on the lots and sales you give it, using the German rules for private investors
(Abgeltungsteuer). It is a calculator, not a filing: your broker's tax certificate (Steuerbescheinigung)
and your tax assessment are what count. Where the law is unsettled it says so (see "Open points").

    python3 steuer.py settings --status single --church 0.09     # once: your tax situation
    python3 steuer.py report [--lots]                            # unrealized gain/loss per position, in euro
    python3 steuer.py sell SAP.DE 5 --price 140 --fees 1        # what a sale would realize and cost in tax
    python3 steuer.py sell SAP.DE 5 --price 140 --record        # it filled: update lots + the realized log
    python3 steuer.py income --ticker KO --type dividend --gross 30 --foreign-tax 4.5 --date 2026-04-15
    python3 steuer.py carry --year 2026 --stock 300 --other 0   # losses carried in from last year's certificate
    python3 steuer.py year --year 2026                           # pots, allowance, tax so far, loss carry-forward
    python3 steuer.py harvest --min-loss 100                     # unrealized losses and what they could offset

Files: trader/portfolio.json (lots), trader/prices.json (euro, from trader-alerts), trader/tax.json (settings,
realized sales, income, loss carry-in). A lot: {"id", "date", "shares", "cost"[, "depot"]}; "cost" is the
total purchase cost in EUR including fees. A position with "fund_type" is a fund/ETF.

The rules, with their source (all read on 2026-11-05 from gesetze-im-internet.de unless noted):
  gain          proceeds - costs of the sale - purchase cost. For a foreign-currency trade, proceeds are
                converted at the sale date and the cost at the purchase date, so a currency move is part
                of the gain (§ 20 Abs. 4 Satz 1 EStG). Cost in the lots must therefore be the EUR amount
                at the purchase-day rate, which is what your broker's statement shows.
  FIFO          shares of one kind in one custody account are sold oldest first (§ 20 Abs. 4 Satz 7 EStG).
                There is no choice of lots; the tool refuses to guess between two depots.
  loss pots     losses from selling STOCKS offset only gains from selling stocks (§ 20 Abs. 6 Satz 4 EStG).
                Everything else (fund/ETF sales, dividends, interest) is one other pot, and a loss in it
                offsets any capital income, stock gains included. Unused losses carry forward in their pot.
  allowance     1,000 EUR, 2,000 EUR for a joint assessment (§ 20 Abs. 9 EStG).
  fund exemption  equity fund 30 %, mixed fund 15 %, real-estate fund 60 %, foreign real-estate fund 80 % of
                the income is tax-free (§ 20 InvStG). It applies to sale losses too (BFH 25.11.2025,
                VIII R 31/23), except the part of a loss from pre-2018 holdings' fictitious purchase cost
                (not modelled; the tool is for holdings you bought since 2018).
  tax           25 % flat; with church tax k the tax is (e - 4q) / (4 + k), q = foreign tax you may credit
                (§ 32d Abs. 1 EStG). Solidarity surcharge 5.5 % of that tax (§ 4 SolZG). Church tax = k x tax.
                Check: 1,000 EUR gain, 9 % church tax -> 27.99 % in total.

Open points (said in the output where they matter):
  - Whether the rule that stock losses only offset stock gains is constitutional is before the Federal
    Constitutional Court (2 BvL 3/21). No decision in any source read on 2026-11-05.
  - Your bank nets and withholds per account during the year. This tool nets over everything you give it,
    which is what the assessment does. A loss left at one bank can be moved to the assessment with a loss
    certificate that must be requested by 15 December (§ 43a Abs. 3 Satz 4-5 EStG).
  - If your personal income tax rate is under 25 %, the assessment can be cheaper (§ 32d Abs. 6 EStG,
    Guenstigerpruefung). Not modelled.
  - The creditable foreign tax (q) is the number you give; the limit for a tax treaty is not modelled.
  - Derivatives, bonds, crypto, accumulating-fund advance lump sums (Vorabpauschale) and gifts/inheritance
    carry-over of cost are not modelled (enter a Vorabpauschale as income of type "fund_distribution").
"""

import argparse
import datetime
import json
import math
import os
import sys

DEFAULT_SETTINGS = {"status": "single", "church_tax_rate": 0.0, "soli_rate": 0.055}
ALLOWANCE = {"single": 1000.0, "joint": 2000.0}
FUND_EXEMPTION = {"equity": 0.30, "mixed": 0.15, "real_estate": 0.60, "foreign_real_estate": 0.80, "other": 0.0}
INCOME_TYPES = ("dividend", "interest", "fund_distribution")
EPS = 1e-9


class TaxError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TaxError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TaxError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def num(x, what, minimum=None, strictly_positive=False):
    if isinstance(x, bool):
        raise TaxError("%s must be a number" % what)
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise TaxError("%s must be a number, got %r" % (what, x))
    if not math.isfinite(v):
        raise TaxError("%s must be a finite number, got %r" % (what, x))
    if strictly_positive and v <= 0:
        raise TaxError("%s must be above zero, got %s" % (what, x))
    if minimum is not None and v < minimum:
        raise TaxError("%s must be at least %s, got %s" % (what, minimum, x))
    return v


def day(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        raise TaxError("%r is not a date (use YYYY-MM-DD)" % (s,))


def money(x):
    x = round(float(x), 2)
    sign = "-" if x < 0 else ""
    s = "{:,.2f}".format(abs(x))
    return sign + "€" + (s[:-3] if s.endswith(".00") else s)


def fmt_qty(x):
    return ("%.4f" % x).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- the rules

def teilfreistellung(fund_type):
    """Share of fund income that is tax-free (§ 20 InvStG)."""
    if fund_type not in FUND_EXEMPTION:
        raise TaxError("unknown fund_type %r (use one of: %s)" % (fund_type, ", ".join(FUND_EXEMPTION)))
    return FUND_EXEMPTION[fund_type]


def taxable_amount(rec):
    """A realized sale or an income line, after the fund exemption. Gains and losses alike."""
    amount = rec["gain"] if "gain" in rec else rec["gross"]
    if rec.get("kind") == "fund" or rec.get("fund_type"):
        amount *= 1.0 - teilfreistellung(rec.get("fund_type", "other"))
    return amount


def compute_tax(taxable, church_rate, soli_rate, foreign_tax=0.0):
    """Tax on `taxable` EUR of capital income after allowance (§ 32d Abs. 1 EStG): (e - 4q) / (4 + k)."""
    e = num(taxable, "taxable income")
    k = num(church_rate, "church tax rate", minimum=0)
    if k > 0.1:
        raise TaxError("church tax rate is 0.08 or 0.09 (a share, not a percent), got %s" % church_rate)
    s = num(soli_rate, "solidarity surcharge rate", minimum=0)
    q = num(foreign_tax, "foreign tax", minimum=0)
    income_tax = max(0.0, (max(e, 0.0) - 4.0 * q) / (4.0 + k))
    soli = income_tax * s
    church = income_tax * k
    return {"income_tax": income_tax, "soli": soli, "church_tax": church, "total": income_tax + soli + church}


def settings_of(state):
    s = dict(DEFAULT_SETTINGS)
    s.update((state or {}).get("settings") or {})
    if s["status"] not in ALLOWANCE:
        raise TaxError("status must be single or joint, got %r" % s["status"])
    return s


def year_summary(state, year):
    """Net the year's sales and income into the two loss pots and work out the tax."""
    s = settings_of(state)
    stock, other = 0.0, 0.0
    sales = [r for r in state.get("realized", []) if day(r["date"]).year == year]
    income = [r for r in state.get("income", []) if day(r["date"]).year == year]
    for r in sales:
        if r.get("kind") == "fund":
            other += taxable_amount(r)
        else:
            stock += r["gain"]
    foreign_tax = 0.0
    for r in income:
        other += taxable_amount(r)
        foreign_tax += num(r.get("foreign_tax", 0) or 0, "foreign tax", minimum=0)
    carry = (state.get("carry_in") or {}).get(str(year)) or {}
    stock -= num(carry.get("stock", 0) or 0, "carried-in stock loss", minimum=0)
    other -= num(carry.get("other", 0) or 0, "carried-in other loss", minimum=0)
    stock_net, other_net = stock, other
    # a loss in the other pot may offset stock gains; a stock loss may not offset the other pot
    if stock_net > 0 and other_net < 0:
        used = min(stock_net, -other_net)
        stock_net -= used
        other_net += used
    before = max(stock_net, 0.0) + max(other_net, 0.0)
    allowance = s.get("allowance")
    allowance = ALLOWANCE[s["status"]] if allowance is None else num(allowance, "allowance", minimum=0)
    used_allowance = min(allowance, before)
    taxable = before - used_allowance
    return {"year": year, "stock_net": stock, "other_net": other, "allowance": allowance,
            "taxable_before_allowance": before, "taxable": taxable, "foreign_tax": foreign_tax,
            "carry_out": {"stock": max(-stock_net, 0.0), "other": max(-other_net, 0.0)},
            "tax": compute_tax(taxable, s["church_tax_rate"], s["soli_rate"], foreign_tax),
            "sales": len(sales), "income_lines": len(income), "settings": s}


# ---------------------------------------------------------------- lots and FIFO

def _lot_order(lots):
    return sorted(enumerate(lots), key=lambda iv: (iv[1]["date"], iv[0]))


def simulate_sale(lots, shares, price, fees=0.0, depot=None):
    """FIFO sale of `shares` at `price` EUR a share. Returns proceeds, cost, gain and the lots used."""
    shares = num(shares, "shares", strictly_positive=True)
    price = num(price, "price", minimum=0)
    fees = num(fees, "fees", minimum=0)
    pool = lots
    depots = {l.get("depot") for l in lots}
    if depot is not None:
        pool = [l for l in lots if l.get("depot") == depot]
    elif len(depots) > 1:
        raise TaxError("these lots sit in more than one depot (%s): FIFO runs inside one custody account, "
                       "name it with --depot" % ", ".join(sorted(str(d) for d in depots)))
    held = sum(num(l["shares"], "lot shares") for l in pool)
    if shares > held + EPS:
        raise TaxError("you hold %s shares there, not %s" % (fmt_qty(held), fmt_qty(shares)))
    left, cost, used = shares, 0.0, []
    for _, l in _lot_order(pool):
        if left <= EPS:
            break
        take = min(left, num(l["shares"], "lot shares"))
        part = num(l["cost"], "lot cost", minimum=0) * take / l["shares"]
        used.append({"id": l.get("id"), "shares": take, "cost": part})
        cost += part
        left -= take
    proceeds = shares * price
    return {"shares": shares, "price": price, "proceeds": proceeds, "fees": fees, "cost": cost,
            "gain": proceeds - fees - cost, "used": used}


def apply_sale(position, sim):
    """Remove the sold shares from the position's lots (cost shrinks in proportion)."""
    left = {u["id"]: u for u in sim["used"]}
    kept = []
    for l in position["lots"]:
        u = left.get(l.get("id"))
        if not u:
            kept.append(l)
            continue
        rest = l["shares"] - u["shares"]
        if rest > EPS:
            kept.append(dict(l, shares=round(rest, 8), cost=round(l["cost"] - u["cost"], 2)))
    position["lots"] = kept
    position["shares"] = round(sum(l["shares"] for l in kept), 8)


# ---------------------------------------------------------------- files

def load_inputs(a, need_prices=True):
    pf = read_json(a.portfolio, "portfolio")
    if not isinstance(pf.get("positions"), list):
        raise TaxError("%s has no positions" % a.portfolio)
    prices = {}
    if need_prices:
        doc = read_json(a.prices, "prices (run trader-alerts: alerts.py quote --holdings --fx USD=RATE --fx-date DATE --write)")
        if str(doc.get("currency", "")).upper() != "EUR":
            raise TaxError("%s does not say its prices are in euro; refresh it with trader-alerts "
                           "(--fx USD=RATE --fx-date DATE --write)" % a.prices)
        prices = {str(k).upper(): v for k, v in (doc.get("prices") or {}).items()}
    state = read_json(a.tax, "tax file", required=False) or {}
    state.setdefault("realized", [])
    state.setdefault("income", [])
    state.setdefault("carry_in", {})
    return pf, prices, state


def position_of(pf, ticker):
    for p in pf["positions"]:
        if str(p.get("ticker", "")).upper() == ticker.upper():
            if not p.get("lots"):
                raise TaxError("%s has shares but no lots; add its purchases to the portfolio first" % ticker)
            return p
    raise TaxError("you do not hold %s" % ticker)


def lots_of(position):
    out = []
    for i, l in enumerate(position["lots"], 1):
        out.append(dict(l, id=l.get("id") or "%s-%d" % (position["ticker"], i)))
    return out


def _kind(position):
    return ("fund", position["fund_type"]) if position.get("fund_type") else ("stock", None)


# ---------------------------------------------------------------- commands

def cmd_settings(a):
    state = read_json(a.tax, "tax file", required=False) or {}
    s = dict(state.get("settings") or {})
    if a.status:
        if a.status not in ALLOWANCE:
            raise TaxError("--status is single or joint")
        s["status"] = a.status
    if a.church is not None:
        compute_tax(0, a.church, 0.055)  # validates the range
        s["church_tax_rate"] = a.church
    if a.soli is not None:
        s["soli_rate"] = num(a.soli, "--soli", minimum=0)
    if a.allowance is not None:
        s["allowance"] = num(a.allowance, "--allowance", minimum=0)
    state["settings"] = s
    write_json(a.tax, state)
    full = settings_of(state)
    print("Saved: %s assessment, church tax %s, solidarity surcharge %s, allowance %s." % (
        full["status"], "%g%%" % (100 * full["church_tax_rate"]) if full["church_tax_rate"] else "none",
        "%g%%" % (100 * full["soli_rate"]), money(ALLOWANCE[full["status"]] if full.get("allowance") is None else full["allowance"])))
    return 0


def cmd_report(a):
    pf, prices, state = load_inputs(a)
    total_gain = 0.0
    for p in pf["positions"]:
        t = str(p["ticker"]).upper()
        lots = lots_of(p)
        price = prices.get(t)
        if price is None:
            print("%-9s no euro price in prices.json: skipped" % t)
            continue
        shares = sum(l["shares"] for l in lots)
        value = shares * float(price)
        cost = sum(l["cost"] for l in lots)
        gain = value - cost
        total_gain += gain
        extra = ("  [fund, %g%% tax-free]" % (100 * teilfreistellung(p["fund_type"]))) if p.get("fund_type") else ""
        print("%-9s %s sh  value %s  cost %s  %s%s" % (t, fmt_qty(shares), money(value), money(cost), money(gain), extra))
        if a.lots:
            for l in lots:
                lv = l["shares"] * float(price)
                print("    %-12s %s  %s sh  cost %s  %s" % (l["id"], l["date"], fmt_qty(l["shares"]), money(l["cost"]),
                                                            money(lv - l["cost"])))
    print("Unrealized in total: %s. Nothing is taxed until you sell." % money(total_gain))
    return 0


def _preview(state, year, rec):
    before = year_summary(state, year)
    after = year_summary(dict(state, realized=state["realized"] + [rec]), year)
    return before, after


def cmd_sell(a):
    pf, prices, state = load_inputs(a, need_prices=False)
    pos = position_of(pf, a.ticker)
    lots = lots_of(pos)
    sim = simulate_sale(lots, a.shares, a.price, a.fees, a.depot)
    kind, fund_type = _kind(pos)
    when = a.date or datetime.date.today().isoformat()
    rec = {"date": day(when).isoformat(), "ticker": pos["ticker"].upper(), "shares": sim["shares"],
           "proceeds": round(sim["proceeds"], 2), "fees": round(sim["fees"], 2), "cost": round(sim["cost"], 2),
           "gain": round(sim["gain"], 2), "kind": kind}
    if fund_type:
        rec["fund_type"] = fund_type
    year = day(rec["date"]).year
    before, after = _preview(state, year, rec)
    print("Selling %s %s at %s (FIFO): proceeds %s, cost %s%s, gain %s." % (
        fmt_qty(sim["shares"]), rec["ticker"], money(sim["price"]), money(sim["proceeds"]), money(sim["cost"]),
        (", fees %s" % money(sim["fees"])) if sim["fees"] else "", money(sim["gain"])))
    for u in sim["used"]:
        print("    from %-12s %s sh, cost %s" % (u["id"], fmt_qty(u["shares"]), money(u["cost"])))
    if kind == "fund":
        print("It is a %s fund: %g%% of the result is tax-free, so %s counts. It goes in the other pot." % (
            fund_type.replace("_", " "), 100 * teilfreistellung(fund_type), money(taxable_amount(rec))))
    else:
        print("It is a stock: the result goes in the stock pot (stock losses only offset stock gains).")
    delta = after["tax"]["total"] - before["tax"]["total"]
    print("Tax for %d so far %s -> %s with this sale (%s%s)." % (
        year, money(before["tax"]["total"]), money(after["tax"]["total"]), "+" if delta >= 0 else "-", money(abs(delta))))
    if after["taxable"] <= 0 < after["taxable_before_allowance"]:
        print("All of it is still inside your allowance of %s." % money(after["allowance"]))
    if after["carry_out"]["stock"] > before["carry_out"]["stock"] + 0.005:
        print("This leaves a stock loss of %s that only stock gains can use." % money(after["carry_out"]["stock"]))
    if not a.record:
        print("Preview only: nothing was changed. Add --record once it has filled.")
        return 0
    apply_sale(pos, sim)
    pf["positions"] = [p for p in pf["positions"] if p.get("shares", 0) > EPS or p is not pos]
    state["realized"].append(rec)
    pf["cash"] = round(float(pf.get("cash", 0) or 0) + sim["proceeds"] - sim["fees"], 2)
    write_json(a.portfolio, pf)
    write_json(a.tax, state)
    print("Recorded: lots and cash updated in %s, sale logged in %s." % (a.portfolio, a.tax))
    return 0


def cmd_income(a):
    state = read_json(a.tax, "tax file", required=False) or {}
    state.setdefault("income", [])
    rec = {"date": day(a.date or datetime.date.today().isoformat()).isoformat(), "ticker": a.ticker.upper(),
           "type": a.type, "gross": num(a.gross, "--gross")}
    if a.foreign_tax:
        rec["foreign_tax"] = num(a.foreign_tax, "--foreign-tax", minimum=0)
    if a.fund_type:
        teilfreistellung(a.fund_type)
        rec["fund_type"] = a.fund_type
        rec["kind"] = "fund"
    state["income"].append(rec)
    write_json(a.tax, state)
    print("Recorded %s %s %s." % (rec["type"], rec["ticker"], money(rec["gross"])))
    return 0


def cmd_carry(a):
    state = read_json(a.tax, "tax file", required=False) or {}
    state.setdefault("carry_in", {})[str(a.year)] = {"stock": num(a.stock, "--stock", minimum=0),
                                                       "other": num(a.other, "--other", minimum=0)}
    write_json(a.tax, state)
    print("Losses carried into %d: stock %s, other %s." % (a.year, money(a.stock), money(a.other)))
    return 0


def cmd_year(a):
    state = read_json(a.tax, "tax file", required=False) or {}
    today = day(a.today) if a.today else datetime.date.today()
    year = a.year or today.year
    y = year_summary(state, year)
    t = y["tax"]
    print("Capital income tax %d (%d sale(s), %d income line(s))" % (year, y["sales"], y["income_lines"]))
    print("  Stock pot (after carry-in):          %s" % money(y["stock_net"]))
    print("  Other pot (funds, dividends, interest): %s" % money(y["other_net"]))
    print("  Taxable before allowance:             %s" % money(y["taxable_before_allowance"]))
    print("  Sparer-Pauschbetrag (allowance):      -%s (%s)" % (money(min(y["allowance"], y["taxable_before_allowance"])).lstrip("-"),
                                                                 y["settings"]["status"]))
    print("  Taxable:                              %s" % money(y["taxable"]))
    print("  Income tax %s + surcharge %s + church tax %s = %s" % (
        money(t["income_tax"]), money(t["soli"]), money(t["church_tax"]), money(t["total"])))
    if y["foreign_tax"]:
        print("  (credited foreign tax: %s)" % money(y["foreign_tax"]))
    co = y["carry_out"]
    if co["stock"] or co["other"]:
        print("  Loss carried to %d: stock %s, other %s" % (year + 1, money(co["stock"]), money(co["other"])))
    sales = [r for r in state.get("realized", []) if day(r["date"]).year == year]
    has_loss = any(r["gain"] < 0 for r in sales) or co["stock"] or co["other"]
    if any(r["gain"] < 0 and r.get("kind") != "fund" for r in sales) or co["stock"]:
        print("Note: the rule that stock losses only offset stock gains is before the Federal Constitutional "
              "Court (2 BvL 3/21), undecided in the sources read on 2026-11-05. Keep your loss certificates and "
              "tax assessments.")
    if has_loss and datetime.date(today.year, 10, 1) <= today <= datetime.date(today.year, 12, 15) and today.year == year:
        print("Reminder: a loss left at one bank can only be used against gains at another bank through a loss "
              "certificate (Verlustbescheinigung), and the request must reach the bank by 15 December (§ 43a Abs. 3 EStG).")
    print("This nets everything you recorded. Your banks withhold per account during the year; the tax "
          "assessment settles the rest. Compare with your broker's Steuerbescheinigung.")
    return 0


def cmd_harvest(a):
    pf, prices, state = load_inputs(a)
    year = a.year or datetime.date.today().year
    y = year_summary(state, year)
    gains_stock = max(y["stock_net"], 0.0)
    gains_other = max(y["other_net"], 0.0)
    rows = []
    for p in pf["positions"]:
        t = str(p["ticker"]).upper()
        price = prices.get(t)
        if price is None:
            continue
        lots = lots_of(p)
        value = sum(l["shares"] for l in lots) * float(price)
        gain = value - sum(l["cost"] for l in lots)
        if -gain >= a.min_loss:
            kind, ft = _kind(p)
            rows.append((t, gain, kind, ft))
    if not rows:
        print("No position is down by %s or more." % money(a.min_loss))
        return 0
    for t, gain, kind, ft in sorted(rows, key=lambda r: r[1]):
        if kind == "stock":
            use = ("would offset stock gains only: %s of stock gains this year so far" % money(gains_stock))
        else:
            use = ("%g%% tax-free, so %s counts; offsets any capital income: %s of gains this year so far"
                   % (100 * teilfreistellung(ft), money(gain * (1 - teilfreistellung(ft))), money(gains_stock + gains_other)))
        print("%-9s unrealized %s  [%s]  %s" % (t, money(gain), kind, use))
    print("A loss only saves tax if there are gains to offset (or you carry it forward). Selling to buy the same "
          "thing straight back is an arrangement the tax office may question; I did not find a statute either "
          "way in what I read, so it is not modelled.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="German capital-gains tax tracker (trader-tax).")
    sub = ap.add_subparsers(dest="cmd")

    def files(p):
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--prices", default="trader/prices.json")
        p.add_argument("--tax", default="trader/tax.json")

    p = sub.add_parser("settings", help="your tax situation")
    files(p)
    p.add_argument("--status", help="single or joint")
    p.add_argument("--church", type=float, help="church tax rate: 0.08 (Bavaria, Baden-Wuerttemberg) or 0.09; 0 if none")
    p.add_argument("--soli", type=float, help="solidarity surcharge rate (default 0.055)")
    p.add_argument("--allowance", type=float, help="allowance left for this tax if part is used elsewhere")
    p = sub.add_parser("report", help="unrealized gain/loss in euro")
    files(p)
    p.add_argument("--lots", action="store_true")
    p = sub.add_parser("sell", help="what a sale would realize (FIFO) and cost in tax")
    files(p)
    p.add_argument("ticker")
    p.add_argument("shares", type=float)
    p.add_argument("--price", type=float, required=True, help="EUR a share")
    p.add_argument("--fees", type=float, default=0.0, help="EUR of fees on the sale")
    p.add_argument("--depot", help="custody account (needed if the lots sit in more than one)")
    p.add_argument("--date", help="sale date (default today)")
    p.add_argument("--record", action="store_true", help="it filled: update lots and the realized log")
    p = sub.add_parser("income", help="record a dividend, interest or fund distribution")
    files(p)
    p.add_argument("--ticker", required=True)
    p.add_argument("--type", choices=INCOME_TYPES, required=True)
    p.add_argument("--gross", type=float, required=True, help="EUR before tax")
    p.add_argument("--foreign-tax", type=float, default=0.0, help="foreign tax withheld that you may credit, EUR")
    p.add_argument("--fund-type", choices=list(FUND_EXEMPTION))
    p.add_argument("--date")
    p = sub.add_parser("carry", help="losses carried into a year (from your bank's certificate / last assessment)")
    files(p)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--stock", type=float, default=0.0)
    p.add_argument("--other", type=float, default=0.0)
    p = sub.add_parser("year", help="the year's pots, allowance and tax")
    files(p)
    p.add_argument("--year", type=int)
    p.add_argument("--today", help="pretend today is this date")
    p = sub.add_parser("harvest", help="unrealized losses and what they could offset")
    files(p)
    p.add_argument("--min-loss", type=float, default=100.0)
    p.add_argument("--year", type=int)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"settings": cmd_settings, "report": cmd_report, "sell": cmd_sell, "income": cmd_income,
                "carry": cmd_carry, "year": cmd_year, "harvest": cmd_harvest}[a.cmd](a)
    except TaxError as e:
        print("steuer.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
