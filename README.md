# fomo-agent

Research tool that finds strong Solana memecoin traders via [fomo.family](https://fomo.family), tracks their swaps on-chain (Helius), scores them with Claude, and keeps a local SQLite watchlist (`active / watch / dropped`).

**Read-only. It never signs or sends transactions.**

Architecture rule: fomo is only a *discovery* source (wallet addresses + PnL). Tracking and analytics are on-chain and work without fomo.

Chains: the new-token trigger watches `DEX_CHAINS` (default `solana,base,robinhood`). On-chain wallet tracking via Helius is **Solana only** for now; EVM wallets are stored but skipped by `track` until an EVM transaction source is added.

### Data sources

**New tokens** (`TOKEN_SOURCES`), merged and deduped per chain+address:

| Source | What it sees | Cost / limits |
|---|---|---|
| `codex` | one GraphQL `filterTokens` across all chains with server-side mcap / liquidity / age filters, plus sniper / bundler / top-10 holder percentages | needs `CODEX_API_KEY`; Almost Free plan: $1 one-time, 10k requests/month, 5 rps |
| `geckoterminal` | trending / top-volume pools per chain, no boost needed (`GECKO_FEEDS`) | free, ~30 rpm |
| `dexscreener` | only tokens that bought a DexScreener profile/boost | free, 60 rpm |

**Wallet trades** (`TRACK_SOURCES`), first source that supports the wallet's chain wins:

| Source | Chains | Notes |
|---|---|---|
| `trenches` | robinhood | [robinhoodtrenches.com](https://robinhoodtrenches.com), free and keyless. One tape request covers every wallet in a pass |
| `codex` | solana, base, robinhood | returns USD per trade; 1+ request per wallet per pass |
| `helius` | solana | needs `HELIUS_API_KEY`; free tier is 1M credits/month and an Enhanced Transactions call costs 100 credits |

**Third-party indexer**: `robinhoodtrenches.com` publishes a keyless JSON API over ~108 curated fomo.family
traders on Robinhood Chain. `fomo-agent discover --trenches` imports them with their execution wallets
already resolved — the mapping fomo's own API hides behind Cloudflare — plus realized PnL, win rate and
hold times that feed straight into the scoring context. `fomo-agent trenches` shows its health and a
live peek. It is unofficial: every call fails soft and the loop runs without it.

**Trader discovery**: `discover --mint <mint> --makers` pulls the wallets currently buying a token and stores those above `DISCOVER_MIN_BUY_USD` as candidates. This works without fomo, and `new-tokens` runs it automatically on the biggest fresh tokens.

### Codex request budget

The Almost Free plan allows 10,000 requests/month and every feature above spends from it. `fomo-agent init` prints a projection for your current config and warns when it exceeds the cap. The shipped intervals land around 9.5k/month.

### Scoring without an Anthropic API key

`SCORER=auto` uses the API when `ANTHROPIC_API_KEY` is set, and otherwise switches to a manual loop you drive from a Claude chat:

```bash
fomo-agent score --export pending_scores.json   # contexts + schema + instructions
# hand that file to Claude, get a JSON array back, save it as scored.json
fomo-agent score --import scored.json --model-label "manual:claude-opus-5"
```

Imported scores go through the same pydantic validation and land in the same tables as API scores. See `docs/example_scores.json` for the expected shape.

## Status

Detailed state, blockers and prioritized work list: [docs/STATUS.md](docs/STATUS.md).

| Phase | State |
|---|---|
| 0. fomo recon (`scripts/capture_fomo_endpoints.py`, `docs/fomo-endpoints.md`) | script ready, capture **not done** |
| 1. skeleton + discovery | done (fomo adapter stubbed until phase 0) |
| 2. DexScreener trigger + Helius tracking | done, needs `HELIUS_API_KEY` to run |
| 3. Claude scoring | done, needs `ANTHROPIC_API_KEY` |
| 4. report + `run` loop | basic version done |
| 5. SKILL.md + GitHub | minimal SKILL.md |

## Install

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[capture,dev]"
.venv/Scripts/playwright install chromium
copy .env.example .env   # fill keys
```

## Commands

```bash
fomo-agent init                                  # create db, show config
fomo-agent new-tokens --dry-run                  # what DexScreener returns right now
fomo-agent new-tokens                            # store fresh tokens, trigger holder discovery
fomo-agent discover --add <wallet> [--chain base] # manual candidate (no fomo needed)
fomo-agent discover --leaderboard                # fomo leaderboard 24h/7d/30d  (phase 0 required)
fomo-agent discover --mint <mint> --makers       # buyers of a token -> candidates (Codex)
fomo-agent discover --mint <mint>                # fomo top-PnL holders          (phase 0 required)
fomo-agent track [--address <wallet>] [--show]   # pull swaps (Codex or Helius)
fomo-agent score [--address <wallet>] [--deep] [--force] [--show-context]
fomo-agent score --export pending.json / --import scored.json   # in-chat scoring
fomo-agent report [--hours 24] [--out report.md]
fomo-agent page --out radar.html            # scored watchlist as a standalone HTML page
fomo-agent run [--once]                          # polling loop
fomo-agent receive                               # local endpoint for the browser extension
fomo-agent trenches [--window 7d] [--tape 10]    # third-party indexer health + peek
fomo-agent fomo-import <file>                    # load a browser export
```

Or `python -m fomo_agent.cli ...` from the repo.

## Getting fomo data

fomo's API cannot be called from a server: Cloudflare rejects every non-browser client at the edge
with `430 {"error":"unauthorized"}`, including the exact cURL Chrome generates with a fresh token.
Its Privy bearer also expires hourly. So fomo data is collected *in* a browser, two ways:

- **Automatic** — load `extension/` as an unpacked Chrome extension and run `fomo-agent receive`.
  It collects on a schedule from your logged-in tab and posts straight into the database.
  See [extension/README.md](extension/README.md).
- **Manual** — paste `scripts/fomo_export.js` into the DevTools console, then
  `fomo-agent fomo-import <downloaded file>`.

Both produce the same payload and go through the same parsers. For Robinhood Chain you may not need
either: `discover --trenches` gets the same handle-to-wallet mapping for free.

## Getting `FOMO_SESSION` (legacy)

Run the phase 0 capture script, log in, browse; the script prints which cookie/header carries the session (values masked). Paste it into `.env`. Then document endpoints in `docs/fomo-endpoints.md` and fill the TODOs in `fomo_agent/sources/fomo.py`.

## Disclaimer

fomo.family has no public API. This uses whatever the web app calls internally, which can change or break at any time, and may violate their terms — your account could be banned. Use at your own risk. Nothing here is financial advice.

## License

MIT
