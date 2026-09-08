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


def who_line(handles, scores, limit: int = 6) -> str:
    """`unipcs 92 \u00b7 rasmr 88 \u00b7 …` — names carry more than a count does.

    Takes either a list or the comma-joined string sqlite's GROUP_CONCAT produces, because one
    caller aggregates in SQL and the other in Python.
    """
    def parts(v):
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v if x is not None]
        return [x for x in (v or "").split(",") if x]

    hs, ss = parts(handles), parts(scores)
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


def fmt_fresh(feed: dict, now: int | None = None) -> str:
    """The launches the cohort is entering, hottest first."""
    tokens = feed.get("tokens") or []
    hours = feed.get("hours", 24)
    if not tokens:
        return (f"No young token has {feed.get('min_buyers', 2)} trusted buyers opening a "
                f"position in the last {hours}h.\n\n"
                "The cohort is sitting in what it already holds.")
    out = [f"<b>FRESH \u00b7 {hours}h \u00b7 Robinhood Chain</b>",
           "<i>only what the cohort has just started buying, weighted by how early</i>", ""]
    for i, t in enumerate(tokens, 1):
        lead = t.get("lead_minutes")
        when = "?" if lead is None else (f"{lead:.0f}m" if lead < 90 else f"{lead / 60:.1f}h")
        out.append(f"{i:>2}. <b>${esc(t['sym'])}</b>  heat {t['heat']:.2f}"
                   f"  \u00b7  {t['buyers']} in, first {when} after launch")
        out.append(f"    <i>{who_line(t.get('who'), t.get('scores'), 4)}</i>")
    if feed.get("drained"):
        out.append(f"\n<i>{feed['drained']} more had trusted buying, but the pool is drained.</i>")
    out.append("\nSend a token address for the full breakdown.")
    return "\n".join(out)


def fmt_launch(t: dict, now: int | None = None) -> str:
    """One pushed launch. The lead time is the headline: it is what this feed knows and the other does not."""
    lead = t.get("lead_minutes")
    when = ("unknown" if lead is None
            else "the same minute" if lead < 1
            else f"{lead:.0f} min" if lead < 90
            else f"{lead / 60:.1f}h")
    out = [f"\u25c6 <b>${esc(t['sym'])}</b> \u00b7 launch \u00b7 heat {t['heat']:.2f}", ""]
    out.append(rows([
        ("wallets in", str(t["buyers"])),
        ("average score", f"{t['avg_score']:.0f}" if t.get("avg_score") else "\u2014"),
        ("first wallet in", when),
        ("token age", f"{t['age_h']:.0f}h" if t.get("age_h") else "\u2014"),
        ("bought", analyze.usd(t.get("usd"))),
        ("liquidity", analyze.usd(t.get("liq"))),
    ]))
    out.append(who_line(t.get("who"), t.get("scores")))
    out.append(f"\n<code>{esc(t['mint'])}</code>")
    out.append(f"/token_{esc(t['mint'])}")
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
    out.append("")
    # A win rate is only stated once there are enough decided trades behind it to mean anything.
    rated = a["round_trips"] >= 5 and a["win_rate"] is not None
    out.append(rows([
        ("fomo 30d", analyze.usd(a["fomo_pnl"])),
        ("open names", str(len(a["positions"]))),
        ("open PnL", analyze.usd(a["open_pnl"])),
        ("realised", analyze.usd(a["realized_usd"]) if a["realized_usd"] is not None else "—"),
        ("round trips", f"{a['wins']}/{a['round_trips']}" if rated else str(a["round_trips"] or "—")),
        (f"bought {a['hours']}h", analyze.usd(a["bought_usd"])),
        (f"sold {a['hours']}h", analyze.usd(a["sold_usd"])),
    ]))
    if a["positions"]:
        out.append("<b>Largest positions</b>")
        lines = []
        for p in a["positions"][:6]:
            cost, pnl = p.get("cost"), p.get("pnl")
            mult = f"  {(cost + pnl) / cost:.1f}x" if cost and pnl is not None else ""
            lines.append(f"{p['sym'][:14]:<14}{analyze.usd(pnl):>10}{mult}")
        out.append(f"<pre>{esc(chr(10).join(lines))}</pre>")
    if a["closed"]:
        out.append("<b>What came back out</b>")
        lines = []
        for p in a["closed"][:5]:
            exit_at = "all" if p["state"] == "closed" else f"{(p['exit_pct'] or 0) * 100:.0f}%"
            lines.append(f"{p['sym'][:14]:<14}{analyze.usd(p['realized']):>10}  {exit_at:>4}")
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
/fresh — launches they are entering right now
/top — the scored leaderboard
/watch, /dropped — the other two verdicts
/subscribe — get launches and signals pushed as they happen
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


def due(conn) -> list[tuple[str, dict, str]]:
    """(kind, token, message) for everything worth pushing right now, launches first.

    Two feeds answer two questions and both are worth a message: a launch several good wallets
    entered in the first minutes, and a name the cohort is piling into whenever it opened. They
    overlap — a hot launch is usually also a signal — and the dedup below is per token rather than
    per feed, so whichever describes it first wins and the other stays quiet.
    """
    chain = settings.dex_chains[0] if settings.dex_chains else None
    hours = settings.telegram_alert_window_h
    out = []
    for t in analyze.fresh(conn, chain, hours=hours, limit=10)["tokens"]:
        if t["heat"] >= settings.telegram_min_heat:
            out.append(("launch", t, fmt_launch(t)))
    for s in analyze.signals(conn, chain, hours=hours, limit=10):
        out.append(("signal", s, fmt_signal(s)))
    return out


def broadcast(conn, tg: Telegram) -> dict:
    """Push everything each subscriber has not already been told about."""
    stats = {"subscribers": 0, "sent": 0, "launches": 0, "skipped": 0, "errors": 0}
    subs = subscribers(conn)
    if not subs:
        return stats
    stats["subscribers"] = len(subs)
    items = due(conn)
    quiet = settings.telegram_realert_hours * 3600
    for sub in subs:
        floor = sub["min_conviction"] if sub["min_conviction"] is not None else settings.telegram_min_conviction
        for kind, t, text in items:
            # A subscriber's own floor governs the signal feed. Launches are gated by heat, which
            # is a different scale, so their floor is the one in the config.
            if kind == "signal" and (t["conviction"] or 0) < floor:
                continue
            if already_sent(conn, sub["chat_id"], t["mint"], quiet):
                stats["skipped"] += 1
                continue
            try:
                tg.send(sub["chat_id"], text)
                mark_sent(conn, sub["chat_id"], t["mint"])
                stats["sent"] += 1
                stats["launches"] += kind == "launch"
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
    if cmd == "/fresh":
        hours = int(args[0]) if args and args[0].isdigit() else 24
        chain = settings.dex_chains[0] if settings.dex_chains else None
        return fmt_fresh(analyze.fresh(conn, chain, hours=hours, limit=10))
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
