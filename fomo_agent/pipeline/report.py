"""Markdown report: top active, new candidates, dropped, and 24h buys grouped by mint."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .. import db


def _h(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M") if ts else "-"


def build_report(conn: sqlite3.Connection, hours: int = 24) -> str:
    now = db.now()
    since = now - hours * 3600
    out = [f"# FOMO Robinhood Radar — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"]

    counts = conn.execute("SELECT status, COUNT(*) c FROM traders GROUP BY status").fetchall()
    out.append("**Traders:** " + ", ".join(f"{r['status']}={r['c']}" for r in counts) + "\n")

    out.append("## Top active\n")
    rows = conn.execute("SELECT * FROM traders WHERE status='active' ORDER BY score DESC LIMIT 20").fetchall()
    if not rows:
        out.append("_none yet_\n")
    for r in rows:
        tags = json.loads(r["tags"]) if r["tags"] else {}
        out.append(f"- `{r['address']}` score={r['score']} {r['fomo_handle'] or ''} style={tags.get('style')} flags={tags.get('red_flags')}")
        if r["ai_summary"]:
            out.append(f"  - {r['ai_summary']}")
    out.append("")

    out.append(f"## New candidates (last {hours}h)\n")
    rows = conn.execute("SELECT * FROM traders WHERE first_seen_at>=? ORDER BY first_seen_at DESC", (since,)).fetchall()
    out.append(f"{len(rows)} new" + ("" if rows else " — _none_"))
    for r in rows[:30]:
        out.append(f"- `{r['address']}` from {r['source']} ({_h(r['first_seen_at'])})")
    out.append("")

    out.append(f"## Dropped (last {hours}h)\n")
    rows = conn.execute(
        "SELECT h.address, h.reason, h.ts FROM score_history h WHERE h.status='dropped' AND h.ts>=? ORDER BY h.ts DESC", (since,)
    ).fetchall()
    if not rows:
        out.append("_none_")
    for r in rows[:20]:
        out.append(f"- `{r['address']}`: {r['reason']}")
    out.append("")

    out.append(f"## Buys by active/watch traders (last {hours}h), grouped by token\n")
    rows = conn.execute(
        "SELECT tr.mint, tk.symbol, tk.chain, COUNT(DISTINCT tr.address) wallets, SUM(tr.sol_amount) sol, "
        "GROUP_CONCAT(DISTINCT substr(tr.address,1,6)) who "
        "FROM trades tr JOIN traders t ON t.address=tr.address LEFT JOIN tokens tk ON tk.mint=tr.mint "
        "WHERE tr.side='buy' AND tr.ts>=? AND t.status IN ('active','watch') "
        "GROUP BY tr.mint ORDER BY wallets DESC, sol DESC LIMIT 30",
        (since,),
    ).fetchall()
    if not rows:
        out.append("_no buys recorded_")
    for r in rows:
        flag = " **SIGNAL**" if r["wallets"] >= 2 else ""
        out.append(f"- `{r['mint']}` {r['symbol'] or ''} [{r['chain'] or 'solana'}]: {r['wallets']} wallets, {r['sol'] or 0:.2f} SOL [{r['who']}]{flag}")
    out.append("")

    runs = conn.execute("SELECT kind, started_at, error FROM runs ORDER BY id DESC LIMIT 5").fetchall()
    if runs:
        out.append("## Recent runs\n")
        for r in runs:
            out.append(f"- {r['kind']} @ {_h(r['started_at'])}" + (f" ERROR: {r['error'][:80]}" if r["error"] else ""))
    return "\n".join(out)
