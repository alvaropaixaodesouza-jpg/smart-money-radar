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
import time
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


def browser_ws(port: int | None = None) -> str:
    """The browser's own debugger socket, as opposed to any one target's."""
    port = port or settings.browser_debug_port
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=10)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise BrowserError(f"no browser answering on 127.0.0.1:{port} ({e})") from e
    url = r.json().get("webSocketDebuggerUrl")
    if not url:
        raise BrowserError("the browser exposes no debugger socket")
    return url


def evaluate(expression: str, match: str = "fomo.family", port: int | None = None,
             timeout: float = 30.0) -> Any:
    """Run one expression in a page or a service worker and return its value.

    Everything goes through the browser's own socket with an attached session rather than a
    target's socket directly.

    An extension's service worker cannot be reached at all: attaching to its own socket leaves it
    paused and silent, and attaching through the browser answers "Not allowed" - Chrome does not
    let a debugger into an extension. That is why the fomo collection is kept alive by restarting
    the browser (deploy/fomo-watchdog.sh) rather than by calling into it from here.

    Anything the expression throws comes back as a BrowserError carrying the page's own message,
    because a silent failure here is what this module exists to stop.
    """
    import asyncio

    try:
        import websockets
    except ImportError as e:  # pragma: no cover - only ever hit off the server
        raise BrowserError("this needs `websockets` (pip install '.[api]')") from e

    target_id = find_target(match, port).get("id")
    if not target_id:
        raise BrowserError("that target has no id")
    ws_url = browser_ws(port)

    async def run() -> Any:
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "id": 1, "method": "Target.attachToTarget",
                "params": {"targetId": target_id, "flatten": True},
            }))
            session = None
            while session is None:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise BrowserError(f"could not attach: {msg['error']}")
                    session = (msg.get("result") or {}).get("sessionId")

            # A worker attached to for the first time is paused until it is told to carry on.
            await ws.send(json.dumps({"id": 2, "method": "Runtime.enable", "sessionId": session}))
            await ws.send(json.dumps({"id": 3, "method": "Runtime.runIfWaitingForDebugger",
                                      "sessionId": session}))
            await ws.send(json.dumps({
                "id": 4, "method": "Runtime.evaluate", "sessionId": session,
                "params": {"expression": expression, "awaitPromise": True,
                           "returnByValue": True, "userGesture": True},
            }))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") != 4:
                    continue          # an event, or one of the setup replies
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
    // A restricted account is signed in, holds a live token, and is refused by every endpoint. From
    // out here that is indistinguishable from health, which is how it went unnoticed for two and a
    // half hours while every explanation except the right one was tried.
    restricted: /account is restricted/i.test(
      document.body ? document.body.innerText.slice(0, 4000) : ''),
  };
})()"""


def state(port: int | None = None) -> dict:
    """What the fomo tab currently is: signed in, showing a login, or restricted."""
    return evaluate(SIGNED_IN, port=port) or {}


def collect_now(port: int | None = None) -> str:
    """Make the collector collect, now, by asking the page to poke the service worker.

    The extension normally runs itself off a `chrome.alarms` period. That alarm stopped firing once
    — five hours of it, with the watchdog restarting the browser every ten minutes and achieving
    nothing, while a single poke through this path collected everything waiting. So this is the
    intervention that actually works, and the watchdog reaches for it before it reaches for a
    restart: a browser that is signed in and holding a live token does not need to be killed.

    The page cannot call chrome.runtime itself — that lives in the isolated world — so the message
    goes out as a window event and content_bridge.js relays it. Returns immediately; the collection
    takes about a minute, and the receiver's log is where it lands.

    Reloads first when the page has no collector in it. A manifest's content scripts only run on
    navigation, so a tab that was already open when the extension was installed or updated is
    running none of them — and a poke into a page with no listener returns "poked" and does
    nothing, which is the most misleading answer available. The watchdog reaches for this exactly
    when the browser has just been restarted, so it has to survive that case.
    """
    present = evaluate('typeof window.__fomoAgent', port=port)
    if present != "object":
        evaluate("(location.reload(), 1)", port=port)
        for _ in range(12):
            time.sleep(3)
            if evaluate('typeof window.__fomoAgent', port=port) == "object":
                break
        else:
            return "the page has no collector in it, even after a reload"

    poke = ('(window.postMessage({__fomoAgent: true, type: "collectNow", payload: {}}, "*"), '
            '"poked")')
    return evaluate(poke, port=port)


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
