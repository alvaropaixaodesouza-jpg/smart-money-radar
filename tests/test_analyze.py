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
