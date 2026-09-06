"""Render the whole watchlist as one self-contained page with three views.

  Signals — tokens more than one trusted trader is buying right now
  Tokens  — what the cohort is holding, and how far in front they are
  Traders — the scored roster, each with a verdict and their largest positions

The database is the source of truth; this only formats it. Everything is inlined so the output
can be published or opened from disk with no server.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import db

STATUS_ORDER = {"active": 0, "watch": 1, "dropped": 2, "needs_review": 3}
TRUSTED_SCORE = 60

# The chain's quote assets. Some sources book a swap from the pool's side, which files "sold token X
# for USDG" as a USDG trade; left in, the stablecoin every trade passes through tops the feed as the
# most-bought token on the board. It is plumbing, not a position.
from ..sources.rpc import QUOTE_TOKENS  # noqa: E402

NOT_QUOTE = " AND {col} NOT IN (%s)" % ",".join("'%s'" % t for t in QUOTE_TOKENS)


# ---------------------------------------------------------------- data

def collect(conn: sqlite3.Connection, chain: str | None = None, hours: int = 48) -> dict:
    since = db.now() - hours * 3600
    where_chain = " AND chain=?" if chain else ""
    params = [chain] if chain else []

    traders = []
    for r in conn.execute(f"SELECT * FROM traders WHERE score IS NOT NULL{where_chain}", params):
        tags = json.loads(r["tags"]) if r["tags"] else {}
        stats = json.loads(r["stats_json"]) if r["stats_json"] else {}
        traders.append({
            "handle": r["fomo_handle"], "address": r["address"], "score": r["score"],
            "status": r["status"], "style": tags.get("style") or [],
            "flags": tags.get("red_flags") or [], "summary": r["ai_summary"] or "",
            "fomo_pnl": r["pnl_30d"] or r["pnl_7d"] or r["pnl_24h"],
            "realized": stats.get("realized_pnl"), "unrealized": stats.get("unrealized_pnl"),
            "win_rate": stats.get("win_rate"), "fills": stats.get("fills"),
            "closed": stats.get("closed_trades"), "volume": stats.get("volume"),
            "bags": stats.get("open_bags"), "model": r["ai_model"],
            "positions": [],
        })
    by_address = {t["address"]: t for t in traders}
    traders.sort(key=lambda t: (STATUS_ORDER.get(t["status"], 9), -(t["score"] or 0)))

    # each trader's largest open positions, straight from fomo's leaderboard payload
    for r in conn.execute(
        "SELECT p.*, t.address AS taddr, COALESCE(tk.symbol, substr(p.token,1,8)) sym "
        "FROM fomo_positions p JOIN traders t ON t.fomo_user_id = p.user_id "
        "LEFT JOIN tokens tk ON tk.mint = p.token "
        "WHERE 1=1" + NOT_QUOTE.format(col="p.token") + " ORDER BY p.unrealized_pnl DESC"
    ):
        holder = by_address.get(r["taddr"])
        if holder is not None and len(holder["positions"]) < 3:
            holder["positions"].append({
                "symbol": r["sym"], "token": r["token"], "pnl": r["unrealized_pnl"],
                "cost": r["cost_basis"],
            })

    # Collapse each buyer's fills into one row before aggregating, so a trader who bought five
    # times counts once toward the headcount and once toward conviction.
    signals = [dict(r) for r in conn.execute(
        "SELECT mint, sym, liq, COUNT(*) buyers, SUM(usd) usd, MIN(first_ts) first_ts, "
        "  AVG(score) avg_score, SUM((score / 100.0) * (score / 100.0)) conviction, "
        "  GROUP_CONCAT(handle) who FROM ("
        "  SELECT tr.mint mint, COALESCE(tk.symbol, substr(tr.mint,1,8)) sym, "
        "    tk.liquidity_usd liq, t.score score, t.fomo_handle handle, "
        "    SUM(tr.usd_value) usd, MIN(tr.ts) first_ts "
        "  FROM trades tr JOIN traders t ON t.address = tr.address "
        "  LEFT JOIN tokens tk ON tk.mint = tr.mint "
        f"  WHERE tr.side='buy' AND tr.ts >= ? AND t.score >= ?{' AND tr.chain=?' if chain else ''}"
        + NOT_QUOTE.format(col="tr.mint") +
        "  GROUP BY tr.mint, tr.address"
        ") GROUP BY mint HAVING buyers >= 2 ORDER BY conviction DESC, usd DESC LIMIT 40",
        [since, TRUSTED_SCORE, *params],
    )]

    tape = [dict(r) for r in conn.execute(
        "SELECT tr.ts, tr.side, tr.usd_value usd, tr.mint, t.fomo_handle handle, t.score, "
        "  COALESCE(tk.symbol, substr(tr.mint,1,8)) sym "
        "FROM trades tr JOIN traders t ON t.address = tr.address "
        "LEFT JOIN tokens tk ON tk.mint = tr.mint "
        f"WHERE t.score >= ? AND tr.usd_value IS NOT NULL{' AND tr.chain=?' if chain else ''}"
        + NOT_QUOTE.format(col="tr.mint") +
        " ORDER BY tr.ts DESC LIMIT 60",
        [TRUSTED_SCORE, *params],
    )]

    tokens = [dict(r) for r in conn.execute(
        "SELECT p.token, COALESCE(tk.symbol, substr(p.token,1,8)) sym, tk.liquidity_usd liq, "
        "  tk.mcap_usd mcap, COUNT(DISTINCT p.user_id) holders, SUM(p.unrealized_pnl) pnl, "
        "  SUM(p.cost_basis) cost, AVG(t.score) avg_score, "
        "  SUM(CASE WHEN t.score >= 60 THEN 1 ELSE 0 END) trusted, "
        "  SUM((t.score / 100.0) * (t.score / 100.0)) conviction, "
        "  GROUP_CONCAT(DISTINCT t.fomo_handle) who "
        "FROM fomo_positions p LEFT JOIN tokens tk ON tk.mint = p.token "
        "LEFT JOIN traders t ON t.fomo_user_id = p.user_id "
        "WHERE p.unrealized_pnl IS NOT NULL" + NOT_QUOTE.format(col="p.token") +
        " GROUP BY p.token ORDER BY pnl DESC LIMIT 60"
    )]

    counts: dict[str, int] = {}
    for t in traders:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {
        "generated_at": db.now(), "hours": hours, "traders": traders, "counts": counts,
        "signals": signals, "tape": tape, "tokens": tokens,
        "trades": conn.execute(
            "SELECT COUNT(*) FROM trades WHERE 1=1" + NOT_QUOTE.format(col="mint")).fetchone()[0],
        "positions": conn.execute("SELECT COUNT(*) FROM fomo_positions").fetchone()[0],
        "open_pnl": conn.execute("SELECT SUM(unrealized_pnl) FROM fomo_positions").fetchone()[0] or 0,
        "model": next((t["model"] for t in traders if t["model"]), "claude"),
    }


# ---------------------------------------------------------------- formatting

def usd(v, dash: str = "—") -> str:
    if v is None:
        return dash
    a = abs(v)
    if a >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if a >= 1_000:
        return f"${v/1_000:.0f}k"
    return f"${v:.0f}"


def ago(ts: int | None, now: int) -> str:
    if not ts:
        return "—"
    d = max(now - ts, 0)
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def handle_link(handle: str | None, fallback: str) -> str:
    if not handle:
        return f'<span class="handle">{esc(fallback)[:10]}</span>'
    return (f'<a class="handle" href="https://fomo.family/profile/{esc(handle)}" '
            f'target="_blank" rel="noopener">{esc(handle)}</a>')


def token_link(token: str, symbol: str) -> str:
    return (f'<a class="tok" href="https://dexscreener.com/robinhood/{esc(token)}" '
            f'target="_blank" rel="noopener">{esc(symbol)}</a>')


def multiple(cost, pnl) -> str:
    if not cost or cost <= 0 or pnl is None:
        return "—"
    return f"{(cost + pnl) / cost:.1f}×"


def signed(v) -> tuple[str, str]:
    """Formatted amount plus the class that colours it."""
    if v is None:
        return "—", "flat"
    return usd(v), ("pos" if v > 0 else "neg" if v < 0 else "flat")


def names(csv: str | None, limit: int = 4) -> str:
    parts = [p for p in (csv or "").split(",") if p][:limit + 1]
    shown = ", ".join(esc(p) for p in parts[:limit])
    return shown + (" +" if len(parts) > limit else "")


# ---------------------------------------------------------------- template

PAGE = """<title>FOMO Radar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{
  --ground:#eaebe5; --surface:#f6f7f2; --raised:#fdfdfb;
  --ink:#1a1c17; --muted:#6b7063; --faint:#8d9285;
  --line:#d4d7ca; --line-soft:#e2e4d9;
  --accent:#a2612a; --accent-soft:#f0e2d1;
  --pos:#3a6b4d; --neg:#a33a2c; --flat:#7a7f70;
  --shadow:0 1px 2px rgba(26,28,23,.06);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#16180f; --surface:#1e2119; --raised:#262a20;
    --ink:#e8eae0; --muted:#9aa08f; --faint:#7d8373;
    --line:#333929; --line-soft:#2a2f22;
    --accent:#d3924f; --accent-soft:#3a2c1a;
    --pos:#79c095; --neg:#e4826c; --flat:#8a9079;
    --shadow:0 1px 2px rgba(0,0,0,.4);
  }
}
:root[data-theme="dark"]{
  --ground:#16180f; --surface:#1e2119; --raised:#262a20;
  --ink:#e8eae0; --muted:#9aa08f; --faint:#7d8373;
  --line:#333929; --line-soft:#2a2f22;
  --accent:#d3924f; --accent-soft:#3a2c1a;
  --pos:#79c095; --neg:#e4826c; --flat:#8a9079;
  --shadow:0 1px 2px rgba(0,0,0,.4);
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font:400 15px/1.55 "IBM Plex Sans",system-ui,-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1100px; margin:0 auto; padding:40px 24px 72px}
a{color:inherit}
.num{font-family:"IBM Plex Mono",ui-monospace,monospace; font-variant-numeric:tabular-nums}
.pos{color:var(--pos)} .neg{color:var(--neg)} .flat{color:var(--flat)}

/* masthead ------------------------------------------------------- */
header{border-bottom:1px solid var(--line); padding-bottom:22px; margin-bottom:26px}
.eyebrow{
  font-family:"IBM Plex Mono",monospace; font-size:11px; letter-spacing:.14em;
  text-transform:uppercase; color:var(--accent); margin:0 0 10px
}
h1{
  font-family:Newsreader,Georgia,serif; font-weight:500; font-size:clamp(34px,5vw,52px);
  line-height:1.04; letter-spacing:-.015em; margin:0 0 12px; text-wrap:balance
}
.lede{max-width:62ch; color:var(--muted); margin:0 0 24px; font-size:15.5px}
.lede b{color:var(--ink); font-weight:500}
.strip{display:flex; flex-wrap:wrap; gap:10px 30px}
.strip div{display:flex; flex-direction:column; gap:2px}
.strip dt{font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--faint)}
.strip dd{margin:0; font-size:20px; font-weight:500}

/* tabs ----------------------------------------------------------- */
nav{display:flex; gap:4px; margin-bottom:28px; border-bottom:1px solid var(--line-soft)}
nav button{
  appearance:none; background:none; border:0; border-bottom:2px solid transparent;
  padding:9px 14px; margin-bottom:-1px; cursor:pointer; color:var(--muted);
  font:500 14px/1 "IBM Plex Sans",sans-serif; letter-spacing:.01em;
}
nav button:hover{color:var(--ink)}
nav button[aria-selected="true"]{color:var(--ink); border-bottom-color:var(--accent)}
nav button:focus-visible{outline:2px solid var(--accent); outline-offset:2px; border-radius:3px}
nav .tally{font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--faint); margin-left:6px}
.panel[hidden]{display:none}
.note{color:var(--muted); font-size:13.5px; max-width:66ch; margin:0 0 18px}

/* signals -------------------------------------------------------- */
.cols{display:grid; grid-template-columns:minmax(0,1.55fr) minmax(0,1fr); gap:34px; align-items:start}
@media (max-width:820px){ .cols{grid-template-columns:1fr; gap:34px} }
h2{
  font-family:Newsreader,Georgia,serif; font-weight:500; font-size:22px; margin:0 0 4px;
  letter-spacing:-.01em
}
.sub{color:var(--faint); font-size:12px; margin:0 0 14px;
  font-family:"IBM Plex Mono",monospace; letter-spacing:.05em; text-transform:uppercase}
.sig{display:flex; flex-direction:column; gap:0}
.sig li{
  list-style:none; display:grid; grid-template-columns:auto 1fr auto; gap:4px 14px;
  padding:13px 0; border-bottom:1px solid var(--line-soft); align-items:baseline
}
.sig li:first-child{border-top:1px solid var(--line-soft)}
.rank{font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--faint); width:2ch}
.tok{font-weight:600; font-size:16px; text-decoration:none; border-bottom:1px solid var(--line)}
.tok:hover{border-bottom-color:var(--accent); color:var(--accent)}
.sig .amt{font-family:"IBM Plex Mono",monospace; font-size:14px; text-align:right}
.sig .who{grid-column:2/4; color:var(--muted); font-size:13px; margin:0}
.sig .who .meta{color:var(--faint)}
.bar{grid-column:2/4; height:3px; background:var(--line-soft); border-radius:2px; overflow:hidden; max-width:280px}
.bar i{display:block; height:100%; background:var(--accent)}
.buyers{
  font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--accent);
  background:var(--accent-soft); padding:2px 6px; border-radius:3px; white-space:nowrap
}

/* tape ----------------------------------------------------------- */
.tape{border:1px solid var(--line); border-radius:6px; background:var(--surface); overflow:hidden}
.tape ol{margin:0; padding:0; list-style:none; max-height:560px; overflow-y:auto}
.tape li{
  display:grid; grid-template-columns:auto 1fr auto; gap:10px; align-items:baseline;
  padding:8px 13px; border-bottom:1px solid var(--line-soft);
  font-family:"IBM Plex Mono",monospace; font-size:12.5px
}
.tape li:last-child{border-bottom:0}
.side{width:7px; height:7px; border-radius:50%; display:inline-block}
.side.buy{background:var(--pos)} .side.sell{background:var(--neg)}
.tape .h{font-family:"IBM Plex Sans",sans-serif; color:var(--muted); font-size:12.5px}

/* tokens table --------------------------------------------------- */
.scroll{overflow-x:auto; border:1px solid var(--line); border-radius:6px; background:var(--surface)}
table{border-collapse:collapse; width:100%; font-size:13.5px; min-width:820px}
th{
  text-align:left; font:500 11px/1 "IBM Plex Mono",monospace; letter-spacing:.08em;
  text-transform:uppercase; color:var(--faint); padding:11px 14px;
  border-bottom:1px solid var(--line); white-space:nowrap
}
td{padding:11px 14px; border-bottom:1px solid var(--line-soft); vertical-align:baseline}
tr:last-child td{border-bottom:0}
th.r,td.r{text-align:right}
tbody tr:hover{background:var(--raised)}
td .who{color:var(--faint); font-size:12px}

/* traders -------------------------------------------------------- */
.controls{display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:20px}
.chip{
  appearance:none; border:1px solid var(--line); background:var(--surface); color:var(--muted);
  border-radius:99px; padding:5px 13px; cursor:pointer; font:500 12.5px/1 "IBM Plex Sans",sans-serif
}
.chip[aria-pressed="true"]{background:var(--ink); border-color:var(--ink); color:var(--ground)}
.chip:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
input[type=search]{
  flex:1; min-width:170px; max-width:280px; padding:6px 11px; border:1px solid var(--line);
  border-radius:5px; background:var(--surface); color:var(--ink); font:400 13px/1.4 inherit
}
input[type=search]:focus{outline:2px solid var(--accent); outline-offset:-1px; border-color:transparent}
.cards{display:flex; flex-direction:column; gap:0}
.card{
  display:grid; grid-template-columns:56px minmax(0,1fr) auto; gap:6px 18px;
  padding:16px 0; border-bottom:1px solid var(--line-soft); align-items:start
}
.card[hidden]{display:none}
.score{
  font-family:"IBM Plex Mono",monospace; font-size:26px; font-weight:500; line-height:1;
  padding-top:2px
}
.score small{display:block; font-size:10px; letter-spacing:.08em; text-transform:uppercase;
  color:var(--faint); font-weight:400; margin-top:5px}
.card .top{display:flex; flex-wrap:wrap; align-items:baseline; gap:8px}
.handle{font-weight:600; font-size:16px; text-decoration:none; border-bottom:1px solid var(--line)}
.handle:hover{color:var(--accent); border-bottom-color:var(--accent)}
.tag{
  font-family:"IBM Plex Mono",monospace; font-size:10.5px; letter-spacing:.05em;
  color:var(--muted); border:1px solid var(--line); border-radius:3px; padding:1px 5px
}
.tag.warn{color:var(--neg); border-color:var(--neg)}
.card p{margin:0; color:var(--muted); font-size:13.5px; max-width:64ch}
.figs{
  grid-column:2; display:flex; flex-wrap:wrap; gap:4px 18px;
  font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--faint)
}
.figs b{font-weight:500; color:var(--ink)}
.bags{grid-column:2; display:flex; flex-wrap:wrap; gap:6px; font-size:12px}
.bags span{
  font-family:"IBM Plex Mono",monospace; border:1px solid var(--line-soft);
  border-radius:3px; padding:1px 6px; color:var(--muted)
}
.pnl{text-align:right; font-family:"IBM Plex Mono",monospace; font-size:16px; white-space:nowrap}
.pnl small{display:block; font-size:10px; letter-spacing:.08em; text-transform:uppercase;
  color:var(--faint); margin-top:3px}
.empty{color:var(--faint); font-size:13.5px; padding:26px 0}

footer{
  margin-top:44px; padding-top:18px; border-top:1px solid var(--line);
  color:var(--faint); font-size:12px; display:flex; flex-wrap:wrap; gap:6px 20px
}
footer b{color:var(--muted); font-weight:500}
</style>

<div class="wrap">
<header>
  <p class="eyebrow">Robinhood Chain &middot; fomo.family</p>
  <h1>What the profitable wallets are actually holding</h1>
  <p class="lede">__TRADERS__ traders from the fomo.family leaderboard, each resolved to a real
  on-chain wallet and read by <b>__MODEL__</b>. The scores below judge whether a run looks like
  skill or like one lucky bag — and the feed shows what those wallets bought today.</p>
  <dl class="strip">
    <div><dt>Scored traders</dt><dd class="num">__TRADERS__</dd></div>
    <div><dt>Trusted (60+)</dt><dd class="num">__TRUSTED__</dd></div>
    <div><dt>Fills recorded</dt><dd class="num">__TRADES__</dd></div>
    <div><dt>Open positions</dt><dd class="num">__POSITIONS__</dd></div>
    <div><dt>Unrealised PnL</dt><dd class="num pos">__OPENPNL__</dd></div>
  </dl>
</header>

<nav role="tablist">
  <button role="tab" id="tab-signals" aria-controls="p-signals" aria-selected="true">Signals<span class="tally">__NSIG__</span></button>
  <button role="tab" id="tab-tokens" aria-controls="p-tokens" aria-selected="false">Tokens<span class="tally">__NTOK__</span></button>
  <button role="tab" id="tab-traders" aria-controls="p-traders" aria-selected="false">Traders<span class="tally">__TRADERS__</span></button>
</nav>

<section class="panel" id="p-signals" role="tabpanel" aria-labelledby="tab-signals">
  <p class="note">A signal is a token that <b>two or more traders scoring 60+</b> bought inside the
  last __HOURS__ hours, ranked by <b>conviction</b> — each buyer counted as the square of their
  score, so five of the best wallets outrank fifteen mediocre ones. Nothing here is advice; it is a
  record of who moved first.</p>
  <div class="cols">
    <div>
      <h2>Converging buys</h2>
      <p class="sub">Last __HOURS__h &middot; ranked by the quality of the wallets that agree</p>
      <ul class="sig">__SIGNALS__</ul>
    </div>
    <div>
      <h2>The tape</h2>
      <p class="sub">Every fill by a 60+ wallet, newest first</p>
      <div class="tape"><ol>__TAPE__</ol></div>
    </div>
  </div>
</section>

<section class="panel" id="p-tokens" role="tabpanel" aria-labelledby="tab-tokens" hidden>
  <p class="note">Every token the cohort still holds, ranked by unrealised profit. <b>Mult</b> is
  what the position is worth against what it cost — the clearest read on how early they were; a dash
  means fomo reports profit already withdrawn, so the entry price is unknowable. <b>Conviction</b>
  weighs <em>whose</em> money is in a token rather than how many wallets hold it: each holder counts
  as the square of their score, so one trader at 85 outweighs a crowd at 40.</p>
  <div class="scroll"><table>
    <thead><tr>
      <th>Token</th><th class="r">Holders</th><th class="r">Conviction</th><th class="r">Open PnL</th>
      <th class="r">Cost</th><th class="r">Mult</th><th class="r">Liquidity</th><th>Held by</th>
    </tr></thead>
    <tbody>__TOKENS__</tbody>
  </table></div>
</section>

<section class="panel" id="p-traders" role="tabpanel" aria-labelledby="tab-traders" hidden>
  <p class="note">Scores are a judgement on process, not a prediction. <b>Fomo PnL</b> is the
  30-day figure from the leaderboard and includes open bags; <b>flow</b> is on-chain cash movement,
  which runs negative for anyone still accumulating.</p>
  <div class="controls">
    __CHIPS__
    <input type="search" id="q" placeholder="Search handle, style or verdict" aria-label="Search traders">
  </div>
  <div class="cards" id="cards">__CARDS__</div>
  <p class="empty" id="none" hidden>Nothing matches that filter.</p>
</section>

<footer>
  <span>Generated <b>__GENERATED__</b></span>
  <span>Sources <b>fomo.family, robinhoodtrenches, Codex</b></span>
  <span>Read-only research tool. Never trades. Not financial advice.</span>
</footer>
</div>

<script>
const tabs = [...document.querySelectorAll('nav button')];
tabs.forEach(btn => btn.addEventListener('click', () => {
  tabs.forEach(b => {
    const on = b === btn;
    b.setAttribute('aria-selected', on);
    document.getElementById(b.getAttribute('aria-controls')).hidden = !on;
  });
  try { localStorage.setItem('fomo-tab', btn.id); } catch (e) {}
}));
try {
  const saved = document.getElementById(localStorage.getItem('fomo-tab'));
  if (saved) saved.click();
} catch (e) {}

const cards = [...document.querySelectorAll('.card')];
const chips = [...document.querySelectorAll('.chip')];
const q = document.getElementById('q');
const none = document.getElementById('none');
let status = 'all';

function apply() {
  const term = q.value.trim().toLowerCase();
  let shown = 0;
  for (const c of cards) {
    const ok = (status === 'all' || c.dataset.status === status)
      && (!term || c.dataset.find.includes(term));
    c.hidden = !ok;
    if (ok) shown++;
  }
  none.hidden = shown > 0;
}
chips.forEach(chip => chip.addEventListener('click', () => {
  status = chip.dataset.status;
  chips.forEach(c => c.setAttribute('aria-pressed', c === chip));
  apply();
}));
q.addEventListener('input', apply);
</script>
"""


# ---------------------------------------------------------------- render

def render_signals(rows: list[dict], now: int) -> str:
    if not rows:
        return '<li class="empty">No token has two trusted buyers in this window yet.</li>'
    top = max(r["conviction"] or 0 for r in rows) or 1
    out = []
    for i, r in enumerate(rows, 1):
        amt, _ = signed(r["usd"])
        out.append(
            f'<li><span class="rank num">{i:02d}</span>'
            f'<span class="top">{token_link(r["mint"], r["sym"])} '
            f'<span class="buyers">{r["buyers"]} buyers</span></span>'
            f'<span class="amt">{amt}</span>'
            f'<span class="bar"><i style="width:{r["buyers"] / top * 100:.0f}%"></i></span>'
            f'<p class="who">{names(r["who"])} '
            f'<span class="meta">&middot; avg score {r["avg_score"]:.0f} '
            f'&middot; first seen {ago(r["first_ts"], now)} ago'
            f'{" &middot; liq " + usd(r["liq"]) if r["liq"] else ""}</span></p></li>'
        )
    return "".join(out)


def render_tape(rows: list[dict], now: int) -> str:
    if not rows:
        return '<li class="empty">No fills recorded yet.</li>'
    return "".join(
        f'<li><span class="side {r["side"]}"></span>'
        f'<span>{token_link(r["mint"], r["sym"])} '
        f'<span class="h">{esc(r["handle"] or r["mint"][:6])}</span></span>'
        f'<span>{usd(r["usd"])} <span class="h">{ago(r["ts"], now)}</span></span></li>'
        for r in rows
    )


def render_tokens(rows: list[dict]) -> str:
    if not rows:
        return '<tr><td colspan="8" class="empty">No positions collected yet.</td></tr>'
    out = []
    for r in rows:
        pnl, cls = signed(r["pnl"])
        held = f'{r["holders"]}'
        if r["trusted"]:
            held += f' <span class="who">{r["trusted"]}&#8239;trusted</span>'
        out.append(
            f'<tr><td>{token_link(r["token"], r["sym"])}</td>'
            f'<td class="r num">{held}</td>'
            f'<td class="r num">{(r["conviction"] or 0):.1f}</td>'
            f'<td class="r num {cls}">{pnl}</td>'
            f'<td class="r num">{usd(r["cost"])}</td>'
            f'<td class="r num">{multiple(r["cost"], r["pnl"])}</td>'
            f'<td class="r num">{usd(r["liq"])}</td>'
            f'<td class="who">{names(r["who"], 3)}</td></tr>'
        )
    return "".join(out)


def render_cards(traders: list[dict]) -> str:
    out = []
    for t in traders:
        tags = "".join(f'<span class="tag">{esc(s)}</span>' for s in t["style"][:3])
        tags += "".join(f'<span class="tag warn">{esc(f)}</span>' for f in t["flags"][:2])
        figs = []
        if t["win_rate"] is not None:
            figs.append(f'win <b>{t["win_rate"] * 100:.0f}%</b>')
        for label, key in (("closed", "closed"), ("fills", "fills"), ("bags", "bags")):
            if t[key]:
                figs.append(f'{label} <b>{t[key]:,}</b>')
        if t["volume"]:
            figs.append(f'volume <b>{usd(t["volume"])}</b>')
        flow, flow_cls = signed(t["realized"])
        if t["realized"] is not None:
            figs.append(f'flow <b class="{flow_cls}">{flow}</b>')
        bags = "".join(
            f'<span>{esc(p["symbol"])} {signed(p["pnl"])[0]}</span>' for p in t["positions"]
        )
        pnl, cls = signed(t["fomo_pnl"])
        blob = " ".join([t["handle"] or "", t["address"], t["summary"], *t["style"], *t["flags"],
                         *(p["symbol"] for p in t["positions"])]).lower()
        out.append(
            f'<article class="card" data-status="{t["status"]}" data-find="{esc(blob)}">'
            f'<div class="score num">{t["score"] if t["score"] is not None else "—"}'
            f'<small>{esc(t["status"].replace("_", " "))}</small></div>'
            f'<div><div class="top">{handle_link(t["handle"], t["address"])}{tags}</div>'
            f'<p>{esc(t["summary"])}</p></div>'
            f'<div class="pnl {cls}">{pnl}<small>fomo 30d</small></div>'
            + (f'<div class="figs">{" &middot; ".join(figs)}</div>' if figs else "")
            + (f'<div class="bags">{bags}</div>' if bags else "")
            + "</article>"
        )
    return "".join(out)


def render(data: dict) -> str:
    now = data["generated_at"]
    counts = data["counts"]
    labels = [("all", "All", len(data["traders"])), ("active", "Active", counts.get("active", 0)),
              ("watch", "Watch", counts.get("watch", 0)),
              ("needs_review", "Needs review", counts.get("needs_review", 0)),
              ("dropped", "Dropped", counts.get("dropped", 0))]
    chips = "".join(
        f'<button class="chip" data-status="{key}" aria-pressed="{"true" if key == "all" else "false"}">'
        f'{label} <span class="num">{n}</span></button>'
        for key, label, n in labels if n or key == "all"
    )
    trusted = sum(1 for t in data["traders"] if (t["score"] or 0) >= TRUSTED_SCORE)
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    out = PAGE
    for key, value in {
        "__TRADERS__": f'{len(data["traders"]):,}',
        "__TRUSTED__": f"{trusted:,}",
        "__TRADES__": f'{data["trades"]:,}',
        "__POSITIONS__": f'{data["positions"]:,}',
        "__OPENPNL__": usd(data["open_pnl"]),
        "__MODEL__": esc(str(data["model"]).split(":")[-1]),
        "__HOURS__": str(data["hours"]),
        "__NSIG__": str(len(data["signals"])),
        "__NTOK__": str(len(data["tokens"])),
        "__SIGNALS__": render_signals(data["signals"], now),
        "__TAPE__": render_tape(data["tape"], now),
        "__TOKENS__": render_tokens(data["tokens"]),
        "__CHIPS__": chips,
        "__CARDS__": render_cards(data["traders"]),
        "__GENERATED__": stamp,
    }.items():
        out = out.replace(key, value)
    return out


def build(conn: sqlite3.Connection, path: Path, chain: str | None = None, hours: int = 48) -> dict:
    data = collect(conn, chain, hours)
    Path(path).write_text(render(data), encoding="utf-8")
    return {"path": str(path), "traders": len(data["traders"]), "signals": len(data["signals"]),
            "tokens": len(data["tokens"]), "counts": data["counts"]}
