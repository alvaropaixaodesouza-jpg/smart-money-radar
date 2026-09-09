"""Thin, isolated adapter to fomo.family's INTERNAL API.

Every path, parameter and field name below comes from `docs/fomo-endpoints.md`, produced
in phase 0 from a HAR recorded in a real browser. Nothing here is guessed.

Two things this adapter exists for:
  1. discovery - who is winning (leaderboard, top holders of a token),
  2. address resolution - the wallet a trader actually swaps from.

Point 2 is the phase-0 gotcha: a fomo profile's `address` / `evmAddress` are NOT the wallets
that execute trades. Swaps come from a separate per-user execution wallet on each chain, and
the only way to learn it is `/v2/users/{id}/swaps`. On-chain tracking must use those.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from ..config import settings
from ..models import HolderRow, TraderRow, norm_addr

log = logging.getLogger(__name__)

# fomo networkId -> our chain name (see docs/fomo-endpoints.md)
NETWORKS = {1399811149: "solana", 4663: "robinhood", 8453: "base", 56: "bsc", 1: "ethereum"}
# stablecoins and wrapped natives: everyone touches them, so they identify nobody
QUOTE_TOKENS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",          # USDC (solana)
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",          # USDT (solana)
    "So11111111111111111111111111111111111111112",           # wSOL
    "0x4200000000000000000000000000000000000006",            # WETH (OP-stack)
}
PERIODS = {"24h": "24h", "7d": "7d", "30d": "30d"}

# (method, path template, default query params). Placeholders are filled from call kwargs.
ENDPOINTS: dict[str, tuple[str, str, dict[str, Any]] | None] = {
    "leaderboard": ("GET", "/v2/leaderboard/{period}", {}),
    "leaderboard_default": ("GET", "/v2/leaderboard", {}),
    "token_holders": ("GET", "/hodlers/top", {"tokens": "{tokens}"}),
    "token_devs": ("GET", "/hodlers/devs", {"tokenAddress": "{mint}", "networkId": "{network_id}"}),
    "user": ("GET", "/v2/users/{user_id}", {}),
    "user_by_handle": ("GET", "/v2/users/userHandle/{handle}", {}),
    "user_swaps": ("GET", "/v2/users/{user_id}/swaps", {}),
    "user_leaderboard": ("GET", "/v2/users/{user_id}/leaderboard", {}),
    "trades": ("GET", "/trades", {"userId": "{user_id}", "orderBy": "{order_by}"}),
}


class FomoError(RuntimeError):
    pass


class FomoNotConfigured(FomoError):
    pass


class FomoSessionExpired(FomoError):
    pass


class FomoEdgeBlocked(FomoError):
    """Cloudflare blocked the client itself, not the credentials. Server-side calls cannot work."""


class FomoClient:
    """1 req/s, retry with backoff, 5-min response cache, clear error when the session dies."""

    def __init__(self, session: str | None = None, base_url: str | None = None, client: httpx.Client | None = None):
        self.session = session if session is not None else settings.fomo_session
        self.base_url = base_url or settings.fomo_base_url or "https://prod-api.fomo.family"
        if not self.session:
            raise FomoNotConfigured(
                "FOMO_SESSION is empty. Record it with scripts/fomo_session_from_curl.py "
                "(see docs/fomo-endpoints.md)."
            )
        headers = {
            "accept": "application/json",
            "origin": "https://fomo.family",
            "referer": "https://fomo.family/",
            "user-agent": settings.fomo_user_agent,
            # the web app always sends this; the API filters results by it
            "x-supported-chains": settings.fomo_supported_chains,
        }
        if settings.fomo_auth_kind == "bearer":
            headers["authorization"] = f"Bearer {self.session}"
        else:
            headers["cookie"] = self.session
        self.http = client or httpx.Client(base_url=self.base_url, headers=headers, timeout=25)
        self._last_call = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}
        self.cache_ttl = 300
        self.requests = 0

    def _throttle(self) -> None:
        gap = 1.0 / max(settings.fomo_rps, 0.01)
        wait = self._last_call + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def call(self, name: str, **fmt: Any) -> Any:
        spec = ENDPOINTS.get(name)
        if spec is None:
            raise FomoNotConfigured(f"endpoint {name!r} is not documented in docs/fomo-endpoints.md")
        method, path, params = spec
        path = path.format(**fmt)
        params = {k: (v.format(**fmt) if isinstance(v, str) else v) for k, v in params.items()}
        params.update(fmt.get("extra_params") or {})
        key = f"{method} {path} {sorted(params.items())}"
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cache_ttl:
            return hit[1]
        last_status = None
        for attempt in range(3):
            self._throttle()
            self.requests += 1
            r = self.http.request(method, path, params=params)
            last_status = r.status_code
            if r.status_code in (430, 431) and r.headers.get("server") == "cloudflare":
                raise FomoEdgeBlocked(
                    f"Cloudflare rejected the request at the edge ({r.status_code} {r.text[:40]}, "
                    f"cf-ray {r.headers.get('cf-ray')}). No header combination gets past this - not even "
                    "the exact cURL Chrome generates. Use the browser export instead: "
                    "scripts/fomo_export.js + `cli fomo-import`. See docs/fomo-endpoints.md."
                )
            if r.status_code in (401, 403, 430, 431):
                raise FomoSessionExpired(
                    f"fomo returned {r.status_code} ({r.text[:80]}). The Privy token lives ~1 hour; "
                    "refresh it with scripts/fomo_session_from_curl.py"
                )
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("success") is False:
                raise FomoError(f"fomo {name}: {data.get('message')}")
            self._cache[key] = (time.monotonic(), data)
            return data
        raise FomoError(f"fomo {name}: gave up after retries (last status {last_status})")

    # ---------- public API ----------

    def leaderboard(self, period: str = "7d", limit: int | None = None) -> list[TraderRow]:
        if period not in PERIODS:
            raise ValueError(f"period must be one of {sorted(PERIODS)}")
        rows = parse_leaderboard(self.call("leaderboard", period=period), period)
        return rows[: (limit or settings.leaderboard_limit)]

    def token_holders(self, mint: str, network_id: int = 1399811149, limit: int | None = None) -> list[HolderRow]:
        tokens = json.dumps([{"address": mint, "networkId": network_id}], separators=(",", ":"))
        rows = parse_holders(self.call("token_holders", tokens=tokens), mint)
        return rows[: (limit or settings.holders_top_n)]

    def user_by_handle(self, handle: str) -> dict:
        return (self.call("user_by_handle", handle=handle) or {}).get("responseObject") or {}

    def user_swaps(self, user_id: str) -> list[dict]:
        payload = self.call("user_swaps", user_id=user_id)
        return ((payload or {}).get("responseObject") or {}).get("swaps") or []

    def execution_addresses(self, user_id: str) -> dict[str, str]:
        """chain -> the wallet this trader actually swaps from. See the module docstring."""
        return parse_execution_addresses(self.user_swaps(user_id))


# ---------- pure parsers (fixture-testable) ----------

def _resp(payload: Any) -> Any:
    return (payload or {}).get("responseObject") if isinstance(payload, dict) else None


def _pnl(entry: dict, period: str) -> float | None:
    """Leaderboard entries carry the period's PnL under pnl24h / pnl7d / pnl30d."""
    for key in (f"pnl{period}", "pnl30d", "pnl7d", "pnl24h", "pnl"):
        if entry.get(key) is not None:
            return float(entry[key])
    return None


def parse_leaderboard(payload: Any, period: str) -> list[TraderRow]:
    ro = _resp(payload) or {}
    out = []
    for e in ro.get("leaderboard") or []:
        if not e.get("id"):
            continue
        pnl = _pnl(e, period)
        out.append(TraderRow(
            # profile addresses are NOT trackable on-chain; kept for reference only
            address=e.get("address") or e["id"],
            fomo_user_id=e["id"],
            fomo_handle=e.get("userHandle") or e.get("displayName"),
            profile_address=e.get("address"),
            evm_address=norm_addr(e["evmAddress"]) if e.get("evmAddress") else None,
            pnl_24h=pnl if period == "24h" else None,
            pnl_7d=pnl if period == "7d" else None,
            pnl_30d=pnl if period == "30d" else None,
            trades_cnt=e.get("numTrades"),
            volume_usd=e.get("totalVolume"),
            source=f"leaderboard_{period}",
        ))
    return out


# The keys a thesis body has been seen under, most likely first. `comment` is documented as
# optional and turned out not to be a string at all but an object — the note plus its metadata —
# so this reads the text out of whichever field holds it and refuses to guess at the rest.
THESIS_KEYS = ("text", "comment", "body", "content", "thesis", "message", "description")
_warned_shapes: set[str] = set()


def thesis_text(raw: Any) -> str | None:
    """The note a trader attached to a position, out of a field whose shape fomo may change.

    Returns None for anything unrecognised rather than raising. A field we cannot read is a
    missing thesis; it must never be a rejected collection, which is what it was once.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, dict):
        for k in THESIS_KEYS:
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # Say what we saw, once per shape, so the next collection names the field for us instead
        # of another round of guessing.
        shape = ",".join(sorted(raw))
        if shape not in _warned_shapes:
            _warned_shapes.add(shape)
            log.warning("thesis field carries no text under %s; keys were: %s",
                        "/".join(THESIS_KEYS), shape)
    return None


def parse_holders(payload: Any, mint: str) -> list[HolderRow]:
    ro = _resp(payload)
    tokens = ro if isinstance(ro, list) else []
    out = []
    for tok in tokens:
        if tok.get("tokenAddress") and norm_addr(tok["tokenAddress"]) != norm_addr(mint):
            continue
        for h in tok.get("topHolders") or []:
            u = h.get("user") or {}
            if not u.get("id"):
                continue
            out.append(HolderRow(
                address=u.get("address") or u["id"],
                fomo_user_id=u["id"],
                fomo_handle=u.get("userHandle") or u.get("displayName"),
                profile_address=u.get("address"),
                evm_address=norm_addr(u["evmAddress"]) if u.get("evmAddress") else None,
                mint=mint,
                pnl_usd=h.get("pnl"),
                realized_pnl_usd=h.get("realizedPnl"),
                cost_basis_usd=h.get("costBasis"),
                value_usd=h.get("value"),
                balance=h.get("humanAmount"),
                avg_hold_seconds=h.get("averageHoldTimeSeconds"),
                is_dev=bool(h.get("isDev")),
                trade_id=h.get("tradeId"),
                thesis=thesis_text(h.get("comment")),
            ))
    return out


def parse_top_holdings(payload: Any) -> list[dict]:
    """Per-token positions, taken straight out of the leaderboard response.

    Each leaderboard entry carries `topHoldings` — the trader's three largest open positions with
    amount, mark price, value and PnL. That is the same per-token detail `/trades` would give, at
    no extra request, and `/trades` cannot replace it: it requires a `tokenAddress` and answers 400
    without one, so it only ever describes a position you already know about.

    Cost basis and entry price are derived: value minus PnL is what was paid.
    """
    ro = _resp(payload) or {}
    out = []
    for u in ro.get("leaderboard") or []:
        user_id = u.get("id")
        if not user_id:
            continue
        for h in u.get("topHoldings") or []:
            token = h.get("tokenAddress")
            if not token:
                continue
            value, pnl = _num(h.get("value")), _num(h.get("pnl"))
            amount = _num(h.get("humanAmount"))
            cost = (value - pnl) if value is not None and pnl is not None else None
            # A holding's `pnl` sometimes exceeds its whole value, which means it also counts
            # profit already taken out of the position. Cost basis cannot be recovered then, and
            # guessing a negative one would poison every multiple computed from it.
            if cost is not None and cost <= 0:
                cost = None
            out.append({
                "trade_id": f"h:{user_id}:{norm_addr(token)}",
                "user_id": user_id,
                "chain": NETWORKS.get(h.get("networkId")),
                "token": norm_addr(token),
                "symbol": None,
                "opened_at": None,
                "closed_at": None,
                "amount": amount,
                "avg_entry": (cost / amount) if cost is not None and amount else None,
                "avg_exit": None,
                "realized_pnl": None,
                "unrealized_pnl": pnl,
                "cost_basis": cost,
                "current_price": _num(h.get("price")),
                "liquidity": None,
            })
    return out


def parse_trade_rows(user_id: str, payload: Any) -> list[dict]:
    """`/trades?userId=` -> one row per position.

    This is the richest thing fomo exposes: per-token realized AND unrealized PnL, average entry
    and exit, cost basis and whether the position is still open. The leaderboard only gives one
    aggregate number per trader; this says which tokens produced it.

    The list endpoint was only ever seen returning 304 in the capture, so the shape is accepted
    loosely: a bare list, a `trades` list, or items wrapped in `{"trade": {...}}`.
    """
    ro = payload.get("responseObject") if isinstance(payload, dict) else payload
    if isinstance(ro, dict):
        ro = ro.get("trades") or ro.get("items") or ([ro["trade"]] if "trade" in ro else [])
    if not isinstance(ro, list):
        return []

    out = []
    for item in ro:
        t = item.get("trade") if isinstance(item, dict) and "trade" in item else item
        if not isinstance(t, dict) or not t.get("id") or not t.get("tokenAddress"):
            continue
        meta = t.get("tokenMetadata") or {}
        out.append({
            "trade_id": t["id"],
            "user_id": user_id,
            "chain": NETWORKS.get(t.get("networkId") or meta.get("networkId")),
            "token": norm_addr(t["tokenAddress"]),
            "symbol": meta.get("symbol"),
            "opened_at": _ts(t.get("createdAt")),
            "closed_at": _ts(t.get("closedAt")),
            "amount": _num(t.get("humanTokenAmount")),
            "avg_entry": _num(t.get("avgEntryPrice")),
            "avg_exit": _num(t.get("avgExitPrice")),
            "realized_pnl": _num(t.get("realizedPnlUsd")),
            "unrealized_pnl": _num(t.get("unrealizedPnlUsd")),
            "cost_basis": _num(t.get("totalCostBasis")),
            "current_price": _num(meta.get("currentPrice")),
            "liquidity": _num(meta.get("liquidity")),
        })
    return out


def _ts(value: Any) -> int | None:
    from datetime import datetime

    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (AttributeError, ValueError):
        return None


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_swap_rows(user_id: str, s: dict) -> list[dict]:
    """One fomo swap -> rows of (token, side, when) per chain leg.

    A swap can be cross-chain, so each leg is recorded against its own network: the token that
    came out was bought, the token that went in was sold. Quote assets are skipped — knowing that
    someone moved USDC tells us nothing about who they are.
    """
    from datetime import datetime

    created = s.get("createdAt")
    try:
        ts = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) if created else None
    except (AttributeError, ValueError):
        ts = None
    sid = s.get("id")
    if not ts or not sid:
        return []
    out = []
    for token_key, net_key, side, usd_key, amt_key in (
        ("outTokenAddress", "outNetworkId", "buy", "humanUsdAmountOut", "outHumanAmount"),
        ("inTokenAddress", "inNetworkId", "sell", "humanUsdAmountIn", "inHumanAmount"),
    ):
        token = s.get(token_key)
        chain = NETWORKS.get(s.get(net_key) or s.get("networkId"))
        if not token or not chain or token in QUOTE_TOKENS:
            continue
        out.append({
            "swap_id": f"{sid}:{side}", "user_id": user_id, "chain": chain,
            "token": norm_addr(token), "side": side, "ts": ts,
            "usd": s.get(usd_key), "amount": s.get(amt_key),
        })
    return out


def parse_execution_addresses(swaps: list[dict]) -> dict[str, str]:
    """Most frequent swapping address per chain.

    A swap can be cross-chain (pay in Solana USDC, receive a Robinhood token), so the chain of
    an address is taken from the leg it belongs to: `address` pays the input, `recipient`
    receives the output.
    """
    counts: dict[str, dict[str, int]] = {}

    def bump(addr: str | None, network_id: Any) -> None:
        chain = NETWORKS.get(network_id)
        if not addr or not chain:
            return
        counts.setdefault(chain, {})
        a = norm_addr(addr)
        counts[chain][a] = counts[chain].get(a, 0) + 1

    for s in swaps:
        net = s.get("networkId")
        bump(s.get("address"), s.get("inNetworkId") or net)
        bump(s.get("recipient"), s.get("outNetworkId") or net)
    return {chain: max(c, key=c.get) for chain, c in counts.items() if c}
