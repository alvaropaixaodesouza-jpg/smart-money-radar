# fomo-agent collector (Chrome extension)

Collects your own fomo.family data in your own browser and posts it to a local fomo-agent receiver.
It reads only your own fomo data, and sends it nowhere except `127.0.0.1`.

## Why an extension

`prod-api.fomo.family` sits behind Cloudflare, which rejects every non-browser client at the edge —
even the exact cURL command Chrome generates, replayed with a fresh token, comes back
`430 {"error":"unauthorized"}`. Rather than pretending to be a browser, this runs *in* one: the
requests are issued by the fomo.family page itself, with its own session, and the fomo bearer token
(which expires hourly) is re-learned automatically each time the app makes a request.

## Install

1. Start the receiver in the repo:

```bash
.venv/Scripts/python -m fomo_agent.cli receive
```

2. Open `chrome://extensions`, turn on **Developer mode** (top right), click **Load unpacked**, and
   select this `extension/` folder.
3. Open <https://fomo.family> and log in as usual. Click anything that loads data (the Leaderboard
   tab) so the collector can learn the token.
4. Click the extension icon. Check the endpoint (`http://127.0.0.1:8787/ingest`), set the interval,
   press **Save**, then **Collect now**.

The popup shows the last run, how many wallets were sent, and what the receiver replied.

## What it collects

| | |
|---|---|
| `/v2/leaderboard/24h\|7d\|30d` | who is winning, with fomo's own PnL |
| `/v2/users/{id}/swaps` | for the top N traders — the wallets they **actually** trade from, which the profile does not show |
| `/hodlers/top` | top holders, when a token page happens to be open |

Wallets already resolved are remembered, so later runs spend their budget on new faces. **Forget
resolved** clears that list.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Receiver endpoint | `http://127.0.0.1:8787/ingest` | must match `RECEIVER_HOST`/`RECEIVER_PORT` |
| Shared token | empty | set it here and as `RECEIVER_TOKEN` in `.env` to reject anything else on that port |
| Every (minutes) | 30 | minimum 5 |
| Wallets per run | 25 | one request each, paced 350 ms apart |

## Requirements and limits

- A fomo.family tab must be open when a collection runs; that is what keeps the requests
  indistinguishable from the app's own.
- Chrome suspends the service worker when idle. The alarm wakes it, so intervals under 5 minutes are
  ignored.
- If the token has expired and you have not touched the app, the run fails and retries on the next
  alarm — opening the tab is enough to refresh it.

## Troubleshooting

| Popup says | Fix |
|---|---|
| `no fomo.family tab open` | open <https://fomo.family> in any tab |
| `could not reach the page` | reload the fomo.family tab (content scripts only attach after a load) |
| `no token seen yet` | click a data-loading page in the app, then Collect now |
| `receiver unreachable` | is `cli receive` running, and does the port match? |
