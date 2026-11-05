#!/usr/bin/env python3
"""Rule-based day-trading strategies for the backtester. Standard library only.

Every strategy is a function `strategy(history, position) -> 0 or 1` that the backtester calls after each
bar closes. `history` holds the bars up to now and nothing later; `position` is 1 if long, else 0. These
strategies are long-only (no shorting: trader-risk blocks it) and use only the bars passed in.

Each entry in REGISTRY says why the idea might work, where it applies, and what its parameters mean. A rule
with no stated reason is not accepted. None of these is a claim that the rule makes money: the evidence says
most day-trading rules do not survive costs (docs/RESEARCH.md); the validation step (roadmap step 4) decides.

"Today" means the UTC calendar day in the bar's time. That is right for exchanges that open and close inside
one UTC day (Xetra, Euronext, the NYSE). For crypto, which never closes, a UTC day is just a convenient reset.
"""

import math


def _whole(name, x):
    if isinstance(x, bool) or not isinstance(x, int) or x < 1:
        raise ValueError("%s must be a whole number of at least 1, got %r" % (name, x))
    return x


def _nonneg(name, x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
        raise ValueError("%s must be a number of at least 0, got %r" % (name, x))
    return float(x)


def _day_start(hist):
    """Index of the first bar of the current day."""
    i = len(hist) - 1
    day = hist[i]["t"][:10]
    while i > 0 and hist[i - 1]["t"][:10] == day:
        i -= 1
    return i


def trend(window):
    """Long while the close is above the average close of the last `window` bars, otherwise flat."""
    window = _whole("window", window)

    def strategy(hist, pos):
        if len(hist) < window:
            return 0
        sma = sum(hist[j]["c"] for j in range(len(hist) - window, len(hist))) / window
        return 1 if hist[len(hist) - 1]["c"] > sma else 0

    return strategy


def orb(range_bars):
    """Opening-range breakout: the first `range_bars` bars of the day set a high and a low. Go long when a bar
    closes above the high; leave when one closes below the low; hold in between. Nothing is traded while the
    range forms."""
    range_bars = _whole("range_bars", range_bars)

    def strategy(hist, pos):
        i0 = _day_start(hist)
        k = len(hist) - i0
        if k <= range_bars:
            return 0
        hi = max(hist[j]["h"] for j in range(i0, i0 + range_bars))
        lo = min(hist[j]["l"] for j in range(i0, i0 + range_bars))
        c = hist[len(hist) - 1]["c"]
        if c > hi:
            return 1
        if c < lo:
            return 0
        return 1 if pos else 0

    return strategy


def vwap(band_bps, warmup):
    """Reversion to the day's volume-weighted average price: buy when the close is more than `band_bps` basis
    points below today's VWAP (after `warmup` bars, so the average means something), sell when it returns to
    the VWAP. Needs volume."""
    band = _nonneg("band_bps", band_bps)
    warmup = _whole("warmup", warmup)

    def strategy(hist, pos):
        i0 = _day_start(hist)
        if len(hist) - i0 < warmup:
            return 0
        pv = vol = 0.0
        for j in range(i0, len(hist)):
            b = hist[j]
            pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
            vol += b["v"]
        if vol <= 0:
            raise ValueError("VWAP needs volume, and today's bars have none")
        v = pv / vol
        c = hist[len(hist) - 1]["c"]
        if pos:
            return 0 if c >= v else 1
        return 1 if c < v * (1.0 - band / 1e4) else 0

    return strategy


REGISTRY = {
    "trend": {
        "factory": trend,
        "reason": "Prices that are above their recent average tend, on average, to keep drifting the same way "
                  "(trend following); staying out when below avoids some falls.",
        "asset_classes": ["stocks", "ETFs", "crypto"],
        "params": {"window": "number of bars in the average"},
    },
    "orb": {
        "factory": orb,
        "reason": "A close beyond the first range of the day may mean that one side of the market is in "
                  "control for the day (opening-range breakout; see the SPY study in docs/RESEARCH.md).",
        "asset_classes": ["stocks", "ETFs"],
        "params": {"range_bars": "bars at the start of the day that define the range"},
    },
    "vwap": {
        "factory": vwap,
        "reason": "Price that has dropped well below the day's volume-weighted average often drifts back "
                  "toward it (mean reversion); sellers who pushed it down may be exhausted.",
        "asset_classes": ["stocks", "ETFs", "crypto"],
        "params": {"band_bps": "how far below VWAP, in basis points, before buying",
                   "warmup": "bars needed today before trading"},
    },
}


def make(name, **params):
    if name not in REGISTRY:
        raise ValueError("unknown strategy %r; choose from %s" % (name, sorted(REGISTRY)))
    info = REGISTRY[name]
    unknown = sorted(set(params) - set(info["params"]))
    if unknown:
        raise ValueError("%s has no parameter(s) %s; it takes %s" % (name, unknown, sorted(info["params"])))
    missing = sorted(set(info["params"]) - set(params))
    if missing:
        raise ValueError("%s needs parameter(s) %s: no defaults are guessed" % (name, missing))
    return info["factory"](**params)
