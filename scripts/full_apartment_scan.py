#!/usr/bin/env python3
"""Reliable full apartment scan orchestrator for Yogev.

This script is the durable, evidence-based wrapper around the individual source
scanners. It writes a state file while running, a manifest at the end of the
collection phase, and a unified Hebrew report after Facebook AI triage exists.

It intentionally does not contact anyone and does not try to solve human
verification. If Facebook needs AI triage, the run status is `awaiting_ai_triage`
until a triage JSON is written and `finalize` is called.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from typing import Any

from facebook_url_utils import classify_facebook_url, normalize_facebook_item_urls
from scan_quality import scan_quality_section
from events import emit as _emit_event

ROOT = pathlib.Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts"
FULL_RUNS = ART / "full_scan_runs"
FACEBOOK_ART = ART / "facebook"
DEFAULT_FACEBOOK_MODEL = "auto-triage"


# ---------------------------------------------------------------------------
# Small utilities


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_id_now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def scan_date_now() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_json(path: pathlib.Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_log(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")


def short(text: str | None, n: int = 220) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def money(value: Any) -> str:
    if isinstance(value, (int, float)) and value:
        return f"₪{int(value):,}"
    return "לא צוין"


def fmt_rooms(value: Any) -> str:
    if value is None:
        return "לא צוין"
    try:
        f = float(value)
        return str(int(f)) if f.is_integer() else str(f)
    except Exception:
        return str(value)


def latest_run_dir() -> pathlib.Path | None:
    if not FULL_RUNS.exists():
        return None
    dirs = [p for p in FULL_RUNS.iterdir() if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# State handling


class RunState:
    def __init__(self, run_dir: pathlib.Path, scan_date: str, run_id: str):
        self.run_dir = run_dir
        self.path = run_dir / "state.json"
        self.data: dict[str, Any] = {
            "run_id": run_id,
            "scan_date": scan_date,
            "run_dir": str(run_dir),
            "status": "initialized",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "steps": {},
            "events": [],
            "definition_of_done": [
                "Yad2 finished successfully",
                "Madlan public scan finished successfully",
                "Facebook feed scan, clean, and AI-input preparation finished successfully",
                "If Facebook selected posts for AI, triage output exists and cache was updated",
                "Unified final report exists",
                "No step is awaiting_ai_triage or failed",
            ],
        }
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = now_iso()
        write_json(self.path, self.data)

    def event(self, message: str) -> None:
        self.data.setdefault("events", []).append({"at": now_iso(), "message": message})
        self.save()

    def set_status(self, status: str, **extra: Any) -> None:
        self.data["status"] = status
        self.data.update(extra)
        self.save()

    def step(self, name: str, status: str, **extra: Any) -> None:
        steps = self.data.setdefault("steps", {})
        current = steps.setdefault(name, {})
        current.update({"status": status, "updated_at": now_iso(), **extra})
        if status == "running" and "started_at" not in current:
            current["started_at"] = now_iso()
        if status in {"ok", "failed", "timeout", "skipped", "awaiting_ai_triage"}:
            current.setdefault("finished_at", now_iso())
        self.save()


# ---------------------------------------------------------------------------
# Process execution with timeouts that kill child process groups


def run_cmd(
    name: str,
    cmd: list[str],
    log_path: pathlib.Path,
    cwd: pathlib.Path = ROOT,
    env: dict[str, str] | None = None,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    started = time.time()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(cmd)}\n")
        log.write(f"started_at={now_iso()} timeout_s={timeout_s}\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        timed_out = False
        while True:
            code = proc.poll()
            if code is not None:
                break
            if timeout_s and (time.time() - started) > timeout_s:
                timed_out = True
                log.write(f"\nTIMEOUT after {timeout_s}s; terminating process group {proc.pid}\n")
                log.flush()
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    log.write("Process group did not terminate; sending SIGKILL\n")
                    log.flush()
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait(timeout=10)
                code = 124
                break
            time.sleep(1)
        ended = time.time()
        log.write(f"\nfinished_at={now_iso()} exit_code={code} elapsed_s={int(ended-started)} timed_out={timed_out}\n")
    return {
        "step_name": name,
        "cmd": cmd,
        "log": str(log_path),
        "exit_code": int(code or 0),
        "timed_out": timed_out,
        "elapsed_s": int(time.time() - started),
    }


# ---------------------------------------------------------------------------
# Browser and source scans


def cdp_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:9223/json/version", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def prepare_browser_env(env: dict[str, str]) -> dict[str, str]:
    """Ensure GUI browser env works from cron/Hermes shells.

    Chrome Remote Desktop provides X on :20, but non-interactive shells may have
    DISPLAY set to an empty string. os.environ.get("DISPLAY", ":20") preserves
    that empty value, so Chromium starts without a display and fails before CDP
    can bind :9223.
    """
    env = env.copy()
    env["DISPLAY"] = env.get("DISPLAY") or ":20"
    if not env.get("XAUTHORITY"):
        xauthority = pathlib.Path.home() / ".Xauthority"
        if xauthority.exists():
            env["XAUTHORITY"] = str(xauthority)
    return env


def ensure_browser(state: RunState, log_dir: pathlib.Path, env: dict[str, str]) -> None:
    state.step("browser", "running")
    if cdp_ready():
        state.step("browser", "ok", note="Chromium CDP already available on :9223")
        return

    chromium_log = log_dir / "chromium.log"
    append_log(chromium_log, f"starting Yogev Chromium at {now_iso()}")
    subprocess.Popen(
        [str(ROOT / "scripts" / "yogev-chromium")],
        cwd=str(ROOT),
        env=env,
        stdout=chromium_log.open("a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(45):
        if cdp_ready():
            state.step("browser", "ok", note="Chromium CDP became ready on :9223", log=str(chromium_log))
            return
        time.sleep(1)
    state.step("browser", "failed", error="Chromium CDP did not become ready on :9223", log=str(chromium_log))
    raise RuntimeError("Chromium CDP did not become ready on :9223")


def _find_running_scan() -> str | None:
    """Return the run_id of an in-progress scan, or None if all scans are finished."""
    if not FULL_RUNS.exists():
        return None
    # Find the most recent run directory
    run_dirs = sorted(FULL_RUNS.iterdir(), key=lambda d: d.name, reverse=True)
    if not run_dirs:
        return None
    latest = run_dirs[0]
    state_path = latest / "state.json"
    if not state_path.exists():
        return None
    try:
        with open(state_path) as f:
            state = json.load(f)
        status = state.get("status", "")
        if status in ("running_collection", "awaiting_ai_triage"):
            # Check if Facebook step is still running (not ok/skipped)
            steps = state.get("steps", {})
            fb = steps.get("facebook", {})
            if fb.get("status") == "running":
                return latest.name
            # Also check if the overall status is still running
            if status == "running_collection":
                return latest.name
    except (json.JSONDecodeError, OSError):
        pass
    return None


def run_collection(args: argparse.Namespace) -> pathlib.Path:
    # Concurrency guard: skip if a previous scan is still running
    prev_run = _find_running_scan()
    if prev_run is not None:
        print(json.dumps({
            "status": "skipped",
            "reason": "previous_scan_still_running",
            "previous_run_id": prev_run,
        }, ensure_ascii=False), flush=True)
        raise SystemExit(0)

    run_id = args.run_id or run_id_now()
    scan_date = args.scan_date or scan_date_now()
    run_dir = FULL_RUNS / run_id
    log_dir = run_dir / "logs"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    state = RunState(run_dir, scan_date, run_id)
    state.set_status("running_collection")
    _emit_event("scan_started", run_id=run_id, scan_date=scan_date)

    env = os.environ.copy()
    env["YOGEV_SCAN_DATE"] = scan_date
    env["YOGEV_RUN_ID"] = run_id
    env = prepare_browser_env(env)

    try:
        ensure_browser(state, log_dir, env)
    except RuntimeError as e:
        state.set_status("failed", reason="browser_startup_failed", error=str(e))
        _emit_event("step_failed", run_id=run_id, step="browser",
                    reason="browser_startup_failed", error=str(e))
        print(json.dumps({"status": "failed", "reason": "browser_startup_failed", "error": str(e)},
                         ensure_ascii=False), flush=True)
        raise

    # Yad2 uses the shared browser; Madlan public scan is HTTP-only. Run Madlan
    # first (quick), then Yad2, then Facebook. This avoids Yad2/Facebook racing
    # each other over the same CDP browser and makes failures easier to reason about.
    # NOTE: Madlan is currently disabled — PerimeterX/פרימטר bot detection blocks
    # headless Chromium with CAPTCHA ("סליחה על ההפרעה..."). Even CDP browser
    # is detected as a robot. Re-enabling requires solving the CAPTCHA or
    # using a different approach (proxies, API reverse-engineering).
    # To re-enable, remove the "if False:" line below.
    if False:
        state.step("madlan", "running")
        madlan = run_cmd(
            "madlan",
            [sys.executable, str(ROOT / "scripts" / "scrape_madlan_public.py")],
            log_dir / "madlan.log",
            env=env,
            timeout_s=args.madlan_timeout,
        )
        state.step("madlan", "ok" if madlan["exit_code"] == 0 else "failed", **madlan)
        if madlan["exit_code"] == 0:
            _emit_event("step_ok", run_id=run_id, step="madlan", exit_code=0)
        else:
            _emit_event("step_failed", run_id=run_id, step="madlan",
                        reason=f"exit_code={madlan['exit_code']}", exit_code=madlan["exit_code"])
    else:
        state.step("madlan", "skipped", note="Madlan disabled due to PerimeterX bot detection / CAPTCHA")
        _emit_event("step_skipped", run_id=run_id, step="madlan", reason="PerimeterX bot detection")

    state.step("yad2", "running")
    yad2 = run_cmd(
        "yad2",
        [sys.executable, str(ROOT / "scripts" / "yad2_broad_search.py")],
        log_dir / "yad2.log",
        env=env,
        timeout_s=args.yad2_timeout,
    )
    state.step("yad2", "ok" if yad2["exit_code"] == 0 else "failed", **yad2)

    if yad2["exit_code"] == 2:
        broad_art = ART / f"broad_search_{run_id}"
        block_state_path = broad_art / "block_state.json"
        block_info = load_json(block_state_path, {}) if block_state_path.exists() else {}
        resume_cmd = f"python3 scripts/full_apartment_scan.py resume --run-dir {run_dir}"
        state.set_status(
            "waiting_for_human",
            blocked_step="yad2",
            blocked_url=block_info.get("url", block_info.get("blocked_href", "")),
            block_type=block_info.get("block_type", "unknown"),
            resume_cmd=resume_cmd,
            instruction="Open the Yogev browser, solve the CAPTCHA/block at the blocked URL, then run resume_cmd",
        )
        _emit_event(
            "step_blocked",
            run_id=run_id,
            step="yad2",
            block_type=block_info.get("block_type", "unknown"),
            blocked_url=block_info.get("url", block_info.get("blocked_href", "")),
            resume_cmd=resume_cmd,
        )
        print(json.dumps({"status": "waiting_for_human", "resume_cmd": resume_cmd}, ensure_ascii=False))
        return run_dir

    fb_timeout = args.facebook_timeout
    state.step("facebook", "running", scrolls=args.facebook_scrolls, delay=args.facebook_delay, timeout_s=fb_timeout)
    facebook = run_cmd(
        "facebook",
        [
            sys.executable,
            str(ROOT / "scripts" / "daily_scan.py"),
            "--scrolls",
            str(args.facebook_scrolls),
            "--delay",
            str(args.facebook_delay),
        ],
        log_dir / "facebook.log",
        env=env,
        timeout_s=fb_timeout,
    )

    if facebook["exit_code"] == 124 and args.facebook_retry_scrolls > 0:
        state.step("facebook", "timeout", **facebook, retry_scrolls=args.facebook_retry_scrolls)
        state.event(
            f"Facebook scan timed out after {fb_timeout}s; retrying with {args.facebook_retry_scrolls} scrolls"
        )
        state.step("facebook_retry", "running", scrolls=args.facebook_retry_scrolls)
        retry = run_cmd(
            "facebook_retry",
            [
                sys.executable,
                str(ROOT / "scripts" / "daily_scan.py"),
                "--scrolls",
                str(args.facebook_retry_scrolls),
                "--delay",
                str(args.facebook_delay),
            ],
            log_dir / "facebook_retry.log",
            env=env,
            timeout_s=args.facebook_retry_timeout,
        )
        state.step("facebook_retry", "ok" if retry["exit_code"] == 0 else "failed", **retry)
        facebook = retry
        # Mark facebook step as ok if retry succeeded (instead of leaving it as "timeout")
        if retry["exit_code"] == 0:
            state.step("facebook", "ok", **facebook)
    else:
        state.step("facebook", "ok" if facebook["exit_code"] == 0 else "failed", **facebook)

    fb_exit = facebook["exit_code"]
    if fb_exit == 0:
        _emit_event("step_ok", run_id=run_id, step="facebook", exit_code=0)
    else:
        _emit_event("step_failed", run_id=run_id, step="facebook",
                    reason=f"exit_code={fb_exit}", exit_code=fb_exit)

    manifest = build_manifest(run_dir, scan_date, state)
    write_json(run_dir / "manifest.json", manifest)

    overall = manifest.get("overall_status", "failed")
    state.set_status(overall, manifest=str(run_dir / "manifest.json"))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return run_dir


# ---------------------------------------------------------------------------
# Manifest and report generation


def latest_facebook_run() -> dict[str, Any] | None:
    hist = load_json(FACEBOOK_ART / "run_history.json", {"runs": []}) or {"runs": []}
    runs = hist.get("runs") or []
    return runs[-1] if runs else None


def facebook_scan_health(fb: dict[str, Any], *, min_ok_groups: int = 5, max_error_rate: float = 0.5) -> dict[str, Any]:
    """Summarize whether the Facebook multi-group scan actually covered enough groups."""
    scan_file = fb.get("scan_file")
    data = load_json(pathlib.Path(scan_file), {}) if scan_file else {}
    runs = (((data or {}).get("source_meta") or {}).get("runs") or (data or {}).get("runs") or [])
    ok_groups = 0
    error_groups = 0
    blocked_groups = 0
    for run in runs:
        if run.get("error"):
            error_groups += 1
        elif run.get("blocked"):
            blocked_groups += 1
        else:
            ok_groups += 1
    total_groups = ok_groups + error_groups + blocked_groups
    error_rate = (error_groups / total_groups) if total_groups else 1.0
    status = "ok"
    reason = None
    if total_groups == 0:
        status = "degraded"
        reason = "No Facebook group scan metadata found"
    elif ok_groups < min_ok_groups:
        status = "degraded"
        reason = f"Too few Facebook groups scanned successfully: {ok_groups}/{total_groups}"
    elif error_rate > max_error_rate:
        status = "degraded"
        reason = f"Too many Facebook group errors: {error_groups}/{total_groups}"
    return {
        "status": status,
        "reason": reason,
        "total_groups": total_groups,
        "ok_groups": ok_groups,
        "error_groups": error_groups,
        "blocked_groups": blocked_groups,
        "error_rate": round(error_rate, 3),
    }


def build_manifest(run_dir: pathlib.Path, scan_date: str, state: RunState | None = None) -> dict[str, Any]:
    broad = ART / f"broad_search_{run_dir.name}"
    steps = (state.data.get("steps") if state else load_json(run_dir / "state.json", {}).get("steps")) or {}
    exits = {
        name: (steps.get(name) or {}).get("exit_code")
        for name in ["madlan", "yad2", "facebook", "facebook_retry"]
        if name in steps
    }

    manifest: dict[str, Any] = {
        "scan_date": scan_date,
        "run_dir": str(run_dir),
        "created_at": now_iso(),
        "state_file": str(run_dir / "state.json"),
        "logs_dir": str(run_dir / "logs"),
        "step_exits": exits,
        "broad_artifact_dir": str(broad),
        "facebook_history": str(FACEBOOK_ART / "run_history.json"),
    }

    summary = load_json(broad / "summary.json")
    if summary:
        manifest["yad2"] = {
            "targets": summary.get("targets", []),
            "finished": summary.get("finished"),
            "details_file": str(broad / "yad2_broad_details.json"),
            "details_count": len(load_json(broad / "yad2_broad_details.json", []) or []),
        }

    madlan = load_json(broad / "madlan_public_scan.json")
    if madlan:
        manifest["madlan"] = {
            "items": len(madlan.get("items", [])),
            "candidates": len(madlan.get("candidates", [])),
            "fetched_at": madlan.get("fetched_at"),
            "file": str(broad / "madlan_public_scan.json"),
        }

    fb = latest_facebook_run()
    facebook_health = None
    if fb:
        manifest["facebook_last_run"] = fb
        facebook_health = facebook_scan_health(fb)
        manifest["facebook_health"] = facebook_health
        triage_json_raw = fb.get("triage_output_json")
        triage_json = pathlib.Path(triage_json_raw) if triage_json_raw else None
        if triage_json and triage_json.exists():
            triage = load_json(triage_json, {}) or {}
            manifest["facebook_triage"] = {
                "status": "completed",
                "file": str(triage_json),
                "md_file": fb.get("triage_output_md"),
                "summary": triage.get("summary", {}),
            }
        elif fb.get("needs_triage", 0) > 0:
            manifest["facebook_triage"] = {
                "status": "awaiting_ai_triage",
                "needs_triage": fb.get("needs_triage"),
                "prompt_file": fb.get("prompt_file"),
                "input_file": fb.get("ai_input_file"),
                "expected_output_json": fb.get("triage_output_json"),
            }

    failed_steps = [name for name, data in steps.items() if data.get("status") in {"failed", "timeout"}]
    pending_steps = [name for name, data in steps.items() if data.get("status") == "awaiting_ai_triage"]
    if facebook_health and facebook_health.get("status") == "degraded":
        failed_steps.append("facebook_health")
    fb_triage_status = (manifest.get("facebook_triage") or {}).get("status")
    if failed_steps:
        overall = "failed"
    elif pending_steps or fb_triage_status == "awaiting_ai_triage":
        overall = "awaiting_ai_triage"
    elif fb_triage_status == "completed" or (manifest.get("facebook_last_run") or {}).get("needs_triage") == 0:
        overall = "completed"
    else:
        overall = "collection_completed"
    manifest["overall_status"] = overall
    manifest["failed_steps"] = failed_steps
    manifest["pending_steps"] = pending_steps
    return manifest


def parse_yad2_entry(item: dict[str, Any]) -> dict[str, Any]:
    detail = item.get("detail_text") or ""
    text = item.get("text") or ""
    entry_match = re.search(r"תאריך כניסה\s*([^\n]{1,60})", detail)
    if not entry_match:
        entry_match = re.search(r"תאריך כניסה(.{0,55})", detail)
    entry = short(entry_match.group(1), 55) if entry_match else None
    entry_context = " ".join([entry or "", detail[:1200], text[:500]])

    fees = []
    for label in ["ועד בית", "ארנונה"]:
        m = re.search(label + r"[^0-9]{0,20}([0-9,]{2,6})", detail)
        if m:
            fees.append(f"{label}: ₪{m.group(1)}")

    desc_lines = [l.strip() for l in detail.splitlines() if l.strip()]
    description = " ".join(desc_lines[:22])
    no_broker = "ללא תיווך" in detail or "ללא דמי תיווך" in detail or "ללא תיווך" in text
    is_broker = (not no_broker) and ("תיווך" in detail or "Real Estate" in text or "קבוצת" in text)
    # Check early entry only in the extracted entry field, not in full detail text
    # (detail text contains publish dates like "11/05/26" which falsely trigger "05/")
    entry_for_check = (entry or "").lower()
    early_entry = bool(any(term in entry_for_check for term in ["מיד", "מייד", "04/", "05/", "06/", "מאי", "יוני"]))
    price = item.get("feed_price")
    rooms = item.get("feed_rooms")
    sqm = item.get("feed_sqm")
    flags = []
    if early_entry:
        flags.append("כניסה מיידית/מוקדמת — נפסל כרגע")
    if is_broker:
        flags.append("ייתכן תיווך")
    if price and price >= 6500:
        flags.append("בקצה התקציב")
    if sqm and sqm < 65:
        flags.append("קטנה יחסית")
    if "במצב שמור" in detail:
        flags.append("מצב שמור, לא בהכרח משופצת")

    pros = []
    if rooms and rooms >= 3:
        pros.append(f"{fmt_rooms(rooms)} חדרים")
    if sqm and sqm >= 70:
        pros.append(f"{sqm} מ״ר")
    if "עורפ" in detail:
        pros.append("עורפית")
    if "משופצ" in detail:
        pros.append("משופצת/שופצה")
    if "מזגן" in detail or "מיזוג" in detail:
        pros.append("יש אינדיקציה למיזוג")
    if "מעלית" in detail:
        pros.append("מעלית")
    if "מקלט" in detail or "ממ\"ד" in detail or "ממ\"ק" in detail:
        pros.append("מקלט/מרחב מוגן")
    if "ללא דמי תיווך" in detail or "ללא תיווך" in detail:
        pros.append("ללא תיווך")

    missing = [
        "רטיבות/עובש/נזילות",
        "רעש בפועל",
        "מספר מזגנים ובאילו חדרים",
        "אפשרות לטווח ארוך",
    ]
    if early_entry:
        missing.insert(0, "לא לבדוק כרגע: כניסה מוקדמת")

    verdict = "לא" if early_entry else ("כן" if price and price <= 6500 and rooms and rooms >= 3 and sqm and sqm >= 70 else "אולי")
    title = short(text, 90)
    price_tail_parts = re.split(r"₪\s*[0-9,]+", text)
    if price_tail_parts:
        tail = price_tail_parts[-1]
        m_title = re.search(r"\s*([^•]+?)\s+דירה[, ]", tail)
        if m_title:
            title = short(m_title.group(1).strip(), 90)

    return {
        "source": "Yad2",
        "title": title,
        "url": item.get("href"),
        "price": price,
        "rooms": rooms,
        "sqm": sqm,
        "floor": item.get("feed_floor"),
        "score": item.get("feed_score") or 0,
        "entry": entry,
        "pros": pros[:6],
        "flags": flags[:5],
        "missing": missing[:5],
        "description": short(description, 360),
        "verdict": verdict,
        "fees": fees,
    }


def madlan_entry(item: dict[str, Any]) -> dict[str, Any]:
    price = item.get("price")
    rooms = item.get("rooms")
    sqm = item.get("sqm")
    notes = item.get("notes") or []
    flags = list(notes)
    if price and price >= 6500:
        flags.append("בקצה התקציב")
    if sqm and sqm < 65:
        flags.append("קטנה יחסית")
    pros = []
    if rooms and rooms >= 3:
        pros.append(f"{fmt_rooms(rooms)} חדרים")
    if sqm and sqm >= 70:
        pros.append(f"{sqm} מ״ר")
    return {
        "source": "Madlan",
        "title": item.get("address") or item.get("source_label"),
        "url": item.get("url"),
        "price": price,
        "rooms": rooms,
        "sqm": sqm,
        "floor": item.get("floor"),
        "score": item.get("score") or 0,
        "pros": pros,
        "flags": flags[:4] or ["כרטיס מדלן בלבד — צריך לפתוח/לאמת פרטים"],
        "missing": ["תיאור מלא", "רטיבות/רעש/מזגנים", "תאריך כניסה", "טווח ארוך"],
        "verdict": "אולי",
    }


def facebook_post_url_is_valid(url: str | None) -> bool:
    return classify_facebook_url(url) == "valid_post" or bool(
        url and "facebook.com/permalink.php" in url and "story_fbid=" in url and "id=" in url
    )


def facebook_text_has_broker_signal(text: str) -> bool:
    t = (text or "").lower()
    broker_terms = [
        "תיווך", "מתווך", "מתווכ", "re/max", "remax", "broker", "realtor",
        "real estate", "property", "נדל\"ן", "נדלן", "דמי תיווך", "license number", "רישיון תיווך",
    ]
    no_broker_terms = ["ללא תיווך", "בלי תיווך", "without mediation", "no broker", "without broker"]
    if any(term in t for term in no_broker_terms):
        return False
    return any(term in t for term in broker_terms)


def facebook_text_is_office_or_sale(text: str) -> bool:
    t = (text or "").lower()
    office_signals = ["משרד", "קליניק", "עורכי דין", "רואי חשבון", "+ מע\"מ", "+ מעמ", "מקצועות חופשיים"]
    sale_signals = [
        "למכירה", "for sale", "sellers", "seller", "buy", "asking price",
        "for investment", "investment or residence", "written in the taboo", "taboo", "טאבו",
        "price reduction", "reduced from", "million", "the master of the real estate",
        "2,500,000", "4,200,000",
        "מחיר שיווק", "פרויקט", "קבלן", "השקעה", "למגורים או השקעה", "מיליון", "ירידת מחיר", "בטאבו", "אטבו",
    ]
    return any(s in t for s in office_signals) or any(s in t for s in sale_signals)


def facebook_entry(item: dict[str, Any]) -> dict[str, Any]:
    ex = item.get("extracted") or {}
    verdict = item.get("verdict") or "maybe"
    confidence_rank = {"high": 3, "medium": 2, "low": 1}.get(item.get("confidence"), 0)
    price = ex.get("price")
    rooms = ex.get("rooms")
    sqm = ex.get("sqm")
    url_bundle = normalize_facebook_item_urls(item)
    # If we cannot reconstruct a direct post link, do not show profile/group URLs
    # as if they were post links.
    url = url_bundle.get("universal_post_url")
    text = str(item.get("text") or "")
    flags = list(item.get("cons") or [])
    hard_reject = False

    if not facebook_post_url_is_valid(url):
        flags.append("אין לינק ישיר לפוסט פייסבוק")
        hard_reject = True
    if facebook_text_is_office_or_sale(text):
        flags.append("לא דירת מגורים")
        hard_reject = True
    broker = facebook_text_has_broker_signal(text)
    if broker:
        if isinstance(price, (int, float)) and price >= 6500:
            flags.append("תיווך במחיר 6,500 — דיל ברייקר")
            hard_reject = True
        else:
            flags.append("תיווך — לבדוק רק אם המחיר מצדיק")

    score = 1000 if verdict == "yes" else 500
    score += confidence_rank * 10
    if isinstance(price, (int, float)):
        score += 18 if price <= 6500 else -12
    if isinstance(rooms, (int, float)):
        score += 10 if rooms >= 3 else 2 if rooms >= 2.5 else -20
    if isinstance(sqm, (int, float)):
        score += 10 if sqm >= 70 else 4 if sqm >= 60 else -8
    if hard_reject:
        score -= 2000

    report_verdict = "כן" if verdict == "yes" else "אולי" if verdict == "maybe" else "לא"
    if hard_reject:
        report_verdict = "לא"

    return {
        "source": "Facebook",
        "title": ex.get("address") or ex.get("area") or item.get("id"),
        "url": url,
        "desktop_post_url": url_bundle.get("desktop_post_url"),
        "url_status": url_bundle.get("url_status"),
        "price": price,
        "rooms": rooms,
        "sqm": sqm,
        "floor": None,
        "score": score,
        "entry": ex.get("entry"),
        "pros": item.get("pros") or [],
        "flags": flags,
        "missing": item.get("missing") or [],
        "followup_needed": item.get("followup_needed") or [],
        "description": item.get("reason_short"),
        "verdict": report_verdict,
        "confidence": item.get("confidence"),
    }


def dedupe_similar_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse obvious cross-post duplicates for report readability."""
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for e in entries:
        title = re.sub(r"\s+", " ", str(e.get("title") or "")).strip().lower()
        title = re.sub(r"^(רחוב\s+)", "", title)
        key = (e.get("source"), title, e.get("price"), e.get("rooms"), e.get("sqm"))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _is_user_rejected(item: dict[str, Any]) -> bool:
    """Return True when the item matches a permanent user rejection.

    Final reports are read directly by Dror, not only through cron_load_results.
    Keep this filter here too so rejected listings cannot reappear in the
    Markdown report or assistant_brief.
    """
    try:
        from user_rejections import is_rejected
    except Exception:
        return False
    candidate = dict(item)
    if candidate.get("canonical_url") and not candidate.get("url"):
        candidate["url"] = candidate.get("canonical_url")
    try:
        rejected, _record = is_rejected(candidate)
        return bool(rejected)
    except Exception:
        return False


def _filter_user_rejected(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if not _is_user_rejected(item)]


def entry_md(e: dict[str, Any]) -> list[str]:
    lines = []
    lines.append(f"### {e.get('title') or 'ללא כותרת'}")
    lines.append(f"- מקור / אתר: {e.get('source')}")
    lines.append(f"- לינק: {e.get('url') or 'לא צוין'}")
    lines.append(f"- אזור: {e.get('location_status') or 'unknown'}")
    lines.append(f"- מחיר: {money(e.get('price'))}")
    lines.append(
        f"- חדרים / מ״ר / קומה: {fmt_rooms(e.get('rooms'))} / {e.get('sqm') or 'לא צוין'} / {e.get('floor') or 'לא צוין'}"
    )
    if e.get("entry"):
        lines.append(f"- כניסה: {e.get('entry')}")
    if e.get("description"):
        lines.append(f"- תקציר: {short(e.get('description'), 260)}")
    lines.append(f"- יתרונות: {', '.join(dict.fromkeys(e.get('pros') or [])) or 'אין מספיק מידע'}")
    lines.append(f"- חסרונות / דגלים: {', '.join(dict.fromkeys(e.get('flags') or [])) or 'אין דגל ברור מהמודעה'}")
    lines.append(f"- מידע חסר: {', '.join(dict.fromkeys(e.get('missing') or [])) or 'אין'}")
    if e.get("followup_needed"):
        lines.append(f"- שאלות המשך: {' / '.join(e.get('followup_needed')[:4])}")
    lines.append(f"- האם שווה להמשיך לבדוק: {e.get('verdict')}")
    return lines


def _enrich_brief_from_evaluation(brief_path: pathlib.Path, run_dir: pathlib.Path) -> None:
    """Enrich assistant_brief.json with evaluation data.

    Replaces top_candidates with evaluated items that have recommended_action
    in (open, ask_price, ask_if_late_july_possible, ask_if_broker_acceptable).
    Adds evaluation_summary with stats.
    """
    if not brief_path.exists():
        return
    eval_path = run_dir / "evaluated_candidates.json"
    if not eval_path.exists():
        return
    brief = load_json(brief_path, {}) or {}
    eval_data = load_json(eval_path, {}) or {}
    eval_items = _filter_user_rejected(eval_data.get("items") or [])

    # Filter to actionable items only, and keep the "open now" shortlist strict on entry date.
    actionable = [i for i in eval_items if i.get("recommended_action") in (
        "open", "ask_price", "ask_if_late_july_possible", "ask_if_broker_acceptable"
    ) and i.get("listing_type") == "rental_apartment"]
    open_now = [i for i in actionable if i.get("entry_status") == "ideal_july_august" and i.get("location_status") == "primary" and (
        i.get("recommended_action") == "open"
        or (i.get("recommended_action") == "ask_if_broker_acceptable" and (i.get("price") or 999999) <= 6200)
    )]
    followup = [i for i in actionable if i.get("entry_status") in {"june_maybe_if_later", "unknown_entry"} and i.get("location_status") in {"primary", "secondary"}]
    # Sort: open first, then by price
    actionable.sort(key=lambda x: (0 if x.get("recommended_action") == "open" else 1, x.get("price") or 99999))
    open_now.sort(key=lambda x: (x.get("price") or 99999, -(x.get("rooms") or 0)))
    followup.sort(key=lambda x: (0 if x.get("recommended_action") == "ask_if_late_july_possible" else 1, x.get("price") or 99999))

    manual_entry_check = [
        i for i in eval_items
        if i.get("listing_type") == "rental_apartment" and i.get("recommended_action") == "manual_check_entry_with_source"
    ]
    manual_entry_check.sort(key=lambda x: (x.get("price") or 99999, -(x.get("rooms") or 0)))

    brief["top_candidates_evaluated"] = [{
        "source": i.get("source"),
        "url": i.get("canonical_url"),
        "price": i.get("price"),
        "rooms": i.get("rooms"),
        "entry_status": i.get("entry_status"),
        "broker_status": i.get("broker_status"),
        "location_status": i.get("location_status"),
        "location_evidence": i.get("location_evidence"),
        "quality_status": i.get("quality_status"),
        "recommended_action": i.get("recommended_action"),
        "reject_reasons": i.get("reject_reasons", []),
        "flags": i.get("flags", []),
        "missing": i.get("missing", []),
        "followup_question": i.get("followup_question"),
    } for i in open_now[:20]]
    brief["followup_candidates_evaluated"] = [{
        "source": i.get("source"),
        "url": i.get("canonical_url"),
        "price": i.get("price"),
        "rooms": i.get("rooms"),
        "entry_status": i.get("entry_status"),
        "broker_status": i.get("broker_status"),
        "location_status": i.get("location_status"),
        "location_evidence": i.get("location_evidence"),
        "quality_status": i.get("quality_status"),
        "recommended_action": i.get("recommended_action"),
        "reject_reasons": i.get("reject_reasons", []),
        "flags": i.get("flags", []),
        "missing": i.get("missing", []),
        "followup_question": i.get("followup_question"),
    } for i in followup[:20]]

    brief["manual_entry_check_candidates"] = [{
        "source": i.get("source"),
        "url": i.get("canonical_url"),
        "price": i.get("price"),
        "rooms": i.get("rooms"),
        "entry_status": i.get("entry_status"),
        "broker_status": i.get("broker_status"),
        "location_status": i.get("location_status"),
        "location_evidence": i.get("location_evidence"),
        "quality_status": i.get("quality_status"),
        "recommended_action": i.get("recommended_action"),
        "reject_reasons": i.get("reject_reasons", []),
        "flags": i.get("flags", []),
        "missing": i.get("missing", []),
        "followup_question": i.get("followup_question"),
    } for i in manual_entry_check[:5]]

    brief["evaluation_summary"] = {
        **(eval_data.get("stats", {}) or {}),
        "manual_entry_check_count": len(manual_entry_check),
    }
    write_json(brief_path, brief)


def generate_unified_report(run_dir: pathlib.Path, manifest: dict[str, Any]) -> pathlib.Path:
    scan_date = manifest.get("scan_date") or scan_date_now()
    broad = pathlib.Path(manifest.get("broad_artifact_dir") or ART / f"broad_search_{run_dir.name}")

    yad2_raw = load_json(broad / "yad2_broad_details.json", []) or []
    yad2_entries = _filter_user_rejected([parse_yad2_entry(x) for x in yad2_raw if not x.get("blocked")])
    yad2_entries.sort(key=lambda e: (e.get("verdict") != "כן", -(e.get("score") or 0), -(e.get("sqm") or 0)))

    madlan_raw = (load_json(broad / "madlan_public_scan.json", {}) or {}).get("candidates") or []
    madlan_entries = _filter_user_rejected([madlan_entry(x) for x in madlan_raw])
    madlan_entries.sort(key=lambda e: (-(e.get("score") or 0), -(e.get("sqm") or 0), e.get("price") or 999999))

    fb_entries: list[dict[str, Any]] = []
    fb_source_posts_by_id: dict[str, dict[str, Any]] = {}
    fb_input_file = (manifest.get("facebook_last_run") or {}).get("ai_input_file")
    fb_input_path = pathlib.Path(fb_input_file) if fb_input_file else None
    if fb_input_path and fb_input_path.is_file():
        fb_input = load_json(fb_input_path, {}) or {}
        fb_source_posts_by_id = {str(p.get("id")): p for p in (fb_input.get("posts") or []) if p.get("id")}

    fb_triage = manifest.get("facebook_triage") or {}
    triage_file = fb_triage.get("file")
    triage_path = pathlib.Path(triage_file) if triage_file else None
    if triage_path and triage_path.is_file():
        triage = load_json(triage_path, {}) or {}
        for item in triage.get("items") or []:
            if item.get("verdict") in {"yes", "maybe"}:
                source_post = fb_source_posts_by_id.get(str(item.get("id"))) or {}
                enriched_item = dict(item)
                if source_post.get("text") and not enriched_item.get("text"):
                    enriched_item["text"] = source_post.get("text")
                if source_post.get("post_url") and not enriched_item.get("post_url"):
                    enriched_item["post_url"] = source_post.get("post_url")
                for url_field in (
                    "desktop_post_url", "mobile_post_url", "permalink_url",
                    "mobile_permalink_url", "universal_post_url", "url_status",
                ):
                    if source_post.get(url_field) and not enriched_item.get(url_field):
                        enriched_item[url_field] = source_post.get(url_field)
                fb_entries.append(facebook_entry(enriched_item))
    fb_entries.sort(key=lambda e: (e.get("verdict") != "כן", -(e.get("score") or 0), e.get("price") or 999999))
    fb_entries = _filter_user_rejected(dedupe_similar_entries(fb_entries))

    eval_path = run_dir / "evaluated_candidates.json"
    eval_data = load_json(eval_path, {}) or {}
    eval_items = _filter_user_rejected(eval_data.get("items") or [])
    eval_by_url: dict[str, dict[str, Any]] = {}
    for item in eval_items:
        url = item.get("canonical_url") or item.get("url")
        if url:
            eval_by_url[str(url)] = item

    def _strict_allow(item: dict[str, Any]) -> bool:
        eval_item = eval_by_url.get(str(item.get("url") or ""))
        if not eval_item:
            return False
        action = eval_item.get("recommended_action")
        price = eval_item.get("price")
        broker_exception = action == "ask_if_broker_acceptable" and isinstance(price, (int, float)) and price <= 6200
        return (
            eval_item.get("listing_type") == "rental_apartment"
            and eval_item.get("quality_status") in {"candidate", "needs_review"}
            and eval_item.get("location_status") == "primary"
            and (action == "open" or broker_exception)
            and eval_item.get("entry_status") == "ideal_july_august"
        )

    def _followup_allow(item: dict[str, Any]) -> bool:
        eval_item = eval_by_url.get(str(item.get("url") or ""))
        if not eval_item:
            return False
        return (
            eval_item.get("listing_type") == "rental_apartment"
            and eval_item.get("quality_status") in {"candidate", "needs_review"}
            and eval_item.get("location_status") in {"primary", "secondary"}
            and eval_item.get("recommended_action") in {"ask_if_late_july_possible", "ask_price", "ask_if_broker_acceptable"}
            and eval_item.get("entry_status") in {"june_maybe_if_later"}
        )

    def _augment_display(item: dict[str, Any]) -> dict[str, Any]:
        eval_item = eval_by_url.get(str(item.get("url") or ""))
        if not eval_item:
            return item
        enriched = dict(item)
        if eval_item.get("broker_status") == "broker":
            flags = list(enriched.get("flags") or [])
            broker_flag = "תיווך — חריג לבדיקה רק בגלל מחיר נמוך; לוודא עמלה"
            if broker_flag not in flags:
                flags.append(broker_flag)
            enriched["flags"] = flags
        if not enriched.get("entry"):
            raw_entry = eval_item.get("entry_raw") or eval_item.get("entry")
            if raw_entry:
                enriched["entry"] = raw_entry
            else:
                status = eval_item.get("entry_status")
                if status == "ideal_july_august":
                    enriched["entry"] = "סוף יולי / תחילת אוגוסט"
                elif status == "june_maybe_if_later":
                    enriched["entry"] = "יוני — רק אם אפשר להתחיל בסוף יולי"
                elif status == "unknown_entry":
                    enriched["entry"] = "לא צוין"
        return enriched

    top = []
    top.extend([_augment_display(e) for e in yad2_entries if _strict_allow(e)][:6])
    top.extend([_augment_display(e) for e in fb_entries if _strict_allow(e)][:6])
    top.extend([_augment_display(e) for e in madlan_entries if _strict_allow(e)][:8])
    top.sort(key=lambda e: (e.get("verdict") != "כן", -(e.get("score") or 0), e.get("price") or 999999))

    fb_followup = [_augment_display(e) for e in fb_entries if _followup_allow(e)]
    fb_followup.sort(key=lambda e: (e.get("verdict") == "לא", -(e.get("score") or 0), e.get("price") or 999999))

    madlan_followup = [_augment_display(e) for e in madlan_entries if _followup_allow(e)]
    madlan_followup.sort(key=lambda e: (-(e.get("score") or 0), -(e.get("sqm") or 0), e.get("price") or 999999))

    fb_summary = (manifest.get("facebook_triage") or {}).get("summary") or {}
    lines: list[str] = []
    lines.append(f"# דו״ח סריקת דירות מאוחד — {scan_date}")
    lines.append("")
    lines.append("## סטטוס")
    lines.append("")
    lines.append(f"- סטטוס כולל: **{manifest.get('overall_status')}**")
    yad2_count = manifest.get('yad2', {}).get('details_count')
    if yad2_count is None:
        yad2_count = len(yad2_entries)
    madlan_candidates = manifest.get('madlan', {}).get('candidates')
    if madlan_candidates is None:
        madlan_candidates = len(madlan_entries)
    madlan_items = manifest.get('madlan', {}).get('items')
    if madlan_items is None:
        madlan_items = len((load_json(broad / "madlan_public_scan.json", {}) or {}).get("items") or [])
    lines.append(f"- Yad2: {yad2_count} מועמדות שנפתחו")
    lines.append(f"- Madlan: {madlan_candidates} מועמדות מתוך {madlan_items} כרטיסים")
    if fb_summary:
        lines.append(
            f"- Facebook AI triage: {fb_summary.get('posts_reviewed', 0)} פוסטים — כן {fb_summary.get('relevant_yes', fb_summary.get('yes', 0))}, אולי {fb_summary.get('relevant_maybe', fb_summary.get('maybe', 0))}, לא {fb_summary.get('relevant_no', fb_summary.get('no', 0))}"
        )
    else:
        triage_status = (manifest.get("facebook_triage") or {}).get("status", "לא קיים")
        lines.append(f"- Facebook triage: {triage_status}")
    lines.append("")

    # Scan quality section
    lines.extend(scan_quality_section(manifest, broad, fb_input_path, triage_path))

    if manifest.get("overall_status") == "awaiting_ai_triage":
        triage = manifest.get("facebook_triage") or {}
        lines.append("## חסר לפני סיום")
        lines.append("")
        lines.append(f"- נשארו {triage.get('needs_triage')} פוסטים לסינון AI.")
        lines.append(f"- קובץ prompt: `{triage.get('prompt_file')}`")
        lines.append(f"- פלט צפוי: `{triage.get('expected_output_json')}`")
        lines.append("")

    lines.append("## מועמדות לפתיחה ראשונה")
    lines.append("")
    for e in top[:18]:
        lines.extend(entry_md(e))
        lines.append("")

    if fb_followup:
        lines.append("## Facebook — אולי רלוונטיות, בעיקר לבדוק גמישות כניסה")
        lines.append("")
        for e in fb_followup[:12]:
            lines.extend(entry_md(e))
            lines.append("")

    if madlan_followup:
        lines.append("## Madlan — כרטיסים גולמיים נוספים")
        lines.append("")
        lines.append("מדלן כאן פחות ודאי: אלה כרטיסים, לא תמיד פרטי מודעה מלאים.")
        lines.append("")
        for e in madlan_followup[:15]:
            lines.extend(entry_md(e))
            lines.append("")

    eval_items_for_report = _filter_user_rejected((load_json(run_dir / "evaluated_candidates.json", {}) or {}).get("items") or [])
    manual_entry_check = [
        i for i in eval_items_for_report
        if i.get("listing_type") == "rental_apartment"
        and i.get("recommended_action") == "manual_check_entry_with_source"
    ]
    manual_entry_check.sort(key=lambda x: (x.get("price") or 99999, -(x.get("rooms") or 0)))
    if manual_entry_check:
        lines.append("## תאריך כניסה — לבדיקה ידנית")
        lines.append("")
        lines.append("כאן נכנסים רק כרטיסים שבהם התאריך לא זוהה אחרי ניסיון חילוץ, ורוצים שתבדוק ידנית.")
        lines.append("")
        for e in manual_entry_check[:5]:
            lines.append(f"### {e.get('address') or e.get('source') or 'ללא כותרת'}")
            lines.append(f"- מקור / אתר: {e.get('source')}")
            lines.append(f"- לינק: {e.get('canonical_url') or 'לא צוין'}")
            lines.append(f"- מחיר: {money(e.get('price'))}")
            lines.append(f"- חדרים: {fmt_rooms(e.get('rooms'))}")
            lines.append(f"- אזור: {e.get('location_status') or 'unknown'}")
            lines.append(f"- תאריך כניסה: {e.get('entry_status') or 'unknown'}")
            lines.append(f"- למה כאן: {e.get('recommended_action')}")
            lines.append("")

    lines.append("## שאלות פתיחה מומלצות")
    lines.append("")
    lines.append("היי, תודה רבה :) הדירה נראית לנו רלוונטית. לפני שנתאם לראות, אפשר לשאול רק כמה דברים כדי לוודא שלא נגיע במיוחד סתם?")
    lines.append("")
    lines.append("1. האם יש רטיבות / עובש / נזילות?")
    lines.append("2. איך הרעש בפועל בדירה?")
    lines.append("3. כמה מזגנים יש ובאילו חדרים? יש מזגן/אפשרות בחדר העבודה?")
    lines.append("4. האם יש אפשרות לטווח ארוך / אופציה להארכה?")
    lines.append("5. אם הכניסה מיידית/מאי/יוני: האם יש גמישות ליולי/אוגוסט?")
    lines.append("")
    lines.append("## קבצי ראיות")
    lines.append("")
    lines.append(f"- Manifest: `{run_dir / 'manifest.json'}`")
    lines.append(f"- State: `{run_dir / 'state.json'}`")
    lines.append(f"- Yad2 details: `{broad / 'yad2_broad_details.json'}`")
    lines.append(f"- Madlan scan: `{broad / 'madlan_public_scan.json'}`")
    if triage_path and triage_path.exists():
        lines.append(f"- Facebook triage: `{triage_path}`")

    out = run_dir / "final_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")

    # Compact machine-readable brief for the Telegram agent. This keeps the next
    # response grounded without forcing the agent to re-read a long Markdown report.
    brief_candidates = []
    for e in top[:20]:
        brief_candidates.append({
            "source": e.get("source"),
            "title": e.get("title"),
            "url": e.get("url"),
            "price": e.get("price"),
            "rooms": e.get("rooms"),
            "sqm": e.get("sqm"),
            "floor": e.get("floor"),
            "entry": e.get("entry"),
            "location_status": e.get("location_status"),
            "verdict": e.get("verdict"),
            "score": e.get("score"),
            "pros": (e.get("pros") or [])[:5],
            "flags": (e.get("flags") or [])[:5],
            "missing": (e.get("missing") or [])[:5],
            "followup_needed": (e.get("followup_needed") or [])[:4],
            "description": short(e.get("description"), 220),
        })
    from scan_quality import analyze_yad2, analyze_madlan, analyze_facebook
    yad2_q = analyze_yad2(broad)
    madlan_q = analyze_madlan(broad)
    fb_q = analyze_facebook(manifest, fb_input_path, triage_path)

    brief = {
        "generated_at": now_iso(),
        "scan_date": scan_date,
        "status": manifest.get("overall_status"),
        "run_dir": str(run_dir),
        "final_report": str(out),
        "manifest": str(run_dir / "manifest.json"),
        "counts": {
            "yad2_opened": yad2_count,
            "madlan_candidates": madlan_candidates,
            "madlan_items": madlan_items,
            "facebook_posts_reviewed": fb_summary.get("posts_reviewed") if fb_summary else None,
            "facebook_yes": fb_summary.get("relevant_yes", fb_summary.get("yes")) if fb_summary else None,
            "facebook_maybe": fb_summary.get("relevant_maybe", fb_summary.get("maybe")) if fb_summary else None,
        },
        "scan_quality": {
            "yad2": {k: v for k, v in yad2_q.items() if k != "source"},
            "madlan": {k: v for k, v in madlan_q.items() if k != "source"},
            "facebook": {k: v for k, v in fb_q.items() if k != "source"},
            "overall_reliable": all(
                s["status"].startswith("✅") for s in (yad2_q, madlan_q, fb_q)
            ),
        },
        "needs_ai_triage": manifest.get("overall_status") == "awaiting_ai_triage",
        "triage": manifest.get("facebook_triage") or {},
        "top_candidates": brief_candidates,
        "standard_followup_questions": [
            "האם יש רטיבות / עובש / נזילות?",
            "איך הרעש בפועל בדירה?",
            "כמה מזגנים יש ובאילו חדרים?",
            "האם יש אפשרות לטווח ארוך / אופציה להארכה?",
            "אם הכניסה מיידית/מאי/יוני: האם יש גמישות ליולי/אוגוסט?",
        ],
    }
    write_json(run_dir / "assistant_brief.json", brief)
    return out


def finalize_run(args: argparse.Namespace) -> pathlib.Path:
    run_dir = pathlib.Path(args.run_dir) if args.run_dir else latest_run_dir()
    if not run_dir:
        raise SystemExit("No run directory found")
    manifest = load_json(run_dir / "manifest.json", {}) or {}
    scan_date = manifest.get("scan_date") or args.scan_date or scan_date_now()
    # Rebuild source counts/paths from artifacts on every finalize. This fixes
    # older manifests and prevents the final report from inheriting stale counts.
    rebuilt = build_manifest(run_dir, scan_date)
    manifest.update({k: v for k, v in rebuilt.items() if k not in {"overall_status", "facebook_triage"}})

    fb = manifest.get("facebook_last_run") or latest_facebook_run() or {}
    triage_json_raw = args.triage_json or fb.get("triage_output_json")
    input_json_raw = args.input_json or fb.get("ai_input_file")
    triage_json = pathlib.Path(triage_json_raw) if triage_json_raw else None
    input_json = pathlib.Path(input_json_raw) if input_json_raw else pathlib.Path("/dev/null")
    model = args.model or fb.get("model") or DEFAULT_FACEBOOK_MODEL

    if triage_json and triage_json.exists():
        cache_log = run_dir / "logs" / "triage_cache_update.log"
        result = run_cmd(
            "triage_cache_update",
            [
                sys.executable,
                str(ROOT / "scripts" / "facebook_ai_triage_cache_update.py"),
                str(triage_json),
                "--input-json",
                str(input_json),
                "--model",
                model,
            ],
            cache_log,
            timeout_s=120,
        )
        manifest["facebook_triage"] = {
            "status": "completed",
            "file": str(triage_json),
            "md_file": fb.get("triage_output_md"),
            "summary": (load_json(triage_json, {}) or {}).get("summary", {}),
            "cache_update": result,
        }
    elif (fb.get("needs_triage") or 0) > 0:
        manifest["facebook_triage"] = {
            "status": "awaiting_ai_triage",
            "needs_triage": fb.get("needs_triage"),
            "prompt_file": fb.get("prompt_file"),
            "input_file": fb.get("ai_input_file"),
            "expected_output_json": fb.get("triage_output_json"),
        }

    pending_steps = [name for name, data in (manifest.get("steps") or {}).items() if data.get("status") == "awaiting_ai_triage"]
    if manifest.get("failed_steps"):
        manifest["overall_status"] = "failed"
    elif pending_steps or (manifest.get("facebook_triage") or {}).get("status") == "awaiting_ai_triage":
        manifest["overall_status"] = "awaiting_ai_triage"
    else:
        manifest["overall_status"] = "completed"
    manifest["pending_steps"] = pending_steps

    # 1. Shadow-mode LLM normalization (optional, non-blocking)
    # Runs BEFORE evaluator so normalized artifacts are available if active mode is enabled.
    normalization_ok_for_evaluator = False
    if args.shadow_mode:
        try:
            import normalization_pipeline as _np
            shadow_result = _np.run_shadow_normalization(
                run_dir,
                max_items=args.shadow_max_items,
                use_llm=_np._use_llm(),
            )
            manifest["ai_normalization"] = shadow_result
            manifest["shadow_normalization"] = shadow_result  # temporary legacy alias
            normalization_ok_for_evaluator = (
                shadow_result.get("status") == "completed"
                and (run_dir / "normalized_listings.json").exists()
            )
        except Exception as exc:
            failure = {"enabled": True, "shadow": True, "status": "failed", "error": str(exc)}
            manifest["ai_normalization"] = failure
            manifest["shadow_normalization"] = failure
            _emit_event("shadow_normalization_failed", run_id=str(run_dir.name), error=str(exc))

    # 2. Evaluate candidates — deterministic pipeline (runs AFTER normalization)
    try:
        import evaluate_candidates as _ec
        use_norm = _ec._use_normalized() and normalization_ok_for_evaluator
        eval_result = _ec.evaluate_run(run_dir, use_normalized=use_norm)
        eval_result["used_normalized"] = use_norm
        manifest["evaluation"] = eval_result
    except Exception as exc:
        manifest["evaluation"] = {"error": str(exc)}
        _emit_event("evaluation_failed", run_id=str(run_dir.name), error=str(exc))

    # 3. Generate unified report + brief (will be enriched by evaluation below)
    report = generate_unified_report(run_dir, manifest)
    brief_path = run_dir / "assistant_brief.json"
    manifest["final_report"] = str(report)

    # 3. Enrich brief with evaluation data if available
    _enrich_brief_from_evaluation(brief_path, run_dir)
    if brief_path.exists():
        manifest["assistant_brief"] = str(brief_path)

    # 4. DB ingest (reads evaluated_candidates.json via _items_from_evaluation)
    db_log = run_dir / "logs" / "apartment_db_ingest.log"
    db_result = run_cmd(
        "apartment_db_ingest",
        [sys.executable, str(ROOT / "scripts" / "apartment_db.py"), "ingest-run", str(run_dir)],
        db_log,
        timeout_s=60,
    )
    delta_report = run_dir / "db_delta_report.md"
    delta_log = run_dir / "logs" / "apartment_db_delta.log"
    delta_result = run_cmd(
        "apartment_db_delta",
        [
            sys.executable,
            str(ROOT / "scripts" / "apartment_db.py"),
            "delta-report",
            "--out",
            str(delta_report),
        ],
        delta_log,
        timeout_s=60,
    )
    manifest["apartment_db"] = {
        "db": str(ART / "apartments.db"),
        "ingest": db_result,
        "delta_report": str(delta_report) if delta_report.exists() else None,
        "delta": delta_result,
    }

    manifest["finalized_at"] = now_iso()
    write_json(run_dir / "manifest.json", manifest)

    state_data = load_json(run_dir / "state.json", {}) or {}
    state_data["status"] = manifest["overall_status"]
    state_data["updated_at"] = now_iso()
    state_data["final_report"] = str(report)
    if brief_path.exists():
        state_data["assistant_brief"] = str(brief_path)
    state_data["manifest"] = str(run_dir / "manifest.json")
    write_json(run_dir / "state.json", state_data)

    brief = load_json(brief_path, {}) if brief_path.exists() else {}
    _emit_event(
        "scan_completed",
        run_id=str(run_dir.name),
        report_path=str(report),
        candidate_count=len(brief.get("top_candidates") or []),
        followup_count=len(brief.get("followup_candidates_evaluated") or []),
    )
    print(json.dumps({"status": manifest["overall_status"], "manifest": str(run_dir / "manifest.json"), "final_report": str(report), "assistant_brief": str(brief_path) if brief_path.exists() else None}, ensure_ascii=False, indent=2))
    return report


def resume_run(args: argparse.Namespace) -> None:
    run_dir = pathlib.Path(args.run_dir)
    if not run_dir.exists():
        raise SystemExit(f"Run dir not found: {run_dir}")

    state_data = load_json(run_dir / "state.json", {}) or {}
    steps = state_data.get("steps", {})
    run_id = state_data.get("run_id", run_dir.name)
    scan_date = state_data.get("scan_date") or (args.scan_date if hasattr(args, "scan_date") else None) or scan_date_now()

    state = RunState.__new__(RunState)
    state.run_dir = run_dir
    state.path = run_dir / "state.json"
    state.data = state_data
    state.set_status("running_collection")

    env = os.environ.copy()
    env["YOGEV_SCAN_DATE"] = scan_date
    env["YOGEV_RUN_ID"] = run_id
    env = prepare_browser_env(env)

    log_dir = run_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    try:
        ensure_browser(state, log_dir, env)
    except RuntimeError as e:
        state.set_status("failed", reason="browser_startup_failed", error=str(e))
        _emit_event("step_failed", run_id=run_id, step="browser",
                    reason="browser_startup_failed", error=str(e))
        raise

    for step_name, script in [("madlan", "scrape_madlan_public.py"), ("yad2", "yad2_broad_search.py")]:
        step_status = (steps.get(step_name) or {}).get("status")
        if step_status == "ok":
            print(f"[resume] Skipping {step_name} — already ok", flush=True)
            continue
        print(f"[resume] Retrying {step_name}...", flush=True)
        state.step(step_name, "running")
        result = run_cmd(
            step_name,
            [sys.executable, str(ROOT / "scripts" / script)],
            log_dir / f"{step_name}_resume.log",
            env=env,
            timeout_s=900 if step_name == "yad2" else 180,
        )
        if result["exit_code"] == 2 and step_name == "yad2":
            broad_art = ART / f"broad_search_{run_id}"
            block_state_path = broad_art / "block_state.json"
            block_info = load_json(block_state_path, {}) if block_state_path.exists() else {}
            resume_cmd = f"python3 scripts/full_apartment_scan.py resume --run-dir {run_dir}"
            state.set_status("waiting_for_human", blocked_step=step_name,
                             resume_cmd=resume_cmd, blocked_url=block_info.get("url", ""))
            _emit_event("step_blocked", run_id=run_id, step=step_name,
                        block_type=block_info.get("block_type", "unknown"),
                        blocked_url=block_info.get("url", ""), resume_cmd=resume_cmd)
            print(json.dumps({"status": "waiting_for_human", "resume_cmd": resume_cmd}, ensure_ascii=False))
            return
        state.step(step_name, "ok" if result["exit_code"] == 0 else "failed", **result)
        if result["exit_code"] == 0:
            _emit_event("step_ok", run_id=run_id, step=step_name, exit_code=0)
        else:
            _emit_event("step_failed", run_id=run_id, step=step_name,
                        reason=f"exit_code={result['exit_code']}", exit_code=result["exit_code"])

    fb_status = (steps.get("facebook") or {}).get("status")
    if fb_status != "ok":
        print("[resume] Retrying facebook...", flush=True)
        state.step("facebook", "running")
        facebook = run_cmd(
            "facebook",
            [sys.executable, str(ROOT / "scripts" / "daily_scan.py"),
             "--scrolls", "15", "--delay", "2.6"],
            log_dir / "facebook_resume.log",
            env=env,
            timeout_s=14400,
        )
        state.step("facebook", "ok" if facebook["exit_code"] == 0 else "failed", **facebook)
        if facebook["exit_code"] == 0:
            _emit_event("step_ok", run_id=run_id, step="facebook", exit_code=0)
        else:
            _emit_event("step_failed", run_id=run_id, step="facebook",
                        reason=f"exit_code={facebook['exit_code']}", exit_code=facebook["exit_code"])

    manifest = build_manifest(run_dir, scan_date, state)
    write_json(run_dir / "manifest.json", manifest)
    finalize_run(argparse.Namespace(
        run_dir=str(run_dir), scan_date=scan_date,
        triage_json=None, input_json=None, model=DEFAULT_FACEBOOK_MODEL,
        shadow_mode=False, shadow_max_items=10,
    ))


def print_status(args: argparse.Namespace) -> None:
    run_dir = pathlib.Path(args.run_dir) if args.run_dir else latest_run_dir()
    if not run_dir:
        print("No run directory found")
        return
    manifest = load_json(run_dir / "manifest.json", {}) or {}
    state = load_json(run_dir / "state.json", {}) or {}
    print(json.dumps({"run_dir": str(run_dir), "state": state.get("status"), "manifest_status": manifest.get("overall_status"), "manifest": manifest}, ensure_ascii=False, indent=2))


def _run_shadow_normalization(run_dir: pathlib.Path, max_items: int = 10) -> dict[str, Any]:
    """Run LLM normalizer in shadow mode on evidence packs from a run.

    This is non-blocking: failures are logged but do not affect the main pipeline.
    Writes shadow artifacts to the run directory for later comparison.
    """
    import ai_normalize_listing as anl

    evidence_packs_path = run_dir / "evidence_packs.json"
    if not evidence_packs_path.exists():
        # Try to build evidence packs from evaluated candidates
        eval_path = run_dir / "evaluated_candidates.json"
        if not eval_path.exists():
            return {"status": "skipped", "reason": "no evidence_packs.json or evaluated_candidates.json"}
        # Build minimal evidence packs from evaluated candidates
        eval_data = load_json(eval_path, {}) or {}
        # evaluated_candidates.json may be a dict with "items" list or a plain list
        if isinstance(eval_data, dict):
            candidates = eval_data.get("items", [])
        else:
            candidates = eval_data
        
        # Check if candidates have raw_text
        candidates_with_text = [c for c in candidates if c.get("raw_text")]
        if not candidates_with_text:
            return {"status": "skipped", "reason": "evaluated_candidates exists but no raw_text (need evidence packs with raw text)"}
        
        packs = []
        for c in candidates_with_text[:max_items]:
            packs.append({
                "listing_id": c.get("listing_id", "unknown"),
                "content_hash": "",
                "source": c.get("source", "unknown"),
                "source_url": c.get("url"),
                "raw_text": c.get("raw_text", ""),
                "known_fields": {
                    "price": c.get("price_nis"),
                    "rooms": c.get("rooms"),
                    "sqm": c.get("sqm"),
                    "floor": c.get("floor"),
                    "entry_raw": c.get("entry_raw"),
                    "address": c.get("city") or c.get("neighborhood"),
                },
                "source_metadata": {},
                "raw_json_excerpt": {},
            })
        evidence_packs_path = run_dir / "shadow_evidence_packs.json"
        write_json(evidence_packs_path, packs)
    else:
        packs = load_json(evidence_packs_path, []) or []

    shadow_out = run_dir / "shadow_normalized_listings.json"
    shadow_audit = run_dir / "shadow_normalization_audit.md"
    shadow_cache = run_dir / "shadow_normalizer_cache.json"

    # Run LLM normalization (non-blocking: catch all errors)
    try:
        results = anl.normalize_packs(
            packs,
            max_items=max_items,
            use_llm=True,
            dry_run=False,
            cache_path=shadow_cache,
        )
        write_json(shadow_out, results)

        # Build comparison audit
        ok_count = sum(1 for r in results if r.get("normalization_status") == "ok")
        fail_count = len(results) - ok_count

        audit_lines = [
            "# Shadow Normalization Audit",
            "",
            f"**Run:** {run_dir.name}",
            f"**Total processed:** {len(results)}",
            f"**OK:** {ok_count}",
            f"**Failed:** {fail_count}",
            "",
            "## Status Breakdown",
        ]
        by_status: dict[str, int] = {}
        for r in results:
            st = r.get("normalization_status", "?")
            by_status[st] = by_status.get(st, 0) + 1
        for st, cnt in sorted(by_status.items()):
            audit_lines.append(f"- {st}: {cnt}")

        audit_lines.append("")
        audit_lines.append("## Sample OK Results")
        for r in results[:5]:
            if r.get("normalization_status") == "ok":
                audit_lines.append(f"\n### {r.get('listing_id', '?')}")
                audit_lines.append(f"- price: {r.get('price_nis')} | rooms: {r.get('rooms')} | entry: {r.get('entry_status_hint')}")
                audit_lines.append(f"- broker: {r.get('broker_status')} | contract: {r.get('contract_type')}")
                if r.get("red_flags"):
                    audit_lines.append(f"- red_flags: {r['red_flags']}")

        shadow_audit.write_text("\n".join(audit_lines), encoding="utf-8")

        return {
            "status": "completed",
            "total": len(results),
            "ok": ok_count,
            "failed": fail_count,
            "output_file": str(shadow_out),
            "audit_file": str(shadow_audit),
            "cache_file": str(shadow_cache),
        }
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# CLI


def main() -> None:
    ap = argparse.ArgumentParser(description="Reliable full apartment scan orchestrator")
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run collection phase: Yad2 + Madlan + Facebook prepare")
    run.add_argument("--run-id")
    run.add_argument("--scan-date")
    run.add_argument("--facebook-scrolls", type=int, default=int(os.environ.get("FACEBOOK_SCROLLS", "15")))
    run.add_argument("--facebook-delay", type=float, default=float(os.environ.get("FACEBOOK_DELAY", "2.6")))
    run.add_argument("--facebook-timeout", type=int, default=int(os.environ.get("FACEBOOK_TIMEOUT_SECONDS", "14400")))
    run.add_argument("--facebook-retry-scrolls", type=int, default=int(os.environ.get("FACEBOOK_RETRY_SCROLLS", "15")))
    run.add_argument("--facebook-retry-timeout", type=int, default=int(os.environ.get("FACEBOOK_RETRY_TIMEOUT_SECONDS", "7200")))
    run.add_argument("--yad2-timeout", type=int, default=int(os.environ.get("YAD2_TIMEOUT_SECONDS", "900")))
    run.add_argument("--madlan-timeout", type=int, default=int(os.environ.get("MADLAN_TIMEOUT_SECONDS", "180")))

    fin = sub.add_parser("finalize", help="Update cache and write unified final report once AI triage exists")
    fin.add_argument("--run-dir")
    fin.add_argument("--scan-date")
    fin.add_argument("--triage-json")
    fin.add_argument("--input-json")
    fin.add_argument("--model", default=DEFAULT_FACEBOOK_MODEL)
    fin.add_argument("--shadow-mode", action="store_true", help="Run LLM normalizer in shadow mode (non-blocking)")
    fin.add_argument("--shadow-max-items", type=int, default=10, help="Max items for shadow normalization")

    status = sub.add_parser("status", help="Print latest run status/manifest")
    status.add_argument("--run-dir")

    res = sub.add_parser("resume", help="Resume a blocked/failed run, skipping already-completed steps")
    res.add_argument("--run-dir", required=True)
    res.add_argument("--scan-date")

    args = ap.parse_args()
    if args.command == "run":
        run_dir = run_collection(args)
        # Generate a partial report immediately. If Facebook is awaiting AI triage,
        # this report explicitly says so rather than pretending the task is done.
        finalize_run(argparse.Namespace(run_dir=str(run_dir), scan_date=args.scan_date, triage_json=None, input_json=None, model=DEFAULT_FACEBOOK_MODEL, shadow_mode=False, shadow_max_items=10))
    elif args.command == "finalize":
        finalize_run(args)
    elif args.command == "status":
        print_status(args)
    elif args.command == "resume":
        resume_run(args)


if __name__ == "__main__":
    main()
