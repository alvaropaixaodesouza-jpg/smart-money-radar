"""Ask the chain what each tracked wallet actually holds.

Everywhere else in this project a position is inferred: from the fills we watched, or from the
three bags fomo publishes. Both are partial in the same direction — they start when we started,
and they miss whatever a source recorded without a size. `balanceOf` is not partial. It is a free
read, forty to a round trip, and it settles in one number whether a position is open, how large it
is, and therefore what it is worth.

What it cannot settle is cost. A wallet holding more than the tape ever saw it buy paid for the
difference before we arrived, at a price nobody here knows, so `analyze.ledger` reports those
positions with a size and a value and no profit — see `position_state`.
"""
from __future__ import annotations

import logging
import sqlite3

from .. import db
from ..config import settings
from ..sources.rpc import CHAIN, QUOTE_TOKENS, RobinhoodRPC
from .track import TRACKED

log = logging.getLogger(__name__)


def stale_pairs(conn: sqlite3.Connection, limit: int | None = None,
                max_age_s: int | None = None) -> list[tuple[str, str]]:
    """(wallet, token) pairs worth re-reading, longest-unread first.

    Every name a tracked wallet has ever bought is a candidate, including the ones it has since
    sold — a balance of zero is how we learn the position closed. Quote assets are skipped: a
    wallet's USDG balance is its cash, not a position.
    """
    max_age = db.now() - (settings.holdings_max_age_s if max_age_s is None else max_age_s)
    quotes = ",".join("?" * len(QUOTE_TOKENS))
    rows = conn.execute(
        "SELECT tr.address AS address, tr.mint AS token FROM trades tr "
        "JOIN traders t ON t.address = tr.address "
        "LEFT JOIN holdings h ON h.address = tr.address AND h.token = tr.mint "
        f"WHERE tr.side='buy' AND tr.chain = ? AND t.status IN ({','.join('?' * len(TRACKED))}) "
        f"  AND tr.mint NOT IN ({quotes}) AND (h.ts IS NULL OR h.ts < ?) "
        "GROUP BY tr.address, tr.mint "
        "ORDER BY COALESCE(MAX(h.ts), 0) ASC, MAX(tr.ts) DESC LIMIT ?",
        (CHAIN, *TRACKED, *QUOTE_TOKENS, max_age, limit or settings.holdings_per_pass),
    ).fetchall()
    return [(r["address"], r["token"]) for r in rows]


def mark_holdings(conn: sqlite3.Connection, rpc: RobinhoodRPC | None = None,
                  limit: int | None = None) -> dict:
    """One pass: read the balances that have gone stale and store them."""
    pairs = stale_pairs(conn, limit)
    stats = {"pairs": len(pairs), "held": 0, "empty": 0, "requests": 0}
    if not pairs:
        log.info("holdings: nothing stale")
        return stats

    rpc = rpc or RobinhoodRPC()
    rpc.load_decimals(db.token_decimals(conn))
    balances = rpc.balances(pairs)
    stats["held"] = sum(1 for v in balances.values() if v > 0)
    stats["empty"] = sum(1 for v in balances.values() if v <= 0)
    with db.tx(conn):
        db.save_holdings(conn, balances)
        db.save_token_decimals(conn, rpc.known_decimals())
    stats["requests"] = rpc.requests
    log.info("holdings: %s", stats)
    return stats
