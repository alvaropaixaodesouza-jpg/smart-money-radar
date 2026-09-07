# The tunnel

fomo.family refuses every non-browser client, so its data has to come from a real logged-in Chrome.
That browser runs at home; the database it feeds lives on the server. These three files bridge the
two.

## How it works

The extension posts to `http://127.0.0.1:8787/ingest` — its own default, unchanged. An SSH local
forward carries that port to the receiver on the server:

```
Chrome extension  ──▶  127.0.0.1:8787 (this machine)
                            │  ssh -L, authenticated by the deploy key
                            ▼
                       127.0.0.1:8787 (server)  ──▶  sqlite
```

Nothing is exposed to the internet. The receiver binds to loopback on the server and is reachable
only through this tunnel, which needs the private key. The shared token is a second lock behind
that: a stray process on the home machine that finds port 8787 still cannot post without it.

## Files

| | |
|---|---|
| `radar-tunnel.bat` | opens the tunnel and reconnects every ten seconds if it drops |
| `radar-tunnel-hidden.vbs` | the same with no console window |
| `radar-tunnel-autostart.bat` | registers the hidden version to run at logon |

## Using it

Double-click `radar-tunnel.bat` and leave the window open, or run
`radar-tunnel-autostart.bat` once to have it start hidden at every logon.

To remove the autostart later:

```
schtasks /Delete /TN "FOMO Radar tunnel" /F
```

## Checking it

```
curl http://127.0.0.1:8787/health
```

A 200 means the tunnel is up and the server is answering. `journalctl -u radar-receive -f` on the
server shows collections arriving.
