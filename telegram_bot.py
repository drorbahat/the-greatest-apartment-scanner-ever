#!/usr/bin/env python3
"""
Yogev Telegram Bot — standalone polling bot for apartment scan management.

Interactive commands:
  /start   — welcome & setup
  /scan    — trigger a full scan now
  /status  — current scan status
  /report  — latest short report
  /recent  — top candidates from latest run
  /help    — this message

Cron integration:
  send_auto_report() — called from cron at :00 to push report automatically

Requires: TELEGRAM_BOT_TOKEN in environment (or .env)
          TELEGRAM_CHAT_ID in environment (or will ask on /start)
"""

import asyncio
import json
import logging
import os
import pathlib
import subprocess
import sys
from datetime import datetime
from typing import Any

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("yogev-bot")

# ── Paths ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
SCRIPTS = PROJECT_ROOT / "scripts"
ARTIFACTS = PROJECT_ROOT / "artifacts" / "full_scan_runs"
DATA = PROJECT_ROOT / "data"

# ── Telegram helpers (raw HTTP API — no ptb dependency for cron) ──────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

CHAT_ID_FILE = DATA / "chat_id.txt"


def _load_chat_id() -> str | None:
    if CHAT_ID_FILE.exists():
        return CHAT_ID_FILE.read_text().strip()
    return None


def _save_chat_id(chat_id: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    CHAT_ID_FILE.write_text(chat_id.strip())


def send_telegram(text: str, chat_id: str | None = None) -> bool:
    """Send a plain text message via Telegram Bot API."""
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set, cannot send")
        return False
    cid = chat_id or _load_chat_id()
    if not cid:
        log.warning("No chat_id known yet")
        return False
    try:
        resp = requests.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        return resp.ok
    except Exception as e:
        log.error("send_telegram failed: %s", e)
        return False


def send_photo(caption: str, photo_url: str, chat_id: str | None = None) -> bool:
    """Send a photo with caption via Telegram Bot API."""
    if not TELEGRAM_TOKEN:
        return False
    cid = chat_id or _load_chat_id()
    if not cid:
        return False
    try:
        resp = requests.post(
            f"{API_BASE}/sendPhoto",
            json={"chat_id": cid, "photo": photo_url, "caption": caption, "parse_mode": "HTML"},
            timeout=15,
        )
        return resp.ok
    except Exception as e:
        log.error("send_photo failed: %s", e)
        return False


# ── Scanner helpers ────────────────────────────────────────────────────────


def _find_latest_run() -> pathlib.Path | None:
    """Find the most recent scan run directory."""
    if not ARTIFACTS.exists():
        return None
    dirs = sorted(ARTIFACTS.iterdir(), reverse=True)
    return dirs[0] if dirs else None


def _read_run_state(run_dir: pathlib.Path) -> dict[str, Any] | None:
    state_file = run_dir / "state.json"
    if state_file.exists():
        return json.loads(state_file.read_text(encoding="utf-8"))
    return None


def _read_report(run_dir: pathlib.Path) -> str | None:
    report_file = run_dir / "final_report.md"
    if report_file.exists():
        return report_file.read_text(encoding="utf-8")
    return None


def _read_candidates(run_dir: pathlib.Path) -> list[dict[str, Any]] | None:
    cand_file = run_dir / "evaluated_candidates.json"
    if cand_file.exists():
        data = json.loads(cand_file.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data.get("candidates", [])
    return None


def _format_candidate(c: dict[str, Any]) -> str:
    """Format one candidate as a short Telegram line."""
    title = c.get("title", c.get("address", "?"))
    price = c.get("price", "?")
    rooms = c.get("rooms", "?")
    entry = c.get("entry_raw", c.get("entry", "?"))
    url = c.get("url", "")
    flags = ""
    if c.get("broker"):
        flags += " ⚠️תיווך"
    if c.get("price", 0) > 6500:
        flags += " 🔴יקר"
    return (
        f"\n• {title}\n"
        f"  {price}₪ | {rooms} חד׳ | כניסה: {entry}{flags}\n"
        f"  {url}"
    )


# ── Commands ───────────────────────────────────────────────────────────────


def cmd_start(chat_id: str) -> str:
    _save_chat_id(chat_id)
    return (
        "👋 ברוכים הבאים ליוגב — סורק נדל״ן אוטומטי!\n\n"
        "פקודות:\n"
        "/scan — הפעל סריקה עכשיו\n"
        "/status — מצב נוכחי\n"
        "/report — דוח אחרון\n"
        "/recent — מועמדויות אחרונות\n"
        "/help — עזרה\n\n"
        "המערכת רצה אוטומטית כל שעה. דוח נשלח אוטומטית."
    )


def cmd_status() -> str:
    run_dir = _find_latest_run()
    if not run_dir:
        return "❌ טרם בוצעה סריקה."

    state = _read_run_state(run_dir)
    if not state:
        return f"❗ אין מידע על הריצה האחרונה ({run_dir.name})."

    status = state.get("status", "unknown")
    steps = state.get("steps", {})
    summary = (
        f"📊 <b>ריצה אחרונה: {run_dir.name}</b>\n"
        f"סטטוס: {status}\n"
    )
    for step, st in steps.items():
        emoji = "✅" if st == "ok" else "⏳" if st == "running" else "❌" if st == "failed" else "⏭️"
        summary += f"  {emoji} {step}: {st}\n"

    return summary.rstrip()


def cmd_report() -> str:
    run_dir = _find_latest_run()
    if not run_dir:
        return "❌ טרם בוצעה סריקה."

    report = _read_report(run_dir)
    if report:
        # Trim very long reports
        if len(report) > 3000:
            report = report[:3000] + "\n\n… (הדוח מלא ארוך, נחתך)"
        return f"📋 <b>דוח {run_dir.name}</b>\n\n{report}"

    state = _read_run_state(run_dir)
    if state and state.get("status") in ("running_collection",):
        return "⏳ הסריקה עדיין רצה. נסה /status לעדכון."

    return "❗ אין דוח זמין עדיין."


def cmd_recent() -> str:
    run_dir = _find_latest_run()
    if not run_dir:
        return "❌ טרם בוצעה סריקה."

    candidates = _read_candidates(run_dir)
    if not candidates:
        return "❗ אין מועמדויות בריצה האחרונה."

    # Sort by score descending, take top 5
    scored = [c for c in candidates if c.get("score", 0) > 0]
    scored.sort(key=lambda c: c.get("score", 0), reverse=True)
    top = scored[:5]

    if not top:
        return "אין מועמדויות שעברו את הסינון."

    lines = [f"🏆 <b>המועמדויות המובילות ({run_dir.name})</b>"]
    for i, c in enumerate(top, 1):
        lines.append(f"\n{i}. {c.get('title', '?'):.50}")
        price = c.get("price", "?")
        rooms = c.get("rooms", "?")
        entry = c.get("entry_raw", c.get("entry", "?"))
        area = c.get("area", c.get("neighborhood", "?"))
        lines.append(f"   {price}₪ | {rooms} חד׳ | {area} | כניסה: {entry}")
        url = c.get("url", "")
        if url:
            lines.append(f"   {url}")

    return "\n".join(lines)


def cmd_scan() -> str:
    """Trigger a full scan in the background."""
    run_dir = _find_latest_run()
    if run_dir:
        state = _read_run_state(run_dir)
        if state and state.get("status") == "running_collection":
            return "⏳ סריקה כבר רצה. נסה /status לעדכון."

    try:
        subprocess.Popen(
            [sys.executable, str(SCRIPTS / "full_apartment_scan.py"), "run"],
            cwd=str(PROJECT_ROOT),
            start_new_session=True,
        )
        return "🚀 סריקה הופעלה! זה לוקח ~40 דקות. דוח יגיע אוטומטית."
    except Exception as e:
        log.error("Failed to start scan: %s", e)
        return f"❌ שגיאה בהפעלת סריקה: {e}"


def cmd_help() -> str:
    return (
        "🤖 <b>יוגב — סורק נדל״ן</b>\n\n"
        "פקודות:\n"
        "/scan — הפעל סריקת דירות עכשיו\n"
        "/status — מצב נוכחי\n"
        "/report — דוח אחרון\n"
        "/recent — 5 המועמדויות המובילות\n"
        "/start — אתחול צ׳אט\n"
        "/help — עזרה\n\n"
        "הסורק רץ אוטומטית כל שעה (7:10-22:10) ושולח דוח אוטומטית כשיש חדש."
    )


# ── Cron integration ───────────────────────────────────────────────────────


def send_auto_report() -> None:
    """Called from cron — send the latest undispatched report to the chat."""
    chat_id = _load_chat_id()
    if not chat_id:
        log.warning("No chat_id configured, can't auto-report")
        return

    # Find the latest completed run that hasn't been reported yet
    if not ARTIFACTS.exists():
        log.info("No artifacts yet, skipping auto-report")
        return

    reported_marker = ".reported_to_user"

    for run_dir in sorted(ARTIFACTS.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue

        if (run_dir / reported_marker).exists():
            continue  # already reported

        state = _read_run_state(run_dir)
        if not state or state.get("status") not in ("completed", "failed"):
            continue

        report = _read_report(run_dir)
        if report:
            # Trim for Telegram
            if len(report) > 3500:
                report = report[:3500] + "\n\n…"
            msg = f"📋 <b>דוח אוטומטי — {run_dir.name}</b>\n\n{report}"
            send_telegram(msg, chat_id)
        else:
            # Try to generate report
            try:
                subprocess.run(
                    [sys.executable, str(SCRIPTS / "full_apartment_scan.py"), "finalize"],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True,
                    timeout=120,
                )
                report = _read_report(run_dir)
                if report:
                    msg = f"📋 <b>דוח אוטומטי — {run_dir.name}</b>\n\n{report}"
                    send_telegram(msg, chat_id)
            except Exception as e:
                log.error("auto-report finalize failed: %s", e)
                send_telegram(f"⚠️ ריצה {run_dir.name} הסתיימה אבל אין דוח.", chat_id)

        # Mark as reported
        (run_dir / reported_marker).touch()
        log.info("Auto-reported run %s", run_dir.name)
        break  # only report the latest
    else:
        # No undispatched runs found — check if something is running
        latest = _find_latest_run()
        if latest:
            state = _read_run_state(latest)
            if state and state.get("status") == "running_collection":
                log.info("Latest run still in progress, skipping auto-report")
                return

        log.info("No completed-but-unreported runs found")


# ── Polling bot (python-telegram-bot) ──────────────────────────────────────


def run_polling() -> None:
    """Start the long-running polling bot using python-telegram-bot."""
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set!")
        print("ERROR: TELEGRAM_BOT_TOKEN is required. Set it in .env or environment.")
        sys.exit(1)

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        msg = cmd_start(chat_id)
        await update.message.reply_text(msg, parse_mode="HTML")

    async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = cmd_scan()
        await update.message.reply_text(msg, parse_mode="HTML")

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = cmd_status()
        await update.message.reply_text(msg, parse_mode="HTML")

    async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = cmd_report()
        await update.message.reply_text(msg, parse_mode="HTML")

    async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = cmd_recent()
        await update.message.reply_text(msg, parse_mode="HTML")

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = cmd_help()
        await update.message.reply_text(msg, parse_mode="HTML")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("help", help_cmd))

    log.info("Starting Telegram bot polling...")
    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=["message"])


# ── CLI entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    # If called with '--send-report', just send and exit (cron mode)
    if "--send-report" in sys.argv:
        send_auto_report()
    else:
        run_polling()
