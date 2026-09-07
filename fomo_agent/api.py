"""HTTP API over the watchlist — the one brain the site, the bot and any future client read from.

Everything the product knows already lives in `pipeline/analyze.py`; this exposes it over HTTP so
the Astro front end can render pages on the server and so a third party can eventually build on it.
No business logic lives here: a route reads a query string, calls the same function the terminal
calls, and returns the dict. Anything else would be a second definition of the truth.

One thing it does add: a token nobody on the watchlist has touched is still a fair question. Rather
than answering "unknown", the token route falls back to a live DexScreener lookup, stores what comes
back, and says plainly that no tracked wallet is in it. That is the difference between a list and a
tool.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .config import settings
from .pipeline import analyze
from .sources.rpc import QUOTE_TOKENS

log = logging.getLogger(__name__)

ADDRESS_LEN = 42


def chain() -> str | None:
    """Read the chain from settings on every call rather than caching it at import.

    A module-level global set during startup silently becomes None in any context that does not
    run the lifespan — tests, a script, a worker — and a None chain quietly widens every query.
    """
    return settings.dex_chains[0] if settings.dex_chains else None


# ---------------------------------------------------------------- plumbing

def get_conn() -> sqlite3.Connection:
    """One connection per request. sqlite is fast enough that pooling buys nothing here."""
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


class RateLimit:
    """A fixed window per client address. Enough to stop a scraper, cheap enough to ignore."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True


limiter = RateLimit(settings.api_rate_per_min)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) FROM traders WHERE score IS NOT NULL").fetchone()[0]
    conn.close()
    log.info("api up: chain=%s, %d scored traders", chain(), n)
    yield


app = FastAPI(
    title="FOMO Radar",
    version="0.1.0",
    summary="Which fomo.family traders actually know what they are doing.",
    description=(
        "Read-only research over Robinhood Chain. Every trader here was resolved from a fomo "
        "profile to a real on-chain wallet, tracked, and judged by Claude. Nothing on this API "
        "places a trade, and none of it is financial advice."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.api_cors_origins) or ["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    client = request.headers.get("x-forwarded-for", "").split(",")[0].strip() \
        or (request.client.host if request.client else "?")
    if not limiter.check(client):
        return JSONResponse({"error": "rate limited", "limit_per_minute": limiter.per_minute},
                            status_code=429)
    return await call_next(request)


# ---------------------------------------------------------------- shaping

def trader_row(r: dict) -> dict:
    """One leaderboard entry, with the tags already unpacked for the client."""
    import json

    tags = json.loads(r["tags"]) if r.get("tags") else {}
    return {
        "handle": r["handle"], "address": r["address"], "score": r["score"],
        "status": r["status"], "summary": r["summary"], "fomo_pnl": r["fomo_pnl"],
        "style": tags.get("style") or [], "red_flags": tags.get("red_flags") or [],
    }


def live_lookup(conn: sqlite3.Connection, mint: str) -> bool:
    """Name a token we have never seen, so an unknown address still gets a real answer.

    One free DexScreener request. Returns True when something was learned.
    """
    from .sources.dexscreener import DexScreener, parse_pair

    try:
        pairs = DexScreener().pairs_for(chain() or "robinhood", [mint])
    except Exception as e:  # noqa: BLE001 - an unknown token is still answerable without this
        log.warning("live lookup for %s failed: %s", mint[:10], e)
        return False
    for p in pairs:
        t = parse_pair(p)
        if t and t.mint.lower() == mint.lower():
            with db.tx(conn):
                db.upsert_token(conn, t.mint, chain=t.chain, symbol=t.symbol, mcap_usd=t.mcap_usd,
                                liquidity_usd=t.liquidity_usd, created_at=t.created_at)
            return True
    return False


# ---------------------------------------------------------------- routes

@app.get("/api/health", tags=["meta"])
def health(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    last = conn.execute("SELECT MAX(ts) FROM trades").fetchone()[0]
    return {"ok": True, "chain": chain(), "last_fill_ts": last,
            "stale_seconds": (db.now() - last) if last else None}


@app.get("/api/stats", tags=["meta"])
def stats(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """The numbers the masthead shows. Quote assets are excluded from the fill count."""
    not_quote = analyze.NOT_QUOTE.format(col="mint")

    def one(sql: str, *args):
        return conn.execute(sql, args).fetchone()[0]

    return {
        "traders": one("SELECT COUNT(*) FROM traders"),
        "scored": one("SELECT COUNT(*) FROM traders WHERE score IS NOT NULL"),
        "active": one("SELECT COUNT(*) FROM traders WHERE status=?", "active"),
        "watch": one("SELECT COUNT(*) FROM traders WHERE status=?", "watch"),
        "dropped": one("SELECT COUNT(*) FROM traders WHERE status=?", "dropped"),
        "fills": one(f"SELECT COUNT(*) FROM trades WHERE 1=1{not_quote}"),
        "positions": one("SELECT COUNT(*) FROM fomo_positions"),
        "open_pnl": one("SELECT SUM(unrealized_pnl) FROM fomo_positions") or 0,
        "tokens": one("SELECT COUNT(*) FROM tokens WHERE symbol IS NOT NULL"),
        "updated_ts": one("SELECT MAX(ts) FROM trades"),
    }


@app.get("/api/activity", tags=["meta"])
def activity(
    hours: int = Query(48, ge=6, le=336),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Fills per hour by trusted wallets — the pulse the masthead draws as a sparkline.

    Empty hours are returned as zeros rather than skipped, otherwise a quiet night reads as a
    gap in the chart instead of as quiet.
    """
    since = db.now() - hours * 3600
    rows = dict(conn.execute(
        "SELECT CAST((tr.ts - ?) / 3600 AS INTEGER) bucket, COUNT(*) n "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        "WHERE tr.ts >= ? AND t.score >= ?" + analyze.NOT_QUOTE.format(col="tr.mint") +
        " GROUP BY bucket", (since, since, analyze.TRUSTED)).fetchall())
    series = [rows.get(i, 0) for i in range(hours)]
    return {"hours": hours, "series": series, "total": sum(series), "peak": max(series or [0])}


@app.get("/api/distribution", tags=["meta"])
def distribution(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    """How the roster's scores are shaped — ten buckets of ten points each."""
    buckets = [0] * 10
    for (score,) in conn.execute("SELECT score FROM traders WHERE score IS NOT NULL"):
        buckets[min(int(score) // 10, 9)] += 1
    return {"buckets": buckets, "total": sum(buckets), "peak": max(buckets)}


@app.get("/api/signals", tags=["signals"])
def signals(
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(40, ge=1, le=100),
    min_buyers: int = Query(2, ge=2, le=50),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Tokens several trusted wallets bought in the window, ranked by conviction.

    Conviction is the sum of each buyer's (score/100)^2 — it answers *whose* money moved rather
    than how many wallets did, because anyone can open a wallet.
    """
    rows = analyze.signals(conn, chain(), hours=hours, min_buyers=min_buyers, limit=limit)
    for r in rows:
        r["who"] = [h for h in (r.get("who") or "").split(",") if h]
        r["scores"] = [int(s) for s in (r.get("scores") or "").split(",") if s]
    return {"hours": hours, "count": len(rows), "signals": rows}


@app.get("/api/tape", tags=["signals"])
def tape(
    limit: int = Query(60, ge=1, le=200),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Every recent fill by a wallet scoring 60+, newest first."""
    rows = [dict(r) for r in conn.execute(
        "SELECT tr.ts, tr.side, tr.usd_value usd, tr.mint, t.fomo_handle handle, t.score, "
        "  COALESCE(tk.symbol, substr(tr.mint,1,8)) sym "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        "LEFT JOIN tokens tk ON tk.mint = tr.mint "
        "WHERE t.score >= ? AND tr.usd_value IS NOT NULL"
        + analyze.NOT_QUOTE.format(col="tr.mint") +
        " ORDER BY tr.ts DESC LIMIT ?", (analyze.TRUSTED, limit))]
    return {"count": len(rows), "fills": rows}


@app.get("/api/leaderboard", tags=["traders"])
def leaderboard(
    status: str = Query("active", pattern="^(active|watch|dropped|all)$"),
    limit: int = Query(50, ge=1, le=400),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Our own ranking — by judgement of the process, not by the headline PnL fomo shows."""
    rows = [trader_row(r) for r in analyze.leaderboard(conn, limit, status)]
    return {"status": status, "count": len(rows), "traders": rows}


@app.get("/api/trader/{who}", tags=["traders"])
def trader(
    who: str,
    hours: int = Query(168, ge=1, le=8760),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """A handle or a wallet address: the verdict, the open book, recent fills, the company kept."""
    a = analyze.analyze_trader(conn, who, hours)
    if a is None:
        raise HTTPException(404, f"no trader matches {who!r}")
    return a


@app.get("/api/token/{mint}", tags=["tokens"])
def token(
    mint: str,
    hours: int = Query(48, ge=1, le=720),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """Whose money is in this token, what it cost them, and who traded it in the window.

    An address nobody tracked has touched still gets an answer: we look it up live, then say so.
    """
    if not (mint.startswith("0x") and len(mint) == ADDRESS_LEN) and len(mint) < 32:
        raise HTTPException(400, "that is not a token address")
    a = analyze.analyze_token(conn, mint, hours)
    if a["symbol"] is None and not a["holders"] and not a["flow"]:
        if live_lookup(conn, a["mint"]):
            a = analyze.analyze_token(conn, mint, hours)
    a["tracked"] = bool(a["holders"] or a["flow"])
    a["is_quote"] = a["mint"] in QUOTE_TOKENS
    return a


@app.get("/api/search", tags=["meta"])
def search(
    q: str = Query(..., min_length=1, max_length=64),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    """One box for both questions: an address or a handle, resolved to where it should go."""
    q = q.strip()
    row = analyze.find_trader(conn, q)
    if row:
        return {"kind": "trader", "handle": row["fomo_handle"], "address": row["address"]}
    if q.startswith("0x") and len(q) == ADDRESS_LEN:
        return {"kind": "token", "address": q.lower()}
    like = f"%{q}%"
    hits = [dict(r) for r in conn.execute(
        "SELECT fomo_handle handle, address, score, status FROM traders "
        "WHERE fomo_handle LIKE ? AND score IS NOT NULL ORDER BY score DESC LIMIT 10", (like,))]
    tokens = [dict(r) for r in conn.execute(
        "SELECT mint, symbol FROM tokens WHERE symbol LIKE ? LIMIT 10", (like,))]
    if not hits and not tokens:
        return {"kind": "none", "query": q}
    return {"kind": "suggestions", "traders": hits, "tokens": tokens}


def serve(host: str | None = None, port: int | None = None, reload: bool = False) -> None:
    import uvicorn

    # The reloader must watch the package and nothing else. Pointed at the working directory it
    # also watches fomo_agent.db-wal, which sqlite rewrites on every read — the service then
    # restarts in a loop and drops requests mid-flight, which looks exactly like flaky data.
    uvicorn.run("fomo_agent.api:app", host=host or settings.api_host,
                port=port or settings.api_port, reload=reload,
                reload_dirs=["fomo_agent"] if reload else None)
