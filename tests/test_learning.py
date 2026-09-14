"""Research accounting, causal snapshots, data loss and restart recovery."""
import json
import sqlite3
from copy import deepcopy
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from fomo_agent import db
from fomo_agent.cli import app
from fomo_agent.learning import runner
from fomo_agent.learning.analytics import report
from fomo_agent.learning.math import dec, path_metrics, performance, position_size
from fomo_agent.learning.outcomes import finish_due, measure
from fomo_agent.learning.paper import barrier_event, simulate
from fomo_agent.learning.store import (CONFIG, HORIZONS, VERSION, capture, ensure_version,
                                      latest, observe, opportunity_score, record_signal)
from fomo_agent.models import NewToken

START = 1_800_000_000


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "radar.db")
    yield c
    c.close()


def quote(c, at, price=100, **kwargs):
    return observe(c, chain="robinhood", asset="token", price=price, observed_at=at,
                   received_at=kwargs.pop("received_at", at), source="test", **kwargs)


def signal(c, with_entry=True, score=95):
    if with_entry:
        quote(c, START, metrics={"liquidity": 100000})
    sid = record_signal(c, event_key="signal1", chain="robinhood", asset="token", now=START,
                        score=score, features={"liquidity": 100000, "buyers": 5}, components={"test": score},
                        observation=latest(c, "robinhood", "token", START))
    return c.execute("SELECT * FROM learning_signals WHERE id=?", (sid,)).fetchone()


def config(**kwargs):
    cfg = deepcopy(CONFIG["paper"])
    cfg.update(fee_bps="0", slip_bps="0")
    cfg.update(kwargs)
    return cfg


def test_migration_and_immutable_records(conn):
    s = signal(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 20
    assert conn.execute("SELECT COUNT(*) FROM learning_outcomes").fetchone()[0] == 9
    assert set(r[0] for r in conn.execute("SELECT horizon_s FROM learning_outcomes")) == set(HORIZONS)
    for table in ("learning_signals", "learning_observations", "learning_versions"):
        for command in (f"DELETE FROM {table}", f"UPDATE {table} SET rowid=rowid"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(command)
    assert s["entry_price"] == "100"


def test_version_19_upgrade_preserves_wallet(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    for version, sql in db.MIGRATIONS.items():
        if version >= 19:
            break
        c.executescript(sql)
    c.execute("ALTER TABLE traders ADD COLUMN ai_summary_pt TEXT")
    c.execute("INSERT INTO traders(address,score,ai_summary_pt) VALUES('wallet',85,'resumo')")
    c.execute("PRAGMA user_version=19")
    c.commit()
    c.close()
    migrated = db.connect(path)
    assert tuple(migrated.execute("SELECT address,score,ai_summary_pt FROM traders").fetchone()) == ("wallet", 85, "resumo")
    migrated.close()


def test_duplicate_events_are_idempotent(conn):
    s = signal(conn)
    assert signal(conn)["id"] == s["id"]
    assert conn.execute("SELECT COUNT(*) FROM learning_signals").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM learning_observations").fetchone()[0] == 1
    with pytest.raises(ValueError, match="conflicting"):
        quote(conn, START, 101)


def test_configuration_cannot_drift(conn, monkeypatch):
    ensure_version(conn, START)
    monkeypatch.setitem(CONFIG, "cooldown_s", 42)
    with pytest.raises(ValueError, match="version"):
        ensure_version(conn, START)


@pytest.mark.parametrize("price", [0, -1, "NaN", "Infinity", None])
def test_invalid_price_rejected(conn, price):
    with pytest.raises(ValueError):
        quote(conn, START, price)


def test_future_and_cross_market_entry_rejected(conn):
    quote(conn, START + 1)
    obs = conn.execute("SELECT * FROM learning_observations").fetchone()
    with pytest.raises(ValueError, match="not known"):
        record_signal(conn, event_key="future", chain="robinhood", asset="token", now=START,
                      score=90, features={}, components={}, observation=obs)
    with pytest.raises(ValueError, match="another instrument"):
        record_signal(conn, event_key="wrong", chain="solana", asset="token", now=START + 2,
                      score=90, features={}, components={}, observation=obs)


def test_stale_entry_is_not_backfilled(conn):
    quote(conn, START - 121)
    s = signal(conn, with_entry=False)
    quote(conn, START + 60)
    assert measure(conn, s, 300, START + 420)["state"] == "no_entry"


def test_explicit_score_formula():
    total, parts = opportunity_score(5, 80, 50000)
    assert parts == {"buyers": 30, "wallet_quality": 32, "liquidity": 15}
    assert total == 77
    assert opportunity_score(5, 80, None)[0] is None
    assert opportunity_score(50, 200, 9999999)[0] == 100


def test_outcome_return_extrema_drawdown_and_no_future(conn):
    s = signal(conn)
    for offset, price in [(60, 110), (120, 120), (180, 90), (240, 100), (300, 105), (400, 900)]:
        quote(conn, START + offset, price)
    assert measure(conn, s, 300, START + 419)["state"] == "pending"
    r = measure(conn, s, 300, START + 420)
    assert dec(r["return_pct"]) == 5
    assert dec(r["mfe_pct"]) == 20 and dec(r["mae_pct"]) == -10
    assert dec(r["max_drawdown_pct"]) == 25
    assert r["time_to_max_s"] == 120 and r["time_to_min_s"] == 180
    assert r["coverage_pct"] == 100 and r["path_complete"]


def test_late_historical_backfill_cannot_repair_deadline(conn):
    s = signal(conn)
    quote(conn, START + 300, 150, received_at=START + 1000)
    assert measure(conn, s, 300, START + 1200)["state"] == "missing"
    finish_due(conn, START + 1200)
    assert finish_due(conn, START + 1200) == 0


def test_sparse_outcomes_are_explicit(conn):
    s = signal(conn)
    quote(conn, START + 390, 110)
    r = measure(conn, s, 300, START + 420)
    assert r["state"] == "measured" and r["horizon_offset_s"] == 90
    assert not r["path_complete"] and r["coverage_pct"] == 0


def test_risk_formula_and_costs():
    r = position_size(5000, 1, 10, 9.5, 0, 0)
    assert dec(r["quantity"]) == 100 and dec(r["cash_required"]) == 1000
    assert dec(r["planned_loss"]) == 50
    costs = position_size(5000, 1, 10, 9.5, 10, 20)
    assert dec(costs["quantity"]) < 100
    assert float(costs["planned_loss"]) == pytest.approx(50)
    limited = position_size(5000, 1, 10, 9.5, 0, 0, 2000)
    assert dec(limited["cash_required"]) == 20
    assert not limited["loss_is_guaranteed"]


@pytest.mark.parametrize("capital,risk,entry,stop", [(0, 1, 10, 9), (100, 101, 10, 9),
    (100, 0, 10, 9), (100, 1, 10, 10), (100, 1, 0, 1), (100, 1, 10, -1)])
def test_invalid_risk(capital, risk, entry, stop):
    with pytest.raises(ValueError):
        position_size(capital, risk, entry, stop)


def test_partial_targets_have_weighted_return_not_sum(conn):
    s = signal(conn)
    for offset, price in [(60, 100), (120, 110), (180, 120)]:
        quote(conn, START + offset, price)
    r = simulate(conn, s, START + 180, config())
    assert r["state"] == "closed"
    assert dec(r["return_pct"]) == 15 and dec(r["pnl"]) == 30
    assert dec(r["realized_r"]) == 3
    assert len(r["fills"]) == 3
    quote(conn, START + 240, 1)
    assert simulate(conn, s, START + 240, config()) == r


def test_gap_through_stop_can_exceed_planned_loss(conn):
    s = signal(conn)
    quote(conn, START + 60, 100)
    quote(conn, START + 120, 90)
    r = simulate(conn, s, START + 120, config())
    assert r["state"] == "closed" and dec(r["return_pct"]) == -10
    assert -dec(r["pnl"]) > dec(r["planned_loss"])


def test_costs_reduce_returns_and_entry_uses_later_quote(conn):
    s = signal(conn)
    quote(conn, START + 60, 110)
    quote(conn, START + 120, 150)
    r = simulate(conn, s, START + 120)
    assert dec(r["entry_price"]) == Decimal("110.22")
    assert r["state"] == "closed" and dec(r["return_pct"]) < 15


def test_ambiguous_bar_and_observation_gap(conn):
    assert barrier_event(90, 120, 95, 110) == "ambiguous"
    s = signal(conn)
    quote(conn, START + 60)
    quote(conn, START + 120, 105, metrics={"low": 90, "high": 120})
    assert simulate(conn, s, START + 120, config())["state"] == "ambiguous"


def test_unobserved_interval_cannot_count_as_a_win(conn):
    s = signal(conn)
    quote(conn, START + 60)
    quote(conn, START + 600, 150)
    r = simulate(conn, s, START + 600, config())
    assert r["state"] == "insufficient_data" and "pnl" not in r


def test_timeout_and_expiry(conn):
    s = signal(conn)
    assert simulate(conn, s, START + 121, config())["state"] == "insufficient_data"
    quote(conn, START + 60)
    quote(conn, START + 120, 105)
    r = simulate(conn, s, START + 120, config(max_hold_s=60))
    assert r["state"] == "closed" and dec(r["return_pct"]) == 5
    assert r["fills"][-1]["reason"] == "time_exit"


def test_performance_cash_example_and_empty_sample():
    rows = [{"state": "closed", "closed_at": i, "pnl": 120 if i < 40 else -50,
             "return_pct": 12 if i < 40 else -5, "realized_r": 2.4 if i < 40 else -1} for i in range(100)]
    r = performance(rows)
    assert dec(r["net_pnl"]) == 1800 and dec(r["expectancy_cash"]) == 18
    assert dec(r["profit_factor"]) == Decimal("1.6") and dec(r["win_rate_pct"]) == 40
    assert r["max_win_streak"] == 40 and r["max_loss_streak"] == 60
    assert r["portfolio_drawdown"] is None
    assert performance([])["win_rate_pct"] is None
    assert performance(rows[:40])["profit_factor"] is None


def candidate():
    return {"mint": "token", "buyers": 5, "avg_score": 90, "conviction": 4.05, "usd": 10000}


def test_worker_integration_and_restart(conn, monkeypatch):
    monkeypatch.setattr(runner, "signals", lambda *a, **kw: [candidate()])
    calls = []
    def lookup(chain, assets):
        calls.append(assets)
        return [NewToken(mint="token", chain=chain, price_usd=100, liquidity_usd=100000)], 1
    first = runner.tick(conn, clock=lambda: START, lookup=lookup)
    assert first["signals_new"] == 1 and first["quotes"] == 1
    assert runner.tick(conn, clock=lambda: START + 1, lookup=lookup)["requested"] == 0
    for t in range(60, 421, 60):
        runner.tick(conn, clock=lambda t=t: START + t, lookup=lookup)
    r = report(conn)
    assert r["signals"] == 1 and r["horizons"][300]["states"]["measured"] == 1
    assert not r["promotion_allowed"]
    assert conn.execute("SELECT state FROM learning_paper").fetchone()[0] == "open"
    assert len(calls) == 8


def test_polling_with_network_latency_does_not_skip_alternate_cycles(conn, monkeypatch):
    """The worker sleeps from cycle start; quote timestamps must stay at response time."""
    monkeypatch.setattr(runner, "signals", lambda *a, **kw: [candidate()])
    current = [START]
    delays = iter([2, 1, 3, 2, 1, 2, 1, 2])

    def lookup(chain, assets):
        current[0] += next(delays)
        return [NewToken(mint="token", chain=chain, price_usd=100, liquidity_usd=100000)], 1

    for offset in range(0, 421, 60):
        current[0] = START + offset
        stats = runner.tick(conn, clock=lambda: current[0], lookup=lookup)
        assert stats["requested"] == 1, "network latency must not suppress the next minute's poll"
        assert stats["quotes"] == 1
        row = conn.execute("SELECT observed_at,received_at FROM learning_observations ORDER BY id DESC LIMIT 1").fetchone()
        assert tuple(row) == (current[0], current[0]), "do not backdate observations to request start"
    assert conn.execute("SELECT state FROM learning_paper").fetchone()[0] == "open"
    assert conn.execute("SELECT state FROM learning_outcomes WHERE horizon_s=300").fetchone()[0] == "measured"


def test_missing_price_can_create_fresh_later_detection(conn):
    assert capture(conn, [candidate()], "robinhood", START) == 1
    assert capture(conn, [candidate()], "robinhood", START + 30) == 0
    quote(conn, START + 60, metrics={"liquidity": 100000})
    assert capture(conn, [candidate()], "robinhood", START + 60) == 1
    assert capture(conn, [candidate()], "robinhood", START + 61) == 0
    entries = [r[0] for r in conn.execute("SELECT entry_price FROM learning_signals ORDER BY id")]
    assert entries == [None, "100"]


def test_canonical_signal_to_snapshot_excludes_future_fills(conn, monkeypatch):
    monkeypatch.setattr(db, "now", lambda: START)
    with db.tx(conn):
        db.upsert_token(conn, "token", chain="robinhood", liquidity_usd=100000)
        for i in range(3):
            addr = f"wallet{i}"
            db.upsert_trader(conn, addr, chain="robinhood", score=90, status="active")
            db.insert_trade(conn, sig=f"tx{i}", address=addr, chain="robinhood", mint="token",
                            side="buy", usd_value=1000, token_amount=10,
                            ts=START - 10 if i < 2 else START + 100, source="rpc", kind="trade")
    stats = runner.tick(conn, clock=lambda: START,
                        lookup=lambda chain, assets: ([NewToken(mint="token", chain=chain,
                            price_usd=100, liquidity_usd=100000)], 1))
    assert stats["signals_new"] == 1
    frozen = conn.execute("SELECT * FROM learning_signals").fetchone()
    assert json.loads(frozen["features_json"])["buyers"] == 2
    assert frozen["entry_price"] == "100.0"


def test_worker_offline_and_failures(conn, monkeypatch):
    monkeypatch.setattr(runner, "signals", lambda *a, **kw: [candidate()])
    def broken(*a):
        raise RuntimeError("test")
    with pytest.raises(RuntimeError):
        runner.tick(conn, clock=lambda: START, lookup=broken)
    assert conn.execute("SELECT error FROM learning_runs").fetchone()[0] == "RuntimeError"
    assert runner.tick(conn, clock=lambda: START + 1, lookup=broken, offline=True)["requests"] == 0


def test_backup_includes_wal_without_migrating_and_lock(tmp_path):
    path = tmp_path / "live.db"
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE evidence(value)")
    c.execute("INSERT INTO evidence VALUES(42)")
    c.commit()
    dst = runner.backup(path, tmp_path / "backup.db")
    with sqlite3.connect(dst) as b:
        assert b.execute("SELECT value FROM evidence").fetchone()[0] == 42
        assert b.execute("PRAGMA user_version").fetchone()[0] == 0
    with pytest.raises(FileExistsError):
        runner.backup(path, dst)
    with runner.worker_lock(path):
        with pytest.raises(RuntimeError, match="ativo"):
            with runner.worker_lock(path):
                pass
    with runner.worker_lock(path):
        pass
    c.close()


def test_cli_report_risk_and_partial_failure(tmp_path, monkeypatch):
    from fomo_agent.learning import cli
    monkeypatch.setattr(cli.settings, "db_path", tmp_path / "cli.db")
    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["learning", "report", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["signals"] == 0
    risk = cli_runner.invoke(app, ["learning", "risk", "5000", "10", "9.5", "--fee-bps", "0", "--slip-bps", "0"])
    assert risk.exit_code == 0, risk.output
    assert dec(json.loads(risk.stdout)["quantity"]) == 100
    monkeypatch.setattr(cli, "tick", lambda *a, **kw: {"unavailable": 1})
    result = cli_runner.invoke(app, ["learning", "tick"])
    assert result.exit_code == 2, result.output
