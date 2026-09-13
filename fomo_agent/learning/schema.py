"""Additive schema. Raw observations and decisions are append-only."""

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS learning_versions(
 version TEXT PRIMARY KEY, created_at INTEGER NOT NULL, config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_signals(
 id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
 chain TEXT NOT NULL, asset TEXT NOT NULL, venue TEXT NOT NULL, quote TEXT NOT NULL,
 detected_at INTEGER NOT NULL, entry_price TEXT, entry_observed_at INTEGER,
 score REAL, version TEXT NOT NULL, features_json TEXT NOT NULL,
 components_json TEXT NOT NULL, quality_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS learning_signals_asset ON learning_signals(chain,asset,detected_at);
CREATE TABLE IF NOT EXISTS learning_observations(
 id INTEGER PRIMARY KEY, chain TEXT NOT NULL, asset TEXT NOT NULL,
 venue TEXT NOT NULL, quote TEXT NOT NULL, observed_at INTEGER NOT NULL,
 received_at INTEGER NOT NULL CHECK(received_at >= observed_at), source TEXT NOT NULL,
 price TEXT NOT NULL, metrics_json TEXT NOT NULL,
 UNIQUE(chain,asset,venue,quote,observed_at,source)
);
CREATE INDEX IF NOT EXISTS learning_observations_asset ON learning_observations(chain,asset,venue,quote,observed_at);
CREATE TABLE IF NOT EXISTS learning_outcomes(
 signal_id INTEGER NOT NULL, horizon_s INTEGER NOT NULL, due_at INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', completed_at INTEGER, result_json TEXT,
 PRIMARY KEY(signal_id,horizon_s)
);
CREATE INDEX IF NOT EXISTS learning_due ON learning_outcomes(state,due_at);
CREATE TABLE IF NOT EXISTS learning_paper(
 signal_id INTEGER NOT NULL, strategy TEXT NOT NULL, state TEXT NOT NULL,
 evaluated_at INTEGER NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(signal_id,strategy)
);
CREATE TABLE IF NOT EXISTS learning_poll(
 chain TEXT NOT NULL, asset TEXT NOT NULL, checked_at INTEGER NOT NULL,
 PRIMARY KEY(chain,asset)
);
CREATE TABLE IF NOT EXISTS learning_runs(
 id INTEGER PRIMARY KEY, started_at INTEGER NOT NULL, finished_at INTEGER,
 stats_json TEXT, error TEXT
);
""" + "\n".join(
    f"CREATE TRIGGER IF NOT EXISTS {table}_{op.lower()} BEFORE {op} ON {table} "
    "BEGIN SELECT RAISE(ABORT, 'immutable research record'); END;"
    for table in ("learning_versions", "learning_signals", "learning_observations")
    for op in ("UPDATE", "DELETE")
) + "\nPRAGMA user_version=20; COMMIT;"
