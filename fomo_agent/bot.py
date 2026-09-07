"""Telegram bot — the one surface that comes to the reader instead of waiting to be opened.

Everything else we build has to be visited. A signal is only worth something while it is fresh, so
this pushes: when a second wallet scoring 60+ enters a token, subscribers hear about it within a
minute. On demand it also answers the two questions the terminal answers — whose money is in this
token, and what is this trader actually doing.

Deliberately dependency-free: the Bot API is plain HTTP and we already ship httpx. It uses long
polling rather than webhooks, so it runs from a laptop behind NAT exactly as well as from a server
with a public address — which keeps the hosting decision open.

Messages are formatted as instrument readouts, per the design system: a monospace block with
aligned columns, no decoration, nothing that has to be scrolled to read on a phone.
"""
from __future__ import annotations

import html
import logging
import time

import httpx

from . import db
from .config import settings
from .pipeline import analyze

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
ADDRESS_LEN = 42  # 0x + 40 hex


class TelegramError(RuntimeError):
    pass


# ---------------------------------------------------------------- transport

class Telegram:
    """The four Bot API calls we need, and nothing else."""

    def __init__(self, token: str | None = None, client: httpx.Client | None = None):
        self.token = token or settings.telegram_bot_token
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set (talk to @BotFather, see .env.example)")
        # api.telegram.org is blocked by some ISPs, Russian ones included: DNS resolves, the TCP
        # connection then times out. A proxy fixes it locally; on a server outside that jurisdiction
        # none is needed, which is the real reason the bot belongs on the server.
        # The read timeout has to outlast a long poll, or every idle poll looks like a failure.
        self.http = client or httpx.Client(
            timeout=settings.telegram_poll_timeout + 15,
            proxy=settings.telegram_proxy or None,
        )
        self.requests = 0

    def call(self, method: str, **params) -> object:
        self.requests += 1
        r = self.http.post(API.format(token=self.token, method=method), json=params)
        if r.status_code == 409:
            raise TelegramError("another copy of this bot is already polling — stop it first")
        if r.status_code == 401:
            raise TelegramError("TELEGRAM_BOT_TOKEN rejected — check it, or /revoke a new one")
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")
        return body.get("result")

    def send(self, chat_id, text: str, preview: bool = False) -> object:
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         disable_web_page_preview=not preview)

    def updates(self, offset: int, timeout: int | None = None) -> list:
        return self.call("getUpdates", offset=offset, allowed_updates=["message"],
                         timeout=timeout if timeout is not None else settings.telegram_poll_timeout)

    def me(self) -> dict:
        return self.call("getMe")


# ---------------------------------------------------------------- formatting

def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def ago(ts: int | None, now: int | None = None) -> str:
    if not ts:
        return "—"
    d = max((now or db.now()) - ts, 0)
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 14 else addr


def rows(pairs: list[tuple[str, str]], width: int = 17) -> str:
    """A block of label/value lines that line up — the readout look, inside <pre>."""
    body = "\n".join(f"{k:<{width}}{v:>9}" for k, v in pairs)
    return f"<pre>{esc(body)}</pre>"


def who_line(handles: str | None, scores: str | None, limit: int = 6) -> str:
    """`unipcs 92 · rasmr 88 · …` — names carry more than a count does."""
    hs = [h for h in (handles or "").split(",") if h]
    ss = [s for s in (scores or "").split(",") if s]
    pairs = [f"{esc(h)} {esc(s)}" for h, s in zip(hs, ss)][:limit]
    tail = f" +{len(hs) - limit}" if len(hs) > limit else ""
    return " · ".join(pairs) + tail


def fmt_signal(s: dict, now: int | None = None) -> str:
    sym, mint = esc(s["sym"]), s["mint"]
    out = [f"◤ <b>${sym}</b> · conviction {s['conviction']:.1f}", ""]
    out.append(rows([
        ("buyers 60+", str(s["buyers"])),
        ("average score", f"{s['avg_score']:.0f}"),
        ("first entry", ago(s["first_ts"], now) + " ago"),
        ("bought", analyze.usd(s["usd"])),
        ("liquidity", analyze.usd(s["liq"])),
    ]))
    out.append(who_line(s.get("who"), s.get("scores")))
    out.append(f"\n<code>{esc(mint)}</code>")
    out.append(f'<a href="https://dexscreener.com/robinhood/{esc(mint)}">chart</a> · '
               f"/token_{esc(mint)}")
    return "\n".join(out)


def fmt_signals(sigs: list[dict], hours: int, now: int | None = None) -> str:
    if not sigs:
        return (f"No token has two trusted buyers in the last {hours}h.\n\n"
                "That is information too — the cohort is sitting still.")
    out = [f"<b>SIGNALS · {hours}h · Robinhood Chain</b>",
           "<i>ranked by conviction, not by headcount</i>", ""]
    for i, s in enumerate(sigs, 1):
        out.append(f"{i:>2}. <b>${esc(s['sym'])}</b>  conviction {s['conviction']:.1f}"
                   f"  ·  {s['buyers']} buyers, avg {s['avg_score']:.0f}")
        out.append(f"    <i>{who_line(s.get('who'), s.get('scores'), 4)}</i>")
    out.append("\nSend a token address for the full breakdown.")
    return "\n".join(out)


def fmt_token(a: dict) -> str:
    name = esc(a["symbol"] or short(a["mint"]))
    if a["is_quote"]:
        return (f"<b>${name}</b> is a quote asset.\n\nEvery swap on this chain passes through it, "
                "so holdings and buys here are plumbing, not conviction. Nothing to read.")
    if not a["holders"] and not a["flow"]:
        return (f"<b>${name}</b>\n<code>{esc(a['mint'])}</code>\n\n"
                "Nobody on the watchlist holds this or has traded it. That is a real answer: "
                "no smart money we track is in this name.")
    out = [f"<b>${name}</b> · conviction {a['conviction']:.1f}", ""]
    out.append(rows([
        ("holders", str(len(a["holders"]))),
        ("of them 60+", str(a["trusted_holders"])),
        ("average score", f"{a['avg_score']:.0f}" if a["avg_score"] else "—"),
        ("cohort cost", analyze.usd(a["cohort_cost"])),
        ("open PnL", analyze.usd(a["cohort_pnl"])),
        (f"bought {a['hours']}h", analyze.usd(a["bought_usd"])),
        (f"sold {a['hours']}h", analyze.usd(a["sold_usd"])),
        ("liquidity", analyze.usd(a["liquidity_usd"])),
    ]))
    if a["holders"]:
        out.append("<b>Held by</b>")
        body = "\n".join(
            f"{(h['handle'] or short(h['address'])):<18}{str(h['score'] or '--'):>3}"
            f"{analyze.usd(h['pnl']):>10}"
            for h in a["holders"][:8])
        out.append(f"<pre>{esc(body)}</pre>")
    out.append(f"<code>{esc(a['mint'])}</code>")
    return "\n".join(out)


def fmt_trader(a: dict) -> str:
    mark = {"active": "◤", "watch": "◈", "dropped": "◣"}.get(a["status"], "·")
    out = [f"{mark} <b>{esc(a['handle'] or short(a['address']))}</b> · "
           f"{a['score']} · {esc(a['status'])}"]
    if a["style"] or a["red_flags"]:
        tags = ", ".join(a["style"]) + ("  ⚠ " + ", ".join(a["red_flags"]) if a["red_flags"] else "")
        out.append(f"<i>{esc(tags)}</i>")
    if a["summary"]:
        out.append(f"\n{esc(a['summary'])}")
    s = a["stats"] or {}
    out.append("")
    out.append(rows([
        ("fomo 30d", analyze.usd(a["fomo_pnl"])),
        ("open bags", str(len(a["positions"]))),
        ("open PnL", analyze.usd(a["open_pnl"])),
        ("realized", analyze.usd(s.get("realized_pnl")) if s.get("realized_pnl") is not None else "—"),
        ("win rate", f"{s['win_rate'] * 100:.0f}%" if s.get("win_rate") is not None else "—"),
        (f"bought {a['hours']}h", analyze.usd(a["bought_usd"])),
        (f"sold {a['hours']}h", analyze.usd(a["sold_usd"])),
    ]))
    if a["positions"]:
        out.append("<b>Largest positions</b>")
        lines = []
        for p in a["positions"][:5]:
            cost, pnl = p.get("cost"), p.get("pnl")
            mult = f"  {(cost + pnl) / cost:.1f}x" if cost and pnl is not None else ""
            lines.append(f"{p['sym']:<14}{analyze.usd(pnl):>10}{mult}")
        out.append(f"<pre>{esc(chr(10).join(lines))}</pre>")
    out.append(f"<code>{esc(a['address'])}</code>")
    return "\n".join(out)


def fmt_leaderboard(board: list[dict], status: str) -> str:
    if not board:
        return "Nothing scored yet."
    label = {"active": "FOLLOW", "watch": "WATCH", "dropped": "DROPPED"}.get(status, status.upper())
    out = [f"<b>LEADERBOARD · {label}</b>",
           "<i>ranked by judgement, not by headline PnL</i>", ""]
    body = "\n".join(
        f"{i:>2}. {(r['handle'] or short(r['address']))[:17]:<18}{r['score']:>3}"
        f"{analyze.usd(r['fomo_pnl']):>9}"
        for i, r in enumerate(board, 1))
    out.append(f"<pre>{esc(body)}</pre>")
    out.append("Send a handle for the full verdict.")
    return "\n".join(out)


HELP = """<b>FOMO ROBINHOOD RADAR</b>
<i>which fomo.family traders on Robinhood Chain actually know what they are doing</i>

/signals — what trusted wallets are buying now
/top — the scored leaderboard
/watch, /dropped — the other two verdicts
/subscribe — get signals pushed as they happen
/unsubscribe — stop
/status — what the database holds

Or just send me:
· a token address → who holds it and at what cost
· a trader handle → the verdict and their book

Read-only research. Never trades. Not financial advice."""


# ---------------------------------------------------------------- subscriptions

def subscribe(conn, chat_id, username: str | None, min_conviction: float | None = None) -> bool:
    """Returns True when this is a new subscriber rather than a threshold change."""
    existed = conn.execute("SELECT 1 FROM bot_subscribers WHERE chat_id=?", (str(chat_id),)).fetchone()
    with db.tx(conn):
        conn.execute(
            "INSERT INTO bot_subscribers(chat_id, username, min_conviction, subscribed_at, active) "
            "VALUES(?,?,?,?,1) ON CONFLICT(chat_id) DO UPDATE SET "
            "  username=excluded.username, min_conviction=excluded.min_conviction, active=1",
            (str(chat_id), username, min_conviction if min_conviction is not None
             else settings.telegram_min_conviction, db.now()),
        )
    return existed is None


def unsubscribe(conn, chat_id) -> None:
    with db.tx(conn):
        conn.execute("UPDATE bot_subscribers SET active=0 WHERE chat_id=?", (str(chat_id),))


def subscribers(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM bot_subscribers WHERE active=1")]


def already_sent(conn, chat_id, mint: str, within_s: int) -> bool:
    row = conn.execute(
        "SELECT ts FROM bot_sent WHERE chat_id=? AND mint=? ORDER BY ts DESC LIMIT 1",
        (str(chat_id), mint),
    ).fetchone()
    return bool(row) and (db.now() - row["ts"]) < within_s


def mark_sent(conn, chat_id, mint: str) -> None:
    with db.tx(conn):
        conn.execute("INSERT INTO bot_sent(chat_id, mint, ts) VALUES(?,?,?)",
                     (str(chat_id), mint, db.now()))


def broadcast(conn, tg: Telegram) -> dict:
    """Push every signal each subscriber has not already been told about."""
    stats = {"subscribers": 0, "sent": 0, "skipped": 0, "errors": 0}
    subs = subscribers(conn)
    if not subs:
        return stats
    stats["subscribers"] = len(subs)
    sigs = analyze.signals(conn, settings.dex_chains[0] if settings.dex_chains else None,
                           hours=settings.telegram_alert_window_h, limit=10)
    quiet = settings.telegram_realert_hours * 3600
    for sub in subs:
        floor = sub["min_conviction"] if sub["min_conviction"] is not None else settings.telegram_min_conviction
        for s in sigs:
            if (s["conviction"] or 0) < floor:
                continue
            if already_sent(conn, sub["chat_id"], s["mint"], quiet):
                stats["skipped"] += 1
                continue
            try:
                tg.send(sub["chat_id"], fmt_signal(s))
                mark_sent(conn, sub["chat_id"], s["mint"])
                stats["sent"] += 1
            except Exception as e:  # noqa: BLE001 - one blocked chat must not stop the rest
                stats["errors"] += 1
                log.warning("send to %s failed: %s", sub["chat_id"], e)
                if "bot was blocked" in str(e) or "chat not found" in str(e):
                    unsubscribe(conn, sub["chat_id"])
    if stats["sent"]:
        log.info("broadcast: %s", stats)
    return stats


# ---------------------------------------------------------------- commands

def status_text(conn) -> str:
    def q(sql: str, *args) -> int:
        return conn.execute(sql, args).fetchone()[0]

    return "<b>DATABASE</b>\n" + rows([
        ("traders", f"{q('SELECT COUNT(*) FROM traders'):,}"),
        ("scored", f"{q('SELECT COUNT(*) FROM traders WHERE score IS NOT NULL'):,}"),
        ("follow", f"{q('SELECT COUNT(*) FROM traders WHERE status=?', 'active'):,}"),
        ("fills", f"{q('SELECT COUNT(*) FROM trades'):,}"),
        ("positions", f"{q('SELECT COUNT(*) FROM fomo_positions'):,}"),
        ("subscribers", f"{q('SELECT COUNT(*) FROM bot_subscribers WHERE active=1'):,}"),
    ], width=15)


def handle_text(conn, text: str, chat_id, username: str | None) -> str:
    """Route one message to its answer. Pure enough to test without a network."""
    text = (text or "").strip()
    if not text:
        return HELP
    parts = text.split()
    cmd, args = parts[0].lower().split("@")[0], parts[1:]

    # /token_0xabc… — the deep link the signal message offers
    if cmd.startswith("/token_"):
        cmd, args = "/token", [cmd[len("/token_"):]]

    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return status_text(conn)
    if cmd == "/signals":
        hours = int(args[0]) if args and args[0].isdigit() else 24
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_signals(analyze.signals(conn, chain, hours=hours, limit=10), hours)
    if cmd in ("/top", "/watch", "/dropped"):
        status = {"/top": "active", "/watch": "watch", "/dropped": "dropped"}[cmd]
        n = int(args[0]) if args and args[0].isdigit() else 15
        return fmt_leaderboard(analyze.leaderboard(conn, min(n, 40), status), status)
    if cmd == "/subscribe":
        floor = float(args[0]) if args and args[0].replace(".", "", 1).isdigit() else None
        fresh = subscribe(conn, chat_id, username, floor)
        level = floor if floor is not None else settings.telegram_min_conviction
        return (("Subscribed." if fresh else "Threshold updated.") +
                f" You will get a signal when its conviction reaches <b>{level:g}</b>.\n\n"
                "That is roughly what four wallets scoring 80 look like. "
                "Send <code>/subscribe 3</code> for more, <code>/subscribe 12</code> for fewer.")
    if cmd == "/unsubscribe":
        unsubscribe(conn, chat_id)
        return "Unsubscribed. /subscribe turns it back on."
    if cmd == "/token":
        if not args:
            return "Send it as <code>/token 0x…</code>, or just paste the address."
        return fmt_token(analyze.analyze_token(conn, args[0]))
    if cmd == "/trader":
        if not args:
            return "Send it as <code>/trader unipcs</code>, or just send the handle."
        a = analyze.analyze_trader(conn, args[0])
        return fmt_trader(a) if a else f"No trader matches <b>{esc(args[0])}</b>."
    if cmd.startswith("/"):
        return f"Unknown command {esc(cmd)}.\n\n" + HELP

    # bare text: an address is a token, anything else is a handle
    if text.startswith("0x") and len(text) == ADDRESS_LEN:
        a = analyze.analyze_trader(conn, text)
        return fmt_trader(a) if a else fmt_token(analyze.analyze_token(conn, text))
    a = analyze.analyze_trader(conn, text)
    if a:
        return fmt_trader(a)
    return (f"No trader called <b>{esc(text)}</b>, and that is not a token address.\n\n"
            "Send a 0x… address for a token, a handle for a trader, or /signals.")


def handle_update(conn, tg: Telegram, update: dict) -> bool:
    msg = update.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    if chat is None or not msg.get("text"):
        return False
    user = (msg.get("from") or {}).get("username")
    try:
        answer = handle_text(conn, msg["text"], chat, user)
    except Exception as e:  # noqa: BLE001 - a bad question must not kill the bot
        log.exception("handling %r failed", msg.get("text"))
        answer = f"That broke something: <code>{esc(type(e).__name__)}</code>. Try /help."
    tg.send(chat, answer)
    return True


def run(conn, tg: Telegram | None = None, once: bool = False) -> dict:
    """Long-poll for questions and push signals on a timer, in one loop."""
    tg = tg or Telegram()
    who = tg.me()
    log.info("bot @%s online", who.get("username"))
    stats = {"handled": 0, "broadcasts": 0, "sent": 0}
    offset, last_alert = 0, 0.0
    while True:
        try:
            for u in tg.updates(offset) or []:
                offset = max(offset, u["update_id"] + 1)
                stats["handled"] += handle_update(conn, tg, u)
        except TelegramError:
            raise
        except Exception as e:  # noqa: BLE001 - a dropped poll is normal, keep going
            log.warning("poll failed: %s", e)
            time.sleep(5)

        if time.monotonic() - last_alert >= settings.telegram_alert_interval_s:
            last_alert = time.monotonic()
            try:
                stats["sent"] += broadcast(conn, tg)["sent"]
                stats["broadcasts"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("broadcast failed: %s", e)
        if once:
            return stats
