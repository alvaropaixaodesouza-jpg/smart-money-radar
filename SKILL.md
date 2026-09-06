---
name: fomo-agent
description: Research watchlist of Robinhood Chain memecoin traders from fomo.family. Use when the user asks who the good traders are, who moved between active/watch/dropped, what the cohort is buying or holding, to analyze a token or a trader, or to score wallets in chat. Read-only; never trades.
---

# fomo-agent skill

Reads the local SQLite database that `fomo-agent` builds, and runs its CLI. It **never** trades,
signs, or moves funds, and it holds no key that could.

## When to trigger

- "покажи топ-трейдеров fomo" / "who are the good traders"
- "кого добавили в active" / "who dropped and why"
- "что купили за сутки" / "what is the cohort buying"
- "разбери токен X" / "analyze this token" / "who holds this"
- "разбери трейдера X" / "what is this trader doing"
- "проскорь кошельки" / "score the unscored wallets"

## Commands (run from the repo root, venv activated)

| Intent | Command |
|---|---|
| Overview, top active, 24h buys | `python -m fomo_agent.cli report --hours 24` |
| Whose money is in a token | `python -m fomo_agent.cli token <address>` |
| One trader in full | `python -m fomo_agent.cli trader <handle-or-address>` |
| Rebuild the published page | `python -m fomo_agent.cli page --out radar.html` |
| Pull fresh fills (free, no key) | `python -m fomo_agent.cli track` |
| Turn collected fomo users into wallets | `python -m fomo_agent.cli resolve` |
| Name tokens known only by address | `python -m fomo_agent.cli enrich-tokens` |
| Refresh everything once | `python -m fomo_agent.cli run --once` |

Direct SQL is fine too (`fomo_agent.db`): `traders`, `tokens`, `trades`, `fomo_users`, `fomo_swaps`,
`fomo_positions`, `score_history`, `runs`.

## Scoring in chat

This is the skill's most useful job when `ANTHROPIC_API_KEY` is not set. The export carries the same
context and instructions the API path would send:

```
python -m fomo_agent.cli score --export pending.json            # everything unscored
python -m fomo_agent.cli score --export pending.json --digest 40 --offset 0   # one batch
```

Read the file, produce one verdict per wallet in the shape `docs/example_scores.json` shows, write
it to `scored.json`, then:

```
python -m fomo_agent.cli score --import scored.json
```

**The rule that matters:** fomo's 30-day PnL is real dollars and it *includes open positions*. A
wallet showing millions on a few thousand of on-chain outlay is not a glitch — the position ran and
was never sold. Negative on-chain cash flow is what accumulating looks like, not what losing looks
like. Judge on the fomo figure first; on-chain flow is the sanity check.

## Reading the numbers

- **Conviction** on a token is the sum of each holder's (score/100)² — it says *whose* money is in a
  name, not how many wallets are. One trader at 85 outweighs a crowd at 40.
- **Mult** is what a position is worth against what it cost. A dash means fomo reports profit
  already withdrawn, so the entry price cannot be recovered.
- **USDG and WETH are quote assets**, not positions. Some sources book a swap from the pool's side,
  filing "sold X for USDG" as a USDG purchase. Never present them as something the cohort bought.
- A fomo profile address is not a wallet. Never track one on-chain; `resolve` finds the real one.

## Limits

- Robinhood Chain only by default (`DEX_CHAINS=robinhood`). Solana and Base still work but need a
  Codex or Helius key.
- fomo's own API is unreachable from a server — its data arrives through the browser extension in
  `extension/` plus `fomo-agent receive`.
- The chain's RPC serves about 5.6 hours of logs per query; older history needs chunked backfill.
