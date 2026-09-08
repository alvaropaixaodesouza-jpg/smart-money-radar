"""The two questions the CLI answers offline: what about this token, what about this trader."""
import json

import pytest

from fomo_agent import db
from fomo_agent.pipeline.analyze import (analyze_token, analyze_trader, conviction, find_trader,
                                         format_token, format_trader)
from fomo_agent.sources.rpc import USDG

ACE = "0x" + "a" * 40      # scores 85
MID = "0x" + "b" * 40      # scores 62
DUD = "0x" + "c" * 40      # scores 30
TOKEN = "0x" + "d" * 40


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "analyze.db")
    now = db.now()
    with db.tx(c):
        for addr, handle, uid, score, status in ((ACE, "ace", "u1", 85, "active"),
                                                 (MID, "mid", "u2", 62, "watch"),
                                                 (DUD, "dud", "u3", 30, "dropped")):
            db.upsert_trader(c, addr, chain="robinhood", fomo_handle=handle, fomo_user_id=uid,
                             score=score, status=status, pnl_30d=1_000_000.0,
                             ai_summary=f"{handle} verdict", ai_model="claude-opus-5",
                             tags=json.dumps({"style": ["swing"], "red_flags": []}),
                             stats_json=json.dumps({"win_rate": 0.55, "fills": 40}))
        db.upsert_token(c, TOKEN, chain="robinhood", symbol="PONS", liquidity_usd=500_000,
                        mcap_usd=40_000_000)
        for tid, uid, pnl, cost in (("p1", "u1", 900_000.0, 30_000.0), ("p2", "u2", 40_000.0, 8_000.0)):
            c.execute("INSERT INTO fomo_positions(trade_id, user_id, token, chain, unrealized_pnl, "
                      "cost_basis, seen_at) VALUES(?,?,?,?,?,?,?)",
                      (tid, uid, TOKEN, "robinhood", pnl, cost, now))
        for i, (addr, side, usd, ago) in enumerate(((ACE, "buy", 5_000.0, 3600),
                                                    (MID, "buy", 2_000.0, 1800),
                                                    (DUD, "sell", 900.0, 600))):
            db.insert_trade(c, sig=f"0xtx{i}", address=addr, chain="robinhood", mint=TOKEN,
                            side=side, usd_value=usd, ts=now - ago, source="rpc")
    return c


def test_conviction_weighs_quality_over_headcount():
    """One trader scoring 85 outweighs a crowd of mediocre wallets."""
    assert conviction([85]) > conviction([40] * 4)
    assert conviction([]) == 0
    assert conviction([None, 0, 50]) == pytest.approx(0.25)


def test_token_report_reads_holders_and_flow(conn):
    a = analyze_token(conn, TOKEN, hours=48)
    assert a["symbol"] == "PONS" and not a["is_quote"]
    assert [h["handle"] for h in a["holders"]] == ["ace", "mid"], "best score first"
    assert a["trusted_holders"] == 2 and a["avg_score"] == pytest.approx(73.5)
    assert a["cohort_cost"] == 38_000 and a["cohort_pnl"] == 940_000
    assert a["bought_usd"] == 7_000 and a["sold_usd"] == 900
    assert a["trusted_buyers"] == 2, "the wallet scoring 30 does not count"


def test_token_report_flags_a_quote_asset(conn):
    a = analyze_token(conn, USDG)
    assert a["is_quote"]
    assert "quote asset" in format_token(a)


def test_token_report_survives_an_unknown_address(conn):
    a = analyze_token(conn, "0x" + "9" * 40)
    assert a["holders"] == [] and a["flow"] == [] and a["conviction"] == 0
    assert "nobody on the watchlist" in format_token(a)


def test_trader_lookup_takes_a_handle_or_an_address(conn):
    assert find_trader(conn, "ace")["address"] == ACE
    assert find_trader(conn, ACE.upper())["fomo_handle"] == "ace"
    assert find_trader(conn, "nobody") is None
    assert analyze_trader(conn, "nobody") is None


def test_trader_report_carries_the_verdict_and_the_book(conn):
    a = analyze_trader(conn, "ace", hours=48)
    assert a["score"] == 85 and a["status"] == "active"
    assert a["summary"] == "ace verdict"
    assert [p["sym"] for p in a["positions"]] == ["PONS"]
    assert a["open_pnl"] == 900_000
    assert a["bought_usd"] == 5_000 and a["sold_usd"] == 0
    assert [c["handle"] for c in a["company"]] == ["mid"], "shares the PONS position"

    text = format_trader(a)
    assert "ace" in text and "PONS" in text and "$900.0k" in text


def test_reports_render_without_scores_or_prices(conn):
    """Nothing here may crash on the half-filled rows that discovery produces."""
    with db.tx(conn):
        db.upsert_trader(conn, "0x" + "e" * 40, chain="robinhood", fomo_handle="fresh",
                         fomo_user_id="u4", status="candidate")
        db.insert_trade(conn, sig="0xtx9", address="0x" + "e" * 40, chain="robinhood", mint=TOKEN,
                        side="buy", usd_value=None, ts=db.now(), source="rpc")
    assert format_token(analyze_token(conn, TOKEN))
    assert format_trader(analyze_trader(conn, "fresh"))


# ---------------------------------------------------------------- bot heuristic

def ctx(trades, tokens, hold):
    return {"last_7d": {"trades": trades, "unique_tokens": tokens, "median_hold_min": hold}}


def test_bot_heuristic_needs_all_three_conditions():
    """Frequency alone is not a bot — the best early-entry traders here are also very fast."""
    from fomo_agent.pipeline.score import looks_automated

    # 600 trades confined to six tokens at a two-minute hold: inventory cycling
    assert looks_automated(ctx(600, 6, 2))
    # the same frequency spread over 100 tokens is a person hunting launches
    assert looks_automated(ctx(600, 100, 2)) is None
    # narrow and fast, but not frequent enough to be automated
    assert looks_automated(ctx(40, 4, 1)) is None
    # narrow and frequent, but held long enough for a thesis
    assert looks_automated(ctx(600, 6, 240)) is None
    # a half-filled context must never trip it
    assert looks_automated({}) is None
    assert looks_automated(ctx(600, 6, None)) is None
    assert looks_automated(ctx(600, 0, 1)) is None


def test_drop_automated_takes_the_bot_out_of_the_queue(conn):
    from fomo_agent import db as _db
    from fomo_agent.pipeline.score import drop_automated

    bot = "0x" + "f" * 40
    with _db.tx(conn):
        _db.upsert_trader(conn, bot, chain="robinhood", status="tracking")
        # 110 round trips over two tokens, each held a minute: high frequency, no breadth
        now = _db.now()
        for i in range(110):
            mint = TOKEN if i % 2 else "0x" + "e" * 40
            _db.insert_trade(conn, sig=f"0xbotb{i}", address=bot, chain="robinhood", mint=mint,
                             side="buy", usd_value=10.0, ts=now - 3600 - i * 600, source="rpc")
            _db.insert_trade(conn, sig=f"0xbots{i}", address=bot, chain="robinhood", mint=mint,
                             side="sell", usd_value=10.0, ts=now - 3540 - i * 600, source="rpc")
    rows = conn.execute("SELECT * FROM traders WHERE address IN (?, ?)", (bot, ACE)).fetchall()
    keep, dropped = drop_automated(conn, rows)

    assert dropped == 1 and [r["address"] for r in keep] == [ACE]
    row = _db.get_trader(conn, bot)
    assert row["status"] == "dropped" and row["ai_model"] == "heuristic:bot"
    assert "automated" in row["ai_summary"]


def test_a_fresh_token_still_answers_from_its_buyers(conn):
    """The bug this covers: a token nobody holds yet looked empty on the page.

    Positions come from fomo's snapshot of a trader's three biggest bags, so a token that has not
    grown into anyone's top three shows no holders — while trusted wallets are already buying it.
    Reading conviction only from holders made the feed and the token page disagree about the same
    token, one saying 13.3 and the other 0.0.
    """
    fresh = "0x" + "f" * 40
    with db.tx(conn):
        db.upsert_token(conn, fresh, chain="robinhood", symbol="CME", liquidity_usd=81_000)
        for i, (addr, side, usd_v) in enumerate(((ACE, "buy", 4_000.0), (ACE, "buy", 2_000.0),
                                                 (MID, "buy", 1_000.0), (MID, "sell", 500.0),
                                                 (DUD, "buy", 9_000.0))):
            db.insert_trade(conn, sig=f"0xfresh{i}", address=addr, chain="robinhood", mint=fresh,
                            side=side, usd_value=usd_v, ts=db.now() - 600, source="rpc")

    a = analyze_token(conn, fresh)
    assert a["holders"] == [], "nobody holds it yet, which is the whole point"
    assert a["conviction"] == 0, "holder conviction is genuinely zero"

    assert [b["handle"] for b in a["buyers"]] == ["ace", "mid", "dud"], "best score first"
    assert a["buyer_conviction"] == pytest.approx(0.85 ** 2 + 0.62 ** 2), "the wallet scoring 30 does not count"
    assert a["buyer_conviction"] > 0, "the page has something real to show"

    ace = a["buyers"][0]
    assert ace["bought"] == 6_000 and ace["sold"] == 0 and ace["fills"] == 2, "fills roll up per wallet"
    mid = a["buyers"][1]
    assert mid["bought"] == 1_000 and mid["sold"] == 500, "both sides are kept"


def test_buyer_conviction_matches_what_the_feed_ranks_by(conn):
    """One token, one number: the feed and the token page must not disagree."""
    from fomo_agent.pipeline.analyze import signals

    sig = next(s for s in signals(conn, "robinhood", hours=24) if s["sym"] == "PONS")
    tok = analyze_token(conn, TOKEN, hours=24)
    assert tok["buyer_conviction"] == pytest.approx(sig["conviction"])


# ---------------------------------------------------------------- the book

def test_position_state_reads_how_much_of_the_entry_is_left():
    """The four things a wallet's buys and sells can say about a position, and the one they cannot."""
    from fomo_agent.pipeline.analyze import position_state

    assert position_state(100, 0) == ("open", 0.0)
    state, exit_pct = position_state(100, 40)
    assert state == "trimmed" and exit_pct == pytest.approx(0.4)
    assert position_state(100, 99)[0] == "closed", "dust left behind is still a closed position"
    assert position_state(100, 100)[0] == "closed"
    assert position_state(0, 500)[0] == "pre-tape", "sold what we never saw bought"
    assert position_state(100, 300)[0] == "pre-tape"
    assert position_state(None, None)[0] == "unknown", "fills recorded without sizes"


def test_the_book_separates_what_is_held_from_what_came_back_out(conn):
    """A round trip, a trim and a position still running, all from one wallet's tape."""
    from fomo_agent.pipeline.analyze import book

    won, cut, run = ("0x" + "1" * 40), ("0x" + "2" * 40), ("0x" + "3" * 40)
    now = db.now()
    with db.tx(conn):
        for mint, sym, price in ((won, "WON", None), (cut, "CUT", 0.002), (run, "RUN", 0.5)):
            db.upsert_token(conn, mint, chain="robinhood", symbol=sym, price_usd=price,
                            price_at=now if price else None)
        rows = (
            (won, "buy", 1_000.0, 1_000_000.0), (won, "sell", 4_000.0, 1_000_000.0),   # sold out
            (cut, "buy", 2_000.0, 1_000_000.0), (cut, "sell", 1_500.0, 250_000.0),     # quarter out
            (run, "buy", 500.0, 1_000.0),                                              # untouched
        )
        for i, (mint, side, usd_v, amt) in enumerate(rows):
            db.insert_trade(conn, sig=f"0xbook{i}", address=ACE, chain="robinhood", mint=mint,
                            side=side, usd_value=usd_v, token_amount=amt, ts=now - 900,
                            source="rpc")

    b = book(conn, ACE, "u1")
    held = {p["sym"]: p for p in b["positions"]}
    assert "WON" not in held, "a position sold out entirely is not still held"
    assert held["CUT"]["state"] == "trimmed" and held["RUN"]["state"] == "open"
    # three quarters of a $2k entry left, priced at 750k tokens x $0.002
    assert held["CUT"]["cost"] == pytest.approx(1_500) and held["CUT"]["value"] == pytest.approx(1_500)
    assert held["RUN"]["value"] == pytest.approx(500), "1000 tokens at 50c"

    out = {p["sym"]: p for p in b["closed"]}
    assert set(out) == {"WON", "CUT"}, "a trim belongs here too, for the part that left"
    assert out["WON"]["realized"] == pytest.approx(3_000), "out less in, sold to nothing"
    assert out["CUT"]["realized"] == pytest.approx(1_000), "out less the quarter it cost"
    assert b["closed"][0]["sym"] == "WON", "ranked by what came back out"
    assert b["round_trips"] == 1 and b["wins"] == 1, "only the position sold out entirely is decided"


def test_a_position_older_than_the_tape_is_reported_as_neither(conn):
    """Selling what we never saw bought cannot be priced, so it must not become a windfall."""
    from fomo_agent.pipeline.analyze import book

    old = "0x" + "4" * 40
    with db.tx(conn):
        db.upsert_token(conn, old, chain="robinhood", symbol="OLD")
        db.insert_trade(conn, sig="0xold", address=MID, chain="robinhood", mint=old, side="sell",
                        usd_value=90_000.0, token_amount=5_000.0, ts=db.now() - 300, source="rpc")

    b = book(conn, MID, "u2")
    assert "OLD" not in {p["sym"] for p in b["positions"]}
    assert "OLD" not in {p["sym"] for p in b["closed"]}, "$90k of profit we cannot claim"
    assert b["pre_tape"] == 1, "and the reader is told it was left out"


def test_the_book_keeps_fomo_marks_and_the_quote_asset_out(conn):
    """fomo prices a whole position; the tape only prices what it watched. USDG is neither."""
    from fomo_agent.pipeline.analyze import book

    with db.tx(conn):
        db.upsert_token(conn, USDG, chain="robinhood", symbol="USDG", price_usd=1.0)
        db.insert_trade(conn, sig="0xusdg", address=ACE, chain="robinhood", mint=USDG, side="buy",
                        usd_value=50_000.0, token_amount=50_000.0, ts=db.now() - 60, source="rpc")

    b = book(conn, ACE, "u1")
    assert "USDG" not in {p["sym"] for p in b["positions"]}, "holding a stablecoin is holding cash"
    pons = next(p for p in b["positions"] if p["sym"] == "PONS")
    assert pons["src"] == "fomo" and pons["pnl"] == 900_000, "fomo's mark covers the whole position"


def test_the_chain_balance_outranks_what_the_tape_watched(conn):
    """What a wallet holds is a fact; what we saw it buy is a window onto one."""
    from fomo_agent.pipeline.analyze import book, position_state

    # the chain settles the three cases the tape argues about
    assert position_state(100, 40, balance=0)[0] == "closed", "nothing left, whatever we watched"
    assert position_state(100, 40, balance=60)[0] == "trimmed"
    assert position_state(100, 0, balance=100)[0] == "open"
    assert position_state(100, 0, balance=5_000)[0] == "held", "holds what it bought before us"
    assert position_state(None, None, balance=42)[0] == "held", "sizeless fills, real balance"

    old, sold = ("0x" + "7" * 40), ("0x" + "8" * 40)
    now = db.now()
    with db.tx(conn):
        db.upsert_token(conn, old, chain="robinhood", symbol="OLD", price_usd=2.0, price_at=now)
        db.upsert_token(conn, sold, chain="robinhood", symbol="SOLD", price_usd=1.0, price_at=now)
        db.insert_trade(conn, sig="0xold1", address=ACE, chain="robinhood", mint=old, side="buy",
                        usd_value=1_000.0, token_amount=100.0, ts=now - 900, source="rpc")
        db.insert_trade(conn, sig="0xsold1", address=ACE, chain="robinhood", mint=sold, side="buy",
                        usd_value=300.0, token_amount=300.0, ts=now - 900, source="rpc")
        db.insert_trade(conn, sig="0xsold2", address=ACE, chain="robinhood", mint=sold, side="sell",
                        usd_value=900.0, token_amount=300.0, ts=now - 300, source="rpc")
        # it holds ten times what we watched it buy, and nothing at all of the other
        db.save_holdings(conn, {(ACE, old): 1_000.0, (ACE, sold): 0.0})

    b = book(conn, ACE, None)
    held = next(p for p in b["positions"] if p["sym"] == "OLD")
    assert held["state"] == "held" and held["held"] == 1_000
    assert held["value"] == pytest.approx(2_000), "priced on what it holds, not on what we saw"
    assert held["pnl"] is None, "the entry predates us, so the profit is not ours to state"

    assert "SOLD" not in {p["sym"] for p in b["positions"]}, "the chain says it is gone"
    out = next(p for p in b["closed"] if p["sym"] == "SOLD")
    assert out["realized"] == pytest.approx(600), "and the round trip is scored"


def test_a_token_lists_the_wallets_whose_balance_says_they_hold_it(conn):
    """fomo publishes three bags per trader; a balance has no such limit."""
    mint = "0x" + "9" * 40
    now = db.now()
    with db.tx(conn):
        db.upsert_token(conn, mint, chain="robinhood", symbol="HOLD", price_usd=0.5, price_at=now)
        # ace holds it and fomo does not carry the position; mid held it and sold out
        db.save_holdings(conn, {(ACE, mint): 1_000.0, (MID, mint): 0.0})

    a = analyze_token(conn, mint)
    assert [h["handle"] for h in a["holders"]] == ["ace"], "a zero balance is not a holder"
    assert a["holders"][0]["value"] == pytest.approx(500), "1000 tokens at 50c"
    assert a["cohort_value"] == pytest.approx(500)
    assert a["conviction"] == pytest.approx(0.85 ** 2), "holder conviction is real now"
    assert a["holders"][0]["pnl"] is None, "no fomo mark, so no profit claimed"
