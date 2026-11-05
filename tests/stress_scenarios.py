"""Made-up price series for stress-testing the validator: no edge, a market drift without skill, a bad tick,
real edges, an edge that fades, and a tiny edge. Synthetic by construction."""
import random, math

def mk(day, i, o, h, l, c, v):
    minutes = 8*60 + 15*i   # 15-minute bars, 8:00Z start
    return {"t": "%sT%02d:%02d:00Z" % (day, minutes//60, minutes%60), "o": o, "h": h, "l": l, "c": c, "v": v}

def build(ret_fn, seed, n_days=80, per_day=34, gap_fn=None, vol_fn=None, glitch=False):
    rnd = random.Random(seed)
    bars, price = [], 100.0
    state = {"prev": 0.0, "vol": 0.002, "day_open_ret": 0.0, "cum": 0.0}
    for d in range(n_days):
        day = "2026-%02d-%02d" % (1 + d // 28, 1 + d % 28)
        if gap_fn:
            price *= 1 + gap_fn(rnd)
        state["cum"] = 0.0
        for i in range(per_day):
            sig = state["vol"] if vol_fn is None else vol_fn(rnd, state, i, per_day)
            r = ret_fn(rnd, state, i, d, n_days, sig)
            state["prev"] = r
            state["cum"] += r
            o = price; c = o * (1 + r)
            h = max(o, c) * (1 + abs(rnd.gauss(0, sig/3))); l = min(o, c) * (1 - abs(rnd.gauss(0, sig/3)))
            v = 500 + rnd.random()*500
            bars.append(mk(day, i, o, h, l, c, v))
            price = c
    if glitch:
        k = len(bars)//2
        b = bars[k]; b["c"] = b["c"]*3; b["h"] = b["c"]
    return bars

def gauss(sig0=0.002, drift=0.0):
    return lambda rnd, st, i, d, nd, sig: drift + rnd.gauss(0, sig)

def student(df=3, scale=0.0015, drift=0.0):
    def f(rnd, st, i, d, nd, sig):
        z = rnd.gauss(0,1); chi = sum(rnd.gauss(0,1)**2 for _ in range(df)); t = z/math.sqrt(chi/df)
        return drift + scale*t
    return f

def garch():
    def f(rnd, st, i, d, nd, sig):
        r = rnd.gauss(0, st["vol"]); st["vol"] = math.sqrt(0.000001 + 0.88*st["vol"]**2 + 0.1*r*r); return r
    return f

def ar(phi, drift=0.0):
    return lambda rnd, st, i, d, nd, sig: drift + phi*st["prev"] + rnd.gauss(0, sig)

def ar_decay(phi):
    return lambda rnd, st, i, d, nd, sig: (phi if d < nd//2 else 0.0)*st["prev"] + rnd.gauss(0, sig)

def regime(phi=0.4):
    # randomly flips between momentum and reversion every ~10 days, no net edge
    sign = {}
    def f(rnd, st, i, d, nd, sig):
        blk = d // 10
        if blk not in sign: sign[blk] = rnd.choice([1, -1])
        return sign[blk]*phi*st["prev"] + rnd.gauss(0, sig)
    return f

def open_drive(strength=0.0004):
    # first-hour move continues for the rest of the day
    def f(rnd, st, i, d, nd, sig):
        if i == 0: st["open_dir"] = 0
        base = rnd.gauss(0, sig)
        if i < 4:
            st["open_dir"] = st.get("open_dir", 0) + base
        elif i < 30:
            base += strength * (1 if st.get("open_dir", 0) > 0 else -1)
        return base
    return f

def ushape(rnd, st, i, per_day):
    x = (i + 0.5) / per_day
    return 0.0015 * (1 + 3 * (abs(x - 0.5) * 2) ** 3)

SCEN = {
 "null: gaussian rw": dict(ret_fn=gauss()),
 "null: fat tails t3": dict(ret_fn=student()),
 "null: vol clustering": dict(ret_fn=garch()),
 "null: big overnight gaps": dict(ret_fn=gauss(), gap_fn=lambda rnd: rnd.gauss(0, 0.01)),
 "null: U-shaped intraday vol": dict(ret_fn=gauss(), vol_fn=ushape),
 "null: random regimes": dict(ret_fn=regime()),
 "BETA: bull drift, no skill": dict(ret_fn=gauss(drift=0.0003)),
 "BETA: bear drift, no skill": dict(ret_fn=gauss(drift=-0.0003)),
 "null + one bad tick": dict(ret_fn=gauss(), glitch=True),
 "EDGE: momentum phi .35": dict(ret_fn=ar(0.35)),
 "EDGE: mean reversion phi -.35": dict(ret_fn=ar(-0.35)),
 "EDGE: open drive": dict(ret_fn=open_drive()),
 "EDGE: decays halfway": dict(ret_fn=ar_decay(0.35)),
 "EDGE small (phi .05)": dict(ret_fn=ar(0.05)),
}
