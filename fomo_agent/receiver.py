"""Local HTTP endpoint the browser extension posts fomo data to.

Bound to loopback only. The payload is the same shape `scripts/fomo_export.js` downloads, so both
the extension and the manual console export land in `import_browser_export`.

    POST /ingest   JSON body -> rows in the database, replies with the import stats
    POST /seed     a fomo session exported from a signed-in browser, held for one read
    GET  /seed     hands that session to the collector extension and deletes it
    GET  /health   liveness + how much fomo data is already stored
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db
from .config import settings
from .pipeline.discover import import_browser_export

log = logging.getLogger(__name__)
MAX_BODY = 32 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "fomo-agent"
    _lock = threading.Lock()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - BaseHTTPRequestHandler API
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        # the extension posts from its own origin, so preflight has to pass
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "content-type, x-agent-token")
        self.send_header("access-control-allow-methods", "POST, GET, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._send(204, {})

    def _authorized(self) -> bool:
        return not settings.receiver_token or self.headers.get("x-agent-token") == settings.receiver_token

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/seed":
            # One read, then gone. A session handed over this way is somebody's login: leaving it
            # on disk after the browser has taken it would be keeping a credential for no reason.
            if not self._authorized():
                return self._send(401, {"error": "bad or missing x-agent-token"})
            seed = settings.seed_path
            if not seed.exists():
                return self._send(404, {"error": "no session waiting"})
            try:
                payload = json.loads(seed.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return self._send(500, {"error": f"unreadable seed: {e}"})
            finally:
                seed.unlink(missing_ok=True)
            log.info("seed handed over (%d local keys) and deleted",
                     len(payload.get("local") or {}))
            return self._send(200, payload)
        if path != "/health":
            return self._send(404, {"error": "not found"})
        conn = db.connect()
        try:
            users, resolved = conn.execute("SELECT COUNT(*), COUNT(resolved_at) FROM fomo_users").fetchone()
            traders = conn.execute("SELECT COUNT(*) FROM traders").fetchone()[0]
        finally:
            conn.close()
        self._send(200, {"ok": True, "fomo_users": users, "resolved": resolved, "traders": traders})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = -1
        # the body must be drained before any reply, otherwise the client sees a reset instead
        # of the status we are trying to tell it about
        body = self.rfile.read(min(length, MAX_BODY)) if length > 0 else b""

        path = self.path.split("?")[0]
        if path not in ("/ingest", "/seed"):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "bad or missing x-agent-token"})
        if path == "/seed":
            try:
                payload = json.loads(body)
                keys = len((payload.get("local") or {}) if isinstance(payload, dict) else {})
            except (ValueError, UnicodeDecodeError) as e:
                return self._send(400, {"error": f"invalid JSON: {e}"})
            seed = settings.seed_path
            seed.write_bytes(body)
            os.chmod(seed, 0o600)
            log.info("seed stored: %d local keys, waiting for the collector", keys)
            return self._send(200, {"ok": True, "keys": keys})
        if length < 0:
            return self._send(400, {"error": "bad content-length"})
        if not body or length > MAX_BODY:
            return self._send(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as e:
            return self._send(400, {"error": f"invalid JSON: {e}"})
        if not isinstance(payload, dict):
            return self._send(400, {"error": "expected a JSON object"})

        # sqlite writes are serialized; collections are rare enough that a lock is simpler than a pool
        with self._lock:
            conn = db.connect()
            run_id = db.run_start(conn, "fomo_ingest")
            wants: list = []
            try:
                stats = import_browser_export(conn, payload)
                db.run_finish(conn, run_id, stats)
                # What to ask about next time. The collector cannot know which tokens matter —
                # that ranking lives here, with the scores — so the answer to a delivery carries
                # the list. It rides the reply the extension already reads, which is why this
                # needs no endpoint, no schedule and no second round trip.
                try:
                    wants = db.mints_wanting_theses(
                        conn, settings.thesis_batch, settings.thesis_window_h,
                        settings.thesis_max_age_s)
                except Exception:  # noqa: BLE001 - a failed suggestion must not fail a delivery
                    log.warning("could not pick tokens to ask about", exc_info=True)
            except Exception as e:  # noqa: BLE001 - a bad payload must not kill the server
                db.run_finish(conn, run_id, error=repr(e)[:500])
                log.exception("ingest failed")
                return self._send(500, {"error": str(e)[:200]})
            finally:
                conn.close()
        log.info("ingest: %s", stats)
        self._send(200, {"ok": True, **stats, "wants": {"mints": wants}})


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or settings.receiver_host
    port = port or settings.receiver_port
    srv = ThreadingHTTPServer((host, port), Handler)
    log.info("receiver listening on http://%s:%d/ingest%s",
             host, port, " (token required)" if settings.receiver_token else "")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("receiver stopped")
    finally:
        srv.server_close()
