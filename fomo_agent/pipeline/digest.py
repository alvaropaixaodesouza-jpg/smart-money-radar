"""One message a day: what the cohort did while you were not looking.

Everything else this product publishes waits to be asked. The signal feed is worth most in the
minutes after a launch and the bot already pushes those, but a day has a shape the individual
alerts cannot show — that the same three names keep appearing, that the wallets which bought on
Monday were gone by Tuesday, that nothing happened at all.

Deliberately one message. A digest people scroll past is worse than no digest, so it carries the
five things that change a decision and nothing else: what came in, what went out, what launched,
who joined the roster, and whether the machine collecting all of it is still running.
"""
from __future__ import annotations

import sqlite3

from .. import db
from . import analyze
from .health import report as health_report


def daily(conn: sqlite3.Connection, hours: int = 24, chain: str | None = None) -> dict:
    since = db.now() - hours * 3600
    counts = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM traders WHERE score IS NOT NULL GROUP BY status")}

    # Wallets that got their first verdict in the window — the roster growing, not being rescored.
    joined = [dict(r) for r in conn.execute(
        "SELECT t.fomo_handle handle, t.address, t.score, t.status FROM traders t "
        "WHERE t.address IN ("
        "  SELECT address FROM score_history GROUP BY address HAVING MIN(ts) >= ?) "
        "AND t.score IS NOT NULL ORDER BY t.score DESC LIMIT 5", (since,))]
    joined_n = conn.execute(
        "SELECT COUNT(*) c FROM (SELECT address FROM score_history GROUP BY address "
        "HAVING MIN(ts) >= ?)", (since,)).fetchone()["c"]

    theses = [dict(r) for r in conn.execute(
        "SELECT th.text, th.mint, COALESCE(tk.symbol, substr(th.mint,1,8)) sym, u.handle, t.score "
        "FROM theses th JOIN fomo_users u ON u.user_id = th.user_id "
        "LEFT JOIN traders t ON t.address = u.onchain_address "
        "LEFT JOIN tokens tk ON tk.mint = th.mint "
        "WHERE th.first_seen_at >= ? AND t.score >= ? "
        "ORDER BY t.score DESC LIMIT 3", (since, analyze.TRUSTED))]

    return {
        "hours": hours,
        "counts": counts,
        "joined": joined,
        "joined_n": joined_n,
        "signals": analyze.signals(conn, chain, hours=hours, limit=3),
        "fresh": (analyze.fresh(conn, chain, hours=hours, limit=3) or {}).get("tokens", [])[:3],
        "exits": analyze.exits(conn, chain, hours=hours, min_sellers=2, limit=3),
        "theses": theses,
        "health": health_report(conn),
    }


def is_quiet(d: dict) -> bool:
    """Nothing moved. Worth saying in one line rather than dressing up as a report."""
    return not (d["signals"] or d["fresh"] or d["exits"] or d["joined_n"] or d["theses"])
