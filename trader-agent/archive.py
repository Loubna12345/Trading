#!/usr/bin/env python3
"""Keep every report: numbered per kind, never overwritten, with an overview of all runs. Standard library only.

    rules-run-01.html, rules-run-02.html, ...   a hand-played rules replay (trader-replay)
    agent-run-01.html, agent-run-02.html, ...   an agent replay (weekly.py)
    comparison-01.html, ...                     an agent run against a rules run
    index.html                                  every saved run with its result in percent and in money

Each save appends a summary to `<reports>/ledger.jsonl` (hash-chained) with the run folder's ledger hash at that
moment, so a report can always be traced to the exact state it was made from. A report file is created only if
no file of that name exists; the same run state is never saved twice. index.html is the only file rewritten: it
is rebuilt from the ledger after every save.

    python3 archive.py rules   --dir REPLAY --reports R --start-money 500 [--label TEXT]
    python3 archive.py agent   --dir AGENT  --reports R --start-money 500 [--label TEXT]
    python3 archive.py compare --dir AGENT --rules-dir REPLAY --reports R --start-money 500 [--label TEXT]
    python3 archive.py index   --reports R
"""

import argparse
import html
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


weekly = _load(os.path.join(HERE, "weekly.py"), "trader_weekly_archive")
report = weekly.report
replay = weekly.replay
T = weekly.T

PREFIX = {"rules": "rules-run", "agent": "agent-run", "comparison": "comparison"}
TITLE = {"rules": "Rules run", "agent": "Agent run", "comparison": "Comparison"}


def _state(folder):
    """The run folder's name and the hash of its last ledger entry: the exact state a report is made from."""
    entries = T.ledger_read(folder)
    T.ledger_verify(folder)
    return os.path.basename(os.path.normpath(folder)), entries[-1]["hash"] if entries else None


def _saved(reports):
    return T.ledger_read(reports) if os.path.exists(T.ledger_path(reports)) else []


def _save(reports, kind, page, summary):
    saved = _saved(reports)
    if saved:
        T.ledger_verify(reports)
    for e in saved:
        if e["kind"] == kind and e.get("state") == summary.get("state"):
            raise ValueError("this run, in this exact state, is already saved as %s" % e["file"])
    n = 1 + max([e["number"] for e in saved if e["kind"] == kind] or [0])
    name = "%s-%02d.html" % (PREFIX[kind], n)
    os.makedirs(reports, exist_ok=True)
    path = os.path.join(reports, name)
    try:
        with open(path, "x", encoding="utf-8") as fh:      # "x": fails if the file exists, so nothing is overwritten
            fh.write(page)
    except FileExistsError:
        raise ValueError("%s already exists; it is never overwritten (move it away if it is not a saved report)"
                         % name)
    entry = dict(summary, kind=kind, number=n, file=name, saved=time.strftime("%Y-%m-%d %H:%M"))
    e = T.ledger_append(reports, entry)
    write_index(reports)
    return e


def save_rules(reports, folder, start_money, label=""):
    d = report.build(folder)
    m = report.money(folder, d, start_money)
    src, state = _state(folder)
    page = report.render(d, label=label, start_money=m)
    f = d["forecast"]
    return _save(reports, "rules", page, {
        "source": src, "state": state, "label": label, "who": "Claude by hand, rules from the library",
        "weeks": [d["weeks"][0]["week"], d["weeks"][-1]["week"]], "n_weeks": len(d["weeks"]),
        "total_return": d["rules_total"], "stock_return": d["stock_total"], "start_money": m["start"],
        "end_money": m["rules_end"], "costs_money": m["rules_costs"], "stock_end_money": m["stock_end"],
        "direction_hits": f.get("direction_hits"), "rules_tried": d["rules_tried"]})


def save_agent(reports, folder, start_money, label=""):
    d = weekly.build(folder)
    m = weekly.agent_money(folder, d, start_money)
    src, state = _state(folder)
    page = weekly.render_agent(d, label=label, start_money=m)
    rows = d["rows"]
    return _save(reports, "agent", page, {
        "source": src, "state": state, "label": label, "who": "agent %s" % d["lock"]["model"],
        "model": d["lock"]["model"], "notebook_from": d["lock"].get("notebook_start_from"),
        "weeks": [rows[0]["week"], rows[-1]["week"]], "n_weeks": d["n"], "total_return": d["agent_total"],
        "stock_return": d["stock_total"], "start_money": m["start"], "end_money": m["agent_end"],
        "costs_money": m["agent_costs"], "stock_end_money": m["stock_end"],
        "direction_hits": d["forecast"]["direction_hits"], "api_spent": d["spent"]})


def save_comparison(reports, agent_folder, rules_folder, start_money, label=""):
    c = weekly.compare_build(agent_folder, rules_folder, start_money=start_money)
    a_src, a_state = _state(agent_folder)
    r_src, r_state = _state(rules_folder)
    saved = _saved(reports)
    a_run = next((e["number"] for e in saved if e["kind"] == "agent" and e["state"] == a_state), None)
    r_run = next((e["number"] for e in saved if e["kind"] == "rules" and e["state"] == r_state), None)
    page = weekly.render_compare(c, label=label)
    m = c["money"]
    return _save(reports, "comparison", page, {
        "source": "%s vs %s" % (a_src, r_src), "state": a_state + ":" + r_state, "label": label,
        "who": "agent run %s vs rules run %s" % (a_run or "(not saved)", r_run or "(not saved)"),
        "agent_run": a_run, "rules_run": r_run, "weeks": [c["rows"][0]["week"], c["rows"][-1]["week"]],
        "n_weeks": c["n"], "total_return": c["agent_total"], "rules_return": c["rules_total"],
        "stock_return": c["stock_total"], "start_money": m["start"], "end_money": m["agent_end"],
        "rules_end_money": m["rules_end"], "stock_end_money": m["stock_end"],
        "verdicts": [{"against": t["against"], "verdict": t["verdict"]} for t in c["tests"]]})


# ---------- the overview ----------

def write_index(reports):
    saved = _saved(reports)
    eur, pct = report.eur, report.pct
    p = [weekly._head("Training runs"), "<h1>Training runs</h1>",
         "<p class=\"sub\">Every saved report, oldest first. Files are numbered per kind and never overwritten. "
         "Money is after costs, from the starting amount shown.</p>"]
    for kind in ("rules", "agent", "comparison"):
        rows = [e for e in saved if e["kind"] == kind]
        if not rows:
            continue
        p.append("<h2>%ss</h2><div class=\"scroll\"><table><tr><th class=\"l\">Report</th><th class=\"l\">Saved</th>"
                 "<th class=\"l\">Who decided</th><th>Weeks</th><th>Result</th><th>Money</th><th>Stock</th>"
                 "<th class=\"l\">%s</th></tr>" % (TITLE[kind], "Verdict" if kind == "comparison" else "Label"))
        for e in rows:
            if kind == "comparison":
                extra = "; ".join("vs %s: %s" % (v["against"], v["verdict"]) for v in e["verdicts"])
                money = "agent %s, rules %s" % (eur(e["end_money"]), eur(e["rules_end_money"]))
                result = "agent %s, rules %s" % (pct(e["total_return"]), pct(e["rules_return"]))
            else:
                extra = e.get("label") or ""
                money = "%s → %s" % (eur(e["start_money"]), eur(e["end_money"]))
                result = pct(e["total_return"])
            p.append("<tr><td class=\"l\"><a href=\"%s\">%s %d</a></td><td class=\"l\">%s</td><td class=\"l\">%s</td>"
                     "<td>%d–%d</td><td>%s</td><td>%s</td><td>%s</td><td class=\"note\">%s</td></tr>" % (
                         html.escape(e["file"]), TITLE[kind], e["number"], html.escape(e["saved"]),
                         html.escape(e["who"]), e["weeks"][0], e["weeks"][1], html.escape(result), html.escape(money),
                         pct(e["stock_return"]), html.escape(extra)))
        p.append("</table></div>")
    agents = [e for e in saved if e["kind"] == "agent"]
    if len(agents) > 1:
        p.append("<h2>Is the agent getting better?</h2><p>Agent runs in order: %s. A rising line over runs on the "
                 "SAME weeks means little (the notebook may have learned those weeks); it counts on new stocks or new "
                 "weeks.</p>" % ", ".join("run %d %s" % (e["number"], pct(e["total_return"])) for e in agents))
    p.append("</main></body></html>")
    os.makedirs(reports, exist_ok=True)
    path = os.path.join(reports, "index.html")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("".join(p))
    os.replace(tmp, path)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="Save numbered reports that are never overwritten.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("rules", "agent", "compare"):
        x = sub.add_parser(name)
        x.add_argument("--dir", required=True)
        if name == "compare":
            x.add_argument("--rules-dir", required=True)
        x.add_argument("--reports", required=True)
        x.add_argument("--start-money", type=float, required=True, help="starting amount in euro, e.g. 500")
        x.add_argument("--label", default="")
    x = sub.add_parser("index")
    x.add_argument("--reports", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "rules":
            e = save_rules(a.reports, a.dir, a.start_money, a.label)
        elif a.cmd == "agent":
            e = save_agent(a.reports, a.dir, a.start_money, a.label)
        elif a.cmd == "compare":
            e = save_comparison(a.reports, a.dir, a.rules_dir, a.start_money, a.label)
        else:
            print(write_index(a.reports))
            return 0
    except (ValueError, OSError) as err:
        print("Refused: %s" % err, file=sys.stderr)
        return 2
    print(json.dumps({k: e[k] for k in ("file", "kind", "number", "end_money", "total_return")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
