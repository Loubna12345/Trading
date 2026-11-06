#!/usr/bin/env python3
"""Make one self-contained HTML page from a replay folder. Standard library only.

The page shows how the rules did against holding the stock and against staying out, week by week, with the
forecast errors, the assumptions behind the rule choices and the limits of the result. It reads the ledger (and
checks its chain), and uses prices only for weeks that were already revealed, to compute the true move of the
stock and its split into overnight and intraday. It never touches the sealed weeks and never prints the stock's
name, dates or file paths.

  python3 report.py --dir trader/replay/a --out report.html [--label "Dry run, placeholder costs"]
"""

import argparse
import html
import importlib.util
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


replay = _load(os.path.join(HERE, "replay.py"), "trader_replay_report")
T = replay.T

NOTE_LETTER = re.compile(r"^\s*([A-Z])\s*[:(]")


# ---------- numbers ----------

def build(folder):
    plan, blind, weeks, entries = replay._open(folder)       # checks the ledger chain, the plan and the data
    st = replay._state(plan, entries)
    results = sorted(st["results"], key=lambda e: e["week"])
    if not results:
        raise ValueError("no week has been scored yet; play at least one week first")
    preds = {e["week"]: e for e in st["predictions"]}
    first, last = results[0]["week"], results[-1]["week"]
    base = blind[weeks[first - 1][0]]["o"]
    rows, cum_rules, cum_stock, total = [], [], [], 1.0
    for r in results:
        a, b = weeks[r["week"] - 1]
        prev_close = blind[weeks[r["week"] - 2][1] - 1]["c"] if r["week"] >= 2 else blind[a]["o"]
        p = preds[r["week"]]
        total *= 1.0 + r["strategy_return"]
        cum_rules.append(total - 1.0)
        cum_stock.append(blind[b - 1]["c"] / base - 1.0)
        rows.append({"week": r["week"], "rule": r["rule"], "strategy": p["strategy"], "params": p["params"],
                     "expected_return": r["expected_return"], "strategy_return": r["strategy_return"],
                     "error": r["error"], "stock_move": blind[b - 1]["c"] / prev_close - 1.0,
                     "buy_and_hold_return": r["buy_and_hold_return"], "trades": r["trades"],
                     "costs_paid": r["costs_paid"], "market_expected": r["market_expected"],
                     "market_actual": r["market_actual"], "direction_hit": r["direction_hit"],
                     "note": p.get("note", "")})
    a0, b1 = weeks[first - 1][0], weeks[last - 1][1]
    days, order = {}, []
    for x in blind[a0:b1]:
        d = x["t"][:10]
        if d not in days:
            days[d] = []
            order.append(d)
        days[d].append(x)
    ds = [days[d] for d in order]
    overnight = sum(math.log(ds[i][0]["o"] / ds[i - 1][-1]["c"]) for i in range(1, len(ds)))
    intraday = sum(math.log(d[-1]["c"] / d[0]["o"]) for d in ds)

    letters = {}
    for w in rows:
        m = NOTE_LETTER.match(w["note"])
        key = m.group(1) if m else "-"
        g = letters.setdefault(key, {"letter": key, "weeks": 0, "sum": 1.0, "stock": 1.0, "hits": 0,
                                     "first_note": w["note"]})
        g["weeks"] += 1
        g["sum"] *= 1.0 + w["strategy_return"]
        g["stock"] *= 1.0 + w["stock_move"]
        g["hits"] += 1 if w["direction_hit"] else 0
    assumptions = [{"letter": g["letter"], "weeks": g["weeks"], "rules_return": g["sum"] - 1.0,
                    "stock_return": g["stock"] - 1.0, "direction_hits": g["hits"], "first_note": g["first_note"]}
                   for g in sorted(letters.values(), key=lambda g: g["letter"])]

    by_rule = {}
    for w in rows:
        g = by_rule.setdefault(w["rule"], {"rule": w["rule"], "label": rule_label(w["strategy"], w["params"]),
                                           "weeks": 0, "prod": 1.0, "wins": 0, "trades": 0,
                                           "costs": 0.0})
        g["weeks"] += 1
        g["prod"] *= 1.0 + w["strategy_return"]
        g["wins"] += 1 if w["strategy_return"] > 0 else 0
        g["trades"] += w["trades"]
        g["costs"] += w["costs_paid"]
    rules = [{"rule": g["rule"], "label": g["label"], "weeks": g["weeks"], "total_return": g["prod"] - 1.0,
              "average_week": (g["prod"] - 1.0) / g["weeks"], "winning_weeks": g["wins"], "trades": g["trades"],
              "costs_paid": g["costs"]} for g in sorted(by_rule.values(), key=lambda g: g["rule"])]

    sens = [{"key": key, "label": text, "per_side_bps": per_side_bps(c),
             "total_return": _rescore(plan, blind, weeks, rows, c)} for key, text, c in scenarios(plan["costs"])]
    s = replay.status(folder)
    return {"sensitivity": sens, "weeks": rows, "first_week": first, "revealed_through": st["revealed"], "weeks_total": plan["weeks"],
            "weeks_open": plan["weeks"] - plan["sealed_weeks"], "weeks_sealed": plan["sealed_weeks"],
            "finals_run": len(st["finals"]), "costs": plan["costs"], "cum_rules": cum_rules,
            "cum_stock": cum_stock, "rules_total": cum_rules[-1], "stock_total": cum_stock[-1],
            "overnight_log": overnight, "intraday_log": intraday, "days": len(ds), "assumptions": assumptions,
            "rules": rules, "forecast": s["forecast"], "rules_tried": s["rules_tried"],
            "total_costs": sum(w["costs_paid"] for w in rows), "total_trades": sum(w["trades"] for w in rows)}


def rule_label(strategy, params):
    p = ", ".join("%s=%s" % (k, v) for k, v in sorted(params.items()))
    return strategy + (" (%s)" % p if p else "")


def _scaled(costs, k):
    return {name: v * k for name, v in costs.items()}


def per_side_bps(costs):
    return costs["spread_bps"] / 2.0 + costs["slippage_bps"] + costs["commission_bps"]


FX_FEE_BPS = 15.0   # 0.15% conversion fee per trade, as Trading 212 states it for an instrument in another currency


def scenarios(costs):
    zero = {k: 0.0 for k in costs}
    fx = dict(costs, commission_bps=costs["commission_bps"] + FX_FEE_BPS)
    return [("none", "No costs at all", zero), ("run", "The costs used in this run", dict(costs)),
            ("double", "Twice the costs of this run", _scaled(costs, 2.0)),
            ("fx", "What-if: stock in another currency than the account (0.15% conversion fee on each trade, "
                   "on top of this run's costs)", fx)]


def _rescore(plan, blind, weeks, rows, costs):
    p = dict(plan, costs=costs)
    total = 1.0
    for w in rows:
        a, b = weeks[w["week"] - 1]
        total *= 1.0 + replay._score(p, blind, a, b, w["strategy"], w["params"])["strategy_return"]
    return total - 1.0


def pct(x, signed=True, digits=1):
    if round(x * 100, digits) == 0:
        x = 0.0
    s = "%+.*f%%" % (digits, x * 100) if signed else "%.*f%%" % (digits, x * 100)
    return s.replace("-", "−")


def verdict(d):
    """Plain sentences computed from the numbers; they never call a lucky stretch an edge."""
    n = len(d["weeks"])
    r, h = d["rules_total"], d["stock_total"]
    out = ["Over %d weeks the rules returned %s after costs. Holding the stock returned %s. Staying out of the "
           "market returns 0%%." % (n, pct(r), pct(h))]
    if r <= 0:
        out.append("The rules lost money after costs.")
        if h < r:
            out.append("They lost less than holding because the stock itself fell and the rules were often out "
                       "of the market; that is not an edge.")
    elif r < h:
        out.append("The rules made money but less than holding, so they would not have been worth the effort "
                   "and the costs in this stretch.")
    else:
        out.append("The rules did better than holding and than staying out. That is not proof: it takes many "
                   "more weeks and several stocks, and every rule tried counts as a trial.")
    f = d["forecast"]
    if f.get("weeks"):
        if f["mean_abs_error"] < 0.9 * f["mean_abs_error_if_always_zero"]:
            out.append("The return forecasts were somewhat closer than always guessing zero.")
        else:
            out.append("The return forecasts were no better than always guessing zero (average miss %s against "
                       "%s)." % (pct(f["mean_abs_error"], False, 2), pct(f["mean_abs_error_if_always_zero"], False, 2)))
        line = "The direction of the stock was right in %d of %d weeks" % (f["direction_hits"], f["weeks"])
        if f["always_up_hits"] > f["direction_hits"]:
            line += "; always saying up would have been right %d times." % f["always_up_hits"]
        else:
            line += " (always saying up: %d); a coin gives p = %.2f." % (f["always_up_hits"],
                                                                        f["direction_p_value_vs_coin"])
        out.append(line)
    out.append("With this few weeks and trades, luck and skill cannot be told apart. This is practice, not proof.")
    return out


# ---------- drawing ----------

def nice_ticks(lo, hi, n=5):
    if hi - lo < 1e-9:
        hi = lo + 0.01
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.5:
        ticks.append(round(v, 10))
        v += step
    return ticks


def _bar_path(x, y0, w, y1, r):
    """A bar from the baseline y0 to y1 with a rounded data end and a square baseline."""
    r = min(r, w / 2.0, abs(y1 - y0))
    if abs(y1 - y0) < 0.5:
        return ""
    s = 1 if y1 < y0 else -1          # s=1: bar goes up (smaller y)
    return ("M%.2f,%.2f V%.2f a%.2f,%.2f 0 0 %d %.2f,%.2f H%.2f a%.2f,%.2f 0 0 %d %.2f,%.2f V%.2f Z" % (
        x, y0, y1 + s * r, r, r, 1 if s == 1 else 0, r, -s * r, x + w - r, r, r, 1 if s == 1 else 0, r, s * r, y0))


W, H = 720, 300
ML, MR, MT, MB = 56, 120, 14, 30


def line_chart(d):
    n = len(d["weeks"])
    a, b = d["cum_rules"], d["cum_stock"]
    lo, hi = min(0.0, min(a), min(b)), max(0.0, max(a), max(b))
    ticks = nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    pw, ph = W - ML - MR, H - MT - MB

    def X(i):
        return ML + (pw * i / (n - 1) if n > 1 else pw / 2)

    def Y(v):
        return MT + ph * (1 - (v - lo) / (hi - lo))

    out = ['<svg class="chart" id="cum" viewBox="0 0 %d %d" role="img" aria-label="Cumulative return of the rules and '
           'of holding the stock, by week">' % (W, H)]
    for t in ticks:
        out.append('<line class="grid" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (ML, W - MR, Y(t), Y(t)))
        out.append('<text class="tick" x="%d" y="%.1f" text-anchor="end">%s</text>' % (ML - 8, Y(t) + 4, pct(t, False, 0)))
    out.append('<line class="zero" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (ML, W - MR, Y(0), Y(0)))
    first = d["weeks"][0]["week"]
    step = 10 if n > 25 else 5 if n > 12 else 1
    for i, w in enumerate(d["weeks"]):
        if (w["week"] - first) % step == 0:
            out.append('<text class="tick" x="%.1f" y="%d" text-anchor="middle">%d</text>' % (X(i), H - 10, w["week"]))
    for series, cls in ((b, "s-stock"), (a, "s-rules")):
        pts = " ".join("%.1f,%.1f" % (X(i), Y(v)) for i, v in enumerate(series))
        out.append('<polyline class="line %s" points="%s"/>' % (cls, pts))
    for series, cls, name in ((b, "stock", "Holding the stock"), (a, "rules", "My rules, after costs")):
        x, y = X(n - 1), Y(series[-1])
        out.append('<circle class="dot d-%s" cx="%.1f" cy="%.1f" r="4"/>' % (cls, x, y))
    ya, yb = Y(a[-1]), Y(b[-1])
    if abs(ya - yb) < 14:
        mid = (ya + yb) / 2
        ya, yb = (mid - 8, mid + 8) if ya <= yb else (mid + 8, mid - 8)
    out.append('<text class="lbl" x="%.1f" y="%.1f">Rules %s</text>' % (X(n - 1) + 10, ya + 4, pct(a[-1])))
    out.append('<text class="lbl" x="%.1f" y="%.1f">Stock %s</text>' % (X(n - 1) + 10, yb + 4, pct(b[-1])))
    out.append('<line id="cross" class="cross" x1="0" x2="0" y1="%d" y2="%d" style="display:none"/>' % (MT, H - MB))
    out.append('<rect id="hit" x="%d" y="%d" width="%d" height="%d" fill="transparent"/>' % (ML, MT, pw, ph))
    out.append("</svg>")
    return "".join(out)


def bar_chart(d):
    rows = d["weeks"]
    n = len(rows)
    vals = [w["strategy_return"] for w in rows]
    lo, hi = min(0.0, min(vals)), max(0.0, max(vals))
    ticks = nice_ticks(lo, hi, 4)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    h = 230
    pw, ph = W - ML - 16, h - MT - MB
    slot = pw / n
    bw = max(2.0, min(24.0, slot - 2.0))

    def Y(v):
        return MT + ph * (1 - (v - lo) / (hi - lo))

    out = ['<svg class="chart" viewBox="0 0 %d %d" role="img" aria-label="Result of the rules in each week">' % (W, h)]
    for t in ticks:
        out.append('<line class="grid" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (ML, W - 16, Y(t), Y(t)))
        out.append('<text class="tick" x="%d" y="%.1f" text-anchor="end">%s</text>' % (ML - 8, Y(t) + 4, pct(t, False, 0)))
    out.append('<line class="zero" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>' % (ML, W - 16, Y(0), Y(0)))
    first = rows[0]["week"]
    for i, w in enumerate(rows):
        x = ML + slot * i + (slot - bw) / 2
        tip = "Week %d: %s, rules %s, stock %s" % (w["week"], w["strategy"], pct(w["strategy_return"]),
                                                  pct(w["stock_move"]))
        cls = {"trend": "b-trend", "vwap": "b-vwap"}.get(w["strategy"], "b-other")
        path = _bar_path(x, Y(0), bw, Y(w["strategy_return"]), 4)
        if path:
            out.append('<path class="%s" d="%s"><title>%s</title></path>' % (cls, path, html.escape(tip)))
        else:
            out.append('<rect class="b-none" x="%.1f" y="%.1f" width="%.1f" height="1.5"><title>%s</title></rect>' % (
                x, Y(0) - 0.75, bw, html.escape(tip)))
        if (w["week"] - first) % 10 == 0:
            out.append('<text class="tick" x="%.1f" y="%d" text-anchor="middle">%d</text>' % (x + bw / 2, h - 10, w["week"]))
    out.append("</svg>")
    return "".join(out)


CSS = """
:root{--bg:#fcfcfb;--panel:#f4f4f1;--ink:#0b0b0b;--ink2:#52514e;--muted:#7a7974;--rule:#e3e2dd;
--c-stock:#2a78d6;--c-rules:#eb6834;--c-trend:#1baf7a;--c-vwap:#eda100;color-scheme:light}
@media (prefers-color-scheme: dark){:root:where(:not([data-theme="light"])){--bg:#1a1a19;--panel:#232322;
--ink:#f0efec;--ink2:#c3c2b7;--muted:#97968d;--rule:#34332f;--c-stock:#3987e5;--c-rules:#d95926;
--c-trend:#199e70;--c-vwap:#c98500;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#1a1a19;--panel:#232322;--ink:#f0efec;--ink2:#c3c2b7;--muted:#97968d;--rule:#34332f;
--c-stock:#3987e5;--c-rules:#d95926;--c-trend:#199e70;--c-vwap:#c98500;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem;margin:0 0 4px}h2{font-size:1.15rem;margin:36px 0 8px}
p,li{color:var(--ink2)}.sub{color:var(--muted);margin:0 0 16px}
.banner{background:var(--panel);border-left:4px solid var(--c-rules);padding:10px 14px;margin:12px 0;color:var(--ink)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0}
.tile{background:var(--panel);padding:12px 14px;border-radius:8px}
.tile b{display:block;font-size:1.7rem;color:var(--ink);line-height:1.2}.tile span{color:var(--muted);font-size:.85rem}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.legend{display:flex;flex-wrap:wrap;gap:16px;margin:8px 0;color:var(--ink2);font-size:.9rem}
svg.chart{width:100%;height:auto;display:block}
.grid{stroke:var(--rule);stroke-width:1}.zero{stroke:var(--muted);stroke-width:1}.cross{stroke:var(--muted);stroke-width:1}
.tick{fill:var(--muted);font-size:11px}.lbl{fill:var(--ink);font-size:12px}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.s-stock{stroke:var(--c-stock)}.s-rules{stroke:var(--c-rules)}
.dot{stroke:var(--bg);stroke-width:2}.d-stock{fill:var(--c-stock)}.d-rules{fill:var(--c-rules)}
.b-trend{fill:var(--c-trend)}.b-vwap{fill:var(--c-vwap)}.b-other{fill:var(--muted)}.b-none{fill:var(--muted)}
#tip{position:fixed;pointer-events:none;background:var(--panel);color:var(--ink);border:1px solid var(--rule);
padding:6px 9px;border-radius:6px;font-size:.85rem;display:none;white-space:nowrap}
table{border-collapse:collapse;width:100%;font-size:.88rem;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;border-bottom:1px solid var(--rule);text-align:right}
th{color:var(--ink);font-weight:600;position:sticky;top:0;background:var(--bg)}
td:first-child,th:first-child,td.l,th.l{text-align:left}td.note{text-align:left;color:var(--ink2);min-width:200px}
.scroll{overflow-x:auto}ul{padding-left:20px}
@media (max-width:560px){.tick{font-size:17px}.lbl{font-size:17px}}
"""

JS = """
(function(){try{
var svg=document.getElementById('cum');if(!svg)return;
var data=JSON.parse(document.getElementById('cumdata').textContent);
var hit=document.getElementById('hit'),cross=document.getElementById('cross'),tip=document.getElementById('tip');
var vb=svg.viewBox.baseVal;
hit.addEventListener('mousemove',function(e){
 var r=svg.getBoundingClientRect(),x=(e.clientX-r.left)*vb.width/r.width;
 var n=data.weeks.length,i=Math.round((x-data.ml)/(data.pw/(n-1)));i=Math.max(0,Math.min(n-1,i));
 var cx=data.ml+data.pw*i/(n-1);cross.setAttribute('x1',cx);cross.setAttribute('x2',cx);cross.style.display='';
 tip.style.display='block';tip.style.left=(e.clientX+14)+'px';tip.style.top=(e.clientY+10)+'px';
 tip.textContent='Week '+data.weeks[i]+': rules '+data.rules[i]+', stock '+data.stock[i];});
hit.addEventListener('mouseleave',function(){cross.style.display='none';tip.style.display='none';});
}catch(err){}})();
"""


def _row(w):
    rule = rule_label(w["strategy"], w["params"])
    return ("<tr><td>%d</td><td class=\"l\">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td class=\"note\">%s</td></tr>" % (
                w["week"], html.escape(rule), pct(w["expected_return"], True, 2), pct(w["strategy_return"], True, 2),
                pct(w["error"], True, 2), pct(w["stock_move"]), "yes" if w["direction_hit"] else "no",
                html.escape(w["note"])))


def render(d, label="", cost_note=""):
    n = len(d["weeks"])
    f = d["forecast"]
    cum = {"weeks": [w["week"] for w in d["weeks"]], "rules": [pct(v) for v in d["cum_rules"]],
           "stock": [pct(v) for v in d["cum_stock"]], "ml": ML, "pw": W - ML - MR}
    costs = d["costs"]
    parts = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
             "<title>Replay report</title><style>%s</style></head><body><main>" % CSS,
             "<h1>Replay report</h1>",
             "<p class=\"sub\">%d weeks played one at a time, stock and dates hidden. %d of %d open weeks scored; %d "
             "sealed weeks untouched.</p>" % (n, d["revealed_through"] - d["first_week"] + 1, d["weeks_open"] - 1,
                                              d["weeks_sealed"])]
    if label:
        parts.append("<div class=\"banner\">%s</div>" % html.escape(label))
    parts.append("<div class=\"tiles\"><div class=\"tile\"><b>%s</b><span><i class=\"sw\" style=\"background:var(--c-rules)\">"
                 "</i>My rules, after costs</span></div><div class=\"tile\"><b>%s</b><span><i class=\"sw\" "
                 "style=\"background:var(--c-stock)\"></i>Holding the stock</span></div><div class=\"tile\"><b>0.0%%</b>"
                 "<span>Staying out of the market</span></div></div>" % (pct(d["rules_total"]), pct(d["stock_total"])))
    parts.append("<h2>What it says</h2><ul>%s</ul>" % "".join("<li>%s</li>" % html.escape(s) for s in verdict(d)))
    parts.append("<h2>Cumulative result</h2><div class=\"legend\"><span><i class=\"sw\" style=\"background:var(--c-rules)\">"
                 "</i>My rules, after costs</span><span><i class=\"sw\" style=\"background:var(--c-stock)\"></i>Holding "
                 "the stock</span></div>%s<div id=\"tip\"></div><script type=\"application/json\" id=\"cumdata\">%s</script>"
                 "<p class=\"sub\">Both start at the same moment, the beginning of the first scored week. Holding is the "
                 "true move of the stock including overnight gaps.</p>" % (line_chart(d), json.dumps(cum)))
    parts.append("<h2>Each week</h2><div class=\"legend\"><span><i class=\"sw\" style=\"background:var(--c-trend)\"></i>"
                 "Trend rule</span><span><i class=\"sw\" style=\"background:var(--c-vwap)\"></i>VWAP rule</span><span>"
                 "Grey tick: stayed out</span></div>%s" % bar_chart(d))
    on, intra = d["overnight_log"], d["intraday_log"]
    parts.append("<h2>When the stock moved</h2><p>Over the scored stretch the overnight gaps added up to %s and the moves "
                 "inside the trading days to %s (log returns, %d days). A rule that closes every evening only ever earns "
                 "the second part.</p>" % (pct(math.exp(on) - 1.0), pct(math.exp(intra) - 1.0), d["days"]))
    parts.append("<h2>The assumptions behind the choices</h2><div class=\"scroll\"><table><tr><th class=\"l\">Assumption</th>"
                 "<th>Weeks</th><th>Rules</th><th>Stock</th><th>Direction right</th></tr>")
    for a in d["assumptions"]:
        parts.append("<tr><td class=\"l\">%s</td><td>%d</td><td>%s</td><td>%s</td><td>%d of %d</td></tr>" % (
            html.escape(a["first_note"][:110]), a["weeks"], pct(a["rules_return"]), pct(a["stock_return"]),
            a["direction_hits"], a["weeks"]))
    parts.append("</table></div><p class=\"sub\">The letter in each note names the assumption; the text is its first note. "
                 "Rules and Stock are compounded over the weeks that assumption was used.</p>")
    parts.append("<h2>Rule by rule</h2><div class=\"scroll\"><table><tr><th class=\"l\">Rule</th><th>Weeks</th>"
                 "<th>Total</th><th>Per week</th><th>Winning weeks</th><th>Trades</th><th>Costs paid</th></tr>")
    for r in d["rules"]:
        parts.append("<tr><td class=\"l\">%s</td><td>%d</td><td>%s</td><td>%s</td><td>%d</td><td>%d</td><td>%.1f</td></tr>" % (
            html.escape(r["label"]), r["weeks"], pct(r["total_return"]), pct(r["average_week"], True, 2),
            r["winning_weeks"], r["trades"], r["costs_paid"]))
    parts.append("</table></div><p class=\"sub\">Costs are in currency units on a 10,000 account.</p>")
    parts.append("<h2>What costs do to this result</h2><p>The same rules and the same weeks, re-scored with different "
                 "costs (the choices were not changed).</p><div class=\"scroll\"><table><tr><th class=\"l\">Costs</th>"
                 "<th>Per trade side</th><th>Rules, total</th></tr>")
    for x in d["sensitivity"]:
        parts.append("<tr><td class=\"l\">%s</td><td>%.1f bps</td><td>%s</td></tr>" % (
            html.escape(x["label"]), x["per_side_bps"], pct(x["total_return"])))
    parts.append("</table></div>")
    if cost_note:
        parts.append("<p>%s</p>" % html.escape(cost_note))
    if f.get("weeks"):
        parts.append("<h2>Were the forecasts any good?</h2><div class=\"tiles\"><div class=\"tile\"><b>%s</b><span>Average "
                     "miss of the return forecast</span></div><div class=\"tile\"><b>%s</b><span>Average miss if always "
                     "guessing zero</span></div><div class=\"tile\"><b>%d / %d</b><span>Direction right (always up: %d)"
                     "</span></div></div>" % (pct(f["mean_abs_error"], False, 2), pct(f["mean_abs_error_if_always_zero"], False, 2),
                                              f["direction_hits"], f["weeks"], f["always_up_hits"]))
    parts.append("<h2>Limits</h2><ul><li>Costs used: commission %.1f bps, spread %.1f bps, slippage %.1f bps, fixed fee %.2f. "
                 "Confirm them against the broker's price list before trusting any number.</li>"
                 "<li>%d rules were tried and the assumptions were revised during the run; every rule and revision "
                 "counts as a trial.</li><li>Whoever or whatever wrote the predictions may remember the real period "
                 "despite the hidden names and dates.</li><li>The sealed weeks were not used. They stay for one final "
                 "test per rule, so this run should not be repeated on the same data by someone who has now seen the "
                 "open weeks.</li><li>The stock's name, the dates and the source file are not shown.</li></ul>" % (
                     costs["commission_bps"], costs["spread_bps"], costs["slippage_bps"], costs["fixed_fee"],
                     d["rules_tried"]))
    parts.append("<h2>All weeks</h2><div class=\"scroll\"><table><tr><th>Week</th><th class=\"l\">Rule</th><th>Expected</th>"
                 "<th>Result</th><th>Miss</th><th>Stock</th><th>Direction</th><th class=\"l\">Note</th></tr>")
    parts.extend(_row(w) for w in d["weeks"])
    parts.append("</table></div><script>%s</script></main></body></html>" % JS)
    return "".join(parts)


def write(folder, out, label="", cost_note=""):
    page = render(build(folder), label=label, cost_note=cost_note)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Write an HTML report for a replay folder.")
    p.add_argument("--dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--cost-note", default="", help="plain text shown under the cost table, e.g. what the broker charges")
    a = p.parse_args(argv)
    try:
        write(a.dir, a.out, a.label, a.cost_note)
    except ValueError as e:
        print("Refused: %s" % e, file=sys.stderr)
        return 2
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
