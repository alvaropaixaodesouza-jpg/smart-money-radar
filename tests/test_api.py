"""The HTTP API, against a temporary database and with no network.

These guard the contract the site and any third-party client will depend on: the shape of a
response, the bounds on a query string, and the promise that a quote asset never reads as a signal.
"""
import json

import pytest
from fastapi.testclient import TestClient

from fomo_agent import api, db
from fomo_agent.config import settings
from fomo_agent.sources.rpc import USDG

ACE = "0x" + "a" * 40      # scores 88
MID = "0x" + "b" * 40      # scores 74
DUD = "0x" + "c" * 40      # scores 30
TOKEN = "0x" + "d" * 40
UNKNOWN = "0x" + "9" * 40


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "api.db")
    c = db.connect()
    now = db.now()
    with db.tx(c):
        for addr, handle, uid, score, status in ((ACE, "ace", "u1", 88, "active"),
                                                 (MID, "mid", "u2", 74, "active"),
                                                 (DUD, "dud", "u3", 30, "dropped")):
            db.upsert_trader(c, addr, chain="robinhood", fomo_handle=handle, fomo_user_id=uid,
                             score=score, status=status, pnl_30d=2_000_000.0,
                             ai_summary=f"{handle} verdict", ai_model="claude-opus-5",
                             tags=json.dumps({"style": ["swing"], "red_flags": ["one-hit"]}),
                             stats_json=json.dumps({"win_rate": 0.6}))
        db.upsert_token(c, TOKEN, chain="robinhood", symbol="PONS", liquidity_usd=500_000)
        db.upsert_token(c, USDG, chain="robinhood", symbol="USDG", liquidity_usd=9_000_000)
        c.execute("INSERT INTO fomo_positions(trade_id, user_id, token, chain, unrealized_pnl, "
                  "cost_basis, seen_at) VALUES(?,?,?,?,?,?,?)",
                  ("p1", "u1", TOKEN, "robinhood", 900_000.0, 30_000.0, now))
        for i, (addr, mint) in enumerate(((ACE, TOKEN), (MID, TOKEN), (DUD, TOKEN),
                                          (ACE, USDG), (MID, USDG))):
            db.insert_trade(c, sig=f"0xsig{i}", address=addr, chain="robinhood", mint=mint,
                            side="buy", usd_value=5_000.0, ts=now - 900, source="rpc")
    c.close()
    api.limiter.hits.clear()
    return TestClient(api.app)


def test_health_reports_freshness(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True and body["chain"] == "robinhood"
    assert body["stale_seconds"] < 3600


def test_stats_excludes_quote_assets_from_the_fill_count(client):
    body = client.get("/api/stats").json()
    assert body["traders"] == 3 and body["scored"] == 3
    assert body["active"] == 2 and body["dropped"] == 1
    assert body["fills"] == 3, "the two USDG rows are plumbing, not fills"


def test_signals_rank_by_conviction_and_name_the_buyers(client):
    body = client.get("/api/signals?hours=24").json()
    assert [s["sym"] for s in body["signals"]] == ["PONS"], "USDG never reaches the feed"
    sig = body["signals"][0]
    assert sig["buyers"] == 2, "the wallet scoring 30 does not count"
    assert sig["who"] == ["ace", "mid"] and sig["scores"] == [88, 74]
    assert sig["conviction"] == pytest.approx(0.88 ** 2 + 0.74 ** 2)


def test_signal_window_and_limits_are_bounded(client):
    assert client.get("/api/signals?hours=0").status_code == 422
    assert client.get("/api/signals?hours=100000").status_code == 422
    assert client.get("/api/signals?limit=9999").status_code == 422
    assert client.get("/api/signals?min_buyers=1").status_code == 422


def test_leaderboard_unpacks_tags(client):
    body = client.get("/api/leaderboard?status=active").json()
    assert [t["handle"] for t in body["traders"]] == ["ace", "mid"]
    assert body["traders"][0]["style"] == ["swing"]
    assert body["traders"][0]["red_flags"] == ["one-hit"]


def test_leaderboard_status_is_validated(client):
    assert client.get("/api/leaderboard?status=nonsense").status_code == 422
    assert client.get("/api/leaderboard?status=all").json()["count"] == 3


def test_trader_by_handle_and_by_address(client):
    for who in ("ace", ACE, ACE.upper()):
        body = client.get(f"/api/trader/{who}").json()
        assert body["score"] == 88 and body["summary"] == "ace verdict"
    assert client.get("/api/trader/nobody").status_code == 404


def test_trader_carries_the_open_book(client):
    body = client.get("/api/trader/ace").json()
    assert [p["sym"] for p in body["positions"]] == ["PONS"]
    assert body["open_pnl"] == 900_000


def test_token_answers_with_the_holders(client):
    body = client.get(f"/api/token/{TOKEN}").json()
    assert body["symbol"] == "PONS" and body["tracked"] is True
    assert body["trusted_holders"] == 1
    assert [h["handle"] for h in body["holders"]] == ["ace"]


def test_a_quote_asset_is_flagged_rather_than_ranked(client):
    body = client.get(f"/api/token/{USDG}").json()
    assert body["is_quote"] is True


def test_an_untracked_token_still_answers(client, monkeypatch):
    """The difference between a list and a tool: an unknown address gets a real reply."""
    monkeypatch.setattr(api, "live_lookup", lambda conn, mint: False)
    body = client.get(f"/api/token/{UNKNOWN}").json()
    assert body["tracked"] is False
    assert body["holders"] == [] and body["conviction"] == 0


def test_nonsense_is_rejected_before_it_reaches_the_database(client):
    assert client.get("/api/token/hello").status_code == 400


def test_search_routes_a_handle_an_address_and_a_fragment(client):
    assert client.get("/api/search?q=ace").json()["kind"] == "trader"
    assert client.get(f"/api/search?q={UNKNOWN}").json()["kind"] == "token"
    body = client.get("/api/search?q=ac").json()
    assert body["kind"] == "suggestions" and body["traders"][0]["handle"] == "ace"
    assert client.get("/api/search?q=zzzzz").json()["kind"] == "none"


def test_tape_only_carries_trusted_wallets(client):
    body = client.get("/api/tape").json()
    assert {f["handle"] for f in body["fills"]} == {"ace", "mid"}
    assert all(f["sym"] != "USDG" for f in body["fills"])


def test_rate_limit_returns_429_rather_than_dying(client, monkeypatch):
    monkeypatch.setattr(api.limiter, "per_minute", 3)
    api.limiter.hits.clear()
    codes = [client.get("/api/health").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]
    assert client.get("/api/health").json()["error"] == "rate limited"


def test_openapi_describes_the_product(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "FOMO Robinhood Radar"
    assert "/api/signals" in spec["paths"] and "/api/token/{mint}" in spec["paths"]
