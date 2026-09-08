"""Answer two questions against the watchlist: what do we know about this token, and this trader.

The database already holds who is good, what they hold and what they bought. These read it back the
way a person asks: point at a contract address and ask whose money is in it, or point at a handle
and ask what that person is actually doing. Nothing here calls an API.

The measure that matters for a token is not how many wallets hold it but *whose*. One trader
scoring 85 says more than ten scoring 40, so holder quality is summed as (score/100)^2 — squaring
keeps a crowd of mediocre wallets from outvoting a good one.
"""
from __future__ import annotations

import json
import sqlite3

from .. import db
from ..sources.rpc import QUOTE_TOKENS

TRUSTED = 60
NOT_QUOTE = " AND {col} NOT IN (%s)" % ",".join("'%s'" % t for t in QUOTE_TOKENS)


def conviction(scores: list[int]) -> float:
    """Holder quality as one number: the sum of (score/100)^2 over distinct holders."""
    return sum((s / 100) ** 2 for s in scores if s)


def signals(conn: sqlite3.Connection, chain: str | None = None, hours: int = 24,
            min_buyers: int = 2, limit: int = 40) -> list[dict]:
    """Tokens that several trusted wallets bought inside the window, best conviction first.

    The canonical definition of a signal — the page, the terminal and the bot all read it from
    here, so they can never drift into disagreeing about what a signal is.

    Each buyer's fills are collapsed before aggregating, so a wallet that bought five times counts
    once toward the headcount and once toward conviction.
    """
    params: list = [db.now() - hours * 3600, TRUSTED]
    if chain:
        params.append(chain)
    return [dict(r) for r in conn.execute(
        "SELECT mint, sym, liq, COUNT(*) buyers, SUM(usd) usd, MIN(first_ts) first_ts, "
        "  AVG(score) avg_score, SUM((score / 100.0) * (score / 100.0)) conviction, "
        "  GROUP_CONCAT(handle) who, GROUP_CONCAT(score) scores FROM ("
        "  SELECT tr.mint mint, COALESCE(tk.symbol, substr(tr.mint,1,8)) sym, "
        "    tk.liquidity_usd liq, t.score score, t.fomo_handle handle, "
        "    SUM(tr.usd_value) usd, MIN(tr.ts) first_ts "
        "  FROM trades tr JOIN traders t ON t.address = tr.address "
        "  LEFT JOIN tokens tk ON tk.mint = tr.mint "
        f"  WHERE tr.side='buy' AND tr.ts >= ? AND t.score >= ?{' AND tr.chain=?' if chain else ''}"
        + NOT_QUOTE.format(col="tr.mint") +
        "  GROUP BY tr.mint, tr.address"
        ") GROUP BY mint HAVING buyers >= ? ORDER BY conviction DESC, usd DESC LIMIT ?",
        [*params, min_buyers, limit],
    )]


def leaderboard(conn: sqlite3.Connection, limit: int = 25, status: str = "active") -> list[dict]:
    """The scored roster, best first — our own ranking by judgement rather than by headline PnL."""
    return [dict(r) for r in conn.execute(
        "SELECT fomo_handle handle, address, score, status, ai_summary summary, "
        "  COALESCE(pnl_30d, pnl_7d, pnl_24h) fomo_pnl, tags "
        "FROM traders WHERE score IS NOT NULL AND (? = 'all' OR status = ?) "
        "ORDER BY score DESC, fomo_pnl DESC LIMIT ?", (status, status, limit),
    )]


def token_symbol(conn: sqlite3.Connection, mint: str) -> str:
    row = conn.execute("SELECT symbol FROM tokens WHERE mint=?", (mint,)).fetchone()
    return (row["symbol"] if row and row["symbol"] else mint[:10])


def analyze_token(conn: sqlite3.Connection, mint: str, hours: int = 48) -> dict:
    """Whose money is in this token, what it cost them, and who moved on it recently."""
    mint = mint.lower() if mint.startswith("0x") else mint
    since = db.now() - hours * 3600

    holders = [dict(r) for r in conn.execute(
        "SELECT t.fomo_handle handle, t.address, t.score, t.status, p.unrealized_pnl pnl, "
        "  p.cost_basis cost, p.amount "
        "FROM fomo_positions p JOIN traders t ON t.fomo_user_id = p.user_id "
        "WHERE p.token = ? ORDER BY COALESCE(t.score,0) DESC, p.unrealized_pnl DESC", (mint,),
    )]
    flow = [dict(r) for r in conn.execute(
        "SELECT t.fomo_handle handle, t.address, t.score, tr.side, tr.usd_value usd, tr.ts "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        "WHERE tr.mint = ? AND tr.ts >= ? ORDER BY tr.ts DESC", (mint, since),
    )]
    first = conn.execute(
        "SELECT MIN(tr.ts) ts, COUNT(DISTINCT tr.address) buyers FROM trades tr "
        "JOIN traders t ON t.address = tr.address "
        "WHERE tr.mint = ? AND tr.side = 'buy' AND t.score >= ?", (mint, TRUSTED),
    ).fetchone()
    token = conn.execute("SELECT * FROM tokens WHERE mint=?", (mint,)).fetchone()

    # Who is *buying* it, grouped by wallet. This is a different question from who holds it, and
    # for a fresh token it is the only one with an answer: positions come from fomo's snapshot of a
    # trader's three biggest bags, so a token nobody has ridden yet appears in nobody's top three
    # while a dozen trusted wallets are already accumulating it.
    by_wallet: dict[str, dict] = {}
    for f in flow:
        w = by_wallet.setdefault(f["address"], {
            "handle": f["handle"], "address": f["address"], "score": f["score"],
            "bought": 0.0, "sold": 0.0, "fills": 0, "first_ts": f["ts"],
        })
        w["fills"] += 1
        w["first_ts"] = min(w["first_ts"], f["ts"])
        w["bought" if f["side"] == "buy" else "sold"] += f["usd"] or 0
    buyers = sorted(by_wallet.values(), key=lambda w: (-(w["score"] or 0), -w["bought"]))

    scores = [h["score"] for h in holders if h["score"]]
    costs = [h["cost"] for h in holders if h["cost"]]
    return {
        # The same measure the feed ranks by, so the two pages can never disagree about a token.
        "buyers": buyers,
        "buyer_conviction": conviction([b["score"] for b in buyers if (b["score"] or 0) >= TRUSTED]),
        "mint": mint,
        "symbol": token["symbol"] if token and token["symbol"] else None,
        "is_quote": mint in QUOTE_TOKENS,
        "liquidity_usd": token["liquidity_usd"] if token else None,
        "mcap_usd": token["mcap_usd"] if token else None,
        "holders": holders,
        "trusted_holders": sum(1 for s in scores if s >= TRUSTED),
        "avg_score": sum(scores) / len(scores) if scores else None,
        "conviction": conviction(scores),
        "cohort_pnl": sum(h["pnl"] for h in holders if h["pnl"]) or None,
        "cohort_cost": sum(costs) or None,
        "flow": flow,
        "bought_usd": sum(f["usd"] or 0 for f in flow if f["side"] == "buy"),
        "sold_usd": sum(f["usd"] or 0 for f in flow if f["side"] == "sell"),
        "first_trusted_buy": first["ts"] if first else None,
        "trusted_buyers": first["buyers"] if first else 0,
        "hours": hours,
    }


def find_trader(conn: sqlite3.Connection, who: str) -> sqlite3.Row | None:
    """Accept a handle or an address, case-insensitively."""
    return conn.execute(
        "SELECT * FROM traders WHERE lower(address) = lower(?) OR lower(fomo_handle) = lower(?)",
        (who, who),
    ).fetchone()


def analyze_trader(conn: sqlite3.Connection, who: str, hours: int = 168) -> dict | None:
    """One trader's verdict, open bags, recent fills and who else is in the same names."""
    row = find_trader(conn, who)
    if row is None:
        return None
    address, since = row["address"], db.now() - hours * 3600

    # Quote assets are excluded from both: a wallet holding USDG is holding cash, and a WETH leg
    # is how a swap is paid for, not a position anyone took.
    positions = [dict(r) for r in conn.execute(
        "SELECT p.token, COALESCE(tk.symbol, substr(p.token,1,10)) sym, p.unrealized_pnl pnl, "
        "  p.cost_basis cost FROM fomo_positions p LEFT JOIN tokens tk ON tk.mint = p.token "
        "WHERE p.user_id = ?" + NOT_QUOTE.format(col="p.token") +
        " ORDER BY p.unrealized_pnl DESC", (row["fomo_user_id"],),
    )]
    fills = [dict(r) for r in conn.execute(
        "SELECT tr.ts, tr.side, tr.usd_value usd, tr.mint, tr.source, "
        "  COALESCE(tk.symbol, substr(tr.mint,1,10)) sym FROM trades tr "
        "LEFT JOIN tokens tk ON tk.mint = tr.mint "
        "WHERE tr.address = ? AND tr.ts >= ?" + NOT_QUOTE.format(col="tr.mint") +
        " ORDER BY tr.ts DESC", (address, since),
    )]
    # who else this trader keeps showing up next to, by shared open positions
    company = [dict(r) for r in conn.execute(
        "SELECT t.fomo_handle handle, t.score, COUNT(*) shared FROM fomo_positions p "
        "JOIN traders t ON t.fomo_user_id = p.user_id "
        "WHERE p.token IN (SELECT token FROM fomo_positions WHERE user_id = ?) "
        "  AND p.user_id != ? AND t.score IS NOT NULL "
        "GROUP BY p.user_id ORDER BY shared DESC, t.score DESC LIMIT 8",
        (row["fomo_user_id"], row["fomo_user_id"]),
    )]
    tags = json.loads(row["tags"]) if row["tags"] else {}
    stats = json.loads(row["stats_json"]) if row["stats_json"] else {}
    return {
        "address": address, "handle": row["fomo_handle"], "chain": row["chain"],
        "score": row["score"], "status": row["status"], "summary": row["ai_summary"],
        "model": row["ai_model"], "style": tags.get("style") or [],
        "red_flags": tags.get("red_flags") or [], "stats": stats,
        "fomo_pnl": row["pnl_30d"] or row["pnl_7d"] or row["pnl_24h"],
        "positions": positions,
        "open_pnl": sum(p["pnl"] for p in positions if p["pnl"]) or None,
        "fills": fills,
        "bought_usd": sum(f["usd"] or 0 for f in fills if f["side"] == "buy"),
        "sold_usd": sum(f["usd"] or 0 for f in fills if f["side"] == "sell"),
        "company": company, "hours": hours,
    }


# ---------------------------------------------------------------- terminal output

def usd(v) -> str:
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if a >= 1_000:
        return f"${v/1_000:.1f}k"
    return f"${v:.0f}"


def format_token(a: dict) -> str:
    name = a["symbol"] or a["mint"][:10]
    out = [f"# {name}  {a['mint']}"]
    if a["is_quote"]:
        out.append("\n**This is a quote asset.** Every swap passes through it, so holdings and buys "
                   "here are plumbing, not conviction.")
    out.append(f"\nliquidity {usd(a['liquidity_usd'])} · mcap {usd(a['mcap_usd'])}")
    out.append(f"\n## Who holds it\n")
    if not a["holders"]:
        out.append("_nobody on the watchlist_")
    else:
        out.append(f"{len(a['holders'])} holders, {a['trusted_holders']} of them scoring {TRUSTED}+ · "
                   f"avg score {a['avg_score']:.0f} · conviction {a['conviction']:.2f}"
                   if a["avg_score"] else f"{len(a['holders'])} holders, none scored")
        out.append(f"cohort cost {usd(a['cohort_cost'])} → open PnL {usd(a['cohort_pnl'])}")
        out.append("")
        for h in a["holders"][:15]:
            out.append(f"  {str(h['score'] or '--'):>3}  {(h['handle'] or h['address'][:10]):<20} "
                       f"{usd(h['pnl']):>9} open   cost {usd(h['cost'])}")
    out.append(f"\n## Flow, last {a['hours']}h\n")
    if not a["flow"]:
        out.append("_no fills recorded_")
    else:
        out.append(f"bought {usd(a['bought_usd'])} · sold {usd(a['sold_usd'])} · "
                   f"{len(a['flow'])} fills by {len({f['address'] for f in a['flow']})} wallets")
        for f in a["flow"][:15]:
            out.append(f"  {f['side']:<4} {usd(f['usd']):>9}  {(f['handle'] or f['address'][:10]):<20} "
                       f"score {f['score'] or '--'}")
    return "\n".join(out)


def format_trader(a: dict) -> str:
    out = [f"# {a['handle'] or a['address']}  ({a['status']}, score {a['score']})", a["address"]]
    if a["summary"]:
        out.append(f"\n{a['summary']}")
    if a["style"] or a["red_flags"]:
        out.append("style: " + ", ".join(a["style"]) + ("  flags: " + ", ".join(a["red_flags"]) if a["red_flags"] else ""))
    out.append(f"\nfomo 30d {usd(a['fomo_pnl'])} · open bags {len(a['positions'])} "
               f"worth {usd(a['open_pnl'])} unrealised")
    s = a["stats"]
    if s:
        bits = [f"{k} {v}" for k, v in (("win", f"{s['win_rate']*100:.0f}%" if s.get("win_rate") is not None else None),
                                        ("closed", s.get("closed_trades")), ("fills", s.get("fills")),
                                        ("volume", usd(s["volume"]) if s.get("volume") else None)) if v]
        out.append(" · ".join(bits))

    out.append("\n## Open positions\n")
    for p in a["positions"][:12] or [None]:
        out.append(f"  {p['sym']:<14} {usd(p['pnl']):>9} open   cost {usd(p['cost'])}" if p else "_none_")
    out.append(f"\n## Fills, last {a['hours']}h\n")
    out.append(f"bought {usd(a['bought_usd'])} · sold {usd(a['sold_usd'])} · {len(a['fills'])} fills")
    for f in a["fills"][:15]:
        out.append(f"  {f['side']:<4} {usd(f['usd']):>9}  {f['sym']:<14} via {f['source']}")
    if a["company"]:
        out.append("\n## Sits in the same names as\n")
        for c in a["company"]:
            out.append(f"  {str(c['score']):>3}  {c['handle']:<20} {c['shared']} shared positions")
    return "\n".join(out)
