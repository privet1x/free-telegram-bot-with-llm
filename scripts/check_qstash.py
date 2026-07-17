"""Check the Upstash QStash keys.

Calls GET /v2/keys with your token: confirms the token is valid and that the
signing keys in .env match what QStash actually returns. Tries QSTASH_URL
(regional) first, then falls back to the global endpoint.

Run from the project root:
    python scripts/check_qstash.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app.settings import settings  # noqa: E402

GLOBAL = "https://qstash.upstash.io"


def main() -> None:
    if not settings.QSTASH_TOKEN:
        print("QSTASH_TOKEN is not set in .env.")
        sys.exit(1)

    bases = []
    if settings.QSTASH_URL:
        bases.append(settings.QSTASH_URL.rstrip("/"))
    if GLOBAL not in bases:
        bases.append(GLOBAL)

    headers = {"Authorization": f"Bearer {settings.QSTASH_TOKEN}"}
    resp = None
    used = None
    for base in bases:
        try:
            r = httpx.get(f"{base}/v2/keys", headers=headers, timeout=30.0)
        except Exception as exc:  # network/DNS
            print(f"{base} -> request error: {exc}")
            continue
        print(f"GET {base}/v2/keys -> {r.status_code}")
        if r.status_code == 200:
            resp, used = r, base
            break

    if resp is None:
        print("Could not get 200 from any endpoint. Token is invalid or no network.")
        sys.exit(1)

    data = resp.json()
    cur, nxt = data.get("current"), data.get("next")
    cur_ok = cur == settings.QSTASH_CURRENT_SIGNING_KEY
    nxt_ok = nxt == settings.QSTASH_NEXT_SIGNING_KEY
    print("endpoint          :", used)
    print("current key match :", cur_ok)
    print("next key match    :", nxt_ok)

    if cur_ok and nxt_ok:
        print("\nOK — QStash token is valid, signing keys in .env match.")
    else:
        print("\nWARNING: token works, but the signing keys in .env do NOT match QStash's response — re-check them.")
        sys.exit(1)


if __name__ == "__main__":
    main()
