"""Phase 0, plan A: capture fomo.family internal API endpoints with Playwright.

NOTE: if fomo.family only offers Google sign-in, this path fails - Google refuses to
authenticate inside automation-driven browsers ("this browser or app may not be secure").
Use scripts/parse_fomo_har.py instead: log in with your own Chrome and export a HAR.

Opens a visible Chromium, lets you log in manually, then records every XHR/fetch
while you browse (leaderboard 24h/7d/30d, token page -> Holders, trader profile).
Auth header/cookie VALUES are masked; only names + shapes are stored.

Usage:
    python scripts/capture_fomo_endpoints.py [--out tests/fixtures] [--persist]

Press ENTER in the terminal when you are done browsing. Output:
    tests/fixtures/fomo_capture_<timestamp>.json   (masked, gitignored)
    fomo_state/                                    (browser profile, gitignored, only with --persist)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Request, Response, sync_playwright

SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token", "x-csrf-token"}
SITE = "https://fomo.family"
SKIP_EXT = re.compile(r"\.(js|css|png|jpg|jpeg|gif|svg|woff2?|ttf|ico|map|webp|mp4)(\?|$)", re.I)


def mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]} (len={len(value)})"


def mask_headers(h: dict[str, str]) -> dict[str, str]:
    return {k: (mask(v) if k.lower() in SENSITIVE_HEADERS else v) for k, v in h.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tests/fixtures")
    ap.add_argument("--persist", action="store_true", help="keep browser profile in ./fomo_state for reuse")
    ap.add_argument("--body-bytes", type=int, default=2048)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    captured: list[dict] = []
    auth_seen: dict[str, str] = {}

    def on_request(req: Request) -> None:
        if req.resource_type not in ("xhr", "fetch"):
            return
        if SKIP_EXT.search(req.url):
            return
        for k, v in req.headers.items():
            if k.lower() in SENSITIVE_HEADERS:
                auth_seen[k.lower()] = mask(v)

    def on_response(resp: Response) -> None:
        req = resp.request
        if req.resource_type not in ("xhr", "fetch") or SKIP_EXT.search(req.url):
            return
        try:
            body = resp.body()[: args.body_bytes].decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            body = f"<unavailable: {e}>"
        entry = {
            "ts": time.time(),
            "page": resp.frame.page.url if resp.frame and resp.frame.page else None,
            "method": req.method,
            "url": req.url,
            "status": resp.status,
            "req_headers": mask_headers(req.headers),
            "post_data": (req.post_data or "")[:1024],
            "resp_content_type": resp.headers.get("content-type"),
            "resp_head": body,
        }
        captured.append(entry)
        print(f"[{resp.status}] {req.method} {req.url[:120]}")

    with sync_playwright() as p:
        if args.persist:
            ctx = p.chromium.launch_persistent_context("fomo_state", headless=False)
        else:
            browser = p.chromium.launch(headless=False)
            ctx = browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(SITE)

        print("\n=== Log in manually, then browse: ===")
        print("  1. Leaderboard: 24h / 7d / 30d")
        print("  2. A token page -> Holders tab (sort by PnL if available)")
        print("  3. A trader profile page")
        print("  4. Scroll / paginate to trigger extra requests")
        print("Press ENTER here when done.\n")
        try:
            input()
        except EOFError:
            print("stdin closed; waiting 120s instead")
            time.sleep(120)

        # cookie names only (values masked) so we know what FOMO_SESSION should carry
        cookies = [{"name": c["name"], "domain": c["domain"], "value": mask(c["value"])} for c in ctx.cookies()]
        ctx.close()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = out_dir / f"fomo_capture_{stamp}.json"
    out.write_text(
        json.dumps({"site": SITE, "auth_headers_seen": auth_seen, "cookies": cookies, "requests": captured}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # quick digest of distinct API paths
    seen: dict[str, int] = {}
    for e in captured:
        path = e["url"].split("?", 1)[0]
        key = e["method"] + " " + path
        seen[key] = seen.get(key, 0) + 1
    print(f"\nSaved {len(captured)} requests -> {out}")
    print("Distinct endpoints:")
    for k, n in sorted(seen.items(), key=lambda x: -x[1]):
        print(f"  {n:3d}  {k}")
    print("\nNext: fill docs/fomo-endpoints.md from this dump.")


if __name__ == "__main__":
    sys.exit(main())
