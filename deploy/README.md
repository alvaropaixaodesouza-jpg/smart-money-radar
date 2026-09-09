# Deploying FOMO Robinhood Radar

What runs in production, why it is arranged this way, and how to change it. Every file in this
directory is a copy of what is actually live — if you edit the server by hand, copy it back here.

## The machine

Contabo Cloud VPS 4, Ubuntu 26.04 LTS, 4 vCPU, 8 GB, 118 GB. Germany, because the bot has to reach
`api.telegram.org` and some jurisdictions block it at the ISP.

The 8 GB is not for what runs today — that fits in about 700 MB. It is headroom for Chrome under
Xvfb, the one piece that would make fomo collection fully unattended.

## What runs

| unit | what it is | port |
|---|---|---|
| `radar-api` | FastAPI over the sqlite database | 127.0.0.1:8000 |
| `radar-site` | Astro, server-rendered | 127.0.0.1:4321 |
| `radar-bot` | Telegram long-polling bot | — |
| `radar-collect.timer` | resolve → track → name tokens, every 15 min | — |
| `radar-backup.timer` | sqlite `.backup` nightly, 14 kept | — |
| `radar-health.timer` | what is quietly broken, pushed to the bot at 07:40 | — |
| `radar-digest.timer` | the day in one message to every subscriber, 18:00 | — |
| `radar-xvfb` / `radar-wm` / `radar-browser` | the signed-in Chrome that collects fomo | — |
| `radar-crx` | serves the extension's update manifest to that Chrome | 127.0.0.1:8098 |
| `caddy` | the only thing listening publicly | 80, 443 |

Nothing but Caddy is reachable from outside. `ufw` allows 22, 80 and 443 and nothing else.

## Layout

```
/opt/fomoradar/
  app/            the repository
  app/.env        secrets — written once at provision, never shipped by a deploy
  venv/           python 3.14
  fomo_agent.db   the database
  backups/        db-YYYYMMDD.gz, fourteen nights
```

The service account `radar` owns all of it and has `nologin` as its shell. Each unit runs with
`ProtectSystem=strict` and `ReadWritePaths=/opt/fomoradar`, so a compromised service can write to
exactly one directory.

## Shipping a change

From the repository root:

```bash
deploy/update.sh
```

It packs the working tree, uploads it, reinstalls dependencies, **runs the test suite on the
server**, rebuilds the site and restarts the three services. It never touches the database and it
preserves `.env` across the deploy — that file belongs to the server, not to the repository.

## Logs

Everything goes to the journal, so rotation is journald's job rather than a logrotate file.
`journald/fomoradar.conf` caps it at a gigabyte and thirty days — about a month of this workload,
and longer than anybody looks back. Copy it to `/etc/systemd/journald.conf.d/` and restart
`systemd-journald`.

## Access

Key-only. Password authentication is off in `/etc/ssh/sshd_config.d/99-fomoradar.conf`, and the root
password was rotated to a value nobody holds. If console access is ever needed, reset the password
from the Contabo panel and use their VNC console.

```bash
ssh -i ~/.ssh/fomoradar root@193.233.209.98
```

## Adding a domain

Point an A record at the IP, then change one line in `/etc/caddy/Caddyfile`:

```diff
-:80 {
+fomoradar.xyz, www.fomoradar.xyz {
```

`systemctl reload caddy` and the certificate arrives on its own. Then set `API_CORS_ORIGINS` and
`PUBLIC_SITE_URL` in `.env` so canonical links and previews use the hostname instead of the IP.

## Looking at it

```bash
systemctl status radar-api radar-site radar-bot
journalctl -u radar-bot -f
journalctl -u radar-collect -n 40 --no-pager
systemctl list-timers 'radar-*'
```

## The one thing that is not automatic

fomo.family is behind Cloudflare, which refuses every non-browser client. Its data therefore comes
from a real logged-in browser: the Chrome extension in `extension/` posts collections to
`fomo-radar receive`.

Today that runs at home. Moving it to the server means Chrome under Xvfb with a persistent profile,
and the open question is whether Cloudflare accepts a datacentre address — nobody can answer that
without trying. Everything else already runs here regardless, so if the answer turns out to be no,
the only cost is that a browser tab stays open at home.
