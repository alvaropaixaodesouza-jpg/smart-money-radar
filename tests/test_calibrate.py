"""The trap this module exists to avoid is grading a verdict on a trade it already knew about."""
import pytest

from fomo_agent import db
from fomo_agent.pipeline.calibrate import calibrate, first_verdicts, report

GOOD = "0x" + "a" * 40
BAD = "0x" + "b" * 40
TOKEN = "0x" + "1" * 40
OTHER = "0x" + "2" * 40


def seed(tmp_path):
    c = db.connect(tmp_path / "cal.db")
    now = db.now()
    with db.tx(c):
        db.upsert_token(c, TOKEN, chain="robinhood", symbol="PONS")
        db.upsert_token(c, OTHER, chain="robinhood", symbol="MEME")
        db.upsert_trader(c, GOOD, chain="robinhood", fomo_handle="good", score=85, status="active")
        db.upsert_trader(c, BAD, chain="robinhood", fomo_handle="bad", score=20, status="dropped")
        db.add_score_history(c, GOOD, 85, "active", "test", "good")
        db.add_score_history(c, BAD, 20, "dropped", "test", "bad")
        # the verdicts land now; everything before `now` is history the score already saw
        c.execute("UPDATE score_history SET ts=?", (now - 3600,))
    return c, now


def fill(c, addr, mint, side, usd, amt, ts, tag):
    db.insert_trade(c, sig=f"0x{tag}", address=addr, chain="robinhood", mint=mint, side=side,
                    usd_value=usd, token_amount=amt, ts=ts, source="rpc")


def test_a_trade_from_before_the_verdict_is_not_evidence(tmp_path):
    """The whole point: a score cannot be credited with a trade it was derived from."""
    c, now = seed(tmp_path)
    with db.tx(c):
        # a spectacular round trip, entirely before the wallet was scored
        fill(c, GOOD, TOKEN, "buy", 1_000.0, 1000.0, now - 7200, "b1")
        fill(c, GOOD, TOKEN, "sell", 50_000.0, 1000.0, now - 5400, "s1")
    r = calibrate(c)
    assert r["positions"] == 0, "nothing here was opened after the verdict"
    assert r["separates"] is None


def test_only_what_came_after_counts(tmp_path):
    c, now = seed(tmp_path)
    with db.tx(c):
        fill(c, GOOD, TOKEN, "buy", 1_000.0, 1000.0, now - 7200, "b1")   # before: ignored
        fill(c, GOOD, TOKEN, "sell", 50_000.0, 1000.0, now - 5400, "s1")
        fill(c, GOOD, OTHER, "buy", 1_000.0, 1000.0, now - 1800, "b2")   # after: counted
        fill(c, GOOD, OTHER, "sell", 3_000.0, 1000.0, now - 600, "s2")
    r = calibrate(c)
    top = r["bands"][0]
    assert top["band"] == "active" and top["closed"] == 1
    assert top["pooled_return"] == pytest.approx(3.0), "3k out on 1k in, and nothing from before"
    assert top["win_rate"] == 1.0


def test_dust_does_not_get_a_vote(tmp_path):
    """Twenty dollars in and thirty out is a 150% return that demonstrates nothing."""
    c, now = seed(tmp_path)
    with db.tx(c):
        fill(c, GOOD, OTHER, "buy", 20.0, 1000.0, now - 1800, "b2")
        fill(c, GOOD, OTHER, "sell", 30.0, 1000.0, now - 600, "s2")
    assert calibrate(c)["positions"] == 0
    assert calibrate(c, min_usd=1.0)["positions"] == 1


def test_an_open_position_counts_only_in_the_marked_reading(tmp_path):
    """It is a price quote, not a result — so it moves `marked` and must not touch `realized`."""
    c, now = seed(tmp_path)
    with db.tx(c):
        c.execute("UPDATE tokens SET price_usd=8.0, price_at=? WHERE mint=?", (now, OTHER))
        fill(c, GOOD, OTHER, "buy", 5_000.0, 1000.0, now - 1800, "b2")
    r = calibrate(c)
    top = r["bands"][0]
    assert top["closed"] == 0 and r["positions"] == 0, "nothing has come back yet"
    assert top["pooled_return"] is None
    assert top["all"] == 1 and top["marked_return"] == pytest.approx(1.6), "1000 held at $8 on 5k in"


def test_a_position_with_no_price_joins_neither_total(tmp_path):
    c, now = seed(tmp_path)
    with db.tx(c):
        fill(c, GOOD, OTHER, "buy", 5_000.0, 1000.0, now - 1800, "b2")
    top = calibrate(c)["bands"][0]
    assert top["all"] == 0 and top["unpriced"] == 1
    assert top["marked_return"] is None


def test_the_bands_are_compared_not_just_reported(tmp_path):
    c, now = seed(tmp_path)
    with db.tx(c):
        c.execute("UPDATE tokens SET price_usd=2.0, price_at=? WHERE mint=?", (now, OTHER))
        c.execute("UPDATE tokens SET price_usd=0.5, price_at=? WHERE mint=?", (now, TOKEN))
        fill(c, GOOD, OTHER, "buy", 1_000.0, 1000.0, now - 1800, "b2")
        fill(c, GOOD, OTHER, "sell", 4_000.0, 1000.0, now - 600, "s2")
        fill(c, BAD, TOKEN, "buy", 1_000.0, 1000.0, now - 1800, "b3")
        fill(c, BAD, TOKEN, "sell", 200.0, 1000.0, now - 600, "s3")
    r = calibrate(c)
    assert r["separates"] is True
    assert r["separates_excess"] is True
    assert r["bands"][0]["pooled_return"] > r["bands"][-1]["pooled_return"]
    assert r["bands"][0]["excess_return"] > r["bands"][-1]["excess_return"]
    assert r["bands"][-1]["win_rate"] == 0.0


def test_the_wallet_is_compared_with_holding_the_same_token(tmp_path):
    """A 4x exit beat a token that is only 2x since the wallet's first entry."""
    c, now = seed(tmp_path)
    with db.tx(c):
        c.execute("UPDATE tokens SET price_usd=2.0, price_at=? WHERE mint=?", (now, OTHER))
        fill(c, GOOD, OTHER, "buy", 1_000.0, 1000.0, now - 1800, "b2")
        fill(c, GOOD, OTHER, "sell", 4_000.0, 1000.0, now - 600, "s2")
    r = calibrate(c)
    top = r["bands"][0]
    assert top["benchmark_positions"] == 1
    assert top["paired_return"] == pytest.approx(4.0)
    assert top["benchmark_return"] == pytest.approx(2.0)
    assert top["benchmark_median_return"] == pytest.approx(2.0)
    assert top["excess_return"] == pytest.approx(2.0)
    text = report(r)
    assert "Matched token benchmark" in text and "4.00x" in text and "2.00x" in text


def test_the_benchmark_starts_at_the_first_entry_not_the_average_buy(tmp_path):
    c, now = seed(tmp_path)
    with db.tx(c):
        c.execute("UPDATE tokens SET price_usd=4.0, price_at=? WHERE mint=?", (now, OTHER))
        fill(c, GOOD, OTHER, "buy", 1_000.0, 1000.0, now - 1800, "b2")  # $1 first entry
        fill(c, GOOD, OTHER, "buy", 2_000.0, 1000.0, now - 1200, "b3")  # $2 later buy
    top = calibrate(c)["bands"][0]
    assert top["benchmark_return"] == pytest.approx(4.0)
    assert top["paired_return"] == pytest.approx(8_000 / 3_000)


def test_an_unpriced_token_is_named_as_missing_from_the_benchmark(tmp_path):
    c, now = seed(tmp_path)
    with db.tx(c):
        fill(c, GOOD, OTHER, "buy", 1_000.0, 1000.0, now - 1800, "b2")
        fill(c, GOOD, OTHER, "sell", 4_000.0, 1000.0, now - 600, "s2")
    top = calibrate(c)["bands"][0]
    assert top["benchmark_positions"] == 0 and top["benchmark_unpriced"] == 1
    assert top["benchmark_return"] is None and top["excess_return"] is None


def test_a_quote_older_than_the_entry_is_not_called_current(tmp_path):
    c, now = seed(tmp_path)
    with db.tx(c):
        c.execute("UPDATE tokens SET price_usd=2.0, price_at=? WHERE mint=?", (now - 7200, OTHER))
        fill(c, GOOD, OTHER, "buy", 1_000.0, 1000.0, now - 1800, "b2")
        fill(c, GOOD, OTHER, "sell", 4_000.0, 1000.0, now - 600, "s2")
    top = calibrate(c)["bands"][0]
    assert top["benchmark_positions"] == 0 and top["benchmark_unpriced"] == 1


def test_the_earliest_verdict_is_the_one_on_trial(tmp_path):
    """A rescore is exactly when the score gets to read the answer, so later ones are ignored."""
    c, now = seed(tmp_path)
    with db.tx(c):
        db.add_score_history(c, GOOD, 40, "watch", "test", "downgraded later")
    v = first_verdicts(c)
    assert v[GOOD]["score"] == 85 and v[GOOD]["ts"] == now - 3600
