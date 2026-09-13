"""Fixed deadlines, explicit missingness and sampled rather than invented extremes."""
import json

from .math import pct, path_metrics
from .store import CONFIG, dumps, trajectory


def measure(conn, signal, horizon, now):
    due = signal["detected_at"] + horizon
    deadline = due + CONFIG["horizon_tolerance_s"]
    if now < deadline:
        return {"state": "pending"}
    if signal["entry_price"] is None:
        return {"state": "no_entry"}
    rows = trajectory(conn, signal, deadline)
    endpoints = [r for r in rows if abs(r["observed_at"] - due) <= CONFIG["horizon_tolerance_s"]]
    if not endpoints:
        return {"state": "missing", "reason": "no_quote_near_horizon"}
    endpoint = min(endpoints, key=lambda r: (abs(r["observed_at"] - due), r["observed_at"]))
    path = [(r["observed_at"], r["price"]) for r in rows if r["observed_at"] <= due]
    metrics = path_metrics(signal["entry_price"], signal["detected_at"], path)
    gap = due - (path[-1][0] if path else signal["detected_at"])
    metrics["max_gap_s"] = max(metrics["max_gap_s"], gap)
    bins = {min((t - signal["detected_at"] - 1) // 60, (horizon - 1) // 60) for t, _ in path}
    metrics["coverage_pct"] = 100 * len(bins) / ((horizon + 59) // 60)
    initial, final = json.loads(signal["features_json"]), json.loads(endpoint["metrics_json"])
    changes = {f"{k}_change_pct": str(pct(final[k], initial[k]))
               if final.get(k) is not None and initial.get(k) is not None and initial[k] > 0 else None
               for k in ("liquidity", "buyers")}
    return {"state": "measured", "price": endpoint["price"], "observed_at": endpoint["observed_at"],
            "horizon_offset_s": endpoint["observed_at"] - due,
            "return_pct": str(pct(endpoint["price"], signal["entry_price"])),
            "path_complete": metrics["max_gap_s"] <= 120, "volume_after": None, **metrics, **changes}


def finish_due(conn, now):
    count = 0
    jobs = conn.execute("SELECT signal_id,horizon_s FROM learning_outcomes WHERE state='pending' AND due_at<=?",
                        (now - CONFIG["horizon_tolerance_s"],)).fetchall()
    for sid, horizon in jobs:
        signal = conn.execute("SELECT * FROM learning_signals WHERE id=?", (sid,)).fetchone()
        result = measure(conn, signal, horizon, now)
        conn.execute("UPDATE learning_outcomes SET state=?,completed_at=?,result_json=? WHERE signal_id=? AND horizon_s=?",
                     (result["state"], now, dumps(result), sid, horizon))
        count += 1
    return count
