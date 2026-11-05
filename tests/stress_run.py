"""Slow stress test of the validator on 14 made-up scenarios (about 30 seconds per seed). Not part of the unit
tests. Run: python3 tests/stress_run.py 12
"""
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load
import stress_scenarios as scen
v = load("trader-validate", "validate"); e = load("trader-backtest", "engine")
costs = e.make_costs(commission_bps=0, spread_bps=2, slippage_bps=1, fixed_fee=0)
GRIDS = {"trend": {"window": [3, 6, 12]}, "orb": {"range_bars": [2, 4, 8]}, "vwap": {"band_bps": [10, 25, 50], "warmup": [6]}}
SEEDS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
out = {}
t0 = time.time()
for sname, kw in scen.SCEN.items():
    for strat, grid in GRIDS.items():
        c = collections.Counter(); crashes = 0; nets = []; bh = []
        for seed in range(SEEDS):
            bars = scen.build(seed=seed, **kw)
            try:
                r = v.validate(bars, strat, grid, costs, 252*34, flatten_eod=True, train_days=30, test_days=10)
                c[r["verdict"]] += 1; nets.append(r["best_net_return"]); bh.append(r["benchmark"]["total_return"])
            except Exception as ex:
                crashes += 1; c["CRASH:"+type(ex).__name__+":"+str(ex)[:50]] += 1
        out[(sname, strat)] = (dict(c), sum(nets)/len(nets) if nets else None, sum(bh)/len(bh) if bh else None)
        print("%-30s %-6s %s  mean best net %s  mean buy&hold %s" % (sname, strat, dict(c),
              None if not nets else round(sum(nets)/len(nets), 3), None if not bh else round(sum(bh)/len(bh), 3)), flush=True)
print("seconds", round(time.time()-t0))
