"""Storage invariants that no caller is allowed to break."""
import pytest

from fomo_agent import db

WALLET = "0x" + "a" * 40
MINT = "0x" + "d" * 40
TX = "0x" + "1" * 64


@pytest.fixture()
def conn(tmp_path):
    return db.connect(tmp_path / "db.db")


def test_the_same_fill_from_two_sources_lands_once(conn):
    """Signatures carry a source-specific suffix, so the primary key alone is not enough."""
    common = dict(address=WALLET, chain="robinhood", mint=MINT, side="buy", ts=db.now())
    with db.tx(conn):
        assert db.insert_trade(conn, sig=f"{TX}:57526", usd_value=10.31, source="trenches", **common)
        # the chain reports the same trade, numbered by log index instead of tape sequence
        assert not db.insert_trade(conn, sig=f"{TX}:83", usd_value=10.30, source="rpc", **common)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_different_fills_in_one_transaction_both_land(conn):
    common = dict(address=WALLET, chain="robinhood", ts=db.now(), source="rpc")
    with db.tx(conn):
        assert db.insert_trade(conn, sig=f"{TX}:1", mint=MINT, side="buy", **common)
        assert db.insert_trade(conn, sig=f"{TX}:2", mint=MINT, side="sell", **common)
        assert db.insert_trade(conn, sig=f"{TX}:3", mint="0x" + "e" * 40, side="buy", **common)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3


def test_fill_key_ignores_the_signature_suffix():
    base = dict(sig=f"{TX}:57526", address=WALLET, mint=MINT, side="buy")
    assert db.fill_key(base) == db.fill_key({**base, "sig": f"{TX}:83"})
    assert db.fill_key(base) != db.fill_key({**base, "side": "sell"})
    assert db.fill_key({}) == ":::"


def test_a_position_never_stores_a_negative_cost(conn):
    """fomo's pnl can exceed a holding's value because it counts profit already taken out."""
    with db.tx(conn):
        db.upsert_fomo_position(conn, trade_id="p1", user_id="u1", token=MINT,
                                unrealized_pnl=900_000.0, cost_basis=-40_000.0, avg_entry=-2.0)
    row = conn.execute("SELECT * FROM fomo_positions WHERE trade_id='p1'").fetchone()
    assert row["cost_basis"] is None and row["avg_entry"] is None
    assert row["unrealized_pnl"] == 900_000.0, "the profit itself is still real"


def test_a_position_keeps_a_real_cost(conn):
    with db.tx(conn):
        db.upsert_fomo_position(conn, trade_id="p2", user_id="u1", token=MINT,
                                unrealized_pnl=100.0, cost_basis=25.0)
    assert conn.execute("SELECT cost_basis FROM fomo_positions WHERE trade_id='p2'").fetchone()[0] == 25.0
