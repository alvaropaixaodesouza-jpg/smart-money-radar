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
    """Every position this wallet opened after it was scored, whatever state it is in now.

    Open ones are kept rather than filtered here, because leaving them out is not the conservative
    choice it looks like. A trader who buys something that runs does not sell it, so the positions
    that close are systematically the ones that did not work — and a table built only from those
    measures how well a cohort cuts losses, not whether it picks winners.
    """
    out = []
    for p in analyze.ledger(conn, address):
        if p["first_ts"] is None or p["first_ts"] < since:
            continue
        # `held` and `pre-tape` mean the entry predates our record, so cost is unknowable; without
        # a cost there is no return to compute either way.
        if p["state"] not in ("closed", "trimmed", "open") or not p["bought_usd"]:
            continue
        out.append(p)
    return out


def first_entry_price(conn: sqlite3.Connection, address: str, token: str,
                      since: int) -> float | None:
    """Preço implícito da primeira compra real feita depois do veredito.

    O benchmark precisa começar no instante em que a carteira decidiu entrar. Usar o custo médio
    de todas as compras deslocaria o ponto de partida para depois da decisão e favoreceria quem
    aumentou posição somente após o preço já ter se movido.
    """
    row = conn.execute(
        "SELECT usd_value / token_amount price FROM trades "
        "WHERE address=? AND mint=? AND side='buy' AND ts>=? "
        "  AND usd_value > 0 AND token_amount > 0 "
        "  AND COALESCE(kind, 'trade') = 'trade' "
        "ORDER BY ts, rowid LIMIT 1",
        (address, token, since),
    ).fetchone()
    return float(row["price"]) if row and row["price"] and row["price"] > 0 else None


def calibrate(conn: sqlite3.Connection, min_usd: float = 100.0) -> dict:
    """Group every post-verdict closed position by the band its wallet was in, and count.

    `min_usd` drops dust: a wallet that put twenty dollars into a launch and took thirty out has a
    150% return and has demonstrated nothing, and enough of those would swamp the real positions.
    """
    verdicts = first_verdicts(conn)
    bands: dict[str, dict] = {
        name: {"band": name, "lo": lo, "hi": hi, "wallets": 0,
               # closed and trimmed, counted at what actually came back
               "closed": 0, "wins": 0, "in_usd": 0.0, "out_usd": 0.0, "returns": [],
               # every position, with what is still held marked at the token's current price
               "all": 0, "unpriced": 0, "in_all": 0.0, "out_all": 0.0,
               # Paired benchmark: what the wallet returned versus putting the same dollars into
               # the same token at its first entry and simply holding it to today's price.
               "benchmark_positions": 0, "benchmark_unpriced": 0,
               "paired_in": 0.0, "paired_out": 0.0, "benchmark_out": 0.0,
               "token_returns": []}
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
            # --- realized: what the part that left had cost, against what it brought back
            cost = p["bought_usd"] * min(p["exit_pct"] or 0, 1.0)
            if cost > 0 and p["realized"] is not None:
                b["closed"] += 1
                b["in_usd"] += cost
                b["out_usd"] += p["sold_usd"]
                b["wins"] += 1 if p["realized"] > 0 else 0
                b["returns"].append(p["sold_usd"] / cost)

            # --- marked: the whole entry against sales plus what the rest is worth now
            still_held = p["state"] in ("open", "trimmed")
            if still_held and p["value"] is None:
                b["unpriced"] += 1          # cannot be marked, so it joins neither total
                b["benchmark_unpriced"] += 1
                continue
            b["all"] += 1
            b["in_all"] += p["bought_usd"]
            b["out_all"] += p["sold_usd"] + (p["value"] or 0)

            # --- matched token benchmark: the same token, the same entry moment, same dollars.
            # Closed positions keep what the wallet actually sold for; open/trimmed positions add
            # the current value of what remains. The passive side ignores the wallet's exits and
            # holds the whole hypothetical entry to the current token price.
            entry_price = first_entry_price(conn, address, p["token"], v["ts"])
            current_price = p.get("price")
            current_at = p.get("price_at")
            if (entry_price is None or current_price is None or current_price <= 0
                    or current_at is None or current_at < p["first_ts"]):
                b["benchmark_unpriced"] += 1
                continue
            token_return = current_price / entry_price
            actual_out = p["sold_usd"] + (p["value"] or 0)
            b["benchmark_positions"] += 1
            b["paired_in"] += p["bought_usd"]
            b["paired_out"] += actual_out
            b["benchmark_out"] += p["bought_usd"] * token_return
            b["token_returns"].append(token_return)

    for b in bands.values():
        b["win_rate"] = b["wins"] / b["closed"] if b["closed"] else None
        b["median_return"] = statistics.median(b["returns"]) if b["returns"] else None
        # Every dollar the band put in against every dollar that came back. A median cannot see the
        # position that paid for all the others, and in this market that position is the business.
        b["pooled_return"] = (b["out_usd"] / b["in_usd"]) if b["in_usd"] else None
        b["marked_return"] = (b["out_all"] / b["in_all"]) if b["in_all"] else None
        b["paired_return"] = (b["paired_out"] / b["paired_in"]) if b["paired_in"] else None
        b["benchmark_return"] = (
            b["benchmark_out"] / b["paired_in"] if b["paired_in"] else None
        )
        b["benchmark_median_return"] = (
            statistics.median(b["token_returns"]) if b["token_returns"] else None
        )
        b["excess_return"] = (
            b["paired_return"] / b["benchmark_return"]
            if b["paired_return"] is not None and b["benchmark_return"]
            else None
        )
        b.pop("returns")
        b.pop("token_returns")

    ranked = [bands[n] for _, _, n in BANDS]
    ages = sorted((db.now() - v["ts"]) / 86400 for v in verdicts.values())
    return {
        "bands": ranked,
        "scored_wallets": len(verdicts),
        # How long the verdicts have had to be right. Without it the table is unreadable: 0.88x
        # over three days and 0.88x over a year are not the same claim.
        "median_age_days": statistics.median(ages) if ages else None,
        "oldest_age_days": ages[-1] if ages else None,
        "positions": sum(b["closed"] for b in ranked),
        "min_usd": min_usd,
        # The claim the product makes, reduced to one boolean per reading: did the top band come
        # out ahead of the bottom one. Null where either band has nothing to say yet.
        "separates": (
            None if not (ranked[0]["pooled_return"] and ranked[-1]["pooled_return"])
            else ranked[0]["pooled_return"] > ranked[-1]["pooled_return"]
        ),
        "separates_marked": (
            None if not (ranked[0]["marked_return"] and ranked[-1]["marked_return"])
            else ranked[0]["marked_return"] > ranked[-1]["marked_return"]
        ),
        "separates_excess": (
            None if (ranked[0]["excess_return"] is None
                     or ranked[-1]["excess_return"] is None)
            else ranked[0]["excess_return"] > ranked[-1]["excess_return"]
        ),
    }


def report(result: dict) -> str:
    """The table, plus the caveat. Never print one without the other."""
    age = (f"a median of {result['median_age_days']:.1f} days, longest {result['oldest_age_days']:.1f}"
           if result.get("median_age_days") is not None else "an unknown period")
    out = [f"Positions opened *after* the verdict that assigned the band, over "
           f"${result['min_usd']:.0f}. Verdicts have had {age} to be right.",
           "Two readings, because neither alone is honest.", ""]
    out.append(f"{'band':<9}{'wallets':>8}{'closed':>8}{'win':>7}{'median':>9}{'realized':>10}"
               f"{'  ':>3}{'all':>7}{'marked':>9}")
    for b in result["bands"]:
        win = f"{b['win_rate']:.0%}" if b["win_rate"] is not None else "—"
        med = f"{b['median_return']:.2f}x" if b["median_return"] is not None else "—"
        pool = f"{b['pooled_return']:.2f}x" if b["pooled_return"] is not None else "—"
        mark = f"{b['marked_return']:.2f}x" if b["marked_return"] is not None else "—"
        out.append(f"{b['band']:<9}{b['wallets']:>8}{b['closed']:>8}{win:>7}{med:>9}{pool:>10}"
                   f"{'  ':>3}{b['all']:>7}{mark:>9}")
    out.append("")
    out.append("realized — closed and trimmed positions only, at what actually came back.")
    out.append("marked   — every position, with what is still held valued at today's price.")
    out.append("")
    out.append("Matched token benchmark — the same dollars buy the same token at the wallet's "
               "first entry and hold it to today's price.")
    out.append(f"{'band':<9}{'paired':>8}{'actual':>10}{'token':>10}{'excess':>10}{'missing':>10}")
    for b in result["bands"]:
        actual = f"{b['paired_return']:.2f}x" if b["paired_return"] is not None else "—"
        token = f"{b['benchmark_return']:.2f}x" if b["benchmark_return"] is not None else "—"
        excess = f"{b['excess_return']:.2f}x" if b["excess_return"] is not None else "—"
        out.append(f"{b['band']:<9}{b['benchmark_positions']:>8}{actual:>10}{token:>10}"
                   f"{excess:>10}{b['benchmark_unpriced']:>10}")
    out.append("actual — wallet exits plus the current value of anything it still holds.")
    out.append("token  — passive buy-and-hold return from the wallet's first entry price.")
    out.append("excess — actual divided by token; above 1.00x means the wallet beat holding it.")
    out.append("")
    if result["separates"] is None:
        out.append("Not enough closed positions to say anything yet. That is the honest answer.")
    elif result["separates"]:
        out.append("Realized: the top band returned more per dollar than the bottom one.")
    else:
        out.append("Realized: the top band did NOT beat the bottom one. The score is not earning "
                   "its place.")
    if result["separates_marked"] is not None:
        out.append(("Marked: so did it." if result["separates_marked"]
                    else "Marked: it did NOT, once open positions are counted."))
    out.append("")
    out.append("Why both: a trader who buys something that runs does not sell it, so the positions "
               "that close are systematically the ones that did not work. Realized measures how "
               "well a cohort cuts losses. Marked measures whether it picks winners, and pays for "
               "that by trusting a price quote.")
    out.append("Small counts mean nothing; wallets that stopped trading stop contributing.")
    out.append("")
    if result["separates_excess"] is None:
        out.append("Not enough paired token prices to compare the top and bottom bands yet.")
    elif result["separates_excess"]:
        out.append("Matched benchmark: the top band beat its own tokens by more than the bottom "
                   "band did.")
    else:
        out.append("Matched benchmark: the top band did NOT add more value over holding the same "
                   "tokens than the bottom band did.")
    out.append("This is a matched token benchmark, not a chain-wide index: it separates wallet "
               "selection and exits from the move of the assets each wallet actually chose.")
    return "\n".join(out)
