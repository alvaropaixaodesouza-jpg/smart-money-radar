"""Decimal cash arithmetic; percentages are percentage points, not fractions."""
from decimal import Decimal, InvalidOperation
from statistics import median


def dec(value):
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ValueError("invalid finite number") from e
    if not n.is_finite():
        raise ValueError("invalid finite number")
    return n


def positive(value):
    n = dec(value)
    if n <= 0:
        raise ValueError("number must be positive")
    return n


def pct(end, start):
    return (dec(end) / positive(start) - 1) * 100


def position_size(capital, risk_pct, entry, stop, fee_bps=10, slip_bps=20,
                  liquidity=None, max_liquidity_pct=1):
    capital, entry, stop = map(positive, (capital, entry, stop))
    risk = positive(risk_pct) / 100
    fee, slip = dec(fee_bps) / 10000, dec(slip_bps) / 10000
    if not 0 < risk <= 1 or not 0 <= fee < 1 or not 0 <= slip < 1 or stop >= entry:
        raise ValueError("invalid risk, costs or long stop")
    execution = entry * (1 + slip)
    unit_cost = execution * (1 + fee)
    unit_loss = unit_cost - stop * (1 - slip) * (1 - fee)
    budget = capital * risk
    cash_cap = capital
    if liquidity is not None:
        cap = positive(max_liquidity_pct) / 100
        if cap > 1:
            raise ValueError("invalid liquidity cap")
        cash_cap = min(cash_cap, positive(liquidity) * cap)
    qty = min(budget / unit_loss, cash_cap / unit_cost)
    return {"quantity": str(qty), "entry_execution": str(execution),
            "cash_required": str(qty * unit_cost), "risk_budget": str(budget),
            "planned_loss": str(qty * unit_loss), "loss_is_guaranteed": False}


def path_metrics(entry, start, points):
    """Observed extrema only. Sampling cannot reconstruct an unobserved path."""
    entry = positive(entry)
    ordered = [(start, entry)] + [(int(t), positive(p)) for t, p in points]
    if any(b[0] < a[0] for a, b in zip(ordered, ordered[1:])):
        raise ValueError("path must be chronological")
    high = max(ordered, key=lambda x: x[1])
    low = min(ordered, key=lambda x: x[1])
    peak, dd = entry, dec(0)
    for _, p in ordered:
        peak = max(peak, p)
        dd = max(dd, (peak - p) / peak * 100)
    return {"max_price": str(high[1]), "min_price": str(low[1]),
            "mfe_pct": str(pct(high[1], entry)), "mae_pct": str(pct(low[1], entry)),
            "max_drawdown_pct": str(dd), "time_to_max_s": high[0] - start,
            "time_to_min_s": low[0] - start, "samples": len(points),
            "max_gap_s": max((b[0] - a[0] for a, b in zip(ordered, ordered[1:])), default=0),
            "extrema_evidence": "observed_samples"}


def performance(results):
    closed = sorted((r for r in results if r["state"] == "closed"),
                    key=lambda r: (r["closed_at"], r.get("signal_id", 0)))
    pnls = [dec(r["pnl"]) for r in closed]
    returns = [dec(r["return_pct"]) for r in closed]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins, dec(0)), -sum(losses, dec(0))
    cw = cl = mw = ml = 0
    for p in pnls:
        cw, cl = (cw + 1 if p > 0 else 0), (cl + 1 if p < 0 else 0)
        mw, ml = max(mw, cw), max(ml, cl)
    def avg(xs):
        return str(sum(xs) / len(xs)) if xs else None
    return {"closed": len(closed), "wins": len(wins), "losses": len(losses),
            "breakeven": len(pnls) - len(wins) - len(losses),
            "win_rate_pct": str(dec(len(wins)) / len(pnls) * 100) if pnls else None,
            "net_pnl": str(sum(pnls, dec(0))), "mean_win": avg(wins), "mean_loss": avg(losses),
            "expectancy_cash": avg(pnls), "mean_return_pct": avg(returns),
            "median_return_pct": str(median(returns)) if returns else None,
            "profit_factor": str(gross_win / gross_loss) if gross_loss else None,
            "profit_factor_status": "measured" if gross_loss else "no_losses_or_no_trades",
            "mean_realized_r": avg([dec(r["realized_r"]) for r in closed]),
            "max_win_streak": mw, "max_loss_streak": ml,
            "portfolio_drawdown": None, "capital_model": "isolated_per_signal"}
