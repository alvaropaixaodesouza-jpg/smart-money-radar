"""Phase 0, plan B: extract fomo.family's internal API from a HAR you export yourself.

Google blocks OAuth inside automation-driven browsers ("this browser may not be secure"),
so instead of logging in under Playwright you record the traffic in your own Chrome:

  1. Log into fomo.family normally.
  2. F12 -> Network tab -> check "Preserve log" -> filter Fetch/XHR.
  3. Browse: leaderboard 24h / 7d / 30d, a token page -> Holders, a trader profile. Scroll to paginate.
  4. Right-click in the request list -> "Save all as HAR with content" -> fomo.har
  5. python scripts/parse_fomo_har.py fomo.har

The HAR holds your live session token, so it never leaves your machine: this script masks
every auth value before writing the fixture, and prints only the masked digest.
Use --write-env to store the real session into .env without it ever being printed.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

SENSITIVE = {"authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token", "x-csrf-token", "x-session-token"}
SKIP_EXT = re.compile(r"\.(js|css|png|jpe?g|gif|svg|woff2?|ttf|ico|map|webp|mp4|avif)(\?|$)", re.I)
DEFAULT_HOST_FILTER = "fomo"


def mask(value: str) -> str:
    return "***" if len(value) <= 8 else f"{value[:4]}…{value[-4:]} (len={len(value)})"


def is_api(entry: dict, host_filter: str) -> bool:
    req = entry.get("request", {})
    url = req.get("url", "")
    if host_filter and host_filter not in urlparse(url).netloc:
        return False
    if SKIP_EXT.search(url):
        return False
    mime = (entry.get("response", {}).get("content", {}) or {}).get("mimeType", "")
    rtype = (entry.get("_resourceType") or "").lower()
    return rtype in ("xhr", "fetch") or "json" in mime


def headers_of(block: dict) -> dict[str, str]:
    return {h.get("name", ""): h.get("value", "") for h in block.get("headers", []) or []}


def load_har(path: Path) -> tuple[list[dict], bool]:
    """Return (entries, truncated). A HAR cut short mid-write still yields its complete entries."""
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return ((json.loads(text).get("log") or {}).get("entries") or []), False
    except json.JSONDecodeError:
        pass
    try:
        pos = text.index("[", text.index('"entries"')) + 1
    except ValueError:
        return [], True
    decoder, entries = json.JSONDecoder(), []
    while pos < len(text):
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] != "{":
            break
        try:
            obj, pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        entries.append(obj)
    return entries, True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("har", help="HAR file exported from DevTools")
    ap.add_argument("--out", default="tests/fixtures")
    ap.add_argument("--host", default=DEFAULT_HOST_FILTER, help="only keep requests whose host contains this (default: fomo)")
    ap.add_argument("--body-bytes", type=int, default=4096)
    ap.add_argument("--write-env", action="store_true", help="write the real session value into .env (never printed)")
    ap.add_argument("--env-path", default=".env")
    args = ap.parse_args()

    entries, truncated = load_har(Path(args.har))
    if truncated:
        print(f"WARNING: {args.har} is truncated JSON; salvaged {len(entries)} complete entries.\n"
              "Re-export the HAR if key endpoints are missing below.")
    kept, auth_seen, real_auth = [], {}, {}
    host_hits: dict[str, int] = {}
    host_auth: set[str] = set()

    for e in entries:
        if not is_api(e, args.host):
            continue
        req, resp = e.get("request", {}), e.get("response", {})
        rh = headers_of(req)
        netloc = urlparse(req.get("url", "")).netloc
        host_hits[netloc] = host_hits.get(netloc, 0) + 1
        for k, v in rh.items():
            if k.lower() in SENSITIVE and v:
                auth_seen[k.lower()] = mask(v)
                real_auth.setdefault(k.lower(), v)
                host_auth.add(netloc)
        content = resp.get("content") or {}
        body = content.get("text") or ""
        if content.get("encoding") == "base64":
            try:
                body = base64.b64decode(body).decode("utf-8", "replace")
            except (ValueError, binascii.Error):
                pass
        body = body[: args.body_bytes]
        kept.append({
            "method": req.get("method"),
            "url": req.get("url"),
            "status": resp.get("status"),
            "req_headers": {k: (mask(v) if k.lower() in SENSITIVE else v) for k, v in rh.items()},
            "query": {q.get("name"): q.get("value") for q in req.get("queryString", []) or []},
            "post_data": ((req.get("postData") or {}).get("text") or "")[:1024],
            "resp_content_type": (resp.get("content") or {}).get("mimeType"),
            "resp_head": body,
        })

    if not kept:
        print(f"No API requests found. {len(entries)} entries in the HAR, none matched host filter {args.host!r}.")
        print("Re-export with the Fetch/XHR filter on, or pass --host '' to keep everything.")
        return 1

    hosts = sorted({urlparse(k["url"]).netloc for k in kept})
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"fomo_capture_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"source": "har", "hosts": hosts, "auth_headers_seen": auth_seen, "requests": kept},
                              indent=2, ensure_ascii=False), encoding="utf-8")

    digest: dict[str, dict] = {}
    for k in kept:
        p = urlparse(k["url"])
        key = f"{k['method']} {p.scheme}://{p.netloc}{p.path}"
        d = digest.setdefault(key, {"n": 0, "params": set(), "statuses": set()})
        d["n"] += 1
        d["params"].update(k["query"].keys())
        d["statuses"].add(k["status"])

    print(f"\nParsed {len(entries)} HAR entries, kept {len(kept)} API calls -> {out}")
    print(f"Hosts: {', '.join(hosts)}")
    print(f"Auth headers present: {', '.join(auth_seen) or 'none (session may be cookie-less)'}")
    print("\nEndpoints (paste this to Claude, it contains no secrets):")
    for key, d in sorted(digest.items(), key=lambda kv: -kv[1]["n"]):
        params = ",".join(sorted(d["params"])) or "-"
        statuses = ",".join(str(s) for s in sorted(d["statuses"]))
        print(f"  {d['n']:3d}x [{statuses}] {key}   params: {params}")

    if args.write_env:
        env = Path(args.env_path)
        text = env.read_text(encoding="utf-8") if env.exists() else ""
        kind = "bearer" if "authorization" in real_auth else "cookie"
        value = real_auth.get("authorization", real_auth.get("cookie", ""))
        if value.lower().startswith("bearer "):
            value = value[7:]
        if not value:
            print("\n--write-env: no auth header found in the HAR; nothing written.")
        else:
            # the API host is the one that actually carried the session, busiest first
            base_host = max(host_hits, key=lambda h: (h in host_auth, host_hits[h]))
            for k, v in (("FOMO_SESSION", value), ("FOMO_AUTH_KIND", kind),
                         ("FOMO_BASE_URL", f"https://{base_host}")):
                text = (re.sub(rf"(?m)^{k}=.*$", f"{k}={v}", text) if re.search(rf"(?m)^{k}=", text)
                        else text.rstrip() + f"\n{k}={v}\n")
            env.write_text(text, encoding="utf-8")
            print(f"\nWrote FOMO_SESSION ({kind}, {len(value)} chars), FOMO_AUTH_KIND and FOMO_BASE_URL into {env}.")
    else:
        print("\nRe-run with --write-env to store the session into .env (the value is never printed).")

    print(f"\nKeep {out} local: it is gitignored, but response bodies may contain your account data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
