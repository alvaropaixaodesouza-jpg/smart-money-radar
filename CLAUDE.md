# fomo-agent

Read-only research tool: discovers Solana/Base/Robinhood memecoin traders (fomo.family + fresh-token holders), tracks Solana wallets on-chain (Helius), scores them with Claude, keeps a sqlite watchlist. Never trades.

**Start every session by reading `docs/STATUS.md`** — it has the current state, blockers, and the prioritized work list. Update it at the end of the session.

## Rules
- Never invent fomo.family endpoints. `sources/fomo.py` is filled only from `docs/fomo-endpoints.md` (phase 0 capture).
- Thresholds/intervals live in `config.py` + `.env`. No hardcoding.
- Every source must fail soft: one API down must not stop the loop.
- Codex budget (10k requests/month) is the system's hard constraint: new tokens, trader discovery and wallet tracking all spend from it. `cli init` prints a projection — check it before adding any new Codex call.
- These Codex queries are plan-gated and unavailable: `filterWallets`, `detailedWalletStats`, `balances`. Working ones: `filterTokens`, `getTokenEvents`, `getTokenEventsForMaker`.
- Scoring must keep working without `ANTHROPIC_API_KEY` via the `score --export` / `--import` loop.
- Keep `pytest -q` green (offline fixture tests in `tests/`). Add a fixture + test for every new parser.
- No secrets in code or docs. `.env` is gitignored.

## Environment quirks (Windows)
- Run everything with `.venv/Scripts/python`; set `PYTHONIOENCODING=utf-8` for live runs (token names break cp1251).
- Bash heredocs with single quotes broke repeatedly here; edit files with Write/Edit or a python script.

## Commands
```
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m fomo_agent.cli init
.venv/Scripts/python -m fomo_agent.cli new-tokens --dry-run
.venv/Scripts/python -m fomo_agent.cli run --once
```
