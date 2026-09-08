"""The local endpoint the browser extension posts fomo collections to.

Runs a real server on an ephemeral loopback port; no network beyond localhost.
"""
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from fomo_agent import db, receiver
from fomo_agent.config import settings

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "rx.db")
    monkeypatch.setattr(settings, "receiver_token", "")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), receiver.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def payload():
    return {
        "exportedAt": 1788614000,
        "leaderboards": {"30d": load("fomo_leaderboard_sample.json")},
        "holders": {"0x385f4f8ae47651ce5f58f5265395a669f8281e18": load("fomo_holders_sample.json")},
        "swaps": {"aefe2ddd-c580-5245-a2f5-e4ed62f7ef10": load("fomo_swaps_sample.json")},
    }


def test_ingest_stores_rows(server, payload):
    assert httpx.get(f"{server}/health", timeout=10).json()["fomo_users"] == 0

    r = httpx.post(f"{server}/ingest", json=payload, timeout=30)
    assert r.status_code == 200
    stats = r.json()
    assert stats["ok"] and stats["new_users"] == 6 and stats["swaps"] > 0

    health = httpx.get(f"{server}/health", timeout=10).json()
    assert health == {"ok": True, "fomo_users": 6, "resolved": 1, "traders": 0}

    conn = db.connect()
    try:
        chains = {r["chain"] for r in conn.execute("SELECT chain FROM fomo_swaps")}
        kinds = [r[0] for r in conn.execute("SELECT kind FROM runs")]
    finally:
        conn.close()
    assert chains == {"solana", "robinhood"}
    assert kinds == ["fomo_ingest"]          # every ingest is recorded like any other step


def test_cors_preflight_allows_the_extension(server):
    r = httpx.request("OPTIONS", f"{server}/ingest", timeout=10)
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "x-agent-token" in r.headers["access-control-allow-headers"]


def test_rejects_bad_input(server):
    assert httpx.get(f"{server}/nope", timeout=10).status_code == 404
    assert httpx.post(f"{server}/nope", json={}, timeout=10).status_code == 404
    bad = httpx.post(f"{server}/ingest", content=b"{oops",
                     headers={"content-type": "application/json"}, timeout=10)
    assert bad.status_code == 400
    assert httpx.post(f"{server}/ingest", json=[1, 2], timeout=10).status_code == 400
    assert httpx.post(f"{server}/ingest", content=b"", timeout=10).status_code == 413


def test_token_is_enforced_when_set(server, payload, monkeypatch):
    monkeypatch.setattr(settings, "receiver_token", "s3cret")
    assert httpx.post(f"{server}/ingest", json=payload, timeout=30).status_code == 401
    ok = httpx.post(f"{server}/ingest", json=payload,
                    headers={"x-agent-token": "s3cret"}, timeout=30)
    assert ok.status_code == 200


def test_a_seed_is_held_for_exactly_one_read(server, tmp_path, monkeypatch):
    """A handed-over session is a transfer, not a stored credential: it survives one GET."""
    monkeypatch.setattr(settings, "seed_path", tmp_path / "fomo-session.json")
    monkeypatch.setattr(settings, "receiver_token", "s3cret")
    payload = {"origin": "https://fomo.family", "local": {"privy:token": "x", "privy:refresh": "y"}}
    auth = {"x-agent-token": "s3cret"}

    assert httpx.post(f"{server}/seed", json=payload, timeout=10).status_code == 401
    r = httpx.post(f"{server}/seed", json=payload, headers=auth, timeout=10)
    assert r.status_code == 200 and r.json()["keys"] == 2
    assert settings.seed_path.exists()

    got = httpx.get(f"{server}/seed", headers=auth, timeout=10)
    assert got.status_code == 200 and got.json()["local"] == payload["local"]
    assert not settings.seed_path.exists(), "read once, then gone"

    assert httpx.get(f"{server}/seed", headers=auth, timeout=10).status_code == 404
    assert httpx.get(f"{server}/seed", timeout=10).status_code == 401
