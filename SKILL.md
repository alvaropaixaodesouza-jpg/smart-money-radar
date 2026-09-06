---
name: fomo-agent
description: Research watchlist of strong Solana memecoin traders. Use when the user asks about fomo top traders, who became active/dropped, what active wallets bought recently, or to score a specific wallet. Read-only; never trades.
---

# fomo-agent skill

This skill reads the local SQLite database produced by `fomo-agent` and runs its CLI. It **never** trades, signs, or moves funds.

## When to trigger

- "покажи топ-трейдеров fomo" / "top fomo traders"
- "кого добавили в active" / "who dropped and why"
- "проскорь кошелёк X" / "score wallet X"
- "что купили active за сутки" / "what did active wallets buy"
- "свежие токены" / "new tokens"

## Commands (run from repo root, venv activated)

| Intent | Command |
|---|---|
| Overview / top active / signals | `python -m fomo_agent.cli report --hours 24` |
| Score one wallet | `python -m fomo_agent.cli score --address <wallet>` |
| Inspect scoring context without paying | `python -m fomo_agent.cli score --address <wallet> --show-context` |
| Add + track a wallet | `python -m fomo_agent.cli track --address <wallet>` |
| Fresh tokens right now | `python -m fomo_agent.cli new-tokens --dry-run` |
| Refresh everything once | `python -m fomo_agent.cli run --once` |

Direct SQL is fine too (`fomo_agent.db`): tables `traders`, `tokens`, `trades`, `score_history`, `runs`.

## Output format

Return the markdown from `report` as-is, or summarize: wallet (short), score, status, style/red flags, one-line summary. For signals, highlight tokens bought by ≥2 active wallets within 24h.

## Limits

- fomo discovery requires phase 0 (`docs/fomo-endpoints.md`); until then only manual wallets + DexScreener tokens work.
- Helius free tier is rate-limited; `track` may take a while for many wallets.
