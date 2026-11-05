#!/usr/bin/env python3
"""Performance numbers for an equity curve and a list of trade results.

Standard library only. The risk-free rate is taken as zero and every function says so where it matters.
"""

import math

MIN_TRADES = 100  # a rule of thumb of this project, not a sourced threshold; see docs/RESEARCH.md


def _check_curve(equity, need=2):
    if len(equity) < need:
        raise ValueError("need at least %d equity points" % need)
    if not all(isinstance(e, (int, float)) and math.isfinite(e) for e in equity):
        raise ValueError("equity values must be finite numbers")
    if equity[0] <= 0:
        raise ValueError("starting equity must be positive")


def period_returns(equity):
    _check_curve(equity)
    out = []
    for i in range(len(equity) - 1):
        if equity[i] <= 0:
            out.append(-1.0)
        else:
            out.append(equity[i + 1] / equity[i] - 1.0)
    return out


def total_return(equity):
    _check_curve(equity)
    return equity[-1] / equity[0] - 1.0


def annualised_return(equity, periods_per_year):
    _check_curve(equity)
    if not periods_per_year or periods_per_year <= 0:
        raise ValueError("periods_per_year is required")
    if equity[-1] <= 0:
        return -1.0
    return (equity[-1] / equity[0]) ** (periods_per_year / (len(equity) - 1)) - 1.0


def max_drawdown(equity):
    """Largest fall from a peak, as a fraction (0.10 = 10%)."""
    _check_curve(equity)
    peak = equity[0]
    worst = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak > 0:
            worst = max(worst, (peak - e) / peak)
    return worst


def sharpe(equity, periods_per_year):
    """Mean over sample standard deviation of per-period returns, scaled to a year; risk-free rate zero.

    None when there are fewer than 2 returns or the returns do not vary."""
    if not periods_per_year or periods_per_year <= 0:
        raise ValueError("periods_per_year is required to annualise the Sharpe ratio")
    if len(equity) < 3:
        return None
    r = period_returns(equity)
    mean = sum(r) / len(r)
    var = sum((x - mean) ** 2 for x in r) / (len(r) - 1)
    if var <= 1e-18:
        return None
    return mean / math.sqrt(var) * math.sqrt(periods_per_year)


def trade_stats(pnls):
    n = len(pnls)
    if n == 0:
        return {"n_trades": 0, "win_rate": None, "avg_win": None, "avg_loss": None,
                "expectancy": None, "profit_factor": None}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "n_trades": n,
        "win_rate": len(wins) / n,
        "avg_win": gross_win / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "expectancy": sum(pnls) / n,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
    }


def summarize(equity, pnls, periods_per_year, min_trades=MIN_TRADES):
    out = {
        "total_return": total_return(equity),
        "annualised_return": annualised_return(equity, periods_per_year),
        "max_drawdown": max_drawdown(equity),
        "sharpe": sharpe(equity, periods_per_year),
        "risk_free_rate": 0.0,
    }
    out.update(trade_stats(list(pnls)))
    warnings = []
    if min_trades and out["n_trades"] < min_trades:
        warnings.append("Too few trades (%d, flag below %d): a result from this few trades is mostly luck."
                        % (out["n_trades"], min_trades))
    out["warnings"] = warnings
    return out
