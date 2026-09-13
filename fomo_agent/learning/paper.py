"""Isolated long-only paper positions. All fills are indicative, never executable orders."""
import json

from .math import dec, positive, pct, position_size, path_metrics
from .store import CONFIG, trajectory


def barrier_event(low, high, stop, target):
    low, high, stop, target = map(positive, (low, high, stop, target))
    if low > high or stop >= target:
        raise ValueError("invalid bar or barriers")
    if low <= stop and high >= target:
        return "ambiguous"
    if low <= stop:
        return "stop"
    if high >= target:
        return "target"
    return None


def simulate(conn, signal, now, config=None):
    cfg = dict(CONFIG["paper"] if config is None else config)
    targets = [(positive(t), positive(f)) for t, f in cfg["targets"]]
    if (not targets or sum(f for _, f in targets) != 1 or
            any(b[0] <= a[0] for a, b in zip(targets, targets[1:]))):
        raise ValueError("targets must increase and fractions sum to 1")
    for k in ("latency_s", "entry_timeout_s", "max_hold_s", "max_gap_s"):
        if cfg[k] <= 0:
            raise ValueError("timing must be positive")
    stop_fraction = positive(cfg["stop_pct"]) / 100
    if stop_fraction >= 1:
        raise ValueError("stop must be less than 100 percent")
    result = {"signal_id": signal["id"], "strategy": cfg["name"], "quote": signal["quote"],
              "state": "waiting", "execution_evidence": "indicative_samples", "fills": [],
              "capital_model": "isolated_per_signal"}
    features = json.loads(signal["features_json"])
    if (signal["entry_price"] is None or signal["score"] is None or signal["score"] < cfg["min_score"] or
            features.get("liquidity") is None or dec(features["liquidity"]) < cfg["min_liquidity"]):
        return dict(result, state="ineligible", reason="entry_score_or_liquidity")
    rows = trajectory(conn, signal, now)
    entry_rows = [r for r in rows if signal["detected_at"] + cfg["latency_s"] <= r["observed_at"] <=
                  signal["detected_at"] + cfg["entry_timeout_s"]]
    if not entry_rows:
        return dict(result, state="insufficient_data" if now > signal["detected_at"] + cfg["entry_timeout_s"] else "waiting",
                    reason="awaiting_executable_entry_sample")
    first = entry_rows[0]
    if first["received_at"] - first["observed_at"] > cfg["max_gap_s"]:
        return dict(result, state="insufficient_data", reason="late_entry_observation")
    stop = positive(signal["entry_price"]) * (1 - stop_fraction)
    if stop >= positive(first["price"]):
        return dict(result, state="ineligible", reason="entry_already_below_invalidation")
    size = position_size(cfg["capital"], cfg["risk_pct"], first["price"], stop,
                         cfg["fee_bps"], cfg["slip_bps"], features["liquidity"], cfg["max_liquidity_pct"])
    qty, entry, cost = map(dec, (size["quantity"], size["entry_execution"], size["cash_required"]))
    fee, slip = dec(cfg["fee_bps"]) / 10000, dec(cfg["slip_bps"]) / 10000
    levels = [(entry * (1 + t / 100), f) for t, f in targets]
    remaining, proceeds, next_target = qty, dec(0), 0
    started, previous = first["observed_at"], first["observed_at"]
    path = []
    result.update(state="open", opened_at=started, entry_price=str(entry), quantity=str(qty),
                  cash_required=str(cost), stop=str(stop), planned_loss=size["planned_loss"])
    result["fills"].append({"side": "buy", "at": started, "price": str(entry), "quantity": str(qty),
                            "fee": str(qty * entry * fee)})

    def sell(amount, reference, at, reason):
        nonlocal remaining, proceeds
        execution = reference * (1 - slip)
        received = amount * execution * (1 - fee)
        proceeds += received
        remaining -= amount
        result["fills"].append({"side": "sell", "at": at, "price": str(execution),
                                "quantity": str(amount), "fee": str(amount * execution * fee), "reason": reason})

    for row in rows:
        at = row["observed_at"]
        if at <= started:
            continue
        if at - previous > cfg["max_gap_s"] or row["received_at"] - at > cfg["max_gap_s"]:
            return dict(result, state="insufficient_data", reason="observation_gap", last_observed_at=previous)
        price = positive(row["price"])
        metadata = json.loads(row["metrics_json"])
        # If bars are supplied, both barriers in one bar cannot be ordered honestly.
        event = barrier_event(metadata.get("low", price), metadata.get("high", price), stop, levels[next_target][0])
        path.append((at, price))
        previous = at
        if event == "ambiguous":
            return dict(result, state="ambiguous", reason="stop_and_target_in_same_bar")
        if at >= started + cfg["max_hold_s"]:
            # At expiry we use the observed executable quote; no imaginary exact-time fill.
            sell(remaining, price, at, "time_exit")
        elif event == "stop":
            sell(remaining, min(price, stop), at, "stop")
        else:
            # A point at a target proves only the sampled rule. Do not infer an unseen high fill.
            while next_target < len(levels) and price >= levels[next_target][0]:
                level, fraction = levels[next_target]
                amount = remaining if next_target == len(levels) - 1 else min(qty * fraction, remaining)
                sell(amount, level, at, f"tp{next_target + 1}")
                next_target += 1
        if remaining == 0:
            pnl = proceeds - cost
            return dict(result, state="closed", closed_at=at, pnl=str(pnl),
                        return_pct=str(pct(proceeds, cost)), realized_r=str(pnl / positive(size["planned_loss"])),
                        path=path_metrics(entry, started, path))
    if now - previous > cfg["max_gap_s"]:
        return dict(result, state="insufficient_data", reason="observation_gap", last_observed_at=previous)
    mark = positive(path[-1][1] if path else first["price"])
    return dict(result, remaining_quantity=str(remaining), realized_proceeds=str(proceeds),
                marked_pnl=str(proceeds + remaining * mark * (1 - slip) * (1 - fee) - cost),
                last_observed_at=previous, path=path_metrics(entry, started, path))
