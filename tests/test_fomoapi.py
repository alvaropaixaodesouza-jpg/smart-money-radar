"""The HTTP route into fomo. Fixtures are real captures, trimmed to a few rows."""
import json
import pathlib

import pytest

from fomo_agent import db
from fomo_agent.pipeline.collect_api import link_wallets, store_rows, store_theses
from fomo_agent.sources.fomoapi import parse_leaderboard, parse_theses

FIX = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_the_leaderboard_carries_the_verified_wallet():
    """The whole reason this route is cheap: no ten-credit resolution call is ever needed."""
    rows = parse_leaderboard(load("fomoapi_leaderboard_sample.json"), "24h")
    assert len(rows) == 4
    top = rows[0]
    assert top.fomo_handle == "PoorGoat_"
    assert top.evm_address == "0x9ce0cb4a193acbce0dca3283972341aed6f3f614"
    assert top.trades_cnt == 3202
    assert top.source == "fomoapi_24h"


def test_the_window_decides_which_pnl_column_is_filled():
    """`pnlUsd` means whichever window was asked for, so three calls fill three columns without
    one clobbering another."""
    for window, filled in (("24h", "pnl_24h"), ("7d", "pnl_7d"), ("30d", "pnl_30d")):
        r = parse_leaderboard(load("fomoapi_leaderboard_sample.json"), window)[0]
        assert getattr(r, filled) == pytest.approx(333127.0)
        assert [getattr(r, k) for k in ("pnl_24h", "pnl_7d", "pnl_30d") if k != filled] == [None, None]


def test_a_row_without_a_wallet_still_lands():
    """Not every trader has an EVM wallet on file, and a missing one is not a reason to drop them
    from the board."""
    rows = parse_leaderboard(load("fomoapi_leaderboard_sample.json"), "24h")
    walletless = [r for r in rows if not r.evm_address]
    assert walletless, "the capture is expected to contain one"
    assert all(r.fomo_handle for r in walletless)


def test_theses_parse_from_the_feed():
    rows = parse_theses(load("fomoapi_thesis_sample.json"))
    assert rows and all(r["text"] and r["mint"] and r["handle"] for r in rows)
    assert rows[0]["mint"].startswith("0x")


def test_a_note_with_no_text_or_no_token_is_not_a_thesis():
    payload = {"theses": [
        {"handle": "a", "text": "   ", "token": {"address": "0x1"}},
        {"handle": "b", "text": "real", "token": {}},
        {"handle": None, "text": "real", "token": {"address": "0x2"}},
        {"handle": "d", "text": "kept", "token": {"address": "0xAB", "symbol": "K"}},
    ]}
    rows = parse_theses(payload)
    assert [r["handle"] for r in rows] == ["d"]
    assert rows[0]["mint"] == "0xab", "addresses are normalised"


# ---------------------------------------------------------------- storage


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "api.db")
    with db.tx(c):
        db.upsert_fomo_user(c, "real-uuid", handle="PoorGoat_")
    return c


def test_a_handle_we_know_is_updated_not_duplicated(conn):
    """Inventing a user id per row would give a trader we already know a second, empty record."""
    rows = parse_leaderboard(load("fomoapi_leaderboard_sample.json"), "24h")
    store_rows(conn, rows, "fomoapi_24h")
    ids = [r["user_id"] for r in conn.execute(
        "SELECT user_id FROM fomo_users WHERE handle='PoorGoat_'")]
    assert ids == ["real-uuid"], "the existing record was updated in place"
    # and the ones we had never heard of arrived under an id that says where they came from
    assert conn.execute(
        "SELECT COUNT(*) FROM fomo_users WHERE user_id LIKE 'api:%'").fetchone()[0] == 3


def test_a_verified_wallet_fills_a_gap_but_never_overwrites_a_conflict(conn):
    rows = parse_leaderboard(load("fomoapi_leaderboard_sample.json"), "24h")
    store_rows(conn, rows, "fomoapi_24h")

    first = link_wallets(conn, rows)
    assert first["new"] >= 1 and first["conflicts"] == 0
    # run again: now we agree with ourselves rather than counting it new twice
    assert link_wallets(conn, rows)["agrees"] >= 1

    # now pretend our own resolver had reached a different answer
    with db.tx(conn):
        conn.execute("UPDATE fomo_users SET onchain_address=? WHERE handle='PoorGoat_'",
                     ("0x" + "9" * 40,))
    out = link_wallets(conn, rows)
    assert out["conflicts"] == 1
    kept, note = conn.execute(
        "SELECT onchain_address, onchain_note FROM fomo_users WHERE handle='PoorGoat_'").fetchone()
    assert kept == "0x" + "9" * 40, "ours is not silently replaced"
    assert "conflict" in note


def test_a_thesis_by_somebody_we_do_not_track_is_not_stored(conn):
    """It could never be shown: the page only prints notes beside a score."""
    rows = [
        {"handle": "PoorGoat_", "mint": "0xaa", "symbol": "K", "text": "known author",
         "pnl_usd": 1.0, "cost_usd": 2.0, "chain": "robinhood"},
        {"handle": "nobody", "mint": "0xbb", "symbol": "N", "text": "stranger",
         "pnl_usd": None, "cost_usd": None, "chain": "robinhood"},
    ]
    out = store_theses(conn, rows)
    assert out["stored"] == 1 and out["unknown_author"] == 1
    assert conn.execute("SELECT COUNT(*) FROM theses").fetchone()[0] == 1


def test_an_edited_thesis_replaces_itself(conn):
    base = {"handle": "PoorGoat_", "mint": "0xaa", "symbol": "K", "chain": "robinhood",
            "pnl_usd": None, "cost_usd": None}
    assert store_theses(conn, [{**base, "text": "first"}])["new"] == 1
    assert store_theses(conn, [{**base, "text": "edited"}])["new"] == 0
    assert conn.execute("SELECT text FROM theses").fetchone()[0] == "edited"
    assert conn.execute("SELECT COUNT(*) FROM theses").fetchone()[0] == 1
