"""Local HTTP endpoint the browser extension posts fomo data to.

Bound to loopback only. The payload is the same shape `scripts/fomo_export.js` downloads, so both
the extension and the manual console export land in `import_browser_export`.

    POST /ingest   JSON body -> rows in the database, replies with the import stats
    GET  /health   liveness + how much fomo data is already stored
"""
from __future__ import annotations

import json
import logging
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

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/health":
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

        if self.path.split("?")[0] != "/ingest":
            return self._send(404, {"error": "not found"})
        if settings.receiver_token and self.headers.get("x-agent-token") != settings.receiver_token:
            return self._send(401, {"error": "bad or missing x-agent-token"})
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
            try:
                stats = import_browser_export(conn, payload)
                db.run_finish(conn, run_id, stats)
            except Exception as e:  # noqa: BLE001 - a bad payload must not kill the server
                db.run_finish(conn, run_id, error=repr(e)[:500])
                log.exception("ingest failed")
                return self._send(500, {"error": str(e)[:200]})
            finally:
                conn.close()
        log.info("ingest: %s", stats)
        self._send(200, {"ok": True, **stats})


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
