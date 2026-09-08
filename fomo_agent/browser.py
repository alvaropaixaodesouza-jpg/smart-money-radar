"""Talk to the collector browser over the DevTools protocol.

The browser on the server has no keyboard in front of it, and driving it by synthesising keystrokes
turned out to be exactly as reliable as it sounds: a devtools window that may or may not have
opened, a paste that may or may not have landed, and a screenshot to guess from afterwards. This
asks the browser directly instead, and gets an answer rather than a picture of one.

It needs Chrome started with --remote-debugging-port, which binds loopback only, and `websockets`,
which arrives with the API extra rather than the base install - so this module is imported lazily
and only ever used on the machine that runs the browser.

Nothing here signs anybody in. It moves a session that already exists, and reads back whether the
app accepted it.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


class BrowserError(RuntimeError):
    pass


def targets(port: int | None = None) -> list[dict]:
    """Every page the browser has open, as the DevTools protocol sees them."""
    port = port or settings.browser_debug_port
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=10)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise BrowserError(
            f"no browser answering on 127.0.0.1:{port} ({e}). It needs --remote-debugging-port; "
            "see deploy/systemd/radar-browser.service"
        ) from e
    return [t for t in r.json() if isinstance(t, dict)]


def find_target(match: str, port: int | None = None) -> dict:
    pages = [t for t in targets(port) if t.get("type") == "page" and match in (t.get("url") or "")]
    if not pages:
        raise BrowserError(f"no open page whose url contains {match!r}")
    return pages[0]


def evaluate(expression: str, match: str = "fomo.family", port: int | None = None,
             timeout: float = 30.0) -> Any:
    """Run one expression in a page and return its value.

    Anything the expression throws comes back as a BrowserError carrying the page's own message,
    because a silent failure here is what this module exists to stop.
    """
    import asyncio

    try:
        import websockets
    except ImportError as e:  # pragma: no cover - only ever hit off the server
        raise BrowserError("this needs `websockets` (pip install '.[api]')") from e

    ws_url = find_target(match, port).get("webSocketDebuggerUrl")
    if not ws_url:
        raise BrowserError("that page exposes no debugger socket")

    async def run() -> Any:
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "id": 1, "method": "Runtime.evaluate",
                "params": {"expression": expression, "awaitPromise": True,
                           "returnByValue": True, "userGesture": True},
            }))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") != 1:
                    continue          # an event; we only asked one question
                if "error" in msg:
                    raise BrowserError(f"devtools refused: {msg['error']}")
                result = msg.get("result") or {}
                if result.get("exceptionDetails"):
                    text = (result["exceptionDetails"].get("exception") or {}).get("description")
                    raise BrowserError(f"page threw: {text or result['exceptionDetails']}")
                return (result.get("result") or {}).get("value")

    return asyncio.run(asyncio.wait_for(run(), timeout))


# ---------------------------------------------------------------- what the page can tell us

SIGNED_IN = """(() => {
  const keys = Object.keys(localStorage);
  const privy = keys.filter((k) => /privy/i.test(k));
  return {
    url: location.href,
    localKeys: keys.length,
    privyKeys: privy.length,
    hasToken: privy.some((k) => /token/i.test(k) && (localStorage.getItem(k) || '').length > 20),
    cookieBytes: document.cookie.length,
    // the app renders a Login button until it has a session, and swaps it for the account menu
    showsLogin: /\\bLogin\\b/.test(document.body ? document.body.innerText.slice(0, 4000) : ''),
  };
})()"""


def state(port: int | None = None) -> dict:
    """What the fomo tab currently is: signed in, or showing a login button."""
    return evaluate(SIGNED_IN, port=port) or {}


def write_session(payload: dict, port: int | None = None) -> dict:
    """Put a session exported from another browser into this one, then reload.

    Only the three stores a page can write are touched. A cookie the other browser marked HttpOnly
    never left it, which is the point of HttpOnly, and no amount of scripting here changes that.
    """
    expression = """(() => {
      const p = %s;
      let local = 0, session = 0, cookies = 0;
      for (const [k, v] of Object.entries(p.local || {})) { localStorage.setItem(k, v); local++; }
      for (const [k, v] of Object.entries(p.session || {})) { sessionStorage.setItem(k, v); session++; }
      for (const [k, v] of Object.entries(p.cookies || {})) {
        document.cookie = k + '=' + v + '; path=/; max-age=2592000; samesite=lax';
        cookies++;
      }
      return { local, session, cookies };
    })()""" % json.dumps({
        "local": payload.get("local") or {},
        "session": payload.get("session") or {},
        "cookies": payload.get("cookies") or {},
    }, separators=(",", ":"))
    wrote = evaluate(expression, port=port) or {}
    evaluate("location.reload(); true", port=port)
    return wrote
