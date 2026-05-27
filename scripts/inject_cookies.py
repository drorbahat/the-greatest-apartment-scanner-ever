#!/usr/bin/env python3
"""
Inject Facebook cookies from a JSON file into Chromium via CDP.

Designed for first-time setup or when cookies expire.

Usage:
  python3 scripts/inject_cookies.py /app/data/facebook_cookies.json

Cookie file format (Netscape/JSON hybrid — compatible with EditThisCookie export):
  [
    {"domain": ".facebook.com", "name": "c_user", "value": "12345", ...},
    ...
  ]

Run this while Chromium CDP is running on port 9223.
"""

import json
import pathlib
import sys
import time
import urllib.request

PORT = 9223


def _get_json(url: str):
    return json.load(urllib.request.urlopen(url, timeout=8))


async def inject_cookies(cookie_path: str) -> int:
    import asyncio
    import websockets

    if not pathlib.Path(cookie_path).exists():
        print(f"❌ Cookie file not found: {cookie_path}")
        return 1

    cookies = json.loads(pathlib.Path(cookie_path).read_text(encoding="utf-8"))
    if not isinstance(cookies, list):
        print("❌ Cookie file must be a JSON array")
        return 1

    print(f"📦 Loaded {len(cookies)} cookies from {cookie_path}")

    # Connect to Chromium CDP
    try:
        ver = _get_json(f"http://127.0.0.1:{PORT}/json/version")
    except Exception as e:
        print(f"❌ Cannot connect to Chromium CDP on port {PORT}: {e}")
        return 1

    ws_url = ver["webSocketDebuggerUrl"]
    print(f"🔗 Connected to CDP: {ws_url[:60]}...")

    async with websockets.connect(ws_url) as ws:
        counter = [0]

        async def cdp(method: str, params: dict | None = None) -> dict:
            counter[0] += 1
            msg = json.dumps({"id": counter[0], "method": method, "params": params or {}})
            await ws.send(msg)
            while True:
                resp = json.loads(await ws.recv())
                if resp.get("id") == counter[0]:
                    return resp

        # First navigate to Facebook to set domain
        await cdp("Page.enable")
        await cdp("Page.navigate", {"url": "https://www.facebook.com"})
        await asyncio.sleep(3)  # let page start loading

        # Inject cookies
        injected = 0
        for c in cookies:
            # Skip non-Facebook domains
            domain = c.get("domain", "")
            if "facebook" not in domain and "fb" not in domain:
                continue

            cookie_params = {
                "url": f"https://{domain.lstrip('.')}",
                "name": c["name"],
                "value": c["value"],
                "domain": domain,
            }
            if c.get("path"):
                cookie_params["path"] = c["path"]
            if c.get("secure"):
                cookie_params["secure"] = True
            if c.get("httpOnly"):
                cookie_params["httpOnly"] = True
            if c.get("expirationDate"):
                cookie_params["expires"] = int(c["expirationDate"])

            try:
                result = await cdp("Network.setCookie", cookie_params)
                if result.get("result", {}).get("success"):
                    injected += 1
            except Exception:
                pass

        # Verify by navigating to Facebook
        await cdp("Page.navigate", {"url": "https://www.facebook.com"})
        await asyncio.sleep(3)

        # Check if login was successful
        page_cdp = await cdp("Runtime.evaluate", {
            "expression": "document.title",
        })
        title = page_cdp.get("result", {}).get("result", {}).get("value", "")
        blocked_terms = ["Log in to Facebook", "Facebook Login", "You must log in"]
        is_logged_in = not any(t in title for t in blocked_terms)

        if is_logged_in:
            print(f"✅ Success! Injected {injected} cookies. Facebook: logged in ✓")
            return 0
        else:
            print(f"⚠️  Injected {injected} cookies, but Facebook still shows login page.")
            print(f"   Page title: {title}")
            print("   Cookies may be expired. Re-export from a logged-in browser.")
            return 1


if __name__ == "__main__":
    import asyncio

    cookie_file = sys.argv[1] if len(sys.argv) > 1 else "/app/data/facebook_cookies.json"
    exit_code = asyncio.run(inject_cookies(cookie_file))
    sys.exit(exit_code)
