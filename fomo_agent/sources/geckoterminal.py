"""GeckoTerminal public API (no key, 30 req/min). Covers tokens that never bought a DexScreener boost.

Endpoints (public, documented at https://apiguide.geckoterminal.com):
  GET /networks/{network}/new_pools?page=N              -> newest pools (20/page, mostly dust)
  GET /networks/{network}/trending_pools?duration=5m|1h|6h|24h
  GET /networks/{network}/pools?sort=h24_volume_usd_desc -> top pools by volume
All accept include=base_token. Network ids for solana/base/robinhood match DexScreener chainIds.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Iterable

import httpx

from ..config import settings
from ..models import NewToken, norm_addr
from ..ratelimit import RateLimiter
from .filters import filter_tokens

log = logging.getLogger(__name__)
BASE = "https://api.geckoterminal.com/api/v2"

# DexScreener chainId -> GeckoTerminal network id, only where they differ
NETWORK_MAP = {"ethereum": "eth", "bsc": "bsc", "arbitrum": "arbitrum", "polygon": "polygon_pos"}


def feed_path(feed: str) -> tuple[str, dict]:
    """'trending_6h' | 'trending_1h' | 'new' | 'new_2' | 'top_volume' -> (path suffix, params)."""
    if feed.startswith("trending"):
        dur = feed.split("_", 1)[1] if "_" in feed else "6h"
        return "/trending_pools", {"duration": dur}
    if feed.startswith("new"):
        page = int(feed.split("_", 1)[1]) if "_" in feed else 1
        return "/new_pools", {"page": page}
    if feed == "top_volume":
        return "/pools", {"sort": "h24_volume_usd_desc"}
    raise ValueError(f"unknown gecko feed: {feed}")


class GeckoTerminal:
    def __init__(self, client: httpx.Client | None = None, chains: Iterable[str] | None = None, feeds: Iterable[str] | None = None):
        self.http = client or httpx.Client(base_url=BASE, timeout=20, headers={"accept": "application/json"})
        self.chains = tuple(chains) if chains else settings.dex_chains
        self.feeds = tuple(feeds) if feeds else settings.gecko_feeds
        self.limiter = RateLimiter(settings.gecko_max_req_per_min, "geckoterminal")
        self._last = 0.0

    def pools(self, chain: str, feed: str) -> list[dict]:
        network = NETWORK_MAP.get(chain, chain)
        suffix, params = feed_path(feed)
        params["include"] = "base_token"
        for attempt in range(3):
            gap = settings.gecko_min_interval_s - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self.limiter.wait()
            r = self.http.get(f"/networks/{network}{suffix}", params=params)
            self._last = time.monotonic()
            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                log.warning("geckoterminal 429 on %s %s; backing off %ds", chain, feed, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json().get("data", [])
        log.warning("geckoterminal %s %s: still 429 after retries, skipping", chain, feed)
        return []

    def new_tokens(self, min_mcap: float | None = None, max_age_hours: float | None = None) -> list[NewToken]:
        min_mcap = settings.new_token_min_mcap_usd if min_mcap is None else min_mcap
        max_age_hours = settings.new_token_max_age_hours if max_age_hours is None else max_age_hours
        tokens: list[NewToken] = []
        for chain in self.chains:
            for feed in self.feeds:
                try:
                    tokens.extend(t for t in map(parse_pool, self.pools(chain, feed)) if t)
                except httpx.HTTPError as e:
                    log.warning("geckoterminal %s %s failed: %s", chain, feed, e)
        return filter_tokens(tokens, min_mcap=min_mcap, max_age_hours=max_age_hours)


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def parse_pool(p: dict) -> NewToken | None:
    """Pure: GeckoTerminal pool object -> NewToken. mcap falls back to FDV (mcap is often null)."""
    a = p.get("attributes") or {}
    rel = ((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}
    tid = rel.get("id") or ""          # "<network>_<address>"
    network, _, address = tid.partition("_")
    if not address:
        return None
    created = a.get("pool_created_at")
    try:
        created_ts = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) if created else None
    except ValueError:
        created_ts = None
    name = a.get("name") or ""
    symbol = name.split(" / ")[0].strip() if " / " in name else name or None
    chain = next((k for k, v in NETWORK_MAP.items() if v == network), network)
    return NewToken(
        mint=norm_addr(address),
        chain=chain,
        symbol=symbol,
        mcap_usd=_f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")),
        liquidity_usd=_f(a.get("reserve_in_usd")),
        created_at=created_ts,
        dex=((p.get("relationships") or {}).get("dex") or {}).get("data", {}).get("id"),
        pair_address=(a.get("address")),
        source="geckoterminal",
    )
