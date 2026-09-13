"""Markdown report: top active, new candidates, dropped, and 24h buys grouped by mint."""
from __future__ import annotations

import math

import json
import sqlite3
from datetime import datetime, timezone

from .. import db
from fomo_agent.pipeline.analyze import signals as ranked_signals


def _h(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M") if ts else "-"


def build_report(conn: sqlite3.Connection, hours: int = 24) -> str:
    now = db.now()
    since = now - hours * 3600
    out = [f"# FOMO Robinhood Radar — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC\n"]

    counts = conn.execute("SELECT status, COUNT(*) c FROM traders GROUP BY status").fetchall()
    status_pt = {"active": "acompanhadas", "dropped": "descartadas", "watch": "observadas"}
    out.append("**Carteiras avaliadas:** " + ", ".join(f"{status_pt.get(r['status'], r['status'])}={r['c']}" for r in counts) + "\n")

    out.append("## Principais traders acompanhados\n")
    rows = conn.execute("SELECT * FROM traders WHERE status='active' ORDER BY score DESC LIMIT 20").fetchall()
    if not rows:
        out.append("_nenhum ainda_\n")
    for r in rows:
        tags = json.loads(r["tags"]) if r["tags"] else {}
        out.append(f"- `{r['address']}` score={r['score']} {r['fomo_handle'] or ''} estilo={tags.get('style')} alertas={tags.get('red_flags')}")
        if r["ai_summary_pt"] or r["ai_summary"]:
            out.append(f"  - {r['ai_summary_pt'] or r['ai_summary']}")
    out.append("")

    out.append(f"## Novos candidatos (últimas {hours}h)\n")
    rows = conn.execute("SELECT * FROM traders WHERE first_seen_at>=? ORDER BY first_seen_at DESC", (since,)).fetchall()
    out.append(f"{len(rows)} novos" + ("" if rows else " — _nenhum_"))
    for r in rows[:30]:
        out.append(f"- `{r['address']}` · origem: {r['source']} ({_h(r['first_seen_at'])})")
    out.append("")

    out.append(f"## Descartados (últimas {hours}h)\n")
    rows = conn.execute(
        "SELECT h.address, h.reason, h.ts, "
        "t.ai_summary_pt, t.ai_summary "
        "FROM score_history h "
        "LEFT JOIN traders t ON t.address = h.address "
        "WHERE h.status='dropped' AND h.ts>=? "
        "ORDER BY h.ts DESC",
        (since,),
    ).fetchall()
    if not rows:
        out.append("_nenhum_")
    for r in rows[:20]:
        motivo = (
            r["ai_summary_pt"]
            or r["reason"]
            or r["ai_summary"]
            or "sem motivo registrado"
        )
        out.append(f"- `{r['address']}`: {motivo}")
    out.append("")

    # Advanced ranked signal engine
    out.append(f"## Sinais Smart Money ranqueados (últimas {hours}h)\n")

    ranked = ranked_signals(
        conn,
        "robinhood",
        hours=hours,
        min_buyers=2,
        limit=10,
    )

    if not ranked:
        out.append("- (nenhum sinal ranqueado)\\n")
    else:
        for rank, s in enumerate(ranked, 1):
            buyers = int(s.get("buyers") or 0)
            usd = float(s.get("usd") or 0)
            avg_score = float(s.get("avg_score") or 0)
            conviction = float(s.get("conviction") or 0)

            # Internal strength index. It is NOT a probability.
            signal_score = (
                min(100, round(100 * (1 - math.exp(-conviction / 1.15))))
                if conviction > 0
                else 0
            )

            if signal_score >= 85:
                signal_label = "SINAL FORTE"
            elif signal_score >= 70:
                signal_label = "SINAL"
            elif signal_score >= 50:
                signal_label = "OBSERVAR"
            else:
                signal_label = "FRACO"

            mint = s.get("mint") or "?"
            sym = s.get("sym") or mint[:10]
            who = s.get("who") or "?"
            scores = s.get("scores") or "?"

            liq = s.get("liq")
            if liq is None:
                liq_txt = "n/d"
            else:
                liq_txt = f"${float(liq):,.0f}"

            out.append(
                f"{rank}. **{signal_score}/100 - {signal_label}** | "
                f"**{sym}** `{mint}` — "
                f"{buyers} compradores | "
                f"${usd:,.0f} comprados | "
                f"score médio {avg_score:.1f} | "
                f"convicção {conviction:.3f} | "
                f"liquidez {liq_txt}"
            )

            out.append(f"   - carteiras: {who}")
            out.append(f"   - pontuações: {scores}")

    out.append("")

    out.append(f"## Compras de traders acompanhados/observados (últimas {hours}h), agrupadas por token\n")
    rows = conn.execute(
        "SELECT tr.mint, tk.symbol, tk.chain, COUNT(DISTINCT tr.address) wallets, SUM(tr.sol_amount) sol, "
        "GROUP_CONCAT(DISTINCT substr(tr.address,1,6)) who "
        "FROM trades tr JOIN traders t ON t.address=tr.address LEFT JOIN tokens tk ON tk.mint=tr.mint "
        "WHERE tr.side='buy' AND tr.ts>=? AND t.status IN ('active','watch') "
        "GROUP BY tr.mint ORDER BY wallets DESC, sol DESC LIMIT 30",
        (since,),
    ).fetchall()
    if not rows:
        out.append("_nenhuma compra registrada_")
    for r in rows:
        flag = " **SINAL**" if r["wallets"] >= 2 else ""
        out.append(f"- `{r['mint']}` {r['symbol'] or ''} [{r['chain'] or 'solana'}]: {r['wallets']} carteiras, {r['sol'] or 0:.2f} SOL [{r['who']}]{flag}")
    out.append("")

    runs = conn.execute("SELECT kind, started_at, error FROM runs ORDER BY id DESC LIMIT 5").fetchall()
    if runs:
        out.append("## Execuções recentes\n")
        nomes_execucao = {
            "track": "acompanhamento",
            "score": "avaliação",
            "report": "relatório",
            "resolve": "resolução",
            "new_tokens": "novos tokens",
            "enrich_tokens": "enriquecimento de tokens",
            "holdings": "posições",
            "discover_trenches": "descoberta via trenches",
            "discover_leaderboard": "descoberta via ranking",
        }

        for r in runs:
            out.append(f"- {nomes_execucao.get(r['kind'], r['kind'])} @ {_h(r['started_at'])}" + (f" ERRO: {r['error'][:80]}" if r["error"] else ""))
    return "\n".join(out)
