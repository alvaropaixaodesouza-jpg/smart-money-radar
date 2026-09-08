"""Walk the chain backwards and fill in the tape from before we were watching.

Everything the product says about a position is bounded by when tracking started. A name a wallet
entered earlier shows a size and a value and no profit, because the entry price is missing — the
`held` state on a trader page, and every dash in the PnL column, is that boundary showing.

The boundary is not fundamental. `eth_getLogs` will answer for any range; it just refuses more than
about 200k blocks at once, which on a chain with 0.1s blocks is under six hours. So the fix is to
ask repeatedly, newest first, until the requested depth is covered. Newest first matters: a run
that is interrupted has still filled the part nearest today, which is the part anybody reads.

It is free. The cost is time — one pass over a month is a few hundred requests against a rate limit
that exists to be polite, not because anybody is charging.
"""
from __future__ import annotations

import logging
import sqlite3

from .. import db
from ..config import settings
from ..sources.rpc import CHAIN, RobinhoodRPC
from .track import TRACKED

log = logging.getLogger(__name__)


def wallets_to_backfill(conn: sqlite3.Connection) -> list[str]:
    """Every tracked wallet on this chain, oldest tape first.

    Ordering by how far back we already have fills means a repeated run widens the shallowest
    histories rather than deepening the ones that are already deep.
    """
    rows = conn.execute(
        f"SELECT t.address address, MIN(tr.ts) first_ts FROM traders t "
        f"LEFT JOIN trades tr ON tr.address = t.address "
        f"WHERE t.chain = ? AND t.status IN ({','.join('?' * len(TRACKED))}) "
        "GROUP BY t.address ORDER BY COALESCE(first_ts, 0) DESC",
        (CHAIN, *TRACKED),
    ).fetchall()
    return [r["address"] for r in rows if r["address"].startswith("0x")]


def backfill(conn: sqlite3.Connection, days: int = 30, rpc: RobinhoodRPC | None = None,
             max_requests: int | None = None) -> dict:
    """Fill the tape back `days`, newest window first, stopping at a request budget.

    Every wallet goes into the same query: the topic filter takes a list, so one range costs the
    same two requests whether the roster is one wallet or three hundred.
    """
    wallets = wallets_to_backfill(conn)
    stats = {"wallets": len(wallets), "windows": 0, "fills": 0, "new": 0, "requests": 0,
             "days": days, "stopped_early": False}
    if not wallets:
        log.info("backfill: nothing tracked on %s", CHAIN)
        return stats

    rpc = rpc or RobinhoodRPC()
    rpc.load_decimals(db.token_decimals(conn))
    budget = max_requests if max_requests is not None else settings.backfill_max_requests
    since = db.now() - days * 86400

    for first, last in rpc.windows(since):
        if rpc.requests >= budget:
            stats["stopped_early"] = True
            log.info("backfill: stopping at the %d-request budget, %d windows in",
                     budget, stats["windows"])
            break
        try:
            found = rpc.scan(wallets, first, last)
        except Exception as e:  # noqa: BLE001 - one bad window must not lose the ones already done
            log.warning("backfill window %d..%d failed: %s", first, last, e)
            continue
        stats["windows"] += 1
        # Written per window rather than at the end: an interrupted backfill keeps what it found,
        # and the unique index on fill_key makes a re-run over the same range a no-op.
        with db.tx(conn):
            for trades in found.values():
                for t in trades:
                    stats["fills"] += 1
                    stats["new"] += db.insert_trade(conn, **t.model_dump())
            db.save_token_decimals(conn, rpc.known_decimals())
    stats["requests"] = rpc.requests
    log.info("backfill: %s", stats)
    return stats
