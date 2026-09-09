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


# A page and an extension's service worker are both things we ask questions of, and the worker is
# the more useful of the two: it is where the collector actually lives.
DRIVABLE = ("page", "service_worker")


def find_target(match: str, port: int | None = None) -> dict:
    found = [t for t in targets(port)
             if t.get("type") in DRIVABLE and match in (t.get("url") or "")]
    if not found:
        raise BrowserError(f"nothing open whose url contains {match!r}")
    # A page beats a worker when both match, because a bare url fragment usually means the page.
    found.sort(key=lambda t: t.get("type") != "page")
    return found[0]


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


def collect(port: int | None = None, timeout: float = 30.0) -> dict:
    """Make the collector run now, from here rather than from Chrome's own alarm.

    The extension schedules itself with `chrome.alarms`, and in Manifest V3 that is a request
    rather than a promise: the service worker is torn down when idle and the alarm is supposed to
    wake it. Observed on this box, it stopped waking it after a few hours - collections simply
    stopped, with the browser still signed in and nothing anywhere complaining.

    A systemd timer does not have that problem. So the schedule moves to the server and the
    extension keeps only the part it is uniquely able to do: making the requests from inside a
    signed-in page. Its own alarm stays as a fallback for whenever nobody is asking.
    """
    import time as _time

    call = "collectNow('server'); true"
    if not _worker_awake(port):
        # A dormant Manifest V3 worker is not listed as a target at all, so there is nothing to
        # call. Reloading the page wakes it: the content script messages the extension the moment
        # it sees an auth header, and that message is what starts the worker.
        #
        # The reload is deliberately not awaited. Navigating destroys the execution context the
        # evaluate is running in, so the reply never comes and the call hangs until it times out;
        # handing it to setTimeout lets the evaluate return before the page goes away.
        log.info("collector worker is asleep; reloading the page to wake it")
        evaluate("setTimeout(() => location.reload(), 50); true", port=port, timeout=15)
        for _ in range(20):
            _time.sleep(3)
            if _worker_awake(port):
                break
        else:
            raise BrowserError("the collector's service worker never woke up")
    # Started, not awaited. A pass takes minutes of deliberately paced requests, and holding a
    # devtools socket open across it only invents a second thing that can time out. Whether it
    # worked is a question for the receiver's log and the health check, which is where anyone
    # would look anyway.
    evaluate(call, match="background.js", port=port, timeout=timeout)
    return {"started": True}


def _worker_awake(port: int | None = None) -> bool:
    """Is the extension's service worker running? It only appears as a target while it is."""
    try:
        return any(t.get("type") == "service_worker" and "background.js" in (t.get("url") or "")
                   for t in targets(port))
    except BrowserError:
        return False


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
