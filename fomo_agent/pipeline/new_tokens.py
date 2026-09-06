"""Poll DexScreener for fresh high-mcap tokens; on a new one, trigger discover_holders (if fomo works)."""
from __future__ import annotations

import logging
import sqlite3

from .. import db
from ..config import settings
from ..models import NewToken
from ..sources.dexscreener import DexScreener, parse_pair
from ..sources.filters import filter_tokens
from ..sources.fomo import FomoClient, FomoError
from .discover import discover_holders, discover_makers

log = logging.getLogger(__name__)


def fetch_new_tokens(sources: tuple[str, ...] | None = None) -> list[NewToken]:
    """Union of all configured sources, deduped per (chain, mint), thresholds from config."""
    tokens: list[NewToken] = []
    for name in sources or settings.token_sources:
        try:
            if name == "dexscreener":
                tokens.extend(DexScreener().new_tokens())
            elif name == "geckoterminal":
                from ..sources.geckoterminal import GeckoTerminal
                tokens.extend(GeckoTerminal().new_tokens())
            elif name == "codex":
                if not settings.codex_api_key:
                    log.debug("codex source skipped: CODEX_API_KEY not set")
                    continue
                from ..sources.codex import Codex
                tokens.extend(Codex().new_tokens())
            else:
                log.warning("unknown token source %r", name)
        except Exception as e:  # noqa: BLE001 - one source down must not kill the trigger
            log.warning("token source %s failed: %s", name, e)
    return filter_tokens(
        tokens, min_mcap=settings.new_token_min_mcap_usd, max_age_hours=settings.new_token_max_age_hours,
        min_liquidity=settings.new_token_min_liquidity_usd, max_mcap=settings.new_token_max_mcap_usd,
    )


def enrich_tokens(conn: sqlite3.Connection, dex: DexScreener | None = None, limit: int = 300) -> dict:
    """Fill in symbol, price and liquidity for tokens we only know by address.

    Positions and fills both arrive as bare contract addresses. DexScreener resolves 30 at a time
    for free, so a few hundred tokens cost a handful of requests and no Codex budget. Fills come
    first: an unnamed token on the signal feed is the one a reader is looking at right now.
    """
    dex = dex or DexScreener()
    rows = conn.execute(
        "SELECT u.token AS token, u.chain AS chain FROM ("
        "  SELECT mint AS token, chain, MAX(ts) AS seen FROM trades GROUP BY mint, chain"
        "  UNION ALL SELECT token, chain, 0 FROM fomo_positions"
        ") u LEFT JOIN tokens t ON t.mint = u.token "
        "WHERE u.chain IS NOT NULL AND (t.mint IS NULL OR t.symbol IS NULL) "
        "GROUP BY u.token, u.chain ORDER BY MAX(u.seen) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    by_chain: dict[str, list[str]] = {}
    for r in rows:
        by_chain.setdefault(r["chain"], []).append(r["token"])

    stats = {"looked_up": len(rows), "named": 0, "requests": 0}
    for chain, mints in by_chain.items():
        try:
            pairs = dex.pairs_for(chain, mints)
            stats["requests"] += (len(mints) + 29) // 30
        except Exception as e:  # noqa: BLE001 - enrichment is optional
            log.warning("token enrichment failed for %s: %s", chain, e)
            continue
        seen: set[str] = set()
        for p in pairs:
            t = parse_pair(p)
            if not t or t.mint in seen:
                continue
            seen.add(t.mint)
            with db.tx(conn):
                db.upsert_token(conn, t.mint, chain=t.chain, symbol=t.symbol, mcap_usd=t.mcap_usd,
                                liquidity_usd=t.liquidity_usd, created_at=t.created_at)
            stats["named"] += 1
    log.info("token enrichment: %s", stats)
    return stats


def poll_new_tokens(conn: sqlite3.Connection, dex: DexScreener | None = None, fomo: FomoClient | None = None,
                    discover: bool | None = None) -> dict:
    """Store fresh tokens; for the biggest new ones, pull their buyers as trader candidates."""
    found = dex.new_tokens() if dex else fetch_new_tokens()
    stats = {"checked": len(found), "new": 0, "holders_runs": 0, "maker_runs": 0, "candidates": 0, "errors": 0}
    fresh: list[NewToken] = []
    for t in found:
        with db.tx(conn):
            created = db.upsert_token(
                conn, t.mint, chain=t.chain, symbol=t.symbol, mcap_usd=t.mcap_usd,
                liquidity_usd=t.liquidity_usd, created_at=t.created_at,
            )
        if not created:
            continue
        stats["new"] += 1
        fresh.append(t)
        log.info("new token %s %s (%s) mcap=%.0f", t.chain, t.symbol, t.mint[:8], t.mcap_usd or 0)
        if fomo is not None:
            try:
                discover_holders(conn, t.mint, fomo)
                stats["holders_runs"] += 1
            except FomoError as e:
                stats["errors"] += 1
                log.warning("holders for %s failed: %s", t.mint[:8], e)

    # Codex discovery on the biggest fresh tokens (1 request each, so it is capped)
    use_codex = settings.codex_api_key and "codex" in settings.token_sources if discover is None else discover
    if use_codex:
        fresh.sort(key=lambda t: -(t.mcap_usd or 0))
        for t in fresh[: settings.discover_tokens_per_pass]:
            try:
                r = discover_makers(conn, t.mint, t.chain)
                stats["maker_runs"] += 1
                stats["candidates"] += r["new"]
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("makers for %s failed: %s", t.mint[:8], e)
    return stats
