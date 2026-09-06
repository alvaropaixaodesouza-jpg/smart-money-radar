# fomo.family internal API — phase 0 findings

> STATUS: **CAPTURED 2026-09-04** from a HAR recorded in a real browser (314 requests, 296 API calls).
> `fomo_agent/sources/fomo.py` implements only what is documented here. Nothing is guessed.
>
> **Server-side calls do not work.** Cloudflare rejects every non-browser client at the edge with
> `430 {"error":"unauthorized"}` — verified by replaying the exact cURL Chrome generates, with a
> fresh valid token, from both `curl` and `httpx`. The brief forbids fighting that, so fomo data
> comes in through [the browser export](#fallback-that-actually-works-browser-export) instead.
> `FomoClient` is kept because it is correct and the parsers are shared, but it will only ever work
> from inside a browser context.

## How to capture

### Endpoints and payloads: HAR from your own browser

Google refuses to authenticate inside automation-driven browsers, so Playwright cannot log in.
Record traffic in the browser you normally use:

1. Log into fomo.family as usual.
2. `F12` → **Network** → tick **Preserve log** → filter **Fetch/XHR**.
3. Browse: leaderboard 24h / 7d / 30d, a token page → **Holders**, a trader profile. Scroll to paginate.
4. Toolbar arrow ↓ (**Export HAR**) → save through the browser's own dialog. Do not route it through the
   clipboard: that is what truncated the first attempt at exactly 100 000 bytes.
5. Parse it:

```bash
.venv/Scripts/python scripts/parse_fomo_har.py fomo.har
```

The parser decodes base64 bodies, drops static assets and third-party hosts, masks auth values, and
prints a digest that is safe to share. `*.har` is gitignored; keep it local.

### The session: one "Copy as cURL"

Chrome strips `Cookie` and `Authorization` from an exported HAR, so the credential must come separately:

1. In the same Network tab, right-click a request to `prod-api.fomo.family` that returned 200.
2. **Copy → Copy as cURL (bash)**, paste into `curl.txt`.
3. Run, then delete the file:

```bash
.venv/Scripts/python scripts/fomo_session_from_curl.py curl.txt
```

It writes `FOMO_SESSION`, `FOMO_AUTH_KIND`, `FOMO_BASE_URL` and `FOMO_SUPPORTED_CHAINS` into `.env`,
printing only lengths. Verify with `python -m fomo_agent.cli fomo-check`.

## Base URL

`https://prod-api.fomo.family` — every data endpoint below. Other hosts seen in the capture and
deliberately ignored: `app-actions*.fomo.family` and `evm-data.prod-edge.fomo.family` (Datadog RUM
telemetry), `fomo-api.mobula.io` (price charts), `status.fomo.family`.

## Auth

- `Authorization: Bearer <JWT>` issued by **Privy** (`iss: privy.io`). Chrome strips it from HAR exports,
  so it is only visible via *Copy as cURL*.
- **TTL is 1 hour** (`exp - iat = 3600`), measured on a captured token. Any design that assumes a
  long-lived fomo session is wrong.
- A `__cf_bm` Cloudflare cookie rides along; it is bound to the browser, not to the account.
- Every request also carries `x-supported-chains` (observed: `1,56,143,4663,8453,1399811149`).
- Failures: `430` / `431` with `{"error":"unauthorized"}` and `server: cloudflare`. `430` from the edge
  means the *client* was rejected, not the credentials — `FomoClient` raises `FomoEdgeBlocked` for that
  and points at the browser export.

### What was tried before concluding this

| attempt | result |
|---|---|
| bearer only | 430 |
| bearer + `__cf_bm` cookie | 430 |
| bearer + browser UA + origin + referer | 430 |
| every header from the cURL, including Datadog tracing | 430 |
| the untouched cURL command replayed by `curl.exe` 8.18 | 430 |

The response carries `server: cloudflare` and a `cf-ray`, with none of the app's own headers, so the
block happens at the edge on client fingerprint. Per the brief ("не пытайся обходить защиту
агрессивно"), TLS-impersonating clients were not attempted.

## Networks

| networkId | chain |
|---|---|
| 1399811149 | solana |
| 4663 | robinhood |
| 8453 | base |
| 56 | bsc |

## Endpoints

### Leaderboard

```
GET /v2/leaderboard/{24h|7d|30d}     -> 150 entries, no pagination parameters observed
GET /v2/leaderboard                  -> same shape, default window
GET /v2/clans/leaderboard?window=30d&limit=50   (clans, not individual traders — unused)
```

Response: `{success, message, responseObject: {leaderboard: [...]}, statusCode}`. Each entry:

| field | meaning |
|---|---|
| `id` | fomo user UUID — the key for every other user endpoint |
| `address` | Solana **profile** address (not the trading wallet, see below) |
| `evmAddress` | EVM **profile** address |
| `userHandle`, `displayName` | handle used in profile URLs |
| `pnl30d` (resp. `pnl7d`, `pnl24h`) | realized PnL in USD for the window |
| `numTrades`, `swapCount`, `totalVolume` | activity totals |
| `totalHoldings`, `topHoldings` | current positions |
| `clan`, `followers`, `verified`, `isRestricted` | social metadata |

### Top holders of a token

```
GET /hodlers/top?tokens=[{"address":"<mint>","networkId":<id>}]
GET /hodlers/devs?tokenAddress=<mint>&networkId=<id>
```

`responseObject` is a list per requested token: `{tokenAddress, networkId, topHolders: [...]}` with 50
holders. Each holder: `user` (same object as a leaderboard entry), plus `pnl`, `realizedPnl`,
`unrealizedPnl`, `costBasis`, `value`, `humanAmount`, `averageEntryPrice`, `averageHoldTimeSeconds`,
`isDev`, `tradeId`, and an optional `comment`.

### Users

```
GET /v2/users/{userId}
GET /v2/users/userHandle/{handle}
GET /v2/users?userIds=<uuid,uuid>
GET /v2/users/{userId}/swaps[?tokenAddress=<mint>]
GET /v2/users/{userId}/balances
GET /v2/users/{userId}/leaderboard
GET /trades?userId=<uuid>&tokenAddress=<mint>&orderBy=<...>
GET /trades/{tradeId}
```

`/v2/users/userHandle/{handle}` returns the profile: `id, address, evmAddress, userHandle, numTrades,
swapCount, totalVolume, averageHoldTimeSeconds, followers, verified`.

`/v2/users/{id}/swaps` returns `{swaps: [...], hasNextPage}`. Each swap:
`id, address, recipient, networkId, inNetworkId, outNetworkId, inTokenAddress, inAmount, inHumanAmount,
outTokenAddress, outAmount, outHumanAmount, humanUsdAmountIn, humanUsdAmountOut, createdAt,
inTradeId, outTradeId, isOffPlatform, isCrossmint, provider`.

Swaps can be cross-chain (pay in Solana USDC, receive a Robinhood token), so `networkId` alone does not
identify the chain of an address — use `inNetworkId` for `address` and `outNetworkId` for `recipient`.

`/trades/{id}` returns an aggregated position: `avgEntryPrice, avgExitPrice, realizedPnlUsd,
unrealizedPnlUsd, totalCostBasis, humanTokenAmount, closedAt`, plus the swaps that make it up.

### Proxied market data (not used by us)

`POST /proxy/filterTokens`, `/proxy/tokenDetails`, `/proxy/tokenWarnings`, `/proxy/verifiedTokens`,
`/proxy/mostHeld`, `/proxy/cryptoTokens`. `filterTokens` is the Codex query name, i.e. fomo proxies the
same provider we already call directly, so there is nothing to gain here.

## Handle → wallet mapping (critical)

**A profile address is not the wallet that trades.** For `frankdegods`:

| field | value |
|---|---|
| profile `address` | `A5SEXYJY4jTEi6sjMLfZs5KAP8SVFvLDPDV67GgSSZSk` |
| profile `evmAddress` | `0x542b6b4cbab54a6ae6aadccd32a24e8c8b75370c` |
| actual Solana swap wallet | `923mtHovLkMhsniQVsfpKKwXYM5GhaEq3X9RyWxRho4C` (73 of 100 swaps) |
| actual EVM swap wallet | `0x3e98397dca6adda872b161297a0fa34288f623b9` (27 of 100 swaps) |

fomo executes through a per-user execution wallet on each chain. The only way to learn it is
`/v2/users/{id}/swaps` → the `address` / `recipient` fields.

Consequences, implemented in the pipeline:
- leaderboard and holder rows are stored in a `fomo_users` table keyed by fomo UUID;
- a second call per user resolves execution wallets, and only then is a trackable `traders` row created,
  one per chain (`pipeline/discover.py::resolve_execution_wallets`, `FOMO_RESOLVE_LIMIT` per run);
- on-chain tracking always uses the execution wallet, never the profile address.

## Rate limit

Not measured yet — the client stays at 1 req/s (`FOMO_RPS`) with backoff on 429/5xx.
The 30-requests-in-30-seconds check from the brief is still open.

## Fallback that actually works: browser export

`scripts/fomo_export.js` is pasted into the DevTools console on fomo.family. It watches one request the
app makes to learn the Bearer token, then fetches the leaderboards (24h/7d/30d), resolves execution
wallets for the top 25 traders, grabs top holders if a token page is open, and downloads a JSON file.
Nothing is bypassed: the page makes its own requests, in the browser, at 350 ms apart.

```bash
.venv/Scripts/python -m fomo_agent.cli fomo-import fomo_export_<ts>.json
```

The importer feeds the untouched payloads through the same parsers `FomoClient` would use, so both paths
produce identical rows. Because the token lives an hour, this is a manual refresh (a few times a week),
not a polling source. Continuous discovery is Codex's job (`discover --mint <mint> --makers`).

A Playwright DOM scrape remains a theoretical option but is worse: fomo signs in through Google, which
refuses to authenticate inside automation-driven browsers.
