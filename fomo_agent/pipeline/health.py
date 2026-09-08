"""What is quietly broken.

Every failure mode this project has hit looked the same from outside: the site kept serving, the
pages kept rendering, and the numbers stopped moving. Nothing 500s when fomo's session expires or
when the router contract changes — the tape simply goes flat, and a flat tape reads exactly like a
quiet market until somebody notices the dates.

So the checks here are about *staleness and silence* rather than errors. Each one knows what it
would look like if the thing it watches had stopped, and says so in a sentence a person can act on.
"""
from __future__ import annotations

import logging
import sqlite3

from .. import db
from ..config import settings
from ..sources.rpc import CHAIN, RobinhoodRPC

log = logging.getLogger(__name__)


def _age_h(ts: int | None) -> float | None:
    return None if not ts else (db.now() - ts) / 3600


def router_alive(conn: sqlite3.Connection, rpc: RobinhoodRPC | None = None) -> dict:
    """Fills stopped, but did the chain?

    `RPC_ROUTERS` is one hardcoded contract. If fomo moves to another one, every fill vanishes and
    nothing anywhere errors: the feed empties, the pages keep working, and the whole product
    quietly becomes a museum. The test that separates "they changed the router" from "nobody
    traded today" is whether the wallets have any ERC-20 traffic at all while producing no fills.
    """
    fills = conn.execute("SELECT COUNT(*) FROM trades WHERE chain = ? AND ts >= ?",
                         (CHAIN, db.now() - 86400)).fetchone()[0]
    if fills:
        return {"name": "router", "ok": True, "detail": f"{fills} fills in the last day"}

    wallets = [r["address"] for r in conn.execute(
        "SELECT address FROM traders WHERE chain = ? AND status IN ('active','watch') LIMIT 60",
        (CHAIN,))]
    if not wallets:
        return {"name": "router", "ok": True, "detail": "nothing tracked on this chain yet"}
    try:
        rpc = rpc or RobinhoodRPC()
        head = rpc.block_number()
        moved = rpc.transfers(wallets, max(head - settings.rpc_window_blocks, 0), head,
                              outgoing=False)
    except Exception as e:  # noqa: BLE001 - a check that cannot run is not a failing check
        return {"name": "router", "ok": True, "detail": f"could not ask the chain: {e}"}

    if not moved:
        return {"name": "router", "ok": True,
                "detail": "no fills, but no transfers either - the cohort is simply still"}
    return {"name": "router", "ok": False,
            "detail": (f"no fills in a day while {len(moved)} transfers moved through these "
                       f"wallets. RPC_ROUTERS ({', '.join(settings.rpc_routers)}) is probably "
                       "stale - find the new one in a recent fomo trade.")}


def checks(conn: sqlite3.Connection, rpc: RobinhoodRPC | None = None) -> list[dict]:
    """Every check, worst first. `ok` False is something a person has to do."""
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731

    fomo_age = _age_h(one("SELECT MAX(finished_at) FROM runs WHERE kind = 'fomo_ingest'"))
    fills_age = _age_h(one("SELECT MAX(ts) FROM trades"))
    holdings_age = _age_h(one("SELECT MAX(ts) FROM holdings"))
    unscored = one("SELECT COUNT(*) FROM traders WHERE score IS NULL AND status IN "
                   "('tracking','active','watch')")
    unresolved = one("SELECT COUNT(*) FROM fomo_users WHERE onchain_address IS NULL")
    priced = one("SELECT COUNT(*) FROM tokens WHERE price_usd IS NOT NULL")
    tokens = one("SELECT COUNT(*) FROM tokens")

    out = [
        router_alive(conn, rpc),
        # The hint belongs on the failure only: a passing check that ends with a warning is how a
        # report trains people to skim past it.
        {"name": "fomo collection", "ok": fomo_age is not None and fomo_age < 3,
         "detail": (f"last collection {fomo_age:.1f}h ago" if fomo_age is not None
                    else "never collected") + ("" if fomo_age is not None and fomo_age < 3
                    else " - the browser on the server is probably signed out")},
        {"name": "on-chain tape", "ok": fills_age is not None and fills_age < 6,
         "detail": f"newest fill {fills_age:.1f}h ago" if fills_age is not None else "no fills"},
        {"name": "holdings", "ok": holdings_age is not None and holdings_age < 6,
         "detail": (f"balances read {holdings_age:.1f}h ago" if holdings_age is not None
                    else "never read")},
        {"name": "scoring queue", "ok": unscored < 25,
         "detail": f"{unscored} tracked wallets waiting for a verdict"},
        {"name": "prices", "ok": tokens == 0 or priced / tokens > 0.6,
         "detail": f"{priced} of {tokens} tokens priced"},
        {"name": "wallet resolution", "ok": True,
         "detail": f"{unresolved} fomo users still without an on-chain address"},
    ]
    return sorted(out, key=lambda c: c["ok"])


def report(conn: sqlite3.Connection, rpc: RobinhoodRPC | None = None) -> dict:
    rows = checks(conn, rpc)
    bad = [c for c in rows if not c["ok"]]
    log.info("health: %d checks, %d failing", len(rows), len(bad))
    return {"checks": rows, "failing": len(bad), "ok": not bad}
