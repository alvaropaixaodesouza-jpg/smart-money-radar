"""Render the watchlist as a single self-contained HTML page.

The database is the source of truth; this only formats it. Everything the page needs is inlined
so the output can be published or opened from disk with no server.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import db

STATUS_ORDER = {"active": 0, "watch": 1, "dropped": 2, "needs_review": 3}


def collect(conn: sqlite3.Connection, chain: str | None = None) -> dict:
    where = "WHERE score IS NOT NULL"
    params: list = []
    if chain:
        where += " AND chain=?"
        params.append(chain)
    rows = conn.execute(f"SELECT * FROM traders {where}", params).fetchall()

    traders = []
    for r in rows:
        tags = json.loads(r["tags"]) if r["tags"] else {}
        stats = json.loads(r["stats_json"]) if r["stats_json"] else {}
        traders.append({
            "handle": r["fomo_handle"],
            "fomo_pnl": r["pnl_30d"] or r["pnl_7d"] or r["pnl_24h"],
            "address": r["address"],
            "chain": r["chain"],
            "score": r["score"],
            "status": r["status"],
            "style": tags.get("style") or [],
            "flags": tags.get("red_flags") or [],
            "summary": r["ai_summary"] or "",
            "model": r["ai_model"],
            "realized": stats.get("realized_pnl"),
            "unrealized": stats.get("unrealized_pnl"),
            "win_rate": stats.get("win_rate"),
            "fills": stats.get("fills"),
            "closed": stats.get("closed_trades"),
            "volume": stats.get("volume"),
            "best": stats.get("best_trade"),
            "worst": stats.get("worst_trade"),
            "bags": stats.get("open_bags"),
            "state": stats.get("state"),
            "followers": stats.get("followers"),
            "last_ts": stats.get("last_ts"),
        })
    traders.sort(key=lambda t: (STATUS_ORDER.get(t["status"], 9), -(t["score"] or 0)))

    counts: dict[str, int] = {}
    for t in traders:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    trades, first_ts, last_ts = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM trades"
    ).fetchone()
    return {
        "generated_at": db.now(),
        "traders": traders,
        "counts": counts,
        "trades": trades or 0,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "tokens": conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0],
    }


def _fmt_ts(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %Y") if ts else "—"


PAGE = """<title>FOMO Radar</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,600;1,6..72,400&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root {
  --ground: #eaebe5; --surface: #f6f7f2; --raised: #fdfdfb;
  --ink: #1a1c17; --muted: #6b7063; --faint: #8d9285;
  --line: #d4d7ca; --line-soft: #e2e4d9;
  --accent: #a2612a; --accent-soft: #f0e2d1;
  --pos: #3a6b4d; --neg: #a33a2c; --flat: #7a7f70;
  --shadow: 0 1px 2px rgba(26,28,23,.06);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground: #16180f; --surface: #1e2119; --raised: #262a20;
    --ink: #e8eae0; --muted: #9aa08f; --faint: #7d8373;
    --line: #333929; --line-soft: #2a2f22;
    --accent: #d3924f; --accent-soft: #3a2c1a;
    --pos: #79c095; --neg: #e4826c; --flat: #8a9079;
    --shadow: 0 1px 2px rgba(0,0,0,.4);
  }
}
:root[data-theme="dark"] {
  --ground: #16180f; --surface: #1e2119; --raised: #262a20;
  --ink: #e8eae0; --muted: #9aa08f; --faint: #7d8373;
  --line: #333929; --line-soft: #2a2f22;
  --accent: #d3924f; --accent-soft: #3a2c1a;
  --pos: #79c095; --neg: #e4826c; --flat: #8a9079;
  --shadow: 0 1px 2px rgba(0,0,0,.4);
}

* { box-sizing: border-box; }
body {
  background: var(--ground); color: var(--ink);
  font-family: "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  font-size: 15px; line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 40px 24px 72px; }
a { color: inherit; }
h1, h2, h3 { text-wrap: balance; margin: 0; }

/* ---------- masthead ---------- */
.masthead { border-bottom: 2px solid var(--ink); padding-bottom: 18px; }
.eyebrow {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
  color: var(--accent); display: flex; gap: 14px; flex-wrap: wrap; align-items: baseline;
}
.eyebrow .sep { color: var(--faint); }
h1 {
  font-family: Newsreader, Georgia, serif; font-weight: 600;
  font-size: clamp(38px, 6vw, 62px); line-height: 1.02; letter-spacing: -.015em;
  margin: 12px 0 0;
}
.standfirst {
  font-family: Newsreader, Georgia, serif; font-size: 19px; line-height: 1.5;
  color: var(--muted); max-width: 62ch; margin-top: 12px;
}
.standfirst em { color: var(--ink); font-style: italic; }

/* ---------- summary strip ---------- */
.strip {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
  gap: 1px; background: var(--line); border: 1px solid var(--line);
  margin: 28px 0 0;
}
.cell { background: var(--surface); padding: 14px 16px; }
.cell dt {
  font-family: "IBM Plex Mono", monospace; font-size: 10px;
  letter-spacing: .14em; text-transform: uppercase; color: var(--faint);
}
.cell dd {
  margin: 4px 0 0; font-family: "IBM Plex Mono", monospace;
  font-size: 26px; font-weight: 500; font-variant-numeric: tabular-nums; letter-spacing: -.02em;
}
.cell.is-active dd { color: var(--accent); }

/* ---------- controls ---------- */
.controls {
  display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
  margin: 32px 0 0; padding-bottom: 14px; border-bottom: 1px solid var(--line);
}
.seg { display: flex; border: 1px solid var(--line); background: var(--surface); }
.seg button {
  font: inherit; font-size: 13px; padding: 6px 14px; border: 0; cursor: pointer;
  background: transparent; color: var(--muted); border-right: 1px solid var(--line-soft);
}
.seg button:last-child { border-right: 0; }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--ground); }
.seg button:focus-visible, #q:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
#q {
  font: inherit; font-size: 13px; padding: 6px 12px; flex: 1; min-width: 180px;
  background: var(--surface); color: var(--ink); border: 1px solid var(--line);
}
#q::placeholder { color: var(--faint); }
.count { font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--faint); }

/* ---------- entries ---------- */
.entries { margin-top: 4px; }
.entry {
  display: grid; grid-template-columns: 68px 1fr; gap: 20px;
  padding: 24px 0; border-bottom: 1px solid var(--line-soft);
}
.entry[hidden] { display: none; }
.score {
  font-family: "IBM Plex Mono", monospace; font-size: 34px; font-weight: 500;
  font-variant-numeric: tabular-nums; letter-spacing: -.03em; line-height: 1;
  text-align: right; padding-top: 2px;
}
.rank { font-family: "IBM Plex Mono", monospace; font-size: 10px; color: var(--faint); text-align: right; margin-top: 6px; }
.entry[data-status="active"] .score { color: var(--accent); }
.entry[data-status="dropped"] .score { color: var(--faint); }

.head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.handle { font-family: Newsreader, Georgia, serif; font-size: 25px; font-weight: 600; letter-spacing: -.01em; }
.handle a { text-decoration: none; border-bottom: 1px solid var(--line); }
.handle a:hover { border-bottom-color: var(--accent); color: var(--accent); }
.verdict {
  font-family: "IBM Plex Mono", monospace; font-size: 10px; letter-spacing: .14em;
  text-transform: uppercase; padding: 3px 8px; border: 1px solid currentColor;
}
.verdict.active { color: var(--accent); background: var(--accent-soft); }
.verdict.watch { color: var(--muted); }
.verdict.dropped { color: var(--faint); border-style: dashed; }
.tag {
  font-family: "IBM Plex Mono", monospace; font-size: 11px; color: var(--muted);
}
.tag::before { content: "· "; color: var(--faint); }
.flag { color: var(--neg); font-weight: 500; }

.summary { margin: 8px 0 0; max-width: 68ch; color: var(--ink); }
.entry[data-status="dropped"] .summary { color: var(--muted); }

.figures {
  display: flex; flex-wrap: wrap; gap: 0 26px; margin-top: 12px;
  font-family: "IBM Plex Mono", monospace; font-size: 12px; font-variant-numeric: tabular-nums;
}
.fig { display: flex; gap: 6px; align-items: baseline; }
.fig b { font-weight: 500; }
.fig span { color: var(--faint); }
.pos { color: var(--pos); } .neg { color: var(--neg); }
.fig.claim b { color: var(--accent); font-weight: 600; }
.fig.claim span { color: var(--accent); opacity: .8; }
.addr { color: var(--faint); font-family: "IBM Plex Mono", monospace; font-size: 11px; margin-top: 8px; word-break: break-all; }

/* ---------- footer ---------- */
.notes {
  margin-top: 48px; padding-top: 24px; border-top: 2px solid var(--ink);
  display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 28px;
}
.notes h3 {
  font-family: "IBM Plex Mono", monospace; font-size: 10px; letter-spacing: .14em;
  text-transform: uppercase; color: var(--accent); margin-bottom: 8px;
}
.notes p { margin: 0 0 8px; font-size: 13.5px; color: var(--muted); max-width: 50ch; }
.notes code {
  font-family: "IBM Plex Mono", monospace; font-size: 12px;
  background: var(--surface); padding: 1px 4px; border: 1px solid var(--line-soft);
}
.empty { padding: 40px 0; color: var(--faint); font-family: "IBM Plex Mono", monospace; font-size: 13px; }

@media (max-width: 620px) {
  .entry { grid-template-columns: 52px 1fr; gap: 14px; }
  .score { font-size: 27px; }
  h1 { font-size: 36px; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>

<div class="wrap">
  <header class="masthead">
    <div class="eyebrow">
      <span>FOMO Radar</span><span class="sep">/</span>
      <span>Robinhood Chain · 4663</span><span class="sep">/</span>
      <span>__GENERATED__</span>
    </div>
    <h1>Who on fomo.family is actually good</h1>
    <p class="standfirst">
      Every wallet on the fomo.family tape, judged on what it has <em>banked</em> rather than what its
      open bags are marked at. __TOTAL__ traders, __TRADES__ recorded fills, one verdict each.
    </p>
  </header>

  <dl class="strip">
    <div class="cell is-active"><dt>Worth following</dt><dd>__ACTIVE__</dd></div>
    <div class="cell"><dt>On watch</dt><dd>__WATCH__</dd></div>
    <div class="cell"><dt>Dropped</dt><dd>__DROPPED__</dd></div>
    <div class="cell"><dt>Fills recorded</dt><dd>__TRADES__</dd></div>
    <div class="cell"><dt>Tokens seen</dt><dd>__TOKENS__</dd></div>
  </dl>

  <div class="controls">
    <div class="seg" role="group" aria-label="Filter by verdict">
      <button data-f="all" aria-pressed="true">All</button>
      <button data-f="active" aria-pressed="false">Follow</button>
      <button data-f="watch" aria-pressed="false">Watch</button>
      <button data-f="dropped" aria-pressed="false">Dropped</button>
    </div>
    <input id="q" type="search" placeholder="Find a handle or address" aria-label="Search traders">
    <span class="count" id="count"></span>
  </div>

  <main class="entries" id="entries">__ENTRIES__</main>
  <p class="empty" id="empty" hidden>Nothing matches that filter.</p>

  <section class="notes">
    <div>
      <h3>Why fomo PnL and on-chain flow differ</h3>
      <p><b>fomo 30d</b> is profit including positions still open. Most of it has not been sold, which
      is why a wallet can show millions there while its on-chain cash flow looks flat or negative —
      the money is in the bag, not in the wallet.</p>
      <p>A 5k entry into a token that runs 500x is millions on paper and nothing realized. That is the
      trade everyone is here for, and it is invisible to anyone counting only completed round trips.</p>
    </div>
    <div>
      <h3>How the score is set</h3>
      <p>Realized profit is the spine of it: money actually taken off the table across closed
      positions. Unrealized marks on open bags are treated as unproven, however large.</p>
      <p>A high win rate with a negative result reads worse than a low one with a positive result —
      it means the losers are being held and the winners cut.</p>
    </div>
    <div>
      <h3>What the flags mean</h3>
      <p><b>one-hit</b> — a single trade is larger than the total profit, so the rest is net negative.
      <b>bot</b> — uniform clip sizes and minute-scale holds. <b>wash</b> — round-tripping volume with
      no meaningful profit.</p>
    </div>
    <div>
      <h3>Where the data comes from</h3>
      <p>Wallet mapping and fills from the public
      <a href="https://robinhoodtrenches.com">robinhoodtrenches.com</a> index; token and swap history
      from Codex. Scored by <code>__MODEL__</code>.</p>
      <p>Research only. Nothing here is advice, and none of it is a reason to copy a wallet.</p>
    </div>
  </section>
</div>

<script>
const entries = [...document.querySelectorAll('.entry')];
const countEl = document.getElementById('count');
const emptyEl = document.getElementById('empty');
const q = document.getElementById('q');
let filter = 'all';

function apply() {
  const needle = q.value.trim().toLowerCase();
  let shown = 0;
  for (const el of entries) {
    const okFilter = filter === 'all' || el.dataset.status === filter;
    const okText = !needle || el.dataset.search.includes(needle);
    const show = okFilter && okText;
    el.hidden = !show;
    if (show) shown++;
  }
  countEl.textContent = shown + ' of ' + entries.length;
  emptyEl.hidden = shown > 0;
}

document.querySelectorAll('.seg button').forEach((b) => {
  b.addEventListener('click', () => {
    filter = b.dataset.f;
    document.querySelectorAll('.seg button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    apply();
  });
});
q.addEventListener('input', apply);
apply();
</script>
"""


def _usd(v) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if a >= 1_000:
        return f"${v/1_000:.0f}k"
    return f"${v:.0f}"


def _entry(t: dict, rank: int) -> str:
    tags = "".join(f'<span class="tag">{s}</span>' for s in t["style"])
    tags += "".join(f'<span class="tag flag">{f}</span>' for f in t["flags"])
    handle = t["handle"] or t["address"][:10]
    link = (f'<a href="https://fomo.family/profile/{handle}" target="_blank" rel="noopener">{handle}</a>'
            if t["handle"] else handle)

    figs = []
    if t["realized"] is not None:
        cls = "pos" if t["realized"] > 0 else ("neg" if t["realized"] < 0 else "")
        figs.append(f'<span class="fig"><span>banked</span><b class="{cls}">{_usd(t["realized"])}</b></span>')
    if t["unrealized"]:
        figs.append(f'<span class="fig"><span>on paper</span><b>{_usd(t["unrealized"])}</b></span>')
    if t["win_rate"] is not None:
        figs.append(f'<span class="fig"><span>win</span><b>{t["win_rate"]*100:.0f}%</b></span>')
    if t["closed"]:
        figs.append(f'<span class="fig"><span>closed</span><b>{t["closed"]}</b></span>')
    if t["fills"]:
        figs.append(f'<span class="fig"><span>fills</span><b>{t["fills"]}</b></span>')
    if t["volume"]:
        figs.append(f'<span class="fig"><span>volume</span><b>{_usd(t["volume"])}</b></span>')
    if t["bags"]:
        figs.append(f'<span class="fig"><span>open bags</span><b>{t["bags"]}</b></span>')
    if t["fomo_pnl"]:
        figs.append(f'<span class="fig claim"><span>fomo 30d</span><b>{_usd(t["fomo_pnl"])}</b></span>')

    search = f'{handle} {t["address"]} {" ".join(t["style"])} {" ".join(t["flags"])}'.lower()
    label = {"active": "follow", "watch": "watch", "dropped": "dropped"}.get(t["status"], t["status"])
    return f"""<article class="entry" data-status="{t['status']}" data-search="{search}">
  <div><div class="score">{t['score']}</div><div class="rank">#{rank}</div></div>
  <div>
    <div class="head">
      <span class="handle">{link}</span>
      <span class="verdict {t['status']}">{label}</span>{tags}
    </div>
    <p class="summary">{t['summary']}</p>
    <div class="figures">{''.join(figs)}</div>
    <div class="addr">{t['address']}</div>
  </div>
</article>"""


def render(data: dict) -> str:
    entries = "\n".join(_entry(t, i) for i, t in enumerate(data["traders"], 1))
    counts = data["counts"]
    model = next((t["model"] for t in data["traders"] if t["model"]), "claude")
    out = PAGE
    for key, val in {
        "__GENERATED__": _fmt_ts(data["generated_at"]),
        "__TOTAL__": str(len(data["traders"])),
        "__ACTIVE__": str(counts.get("active", 0)),
        "__WATCH__": str(counts.get("watch", 0)),
        "__DROPPED__": str(counts.get("dropped", 0)),
        "__TRADES__": f'{data["trades"]:,}',
        "__TOKENS__": f'{data["tokens"]:,}',
        "__MODEL__": model,
        "__ENTRIES__": entries,
    }.items():
        out = out.replace(key, val)
    return out


def build(conn: sqlite3.Connection, path: Path, chain: str | None = None) -> dict:
    data = collect(conn, chain)
    Path(path).write_text(render(data), encoding="utf-8")
    return {"path": str(path), "traders": len(data["traders"]), "counts": data["counts"]}
