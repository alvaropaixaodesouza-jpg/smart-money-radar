"""Shared, pure filtering for new-token candidates from any source."""
from __future__ import annotations

import time
from typing import Iterable

from ..models import NewToken


def dedupe_best(tokens: Iterable[NewToken]) -> dict[tuple[str, str], NewToken]:
    """One NewToken per (chain, mint): keep the pool with the highest liquidity."""
    best: dict[tuple[str, str], NewToken] = {}
    for t in tokens:
        key = (t.chain, t.mint)
        cur = best.get(key)
        if cur is None or (t.liquidity_usd or 0) > (cur.liquidity_usd or 0):
            best[key] = t
    return best


def filter_tokens(
    tokens: Iterable[NewToken], *, min_mcap: float, max_age_hours: float, min_liquidity: float = 0.0,
    max_mcap: float | None = None, now_ts: int | None = None
) -> list[NewToken]:
    now_ts = now_ts or int(time.time())
    out = []
    for t in dedupe_best(tokens).values():
        if (t.mcap_usd or 0) < min_mcap:
            continue
        if max_mcap and (t.mcap_usd or 0) > max_mcap:
            continue
        if (t.liquidity_usd or 0) < min_liquidity:
            continue
        if t.created_at and (now_ts - t.created_at) > max_age_hours * 3600:
            continue
        out.append(t)
    out.sort(key=lambda x: -(x.mcap_usd or 0))
    return out
