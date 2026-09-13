"""Descriptive statistics. Sample size is not proof of an out-of-sample edge."""
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .math import dec, performance
from .store import VERSION


def report(conn):
    signals = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM learning_signals")}
    papers = [json.loads(r[0]) for r in conn.execute("SELECT result_json FROM learning_paper")]
    groups = {k: defaultdict(list) for k in ("score", "version", "strategy", "day_utc")}
    for r in papers:
        signal = signals[r["signal_id"]]
        score = signal["score"]
        band = "unknown" if score is None else ("90-100" if score >= 90 else f"{int(score // 10) * 10}-{int(score // 10) * 10 + 9}")
        groups["score"][band].append(r)
        groups["version"][signal["version"]].append(r)
        groups["strategy"][r["strategy"]].append(r)
        day = datetime.fromtimestamp(signal["detected_at"], timezone.utc).date().isoformat()
        groups["day_utc"][day].append(r)
    outcomes = defaultdict(list)
    for r in conn.execute("SELECT * FROM learning_outcomes"):
        outcomes[r["horizon_s"]].append(r)
    horizon_stats = {}
    for horizon, rows in outcomes.items():
        measured = [json.loads(r["result_json"]) for r in rows if r["state"] == "measured"]
        values = [dec(r["return_pct"]) for r in measured]
        horizon_stats[horizon] = {"states": dict(Counter(r["state"] for r in rows)),
                                  "mean_return_pct": str(sum(values) / len(values)) if values else None,
                                  "paths_with_gaps": sum(not r["path_complete"] for r in measured)}
    last_run = conn.execute("SELECT * FROM learning_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {"version": VERSION, "state": "experimental", "signals": len(signals), "quote": "USD",
            "paper_states": dict(Counter(r["state"] for r in papers)), "performance": performance(papers),
            "by": {k: {name: performance(rs) for name, rs in gs.items()} for k, gs in groups.items()},
            "horizons": horizon_stats, "confidence": "insufficient_validated_evidence",
            "promotion_allowed": False, "last_run": dict(last_run) if last_run else None}


def render(result):
    p = result["performance"]
    return "\n".join([
        f"Aprendizado experimental — {result['version']}",
        f"Sinais registrados: {result['signals']}",
        f"Simulações por estado: {result['paper_states']}",
        f"Encerradas: {p['closed']} | Taxa de acerto: {p['win_rate_pct'] or 'indisponível'}%",
        f"Resultado líquido das simulações isoladas (USD): {p['net_pnl']}",
        f"Expectativa por simulação (USD): {p['expectancy_cash'] or 'indisponível'}",
        f"Profit Factor: {p['profit_factor'] or 'indisponível'}",
        f"Horizontes: {json.dumps(result['horizons'], ensure_ascii=False)}",
        "Confiança: evidência validada insuficiente. Promoção de modelo: desativada.",
        "Capital por sinal é independente; o resultado não representa uma carteira financiável.",
        "Extremos e execuções são amostrados. Consulte learning report --json para detalhes.",
    ])
