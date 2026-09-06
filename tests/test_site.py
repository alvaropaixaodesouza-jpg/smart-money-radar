"""The published page: what it reads out of the database and what it must never show.

Built against a temporary database, so these run offline and assert on the HTML itself.
"""
import json

import pytest

from fomo_agent import db
from fomo_agent.pipeline import site
from fomo_agent.sources.rpc import USDG

GOOD = "0x" + "a" * 40
ALSO_GOOD = "0x" + "b" * 40
WEAK = "0x" + "c" * 40
TOKEN = "0x" + "d" * 40


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "site.db")
    now = db.now()
    with db.tx(c):
        for addr, handle, score, status, pnl, uid in (
            (GOOD, "alice", 82, "active", 1_900_000.0, "u1"),
            (ALSO_GOOD, "bob", 71, "watch", 240_000.0, "u2"),
            (WEAK, "carol", 20, "dropped", -500.0, "u3"),
        ):
            db.upsert_trader(c, addr, chain="robinhood", fomo_handle=handle, fomo_user_id=uid,
                             score=score, status=status, pnl_30d=pnl, ai_model="claude-opus-5",
                             ai_summary=f"{handle} summary", tags=json.dumps({"style": ["swing"]}),
                             stats_json=json.dumps({"win_rate": 0.6, "fills": 12}))
        db.upsert_token(c, TOKEN, chain="robinhood", symbol="PONS", liquidity_usd=800_000)
        db.upsert_token(c, USDG, chain="robinhood", symbol="USDG", liquidity_usd=9_000_000)
        for i, (addr, mint) in enumerate((
            (GOOD, TOKEN), (ALSO_GOOD, TOKEN),          # two trusted buyers: a signal
            (GOOD, USDG), (ALSO_GOOD, USDG),            # the quote asset: never a signal
            (WEAK, TOKEN),                              # an untrusted buyer does not count
        )):
            db.insert_trade(c, sig=f"0xsig{i}", address=addr, chain="robinhood", mint=mint,
                            side="buy", usd_value=1000.0 + i, ts=now - 600, source="rpc")
        for tid, uid, mint, pnl, cost in (("t1", "u1", TOKEN, 500_000.0, 25_000.0),
                                           ("t2", "u2", USDG, 10.0, 10.0)):
            c.execute("INSERT INTO fomo_positions(trade_id, user_id, token, chain, unrealized_pnl, "
                      "cost_basis, seen_at) VALUES(?,?,?,?,?,?,?)",
                      (tid, uid, mint, "robinhood", pnl, cost, now))
    return c


def test_collect_reads_the_scored_roster(conn):
    data = site.collect(conn, "robinhood", hours=24)
    assert [t["handle"] for t in data["traders"]] == ["alice", "bob", "carol"], "active first"
    assert data["counts"] == {"active": 1, "watch": 1, "dropped": 1}
    assert data["traders"][0]["positions"][0]["symbol"] == "PONS"


def test_a_signal_needs_two_trusted_buyers(conn):
    signals = site.collect(conn, "robinhood", hours=24)["signals"]
    assert [s["sym"] for s in signals] == ["PONS"]
    assert signals[0]["buyers"] == 2, "carol scores too low to count"


def test_the_quote_asset_never_reaches_the_page(conn):
    """Some sources book a swap from the pool's side, filing every sale as a USDG purchase."""
    data = site.collect(conn, "robinhood", hours=24)
    assert USDG not in [s["mint"] for s in data["signals"]]
    assert USDG not in [t["token"] for t in data["tokens"]]
    assert USDG not in [t["mint"] for t in data["tape"]]
    assert "USDG" not in site.render(data)


def test_render_fills_every_placeholder(conn):
    html = site.render(site.collect(conn, "robinhood", hours=24))
    assert "__" not in html.split("<style>")[0] + html.split("</style>")[-1]
    for handle in ("alice", "bob", "carol"):
        assert f">{handle}</a>" in html
    assert "$1.9M" in html, "fomo PnL is shown in the trader's own scale"
    assert html.count('role="tabpanel"') == 3


def test_build_writes_a_standalone_file(conn, tmp_path):
    out = tmp_path / "radar.html"
    stats = site.build(conn, out, "robinhood", hours=24)
    html = out.read_text(encoding="utf-8")
    assert stats["traders"] == 3 and stats["signals"] == 1
    assert html.startswith("<title>") and "<script>" in html
    assert "fonts.googleapis.com" in html, "the only external request the page makes"


def test_escaping_keeps_markup_out_of_the_page(conn):
    with db.tx(conn):
        db.upsert_trader(conn, GOOD, ai_summary="<script>alert(1)</script> & co")
    html = site.render(site.collect(conn, "robinhood", hours=24))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in html


def test_formatting_helpers():
    assert site.usd(1_500_000) == "$1.5M"
    assert site.usd(2_400) == "$2k"
    assert site.usd(-30) == "$-30"
    assert site.usd(None) == "—"
    assert site.multiple(1000, 9000) == "10.0×"
    assert site.multiple(0, 9000) == "—", "no cost basis, no multiple"
    assert site.multiple(None, None) == "—"
    now = 1_700_000_000
    assert site.ago(now - 90, now) == "1m"
    assert site.ago(now - 7200, now) == "2h"
    assert site.ago(now - 86400 * 3, now) == "3d"
