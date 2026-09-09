"""Did the score predict anything?

We have handed out 374 verdicts and never once checked whether they were worth anything. A score
nobody has measured is an opinion with a number printed on it, and this whole product rests on the
claim that the number means something — so it is the one thing that has to be tested before any of
it is published.

**The test is out-of-sample by construction.** `score_history` records when each wallet was first
scored, and only positions *opened after that moment* are counted. A verdict cannot be credited
with a trade it was partly derived from, which is exactly the trap a naive version of this falls
into: the scores were informed by fomo's PnL, so measuring them against that PnL would prove only
that the pipeline can read.

What it measures, per score band: how many positions were opened after the verdict, what fraction
of the closed ones made money, and what came back out against what went in. Realized profit only —
an open position is a price quote, not a result, and counting it would let a band's number be
whatever the market did this morning.

The honest limits, stated because a number like this invites more weight than it can carry:
  · a band with few closed positions says nothing, and is reported with its count so you can see
  · fills the tape never sized are excluded rather than guessed at
  · positions the wallet held before our tape starts are excluded: the entry price is unknowable
  · survivorship is not corrected for — a wallet that stopped trading stops contributing
"""
from __future__ import annotations

import sqlite3
import statistics

from .. import db
from . import analyze

# The bands the product publishes. Fixed here rather than derived, because the question is whether
# *these* thresholds — the ones on the site — separate anything.
BANDS = ((70, 101, "active"), (40, 70, "watch"), (0, 40, "dropped"))


def first_verdicts(conn: sqlite3.Connection) -> dict[str, dict]:
    """Each wallet's earliest recorded verdict, which is the one the test is run against.

    Later rescores are ignored on purpose. Judging a trade against a score assigned after it would
    be reading the answer first, and rescoring is exactly when that happens.
    """
    rows = conn.execute(
        "SELECT address, score, status, ts FROM score_history h WHERE ts = ("
        "  SELECT MIN(ts) FROM score_history WHERE address = h.address) "
        "GROUP BY address"
    ).fetchall()
    return {r["address"]: {"score": r["score"], "status": r["status"], "ts": r["ts"]}
            for r in rows if r["score"] is not None}


def positions_after_verdict(conn: sqlite3.Connection, address: str, since: int) -> list[dict]:
    """Closed and trimmed positions this wallet opened after it was scored."""
    out = []
    for p in analyze.ledger(conn, address):
        if p["first_ts"] is None or p["first_ts"] < since:
            continue
        if p["state"] not in ("closed", "trimmed") or p["realized"] is None:
            continue
        if not p["bought_usd"]:
            continue
        out.append(p)
    return out


def calibrate(conn: sqlite3.Connection, min_usd: float = 100.0) -> dict:
    """Group every post-verdict closed position by the band its wallet was in, and count.

    `min_usd` drops dust: a wallet that put twenty dollars into a launch and took thirty out has a
    150% return and has demonstrated nothing, and enough of those would swamp the real positions.
    """
    verdicts = first_verdicts(conn)
    bands: dict[str, dict] = {
        name: {"band": name, "lo": lo, "hi": hi, "wallets": 0, "positions": 0, "wins": 0,
               "in_usd": 0.0, "out_usd": 0.0, "returns": []}
        for lo, hi, name in BANDS
    }

    for address, v in verdicts.items():
        band = next((n for lo, hi, n in BANDS if lo <= v["score"] < hi), None)
        if band is None:
            continue
        ps = [p for p in positions_after_verdict(conn, address, v["ts"])
              if p["bought_usd"] >= min_usd]
        if not ps:
            continue
        b = bands[band]
        b["wallets"] += 1
        for p in ps:
            # what the part that left had cost, against what it brought back
            cost = p["bought_usd"] * min(p["exit_pct"] or 0, 1.0)
            if cost <= 0:
                continue
            b["positions"] += 1
            b["in_usd"] += cost
            b["out_usd"] += p["sold_usd"]
            b["wins"] += 1 if p["realized"] > 0 else 0
            b["returns"].append(p["sold_usd"] / cost)

    for b in bands.values():
        n = b["positions"]
        b["win_rate"] = b["wins"] / n if n else None
        b["median_return"] = statistics.median(b["returns"]) if b["returns"] else None
        # The number that decides it: every dollar the band put in, against every dollar that came
        # back. A median cannot see the position that paid for all the others, and in this market
        # that position is the whole business.
        b["pooled_return"] = (b["out_usd"] / b["in_usd"]) if b["in_usd"] else None
        b.pop("returns")

    ranked = [bands[n] for _, _, n in BANDS]
    return {
        "bands": ranked,
        "scored_wallets": len(verdicts),
        "positions": sum(b["positions"] for b in ranked),
        "min_usd": min_usd,
        # The claim the product makes, reduced to one boolean: did the top band come out ahead of
        # the bottom one on pooled return. Null when either band has nothing to say yet.
        "separates": (
            None if not (ranked[0]["pooled_return"] and ranked[-1]["pooled_return"])
            else ranked[0]["pooled_return"] > ranked[-1]["pooled_return"]
        ),
    }


def report(result: dict) -> str:
    """The table, plus the caveat. Never print one without the other."""
    out = [f"Positions opened *after* the verdict that assigned the band, closed since, "
           f"over ${result['min_usd']:.0f}.", ""]
    out.append(f"{'band':<9}{'wallets':>8}{'closed':>8}{'win':>7}{'median':>9}{'pooled':>9}"
               f"{'in':>12}{'out':>12}")
    for b in result["bands"]:
        win = f"{b['win_rate']:.0%}" if b["win_rate"] is not None else "—"
        med = f"{b['median_return']:.2f}x" if b["median_return"] is not None else "—"
        pool = f"{b['pooled_return']:.2f}x" if b["pooled_return"] is not None else "—"
        out.append(f"{b['band']:<9}{b['wallets']:>8}{b['positions']:>8}{win:>7}{med:>9}{pool:>9}"
                   f"{analyze.usd(b['in_usd']):>12}{analyze.usd(b['out_usd']):>12}")
    out.append("")
    if result["separates"] is None:
        out.append("Not enough closed positions to say anything yet. That is the honest answer.")
    elif result["separates"]:
        out.append("The top band returned more per dollar than the bottom one.")
    else:
        out.append("The top band did NOT return more per dollar than the bottom one. "
                   "The score is not earning its place.")
    out.append("Small counts mean nothing; open positions are excluded; wallets that stopped "
               "trading stop contributing.")
    return "\n".join(out)
