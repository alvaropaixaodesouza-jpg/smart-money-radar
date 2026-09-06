"""Codex (defined.fi data) GraphQL API: server-side filtered new-token feed. Needs CODEX_API_KEY.

Docs: https://docs.codex.io  (api-reference/queries/filtertokens.md, concepts/rate-limits.md)
Endpoint: POST https://graph.codex.io/graphql, header "Authorization: <api_key>" (no Bearer for secret keys).

Why it is worth it: ONE request returns fresh tokens across all our networks already filtered by
createdAt / marketCap / liquidity, plus holder-quality signals (sniper/bundler/top10 %). No boost needed.

Budget: "Almost Free" plan = $1 one-time, 10,000 requests/month, 5 rps. Requests are counted per query
response. One filterTokens per poll at CODEX_MIN_INTERVAL_S=300 -> ~8,640/month, under the cap.
The source keeps a module-level cache so a faster loop does not burn extra requests.
"""
from __future__ import annotations

import logging
import time

import httpx

from ..config import settings
from ..models import QUOTE_DECIMALS, WSOL_MINT, NewToken, Trade, norm_addr
from .filters import filter_tokens

log = logging.getLogger(__name__)
ENDPOINT = "https://graph.codex.io/graphql"

# DexScreener/GeckoTerminal chainId -> Codex networkId (docs.codex.io/networks.md)
NETWORK_IDS = {"solana": 1399811149, "base": 8453, "robinhood": 4663, "ethereum": 1, "bsc": 56, "arbitrum": 42161}
ID_TO_CHAIN = {v: k for k, v in NETWORK_IDS.items()}

QUERY = """
query NewTokens($filters: TokenFilters, $rankings: [TokenRanking], $limit: Int) {
  filterTokens(filters: $filters, rankings: $rankings, limit: $limit) {
    results {
      marketCap circulatingMarketCap liquidity createdAt volume24 holders
      top10HoldersPercent sniperHeldPercentage bundlerHeldPercentage
      pair { address }
      exchanges { name }
      token { address symbol name networkId }
    }
  }
}
"""

MAKER_QUERY = """
query MakerEvents($query: MakerEventsQueryInput!, $limit: Int, $cursor: String) {
  getTokenEventsForMaker(query: $query, limit: $limit, cursor: $cursor) {
    cursor
    items {
      networkId eventType eventDisplayType timestamp transactionHash maker
      token0Address token1Address liquidityToken
      data { ... on SwapEventData { amount0 amount1 amountNonLiquidityToken priceUsd priceUsdTotal } }
    }
  }
}
"""

TOKEN_EVENTS_QUERY = """
query TokenEvents($query: EventsQueryInput!, $limit: Int, $cursor: String) {
  getTokenEvents(query: $query, limit: $limit, cursor: $cursor) {
    cursor
    items {
      maker eventDisplayType timestamp
      data { ... on SwapEventData { priceUsdTotal amountNonLiquidityToken } }
    }
  }
}
"""

_cache: tuple[float, list[NewToken]] | None = None


class CodexError(RuntimeError):
    pass


class Codex:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or settings.codex_api_key
        if not self.api_key:
            raise CodexError("CODEX_API_KEY is not set (sign up at dashboard.codex.io, $1 one-time)")
        self.http = client or httpx.Client(timeout=30, headers={"Authorization": self.api_key, "content-type": "application/json"})
        self.requests = 0

    def _post(self, query: str, variables: dict) -> dict:
        """One GraphQL call = one billed request. Raises CodexError with a readable message."""
        self.requests += 1
        r = self.http.post(ENDPOINT, json={"query": query, "variables": variables})
        if r.status_code in (401, 403):
            raise CodexError(f"codex auth failed ({r.status_code}): check CODEX_API_KEY")
        if r.status_code == 429:
            raise CodexError("codex rate limited (429): 5 req/s on the Almost Free plan")
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            msg = body["errors"][0].get("message", "")
            if "upgrade your plan" in msg.lower():
                raise CodexError(f"codex query not available on this plan: {msg}")
            raise CodexError(f"codex graphql error: {msg}")
        return body

    def filter_tokens(self, *, chains: tuple[str, ...], min_mcap: float, max_age_hours: float, min_liquidity: float, limit: int = 200) -> list[dict]:
        ids = [NETWORK_IDS[c] for c in chains if c in NETWORK_IDS]
        missing = [c for c in chains if c not in NETWORK_IDS]
        if missing:
            log.warning("codex: no network id for %s, skipped", missing)
        variables = {
            "filters": {
                "network": ids,
                "createdAt": {"gte": int(time.time() - max_age_hours * 3600)},
                "marketCap": {"gte": min_mcap, "lte": settings.new_token_max_mcap_usd},
                "liquidity": {"gte": min_liquidity},
            },
            "rankings": [{"attribute": "marketCap", "direction": "DESC"}],
            "limit": limit,
        }
        if settings.codex_exclude_potential_scam:
            variables["filters"]["potentialScam"] = False
        body = self._post(QUERY, variables)
        return ((body.get("data") or {}).get("filterTokens") or {}).get("results") or []

    # ---------- wallet tracking (replaces Helius; works on every supported chain) ----------

    def supports(self, chain: str) -> bool:
        return chain in NETWORK_IDS

    def maker_events(self, address: str, chain: str, since_ts: int | None = None,
                     max_pages: int | None = None) -> list[dict]:
        """Raw swap events made by `address`, newest first, paging until `since_ts` is reached.

        Costs one request per page. Budget: 10k requests/month on the Almost Free plan.
        """
        net = NETWORK_IDS.get(chain)
        if net is None:
            raise CodexError(f"codex: unknown chain {chain!r}")
        query: dict = {"maker": address, "networkId": net, "eventType": "Swap"}
        if since_ts:
            query["timestamp"] = {"from": int(since_ts), "to": int(time.time())}
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages or settings.codex_track_max_pages):
            body = self._post(MAKER_QUERY, {"query": query, "limit": settings.codex_track_page_limit, "cursor": cursor})
            blk = (body.get("data") or {}).get("getTokenEventsForMaker") or {}
            items = blk.get("items") or []
            out.extend(items)
            cursor = blk.get("cursor")
            if not cursor or not items:
                break
        return out

    def token_makers(self, mint: str, chain: str, limit: int | None = None) -> list[dict]:
        """Wallets that recently traded `mint`, aggregated by USD bought. One request.

        This is the fomo-free replacement for "top holders of a fresh token": whoever is
        buying a token that just crossed our mcap threshold is a discovery candidate.
        """
        net = NETWORK_IDS.get(chain)
        if net is None:
            raise CodexError(f"codex: unknown chain {chain!r}")
        body = self._post(TOKEN_EVENTS_QUERY, {
            "query": {"address": mint, "networkId": net, "eventType": "Swap"},
            "limit": limit or settings.codex_track_page_limit,
            "cursor": None,
        })
        items = ((body.get("data") or {}).get("getTokenEvents") or {}).get("items") or []
        return aggregate_makers(items)

    def get_trades(self, address: str, chain: str = "solana", since_ts: int | None = None) -> list[Trade]:
        events = self.maker_events(address, chain, since_ts)
        trades = [t for t in (parse_maker_event(e, address, chain) for e in events) if t]
        if since_ts:
            trades = [t for t in trades if t.ts > since_ts]
        return trades

    def new_tokens(self, min_mcap: float | None = None, max_age_hours: float | None = None) -> list[NewToken]:
        """Cached per CODEX_MIN_INTERVAL_S so the monthly request budget holds regardless of loop speed."""
        global _cache
        now = time.monotonic()
        if _cache and now - _cache[0] < settings.codex_min_interval_s:
            return _cache[1]
        min_mcap = settings.new_token_min_mcap_usd if min_mcap is None else min_mcap
        max_age_hours = settings.new_token_max_age_hours if max_age_hours is None else max_age_hours
        raw = self.filter_tokens(
            chains=settings.dex_chains, min_mcap=min_mcap, max_age_hours=max_age_hours,
            min_liquidity=settings.new_token_min_liquidity_usd,
        )
        tokens = filter_tokens([t for t in map(parse_result, raw) if t], min_mcap=min_mcap, max_age_hours=max_age_hours)
        _cache = (now, tokens)
        log.info("codex: %d results -> %d tokens (requests this process: %d)", len(raw), len(tokens), self.requests)
        return tokens


def _f(x) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def aggregate_makers(items: list[dict]) -> list[dict]:
    """Pure: swap events for one token -> per-wallet totals, best buyers first."""
    agg: dict[str, dict] = {}
    for e in items:
        maker = e.get("maker")
        if not maker:
            continue
        side = (e.get("eventDisplayType") or "").lower()
        usd = _f((e.get("data") or {}).get("priceUsdTotal")) or 0.0
        a = agg.setdefault(maker, {"address": maker, "buys": 0, "sells": 0, "usd_bought": 0.0, "usd_sold": 0.0})
        if side == "buy":
            a["buys"] += 1
            a["usd_bought"] += usd
        elif side == "sell":
            a["sells"] += 1
            a["usd_sold"] += usd
    out = [a for a in agg.values() if a["buys"]]
    out.sort(key=lambda a: -a["usd_bought"])
    return out


def parse_maker_event(e: dict, address: str, chain: str | None = None) -> Trade | None:
    """Pure: one maker Swap event -> Trade.

    Codex gives us what Helius did not: `eventDisplayType` (Buy/Sell) and `priceUsdTotal`.
    `liquidityToken` is the quote side of the pool, so the other token is the traded one.
    Amount signs: positive = left the wallet, negative = entered it.
    Token-to-token swaps (no known liquidity token) are skipped, same as the Helius parser.
    """
    sig = e.get("transactionHash")
    ts = e.get("timestamp")
    data = e.get("data") or {}
    if not sig or not ts or not data:
        return None
    side_raw = (e.get("eventDisplayType") or "").lower()
    if side_raw not in ("buy", "sell"):
        return None
    quote = e.get("liquidityToken")
    t0, t1 = e.get("token0Address"), e.get("token1Address")
    if not quote or quote not in (t0, t1):
        return None
    mint = t1 if quote == t0 else t0
    if not mint:
        return None
    quote_amount_raw = data.get("amount0") if quote == t0 else data.get("amount1")
    decimals = QUOTE_DECIMALS.get(quote)
    sol_amount = None
    if quote == WSOL_MINT and decimals is not None:
        try:
            sol_amount = abs(float(quote_amount_raw)) / (10 ** decimals)
        except (TypeError, ValueError):
            sol_amount = None
    chain = chain or ID_TO_CHAIN.get(e.get("networkId"), "solana")
    return Trade(
        sig=sig,
        address=address,
        chain=chain,
        mint=norm_addr(mint),
        side=side_raw,
        sol_amount=sol_amount,
        token_amount=_f(data.get("amountNonLiquidityToken")),
        usd_value=_f(data.get("priceUsdTotal")),
        ts=int(ts),
        source="codex",
    )


def parse_result(r: dict) -> NewToken | None:
    """Pure: one TokenFilterResult -> NewToken."""
    tok = r.get("token") or {}
    addr = tok.get("address")
    if not addr:
        return None
    chain = ID_TO_CHAIN.get(tok.get("networkId"), str(tok.get("networkId")))
    ex = r.get("exchanges") or []
    return NewToken(
        mint=norm_addr(addr),
        chain=chain,
        symbol=tok.get("symbol"),
        mcap_usd=_f(r.get("marketCap")) or _f(r.get("circulatingMarketCap")),
        liquidity_usd=_f(r.get("liquidity")),
        created_at=int(r["createdAt"]) if r.get("createdAt") else None,
        dex=(ex[0].get("name") if ex else None),
        pair_address=(r.get("pair") or {}).get("address"),
        source="codex",
        holders=r.get("holders"),
        top10_pct=_f(r.get("top10HoldersPercent")),
        sniper_pct=_f(r.get("sniperHeldPercentage")),
        bundler_pct=_f(r.get("bundlerHeldPercentage")),
    )
