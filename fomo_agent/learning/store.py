"""As-known-at-the-time snapshots. Never manufacture historic detections."""
import hashlib
import json

from .math import dec, positive

VERSION = "onchain-research-v1"
HORIZONS = (300, 900, 1800, 3600, 14400, 43200, 86400, 259200, 604800)
CONFIG = {
    "state": "experimental", "feature_version": "onchain-v1", "window_hours": 24,
    "score": {"buyers": [5, 30], "wallet_quality": [100, 40], "liquidity": [100000, 30]},
    "entry_max_age_s": 120, "horizon_tolerance_s": 120, "cooldown_s": 86400,
    "observation_interval_s": 60,
    "paper": {"name": "long-points-v1", "capital": "1000", "risk_pct": "1",
              "stop_pct": "5", "targets": [["10", "0.5"], ["20", "0.5"]],
              "fee_bps": "10", "slip_bps": "20", "latency_s": 1,
              "entry_timeout_s": 120, "max_hold_s": 86400, "max_gap_s": 120,
              "min_score": 70, "min_liquidity": 5000, "max_liquidity_pct": "1"},
}


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def ensure_version(conn, now):
    raw = dumps(CONFIG)
    conn.execute("INSERT OR IGNORE INTO learning_versions VALUES(?,?,?)", (VERSION, now, raw))
    if conn.execute("SELECT config_json FROM learning_versions WHERE version=?", (VERSION,)).fetchone()[0] != raw:
        raise ValueError("configuration changed: create a new model version")


def observe(conn, *, chain, asset, price, observed_at, received_at, source,
            metrics=None, venue="dex", quote="USD"):
    price = str(positive(price))
    if not all((chain, asset, source, venue, quote)) or observed_at > received_at or observed_at < 0:
        raise ValueError("invalid identity or timestamp")
    raw = dumps(metrics or {})
    row = conn.execute(
        "SELECT * FROM learning_observations WHERE chain=? AND asset=? AND venue=? AND quote=? "
        "AND observed_at=? AND source=?", (chain, asset, venue, quote, observed_at, source)).fetchone()
    if row:
        if dec(row["price"]) != dec(price) or row["metrics_json"] != raw:
            raise ValueError("conflicting duplicate observation")
        return row["id"]
    return conn.execute(
        "INSERT INTO learning_observations(chain,asset,venue,quote,observed_at,received_at,source,price,metrics_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)", (chain, asset, venue, quote, observed_at, received_at, source, price, raw)).lastrowid


def latest(conn, chain, asset, now):
    return conn.execute(
        "SELECT * FROM learning_observations WHERE chain=? AND asset=? AND venue='dex' AND quote='USD' "
        "AND observed_at<=? AND received_at<=? ORDER BY observed_at DESC,id ASC LIMIT 1",
        (chain, asset, now, now)).fetchone()


def opportunity_score(buyers, quality, liquidity):
    values = {"buyers": buyers, "wallet_quality": quality, "liquidity": liquidity}
    components = {k: None if values[k] is None else float(
        min(max(dec(values[k]), dec(0)), dec(cap)) / cap * weight)
        for k, (cap, weight) in CONFIG["score"].items()}
    return (None if any(v is None for v in components.values()) else sum(components.values())), components


def record_signal(conn, *, event_key, chain, asset, now, score, features, components,
                  observation=None, venue="dex", quote="USD"):
    if venue != "dex" or quote != "USD" or not chain or not asset or now < 0:
        raise ValueError("this version supports DEX USD observations only")
    if score is not None and not 0 <= dec(score) <= 100:
        raise ValueError("invalid score")
    ensure_version(conn, now)
    existing = conn.execute("SELECT id FROM learning_signals WHERE event_key=?", (event_key,)).fetchone()
    if existing:
        return existing[0]
    entry = entry_at = None
    if observation is not None:
        if (observation["chain"], observation["asset"], observation["venue"], observation["quote"]) != (chain, asset, venue, quote):
            raise ValueError("observation belongs to another instrument")
        if observation["observed_at"] > now or observation["received_at"] > now:
            raise ValueError("observation was not known at detection")
        if now - observation["observed_at"] <= CONFIG["entry_max_age_s"]:
            entry, entry_at = str(positive(observation["price"])), observation["observed_at"]
    cur = conn.execute(
        "INSERT INTO learning_signals(event_key,chain,asset,venue,quote,detected_at,entry_price,entry_observed_at,"
        "score,version,features_json,components_json,quality_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_key, chain, asset, venue, quote, now, entry, entry_at, score, VERSION,
         dumps(features), dumps(components), dumps({"entry": "fresh" if entry else "missing_or_stale",
         "confidence": "insufficient_validated_evidence", "official_score_changed": False})))
    conn.executemany("INSERT INTO learning_outcomes(signal_id,horizon_s,due_at) VALUES(?,?,?)",
                     [(cur.lastrowid, h, now + h) for h in HORIZONS])
    return cur.lastrowid


def capture(conn, candidates, chain, now):
    count = 0
    for r in candidates:
        asset = r["mint"]
        observation = latest(conn, chain, asset, now)
        fresh = observation is not None and now - observation["observed_at"] <= CONFIG["entry_max_age_s"]
        last = conn.execute("SELECT * FROM learning_signals WHERE chain=? AND asset=? AND version=? "
                            "ORDER BY detected_at DESC,id DESC LIMIT 1", (chain, asset, VERSION)).fetchone()
        if last and now - last["detected_at"] < CONFIG["cooldown_s"] and (last["entry_price"] or not fresh):
            continue
        metadata = json.loads(observation["metrics_json"]) if fresh else {}
        liquidity = metadata.get("liquidity")
        score, components = opportunity_score(r["buyers"], r["avg_score"], liquidity)
        features = dict.fromkeys(("buyers_acceleration", "momentum", "volume", "volume_acceleration",
                                 "holder_concentration", "cvd", "order_book_imbalance", "btc_context"))
        features.update(buyers=r["buyers"], wallet_quality=r["avg_score"],
                        smart_money_activity=r["conviction"], tracked_buy_volume_24h=r["usd"],
                        liquidity=liquidity, market_cap=metadata.get("market_cap"),
                        token_age_s=metadata.get("token_age_s"), canonical_ranking=dict(r))
        key = hashlib.sha256(f"{VERSION}:{chain}:{asset}:{now}".encode()).hexdigest()
        record_signal(conn, event_key=key, chain=chain, asset=asset, now=now, score=score,
                      features=features, components=components, observation=observation)
        count += 1
    return count


def trajectory(conn, signal, now):
    rows = conn.execute(
        "SELECT * FROM learning_observations WHERE chain=? AND asset=? AND venue=? AND quote=? "
        "AND observed_at>? AND observed_at<=? AND received_at<=? ORDER BY observed_at,id",
        (signal["chain"], signal["asset"], signal["venue"], signal["quote"], signal["detected_at"], now, now)).fetchall()
    # One source per instant: first received/inserted observation is deterministic.
    out, seen = [], set()
    for row in rows:
        if row["observed_at"] not in seen:
            out.append(row)
            seen.add(row["observed_at"])
    return out
