"""Helius Enhanced Transactions API -> normalized swaps for a wallet.

GET https://api.helius.xyz/v0/addresses/{address}/transactions?api-key=..&type=SWAP&limit=100&before=<sig>

Free tier is rate limited; we throttle to settings.helius_max_req_per_min and log usage.
Phase 0 item 4 (fomo smart-account / router pattern) may require extending `parse_swap`:
the address that *signs* may differ from the address that *receives* tokens. We therefore
attribute a trade to `address` if it appears as feePayer OR as a user account in token/native
transfers of the swap event.
"""
from __future__ import annotations

import logging
import time

import httpx

from ..config import settings
from ..models import QUOTE_MINTS, WSOL_MINT, Trade
from ..ratelimit import RateLimiter

log = logging.getLogger(__name__)
BASE = "https://api.helius.xyz"
LAMPORTS = 1_000_000_000


class HeliusClient:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or settings.helius_api_key
        if not self.api_key:
            raise RuntimeError("HELIUS_API_KEY is not set (see .env.example)")
        self.http = client or httpx.Client(base_url=BASE, timeout=30)
        self.limiter = RateLimiter(settings.helius_max_req_per_min, "helius")

    def _get(self, path: str, **params) -> list:
        params["api-key"] = self.api_key
        for attempt in range(4):
            self.limiter.wait()
            r = self.http.get(path, params=params)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("helius: too many 429s")

    def raw_transactions(self, address: str, *, since_ts: int | None = None, max_pages: int = 5) -> list[dict]:
        """Page backwards through SWAP txs until we reach `since_ts` or `max_pages`."""
        out: list[dict] = []
        before: str | None = None
        for _ in range(max_pages):
            params = {"type": "SWAP", "limit": settings.helius_tx_page_limit}
            if before:
                params["before"] = before
            batch = self._get(f"/v0/addresses/{address}/transactions", **params)
            if not batch:
                break
            out.extend(batch)
            oldest = batch[-1].get("timestamp", 0)
            if since_ts and oldest <= since_ts:
                break
            before = batch[-1].get("signature")
            if len(batch) < settings.helius_tx_page_limit:
                break
        if since_ts:
            out = [t for t in out if t.get("timestamp", 0) > since_ts]
        return out

    def get_swaps(self, address: str, since_ts: int | None = None, max_pages: int = 5) -> list[Trade]:
        trades = []
        for raw in self.raw_transactions(address, since_ts=since_ts, max_pages=max_pages):
            t = parse_swap(raw, address)
            if t:
                trades.append(t)
        return trades


# ---------- pure parsing (fixture-testable) ----------

def _amt(x: dict | None) -> float:
    if not x:
        return 0.0
    raw = x.get("rawTokenAmount")
    if raw:
        try:
            return float(raw.get("tokenAmount", 0)) / (10 ** int(raw.get("decimals", 0)))
        except (TypeError, ValueError):
            return 0.0
    return float(x.get("tokenAmount") or x.get("amount") or 0)


def parse_swap(tx: dict, address: str) -> Trade | None:
    """Turn one enhanced tx into a Trade for `address`, or None if not attributable.

    Strategy:
      1. events.swap (preferred): nativeInput/Output + tokenInputs/Outputs filtered by userAccount.
      2. fallback: tokenTransfers / nativeTransfers touching `address`.
    A trade is a `buy` when the wallet spends SOL/stable and receives a non-quote token,
    `sell` when the reverse. Multi-hop / token-to-token swaps are skipped for MVP.
    """
    sig = tx.get("signature")
    ts = tx.get("timestamp")
    if not sig or not ts:
        return None

    sol_out = sol_in = 0.0            # SOL leaving / entering the wallet
    tok_out: dict[str, float] = {}    # mint -> amount leaving wallet
    tok_in: dict[str, float] = {}     # mint -> amount entering wallet

    swap = (tx.get("events") or {}).get("swap")
    if swap:
        ni, no = swap.get("nativeInput"), swap.get("nativeOutput")
        if ni and ni.get("account") == address:
            sol_out += float(ni.get("amount", 0)) / LAMPORTS
        if no and no.get("account") == address:
            sol_in += float(no.get("amount", 0)) / LAMPORTS
        for ti in swap.get("tokenInputs") or []:
            if ti.get("userAccount") == address:
                tok_out[ti["mint"]] = tok_out.get(ti["mint"], 0) + _amt(ti)
        for to in swap.get("tokenOutputs") or []:
            if to.get("userAccount") == address:
                tok_in[to["mint"]] = tok_in.get(to["mint"], 0) + _amt(to)
    else:
        for tt in tx.get("tokenTransfers") or []:
            amt = float(tt.get("tokenAmount") or 0)
            if tt.get("fromUserAccount") == address:
                tok_out[tt["mint"]] = tok_out.get(tt["mint"], 0) + amt
            elif tt.get("toUserAccount") == address:
                tok_in[tt["mint"]] = tok_in.get(tt["mint"], 0) + amt
        for nt in tx.get("nativeTransfers") or []:
            amt = float(nt.get("amount", 0)) / LAMPORTS
            if nt.get("fromUserAccount") == address:
                sol_out += amt
            elif nt.get("toUserAccount") == address:
                sol_in += amt

    # wrapped SOL counts as SOL
    sol_out += tok_out.pop(WSOL_MINT, 0.0)
    sol_in += tok_in.pop(WSOL_MINT, 0.0)
    # stables: treat as quote, drop from token legs (usd value unknown without price -> keep sol_amount None)
    stable_out = sum(v for m, v in tok_out.items() if m in QUOTE_MINTS)
    stable_in = sum(v for m, v in tok_in.items() if m in QUOTE_MINTS)
    tok_out = {m: v for m, v in tok_out.items() if m not in QUOTE_MINTS}
    tok_in = {m: v for m, v in tok_in.items() if m not in QUOTE_MINTS}

    if len(tok_in) == 1 and not tok_out and (sol_out > 0 or stable_out > 0):
        mint, amount = next(iter(tok_in.items()))
        return Trade(sig=sig, address=address, mint=mint, side="buy", ts=ts,
                     sol_amount=sol_out or None, token_amount=amount,
                     usd_value=stable_out or None)
    if len(tok_out) == 1 and not tok_in and (sol_in > 0 or stable_in > 0):
        mint, amount = next(iter(tok_out.items()))
        return Trade(sig=sig, address=address, mint=mint, side="sell", ts=ts,
                     sol_amount=sol_in or None, token_amount=amount,
                     usd_value=stable_in or None)
    return None
