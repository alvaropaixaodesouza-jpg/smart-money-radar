"""Store a fomo session in .env from a single DevTools "Copy as cURL", without ever printing it.

Chrome strips cookies and Authorization from an exported HAR, so the session has to come from a
single request instead:

  1. On fomo.family open DevTools -> Network -> filter Fetch/XHR.
  2. Find any request to prod-api.fomo.family (e.g. `leaderboard`).
  3. Right-click it -> Copy -> **Copy as cURL (bash)**.
  4. Paste into a file, e.g. curl.txt, then:

         python scripts/fomo_session_from_curl.py curl.txt

The script writes FOMO_SESSION / FOMO_AUTH_KIND / FOMO_BASE_URL into .env and prints only the
lengths. Delete curl.txt afterwards: it holds a live session.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

HEADER_RE = re.compile(r"""(?:-H|--header)\s+(['"])(.*?)\1""", re.S)
URL_RE = re.compile(r"""curl\s+(?:-[A-Za-z-]+\s+)*(['"]?)(https?://[^'"\s]+)\1""")
COOKIE_FLAG_RE = re.compile(r"""(?:-b|--cookie)\s+(['"])(.*?)\1""", re.S)


def parse_curl(text: str) -> tuple[str | None, dict[str, str]]:
    text = text.replace("\\\n", " ").replace("^\n", " ")
    m = URL_RE.search(text)
    url = m.group(2) if m else None
    headers: dict[str, str] = {}
    for _, raw in HEADER_RE.findall(text):
        if ":" not in raw:
            continue
        k, v = raw.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    m = COOKIE_FLAG_RE.search(text)
    if m and "cookie" not in headers:
        headers["cookie"] = m.group(2).strip()
    return url, headers


def upsert_env(path: Path, values: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for k, v in values.items():
        if re.search(rf"(?m)^{k}=", text):
            text = re.sub(rf"(?m)^{k}=.*$", f"{k}={v}", text)
        else:
            text = text.rstrip() + f"\n{k}={v}\n"
    path.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("curl_file", nargs="?", help="file with the copied cURL (reads stdin if omitted)")
    ap.add_argument("--env-path", default=".env")
    args = ap.parse_args()

    text = Path(args.curl_file).read_text(encoding="utf-8", errors="replace") if args.curl_file else sys.stdin.read()
    url, headers = parse_curl(text)
    if not url:
        print("Could not find a URL: is this a 'Copy as cURL' command?")
        return 1
    host = urlparse(url).netloc
    print(f"Request: {urlparse(url).scheme}://{host}{urlparse(url).path}")
    print(f"Headers found: {', '.join(sorted(headers)) or 'none'}")

    auth, kind = headers.get("authorization"), "bearer"
    if auth and auth.lower().startswith("bearer "):
        auth = auth[7:]
    if not auth:
        auth, kind = headers.get("cookie"), "cookie"
    if not auth:
        print("\nNo Authorization or Cookie header in this request.")
        print("Copy a request that is actually authenticated (one to prod-api.fomo.family that "
              "returns 200, not a preflight/OPTIONS), and make sure you used 'Copy as cURL', not 'as fetch'.")
        return 1

    values = {"FOMO_SESSION": auth, "FOMO_AUTH_KIND": kind, "FOMO_BASE_URL": f"https://{host}"}
    if headers.get("x-supported-chains"):
        values["FOMO_SUPPORTED_CHAINS"] = headers["x-supported-chains"]
    upsert_env(Path(args.env_path), values)
    print(f"\nWrote to {args.env_path}: FOMO_AUTH_KIND={kind}, FOMO_BASE_URL=https://{host}, "
          f"FOMO_SESSION ({len(auth)} chars, value not shown).")
    print("Now verify with:  python -m fomo_agent.cli fomo-check")
    if args.curl_file:
        print(f"Then delete {args.curl_file}: it contains a live session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
