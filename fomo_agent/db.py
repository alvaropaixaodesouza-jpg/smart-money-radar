"""SQLite schema, versioned migrations, and small upsert helpers. No ORM."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS traders(
      address TEXT PRIMARY KEY,
      chain TEXT DEFAULT 'solana',
      fomo_handle TEXT,
      source TEXT,
      first_seen_at INTEGER,
      last_seen_at INTEGER,
      pnl_7d REAL, pnl_30d REAL, win_rate REAL, trades_cnt INTEGER,
      status TEXT DEFAULT 'candidate',
      score INTEGER, tags TEXT, ai_summary TEXT, ai_scored_at INTEGER, ai_model TEXT,
      last_tracked_ts INTEGER
    );
    CREATE TABLE IF NOT EXISTS tokens(
      mint TEXT PRIMARY KEY,
      symbol TEXT, mcap_usd REAL, liquidity_usd REAL,
      created_at INTEGER, first_seen_at INTEGER, triggered_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS trades(
      sig TEXT PRIMARY KEY,
      address TEXT, mint TEXT, side TEXT,
      sol_amount REAL, token_amount REAL, usd_value REAL,
      ts INTEGER, source TEXT
    );
    CREATE TABLE IF NOT EXISTS score_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      address TEXT, score INTEGER, status TEXT, model TEXT, reason TEXT, ts INTEGER
    );
    CREATE TABLE IF NOT EXISTS runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT, started_at INTEGER, finished_at INTEGER, stats_json TEXT, error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_trades_addr_ts ON trades(address, ts);
    CREATE INDEX IF NOT EXISTS idx_trades_mint_ts ON trades(mint, ts);
    CREATE INDEX IF NOT EXISTS idx_traders_status ON traders(status);
    """,
    2: """
    ALTER TABLE tokens ADD COLUMN chain TEXT DEFAULT 'solana';
    CREATE INDEX IF NOT EXISTS idx_traders_chain ON traders(chain);
    """,
    3: """
    ALTER TABLE trades ADD COLUMN chain TEXT DEFAULT 'solana';
    """,
    4: """
    ALTER TABLE traders ADD COLUMN fomo_user_id TEXT;
    ALTER TABLE traders ADD COLUMN profile_address TEXT;
    ALTER TABLE traders ADD COLUMN evm_address TEXT;
    ALTER TABLE traders ADD COLUMN pnl_24h REAL;
    ALTER TABLE traders ADD COLUMN volume_usd REAL;
    CREATE INDEX IF NOT EXISTS idx_traders_fomo_user ON traders(fomo_user_id);
    """,
    5: """
    CREATE TABLE IF NOT EXISTS fomo_users(
      user_id TEXT PRIMARY KEY,
      handle TEXT, profile_address TEXT, evm_address TEXT,
      pnl_24h REAL, pnl_7d REAL, pnl_30d REAL, trades_cnt INTEGER, volume_usd REAL,
      source TEXT, first_seen_at INTEGER, last_seen_at INTEGER,
      resolved_at INTEGER, resolve_error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_fomo_users_resolved ON fomo_users(resolved_at);
    """,
    6: """
    ALTER TABLE traders ADD COLUMN stats_json TEXT;
    ALTER TABLE traders ADD COLUMN stats_at INTEGER;
    ALTER TABLE traders ADD COLUMN stats_source TEXT;
    """,
    7: """
    CREATE TABLE IF NOT EXISTS fomo_swaps(
      swap_id TEXT PRIMARY KEY,
      user_id TEXT, chain TEXT, token TEXT, side TEXT,
      ts INTEGER, usd REAL, amount REAL
    );
    CREATE INDEX IF NOT EXISTS idx_fomo_swaps_user ON fomo_swaps(user_id, chain, ts);
    ALTER TABLE fomo_users ADD COLUMN onchain_at INTEGER;
    ALTER TABLE fomo_users ADD COLUMN onchain_address TEXT;
    ALTER TABLE fomo_users ADD COLUMN onchain_note TEXT;
    """,
    8: """
    CREATE TABLE IF NOT EXISTS fomo_positions(
      trade_id TEXT PRIMARY KEY,
      user_id TEXT, chain TEXT, token TEXT, symbol TEXT,
      opened_at INTEGER, closed_at INTEGER,
      amount REAL, avg_entry REAL, avg_exit REAL,
      realized_pnl REAL, unrealized_pnl REAL, cost_basis REAL,
      current_price REAL, liquidity REAL, seen_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_positions_user ON fomo_positions(user_id);
    CREATE INDEX IF NOT EXISTS idx_positions_token ON fomo_positions(token);
    """,
    9: """
    -- Two sources describing the same fill number their signatures differently (a log index, a
    -- tape sequence), so the primary key alone lets one trade land twice. `fill_key` is what the
    -- chain considers the same event, and the unique index makes the duplicate impossible.
    ALTER TABLE trades ADD COLUMN fill_key TEXT;
    UPDATE trades SET fill_key = substr(sig, 1, 66) || ':' || address || ':' || mint || ':' || side;
    DELETE FROM trades WHERE rowid NOT IN (
      SELECT MIN(rowid) FROM trades GROUP BY fill_key
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_fill ON trades(fill_key);
    """,
    10: """
    -- Telegram: who wants signals pushed, and what each of them has already been told, so a
    -- token that stays hot for a day does not become a day of identical messages.
    CREATE TABLE IF NOT EXISTS bot_subscribers(
      chat_id TEXT PRIMARY KEY,
      username TEXT, min_conviction REAL, subscribed_at INTEGER, active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS bot_sent(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat_id TEXT, mint TEXT, ts INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_bot_sent ON bot_sent(chat_id, mint, ts);
    """,
    11: """
    -- A position nobody can price is half an answer: we knew what a wallet paid and how much it
    -- still holds, but not what that is worth today. DexScreener returns the price in the same
    -- response the liquidity already came from, so the mark costs no extra request; `price_at` is
    -- what lets a refresh pass tell a stale quote from a fresh one.
    ALTER TABLE tokens ADD COLUMN price_usd REAL;
    ALTER TABLE tokens ADD COLUMN price_at INTEGER;
    -- Rebuilding a wallet's book reads its whole tape one token at a time; without this the
    -- ledger scans every fill in the table for every trader page.
    CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(address, mint);
    """,
    12: """
    -- How many base units make one token. It is a constant the chain will answer for free, but
    -- every collection pass is a fresh process, so without somewhere to keep it the tracker
    -- re-asks about the same few hundred tokens four times an hour.
    ALTER TABLE tokens ADD COLUMN decimals INTEGER;
    """,
    13: """
    -- When a price source was last *asked* about this address, whatever it answered. Without it a
    -- token nobody indexes is re-requested every pass forever, and because the queue is ordered by
    -- how stale a quote is, those permanent failures sit at the front of it and starve the tokens
    -- that do have a price.
    ALTER TABLE tokens ADD COLUMN checked_at INTEGER;
    """,
    14: """
    -- What a wallet actually holds, read off the chain rather than inferred from the fills we
    -- happened to watch. This is what makes a position's size and value true for a name entered
    -- before we started watching, and for the fills older sources recorded without a size.
    CREATE TABLE IF NOT EXISTS holdings(
      address TEXT, token TEXT, amount REAL, ts INTEGER,
      PRIMARY KEY(address, token)
    );
    CREATE INDEX IF NOT EXISTS idx_holdings_ts ON holdings(ts);
    """,
    15: """
    -- The pool a token actually trades in. It arrives free alongside the price, and it is the only
    -- handle the candle endpoint takes, so without it a token page has no chart.
    ALTER TABLE tokens ADD COLUMN pool_address TEXT;
    """,
}

STATUSES = ("candidate", "tracking", "active", "watch", "dropped", "needs_review")


def now() -> int:
    return int(time.time())


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else settings.db_path
    # WAL lets a long read run beside a write; the busy timeout covers the moment two writers
    # meet, which happens whenever a collection loop and a one-off command overlap.
    #
    # check_same_thread is off because the API hands each request its own connection, and a web
    # framework is free to run the handler on one worker thread and the teardown that closes it on
    # another. sqlite's guard sees that as illegal and raises mid-request. Turning it off is safe
    # here precisely because no connection is ever shared between two units of work — every caller
    # opens its own and closes it.
    conn = sqlite3.connect(p, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for v in sorted(MIGRATIONS):
        if v > version:
            conn.executescript(MIGRATIONS[v])
            conn.execute(f"PRAGMA user_version={v}")
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _placeholders(n: int) -> str:
    return ",".join(["?"] * n)


# ---------- traders ----------

def upsert_trader(conn: sqlite3.Connection, address: str, **fields: Any) -> bool:
    """Insert or update a trader. Returns True if the row was newly created.
    Status of an existing row is never touched here; use set_status."""
    ts = now()
    exists = conn.execute("SELECT 1 FROM traders WHERE address=?", (address,)).fetchone()
    clean = {k: v for k, v in fields.items() if v is not None}
    if exists is None:
        cols = ["address", "first_seen_at", "last_seen_at", *clean]
        vals = [address, ts, ts, *clean.values()]
        conn.execute(f"INSERT INTO traders({','.join(cols)}) VALUES({_placeholders(len(cols))})", vals)
        return True
    clean.pop("status", None)
    clean["last_seen_at"] = ts
    sets = ",".join(f"{k}=?" for k in clean)
    conn.execute(f"UPDATE traders SET {sets} WHERE address=?", [*clean.values(), address])
    return False


def set_status(conn: sqlite3.Connection, address: str, status: str) -> None:
    assert status in STATUSES, status
    conn.execute("UPDATE traders SET status=? WHERE address=?", (status, address))


def get_trader(conn: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM traders WHERE address=?", (address,)).fetchone()


def traders_by_status(conn: sqlite3.Connection, *statuses: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM traders WHERE status IN ({_placeholders(len(statuses))}) "
        "ORDER BY score DESC NULLS LAST, last_seen_at DESC",
        statuses,
    ).fetchall()


# ---------- tokens ----------

def upsert_token(conn: sqlite3.Connection, mint: str, **fields: Any) -> bool:
    ts = now()
    exists = conn.execute("SELECT 1 FROM tokens WHERE mint=?", (mint,)).fetchone()
    clean = {k: v for k, v in fields.items() if v is not None}
    if exists is None:
        cols = ["mint", "first_seen_at", *clean]
        vals = [mint, ts, *clean.values()]
        conn.execute(f"INSERT INTO tokens({','.join(cols)}) VALUES({_placeholders(len(cols))})", vals)
        return True
    if clean:
        sets = ",".join(f"{k}=?" for k in clean)
        conn.execute(f"UPDATE tokens SET {sets} WHERE mint=?", [*clean.values(), mint])
    return False


def token_decimals(conn: sqlite3.Connection) -> dict[str, int]:
    """Every token whose base unit we already know, so nobody asks the chain about it twice."""
    return {r["mint"]: r["decimals"] for r in
            conn.execute("SELECT mint, decimals FROM tokens WHERE decimals IS NOT NULL")}


def save_token_decimals(conn: sqlite3.Connection, decimals: dict[str, int]) -> int:
    """Store what a pass learned. Writes only what is new, so this is free when nothing changed."""
    known = token_decimals(conn)
    fresh = {m: d for m, d in decimals.items() if d is not None and known.get(m) != d}
    for mint, value in fresh.items():
        upsert_token(conn, mint, decimals=value)
    return len(fresh)


def save_holdings(conn: sqlite3.Connection, balances: dict[tuple[str, str], float]) -> int:
    """Store a balance read. A zero is a fact worth keeping — it says a position was closed."""
    ts = now()
    conn.executemany(
        "INSERT INTO holdings(address, token, amount, ts) VALUES(?,?,?,?) "
        "ON CONFLICT(address, token) DO UPDATE SET amount=excluded.amount, ts=excluded.ts",
        [(w, m, amount, ts) for (w, m), amount in balances.items()],
    )
    return len(balances)


def holdings_for(conn: sqlite3.Connection, address: str) -> dict[str, tuple[float, int]]:
    """token -> (amount, when it was read) for one wallet."""
    return {r["token"]: (r["amount"], r["ts"]) for r in
            conn.execute("SELECT token, amount, ts FROM holdings WHERE address = ?", (address,))}


# ---------- trades ----------

TRADE_COLS = ("sig", "address", "chain", "mint", "side", "sol_amount", "token_amount", "usd_value",
              "ts", "source", "fill_key")


def fill_key(t: dict[str, Any]) -> str:
    """What makes two rows the same trade, whichever source reported it.

    Signatures carry a source-specific suffix — a log index from the chain, a sequence number from
    a tape — so the transaction hash is truncated back out of them before comparing. Addresses are
    taken as stored: sources normalize EVM case on the way in, and Solana base58 is case-sensitive.
    """
    return ":".join((str(t.get("sig", ""))[:66], str(t.get("address", "")),
                     str(t.get("mint", "")), str(t.get("side", ""))))


def insert_trade(conn: sqlite3.Connection, **t: Any) -> bool:
    """Idempotent insert keyed by signature. Returns True if a new row was inserted."""
    t = {**t, "fill_key": t.get("fill_key") or fill_key(t)}
    vals = [t.get(c) for c in TRADE_COLS]
    cur = conn.execute(
        f"INSERT OR IGNORE INTO trades({','.join(TRADE_COLS)}) VALUES({_placeholders(len(TRADE_COLS))})", vals
    )
    return cur.rowcount == 1


def last_trade_ts(conn: sqlite3.Connection, address: str) -> int | None:
    row = conn.execute("SELECT MAX(ts) FROM trades WHERE address=?", (address,)).fetchone()
    return row[0]


# ---------- runs / history ----------

def run_start(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute("INSERT INTO runs(kind, started_at) VALUES(?,?)", (kind, now()))
    conn.commit()
    return cur.lastrowid


def run_finish(conn: sqlite3.Connection, run_id: int, stats: dict | None = None, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, stats_json=?, error=? WHERE id=?",
        (now(), json.dumps(stats or {}), error, run_id),
    )
    conn.commit()


def upsert_fomo_position(conn: sqlite3.Connection, **p: Any) -> bool:
    """Positions change as prices move, so this overwrites rather than ignoring duplicates."""
    cols = ("trade_id", "user_id", "chain", "token", "symbol", "opened_at", "closed_at", "amount",
            "avg_entry", "avg_exit", "realized_pnl", "unrealized_pnl", "cost_basis",
            "current_price", "liquidity", "seen_at")
    # fomo's `pnl` can exceed a holding's whole value because it also counts profit already taken
    # out. Cost basis is then unrecoverable, and a negative one would poison every multiple
    # computed from it, so it is stored as unknown. Enforced here, where no caller can skip it.
    p = dict(p)
    for k in ("cost_basis", "avg_entry"):
        if p.get(k) is not None and p[k] <= 0:
            p[k] = None
    vals = [p.get(c) for c in cols[:-1]] + [now()]
    existed = conn.execute("SELECT 1 FROM fomo_positions WHERE trade_id=?", (p.get("trade_id"),)).fetchone()
    conn.execute(
        f"INSERT OR REPLACE INTO fomo_positions({','.join(cols)}) VALUES({_placeholders(len(cols))})", vals
    )
    return existed is None


def insert_fomo_swap(conn: sqlite3.Connection, **s: Any) -> bool:
    cols = ("swap_id", "user_id", "chain", "token", "side", "ts", "usd", "amount")
    cur = conn.execute(
        f"INSERT OR IGNORE INTO fomo_swaps({','.join(cols)}) VALUES({_placeholders(len(cols))})",
        [s.get(c) for c in cols],
    )
    return cur.rowcount == 1


def upsert_fomo_user(conn: sqlite3.Connection, user_id: str, **fields: Any) -> bool:
    ts = now()
    exists = conn.execute("SELECT 1 FROM fomo_users WHERE user_id=?", (user_id,)).fetchone()
    clean = {k: v for k, v in fields.items() if v is not None}
    if exists is None:
        cols = ["user_id", "first_seen_at", "last_seen_at", *clean]
        vals = [user_id, ts, ts, *clean.values()]
        conn.execute(f"INSERT INTO fomo_users({','.join(cols)}) VALUES({_placeholders(len(cols))})", vals)
        return True
    clean["last_seen_at"] = ts
    sets = ",".join(f"{k}=?" for k in clean)
    conn.execute(f"UPDATE fomo_users SET {sets} WHERE user_id=?", [*clean.values(), user_id])
    return False


def unresolved_fomo_users(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Leaderboard entries whose execution wallet we have not looked up yet, best PnL first."""
    return conn.execute(
        "SELECT * FROM fomo_users WHERE resolved_at IS NULL AND resolve_error IS NULL "
        "ORDER BY COALESCE(pnl_30d, pnl_7d, pnl_24h) DESC NULLS LAST LIMIT ?",
        (limit,),
    ).fetchall()


def add_score_history(conn: sqlite3.Connection, address: str, score: int, status: str, model: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO score_history(address, score, status, model, reason, ts) VALUES(?,?,?,?,?,?)",
        (address, score, status, model, reason, now()),
    )
