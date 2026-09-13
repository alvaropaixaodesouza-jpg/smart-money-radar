"""Bounded polling with restart-safe jobs and one research worker per database."""
import fcntl
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .. import db
from ..pipeline.analyze import signals
from ..pipeline.new_tokens import lookup_tokens
from .outcomes import finish_due
from .paper import simulate
from .store import CONFIG, VERSION, capture, dumps, ensure_version, observe

log = logging.getLogger(__name__)


@contextmanager
def worker_lock(database):
    path = Path(str(Path(database).resolve()) + ".learning.lock")
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError("Já existe um worker learning ativo para este banco.") from e
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def backup(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or not source.is_file():
        raise ValueError("source database must exist and differ from backup")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    # Never call db.connect here: a backup must not migrate the source first.
    src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("backup failed integrity check")
    finally:
        dst.close()
        src.close()
    return target


def tick(conn, chain="robinhood", limit=40, offline=False, clock=None, lookup=None):
    if not 1 <= limit <= 100 or not chain:
        raise ValueError("limit must be between 1 and 100")
    clock = clock or (lambda: int(time.time()))
    lookup = lookup or lookup_tokens
    now = int(clock())
    run_id = conn.execute("INSERT INTO learning_runs(started_at) VALUES(?)", (now,)).lastrowid
    conn.commit()
    try:
        candidates = signals(conn, chain=chain, limit=limit)
        active = conn.execute(
            "SELECT DISTINCT s.asset FROM learning_signals s JOIN learning_outcomes o ON o.signal_id=s.id "
            "WHERE s.chain=? AND s.version=? AND s.entry_price IS NOT NULL AND o.state='pending'",
            (chain, VERSION)).fetchall()
        assets = {r["mint"] for r in candidates} | {r[0] for r in active}
        polled = {r[0]: r[1] for r in conn.execute("SELECT asset,checked_at FROM learning_poll WHERE chain=?", (chain,))}
        due = sorted((a for a in assets if now - polled.get(a, 0) >= CONFIG["observation_interval_s"]),
                     key=lambda a: (polled.get(a, 0), a))[:limit]
        tokens, requests = ([], 0) if offline or not due else lookup(chain, due)
        now = int(clock())  # response time, never the request-start timestamp
        candidates = signals(conn, chain=chain, limit=limit)
        stats = {"candidates": len(candidates), "active_assets": len(assets), "quotes": 0,
                 "requested": 0 if offline else len(due), "requests": requests, "offline": offline}
        conn.execute("BEGIN IMMEDIATE")
        ensure_version(conn, now)
        seen = set()
        for token in tokens:
            if token.chain != chain or token.mint not in due or token.mint in seen:
                continue
            if token.price_usd is None or token.price_usd <= 0:
                continue
            metrics = {"liquidity": token.liquidity_usd, "market_cap": token.mcap_usd,
                       "token_age_s": max(0, now - token.created_at) if token.created_at else None,
                       "timestamp_basis": "provider_response_time", "source_event_at": None}
            observe(conn, chain=chain, asset=token.mint, price=token.price_usd,
                    observed_at=now, received_at=now, source=token.source, metrics=metrics)
            seen.add(token.mint)
            stats["quotes"] += 1
        if not offline:
            conn.executemany("INSERT INTO learning_poll VALUES(?,?,?) ON CONFLICT(chain,asset) "
                             "DO UPDATE SET checked_at=excluded.checked_at", [(chain, a, now) for a in due])
        stats["unavailable"] = 0 if offline else len(due) - len(seen)
        stats["signals_new"] = capture(conn, candidates, chain, now)
        stats["outcomes_completed"] = finish_due(conn, now)
        stats["paper_evaluated"] = 0
        pending = conn.execute(
            "SELECT s.* FROM learning_signals s LEFT JOIN learning_paper p ON p.signal_id=s.id AND p.strategy=? "
            "WHERE s.version=? AND (p.signal_id IS NULL OR p.state IN ('waiting','open'))",
            (CONFIG["paper"]["name"], VERSION)).fetchall()
        for signal in pending:
            result = simulate(conn, signal, now)
            conn.execute("INSERT INTO learning_paper VALUES(?,?,?,?,?) ON CONFLICT(signal_id,strategy) "
                         "DO UPDATE SET state=excluded.state,evaluated_at=excluded.evaluated_at,result_json=excluded.result_json",
                         (signal["id"], result["strategy"], result["state"], now, dumps(result)))
            stats["paper_evaluated"] += 1
        conn.execute("UPDATE learning_runs SET finished_at=?,stats_json=? WHERE id=?", (now, dumps(stats), run_id))
        conn.commit()
        return stats
    except Exception as e:
        conn.rollback()
        conn.execute("UPDATE learning_runs SET finished_at=?,error=? WHERE id=?", (int(clock()), type(e).__name__, run_id))
        conn.commit()
        raise
