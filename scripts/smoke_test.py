#!/usr/bin/env python3
"""Smoke test — verify the apartment scanner environment is ready.

Usage:
    python3 scripts/smoke_test.py           # Full check
    python3 scripts/smoke_test.py --quick   # Fast check (skip slow tests)
    python3 scripts/smoke_test.py --json    # Machine-readable output

Exit code 0 = all good. Exit code 1 = something needs attention.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def check(label: str, ok: bool, detail: str = "") -> dict:
    return {"label": label, "ok": ok, "detail": detail}


def run_checks(quick: bool = False) -> list[dict]:
    results = []

    # ── Python version ──────────────────────────────────────────────────────
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    results.append(check(
        "Python ≥ 3.10",
        sys.version_info >= (3, 10),
        f"Python {py_ver}"
    ))

    # ── Dependencies ────────────────────────────────────────────────────────
    deps_ok = True
    missing = []
    for mod in ["requests", "websockets", "lxml", "watchdog"]:
        try:
            __import__(mod)
        except ImportError:
            deps_ok = False
            missing.append(mod)
    results.append(check(
        "Core dependencies",
        deps_ok,
        "All installed" if deps_ok else f"Missing: {', '.join(missing)}"
    ))

    # ── Optional: google-genai ──────────────────────────────────────────
    try:
        __import__("google.genai")
        llm_ok = True
        llm_detail = "Available"
    except ImportError:
        try:
            __import__("google.generativeai")
            llm_ok = True
            llm_detail = "Available (via google-generativeai, consider upgrading to google-genai)"
        except ImportError:
            llm_ok = False
            llm_detail = "Not installed (optional — AI triage disabled)"
    results.append(check("LLM (google-generativeai)", llm_ok, llm_detail))

    # ── Optional: python-telegram-bot ──────────────────────────────────────
    try:
        __import__("telegram")
        tgbot_ok = True
        tgbot_detail = "Available"
    except ImportError:
        tgbot_ok = False
        tgbot_detail = "Not installed (optional — Telegram bot disabled)"
    results.append(check("Telegram bot (python-telegram-bot)", tgbot_ok, tgbot_detail))

    # ── Chromium / CDP ─────────────────────────────────────────────────────
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://127.0.0.1:9223/json/version", timeout=3)
        data = json.loads(resp.read())
        browser = data.get("Browser", "Unknown")
        results.append(check(
            "Chromium CDP (port 9223)",
            True,
            f"Running — {browser}"
        ))
    except Exception:
        # Try to find chromium binary
        chromium_paths = [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ]
        found = None
        for p in chromium_paths:
            if os.path.exists(p):
                found = p
                break
        
        if found:
            detail = f"Not running, but found at {found}. Run: {SCRIPTS / 'scanner-chromium'} &"
        else:
            detail = "Not running and no chromium binary found. Install: apt install chromium"
        results.append(check("Chromium CDP (port 9223)", False, detail))

    # ── Facebook cookies ───────────────────────────────────────────────────
    cookie_path = ROOT / "data" / "facebook_cookies.json"
    if cookie_path.exists():
        try:
            data = json.loads(cookie_path.read_text())
            cookie_count = len(data) if isinstance(data, list) else 0
            results.append(check(
                "Facebook cookies",
                cookie_count > 0,
                f"Found ({cookie_count} cookies)" if cookie_count > 0 else "File exists but empty"
            ))
        except (json.JSONDecodeError, OSError) as e:
            results.append(check("Facebook cookies", False, f"Invalid file: {e}"))
    else:
        results.append(check(
            "Facebook cookies",
            False,
            f"Not found at {cookie_path}. Export from browser."
        ))

    # ── Key scripts exist ──────────────────────────────────────────────────
    key_scripts = [
        "full_apartment_scan.py",
        "yad2_broad_search.py",
        "evaluate_candidates.py",
        "facebook_group_feed_scan.py",
        "facebook_clean_posts.py",
        "facebook_auto_triage.py",
        "inject_cookies.py",
    ]
    all_exist = True
    for s in key_scripts:
        if not (SCRIPTS / s).exists():
            all_exist = False
            break
    results.append(check(
        f"Key scripts ({len(key_scripts)})",
        all_exist,
        "All present" if all_exist else f"Missing some"
    ))

    # ── criteria.yaml ──────────────────────────────────────────────────────
    criteria_path = ROOT / "criteria.yaml"
    results.append(check(
        "criteria.yaml",
        criteria_path.exists(),
        "Found" if criteria_path.exists() else "Missing — create from template"
    ))

    # ── GEMINI_API_KEY (optional) ──────────────────────────────────────────
    gemini_key = os.environ.get("GEMINI_API_KEY")
    results.append(check(
        "GEMINI_API_KEY",
        bool(gemini_key),
        "Set" if gemini_key else "Not set (optional — AI features disabled)"
    ))

    # ── Slow: verify Chromium can actually browse ──────────────────────────
    if not quick:
        try:
            import urllib.request
            resp = urllib.request.urlopen("http://127.0.0.1:9223/json/version", timeout=2)
            # Try creating a new page
            import json as _j
            version_data = _j.loads(resp.read())
            ws_url = version_data.get("webSocketDebuggerUrl", "")
            if ws_url:
                results.append(check(
                    "Chromium WebSocket",
                    True,
                    "WebSocket endpoint available"
                ))
            else:
                results.append(check(
                    "Chromium WebSocket",
                    False,
                    "CDP running but no WebSocket URL"
                ))
        except Exception:
            pass  # Already reported in CDP check

    return results


def main():
    parser = argparse.ArgumentParser(description="Apartment Scanner smoke test")
    parser.add_argument("--quick", action="store_true", help="Skip slow tests")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    args = parser.parse_args()

    results = run_checks(quick=args.quick)

    if args.json:
        print(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "all_ok": all(r["ok"] for r in results),
            "checks": results,
        }, ensure_ascii=False, indent=2))
    else:
        ok_count = sum(1 for r in results if r["ok"])
        total = len(results)
        
        print("╔══════════════════════════════════════════╗")
        print("║   🏠 Apartment Scanner — Smoke Test      ║")
        print("╚══════════════════════════════════════════╝")
        print()
        
        for r in results:
            icon = "✅" if r["ok"] else "❌"
            print(f"{icon} {r['label']}")
            if r["detail"]:
                print(f"   {r['detail']}")
        
        print()
        print(f"Result: {ok_count}/{total} checks passed")
        
        if ok_count == total:
            print("✅ Environment ready!")
        else:
            print("⚠️  Some checks failed — see details above.")
            print("   Core checks must pass. Optional checks can be ignored.")

    sys.exit(0 if all(r["ok"] for r in results) else 1)


if __name__ == "__main__":
    main()
