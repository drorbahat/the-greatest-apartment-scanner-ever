#!/usr/bin/env python3
"""Injected into the apt-scan-report cron job via --script.

Finds the most recent completed scan (within the last 3 hours), reads a small
set of result files, and prints a compact but *safe* summary for the cron LLM.

Goals:
- prefer strict shortlist fields over legacy ones
- mark partial / failed runs explicitly
- separate strong candidates from follow-up / manual-check buckets
- avoid leaking linkless items into the shortlist
"""
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVENTS_DIR = ROOT / "artifacts" / "events"
RUNS_DIR = ROOT / "artifacts" / "full_scan_runs"
MAX_AGE_S = 3 * 3600  # 3 hours
MIN_REASONABLE_RENT = 3500
MAX_REASONABLE_RENT = 7500
_price_filter_count = 0


def _has_reasonable_rent(item):
    """Drop obvious Facebook price parser garbage from Telegram summaries.

    Examples observed in production: ₪450 / ₪2045 from dates, distances, or
    Facebook UI noise. Missing price is allowed because it can still be an
    ask-price followup; absurd numeric prices are not.
    """
    price = item.get("price")
    if price is None or price == "":
        return True
    try:
        value = float(price)
    except (TypeError, ValueError):
        return True
    return MIN_REASONABLE_RENT <= value <= MAX_REASONABLE_RENT


def _filter_bad_prices(items):
    global _price_filter_count
    kept = []
    for item in items:
        if _has_reasonable_rent(item):
            kept.append(item)
        else:
            _price_filter_count += 1
    return kept

# Filter user rejections so permanently rejected apartments don't appear again
try:
    sys.path.insert(0, str(ROOT / "scripts"))
    from user_rejections import is_rejected
    _rejection_count = 0

    def _filter_rejected(items):
        global _rejection_count
        kept = []
        for item in items:
            rejected, _ = is_rejected(item)
            if rejected:
                _rejection_count += 1
                continue
            kept.append(item)
        return kept
except Exception:
    _rejection_count = 0
    def _filter_rejected(items):
        return items


def _get_ai_normalization_from_manifest(manifest: dict) -> dict | None:
    if not isinstance(manifest, dict):
        return None
    return manifest.get("ai_normalization") or manifest.get("shadow_normalization")


def find_latest_completed_event():
    if not EVENTS_DIR.exists():
        return None
    events = sorted(EVENTS_DIR.glob("*_scan_completed.json"), reverse=True)
    now_ms = time.time() * 1000
    for path in events:
        try:
            data = json.loads(path.read_text())
            age_s = (now_ms - data["ts"]) / 1000
            if age_s <= MAX_AGE_S:
                return data
        except Exception:
            continue
    return None


def load_json(path: pathlib.Path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def has_direct_url(item):
    url = item.get("url") or item.get("post_url") or ""
    return isinstance(url, str) and url.startswith("http") and "/user/" not in url


def fmt_val(value, suffix=""):
    if value is None or value == "":
        return "?"
    return f"{value}{suffix}"


def summarize_candidate(item, include_action=False):
    parts = [
        item.get("source", "?"),
        f"₪{fmt_val(item.get('price'))}",
        f"{fmt_val(item.get('rooms'))} חדרים",
        item.get("entry_status", "לא צוין"),
    ]
    broker = item.get("broker_status")
    if broker and broker != "unknown_broker":
        parts.append(f"broker:{broker}")
    if include_action:
        parts.append(f"action:{item.get('recommended_action', '?')}")
    parts.append(item.get("url", ""))
    return " | ".join(parts)


def _madlan_artifact_from_manifest(manifest):
    if not isinstance(manifest, dict):
        return {}
    madlan_file = ((manifest.get("madlan") or {}).get("file"))
    if madlan_file:
        return load_json(pathlib.Path(madlan_file))
    broad_dir = manifest.get("broad_artifact_dir")
    if broad_dir:
        return load_json(pathlib.Path(broad_dir) / "madlan_public_scan.json")
    return {}


def _madlan_status_for_cron(status, manifest):
    """Avoid saying Madlan is fully OK when detail enrichment or pages are blocked.
    
    Returns clear, actionable status messages for the cron report.
    """
    data = _madlan_artifact_from_manifest(manifest)
    if not data:
        return status
    pages = data.get("pages") or []
    blocked_pages = data.get("blocked_pages") or []
    blocked_page_count = len(blocked_pages) or sum(1 for p in pages if p.get("blocked"))
    candidates = data.get("candidates") or []
    metadata = data.get("metadata") or {}
    enrichment = metadata.get("madlan_enrichment") or {}
    enrich_status = enrichment.get("status")
    
    # Derive status from candidate statuses if not explicitly set
    if not enrich_status:
        statuses = {c.get("madlan_enrich_status") for c in candidates}
        if "blocked" in statuses or "skipped_block_cooldown" in statuses or "skipped_block_in_run" in statuses:
            enrich_status = "blocked_cooldown"
    
    # Build actionable message
    if enrich_status in {"blocked", "blocked_cooldown"}:
        human_action = enrichment.get("human_action_required", False)
        if human_action:
            return (
                "⚠️ מדלן דורש אימות ידני — "
                "פתור CAPTCHA בלשונית הפתוחה בדפדפן יוגב. "
                "אחרי הפתרון אפשר לנקות cooldown עם: "
                "python3 scripts/scrape_madlan_public.py clear-block-state"
            )
        return "⚠️ איסוף בסיסי תקין, enrichment חסום זמנית"
    
    if enrich_status == "partial":
        counts = enrichment.get("status_counts") or {}
        ok_count = enrichment.get("enriched_ok_count", counts.get("ok", 0))
        candidate_count = enrichment.get("candidate_count") or len(candidates)
        attempted_count = enrichment.get("attempted_count")
        if ok_count:
            detail = f" ({ok_count}/{candidate_count} עם תאריך)" if candidate_count else ""
            return f"⚠️ מדלן בסיסי תקין, enrichment חלקי{detail}"
        if counts:
            detail = f" ({attempted_count}/{candidate_count} נבדקו)" if attempted_count is not None and candidate_count else ""
            return f"⚠️ מדלן בסיסי תקין, enrichment לא הצליח{detail}"

    if enrich_status == "not_attempted" and candidates:
        return "⚠️ מדלן בסיסי תקין, enrichment לא בוצע"

    if blocked_page_count:
        return "⚠️ חלקי — חלק מעמודי מדלן חסומים"
    
    return status


def main():
    event = find_latest_completed_event()
    if not event:
        sys.exit(0)

    run_id = event.get("run_id", "")
    run_dir = RUNS_DIR / run_id if run_id else None
    if not run_dir or not run_dir.exists():
        sys.exit(0)

    state = load_json(run_dir / "state.json")
    if state.get("status") == "waiting_for_human":
        blocked = state.get("blocked_step", "unknown")
        url = state.get("blocked_url", "")
        cmd = state.get("resume_cmd", "")
        print(f"BLOCKED=true\nblocked_step={blocked}\nblocked_url={url}\nresume_cmd={cmd}")
        sys.exit(0)

    brief = load_json(run_dir / "assistant_brief.json")
    if not brief:
        sys.exit(0)
    manifest = load_json(run_dir / "manifest.json")

    counts = brief.get("counts", {})
    quality = brief.get("scan_quality", {})
    evaluation_summary = brief.get("evaluation_summary", {})

    raw_top = brief.get("top_candidates_evaluated", [])
    # Filter user rejections and obvious price-parser garbage BEFORE slicing,
    # so removed top items are backfilled by later candidates instead of
    # shrinking the Telegram shortlist.
    strong = _filter_rejected(_filter_bad_prices([
        c for c in raw_top
        if has_direct_url(c) and c.get("recommended_action") == "open"
    ]))[:5]

    raw_followup = list(brief.get("followup_candidates_evaluated", [])) + [
        c for c in raw_top
        if has_direct_url(c) and c.get("recommended_action") != "open"
    ]
    followup_candidates = _filter_rejected(_filter_bad_prices([c for c in raw_followup if has_direct_url(c)]))[:5]

    raw_manual = brief.get("manual_entry_check_candidates", [])
    manual_entry = _filter_rejected(_filter_bad_prices([c for c in raw_manual if has_direct_url(c)]))[:5]

    failed_steps = manifest.get("failed_steps", []) if isinstance(manifest, dict) else []
    pending_steps = manifest.get("pending_steps", []) if isinstance(manifest, dict) else []
    partial = brief.get("status") != "completed" or bool(failed_steps or pending_steps)

    source_statuses = []
    for source_name in ("yad2", "madlan", "facebook"):
        source_info = quality.get(source_name, {})
        if isinstance(source_info, dict) and source_info.get("status"):
            status = source_info.get("status")
            if source_name == "madlan":
                status = _madlan_status_for_cron(status, manifest)
            source_statuses.append(f"{source_name}:{status}")

    lines = []
    lines.append(f"run_id={run_id}")
    lines.append(f"status={brief.get('status', 'unknown')}")
    lines.append(f"partial_report={str(partial).lower()}")
    if _rejection_count > 0:
        lines.append(f"rejected_filter={_rejection_count}")
        lines.append(f"report_note=סוננו {_rejection_count} מודעות שכבר נדחו על ידך")
    if _price_filter_count > 0:
        lines.append(f"bad_price_filter={_price_filter_count}")
        lines.append(f"report_note=סוננו {_price_filter_count} מודעות עם מחיר לא סביר/שגיאת פרסור")
    if failed_steps:
        lines.append("failed_steps=" + ",".join(failed_steps))
    if pending_steps:
        lines.append("pending_steps=" + ",".join(pending_steps))
    if source_statuses:
        lines.append("source_statuses=" + ",".join(source_statuses))
    lines.append(
        "counts="
        f"yad2_opened:{counts.get('yad2_opened', 0)},"
        f"madlan_candidates:{counts.get('madlan_candidates', 0)},"
        f"facebook_yes:{counts.get('facebook_yes', 0)},"
        f"facebook_maybe:{counts.get('facebook_maybe', 0)}"
    )
    lines.append(
        "quality="
        f"overall_reliable:{quality.get('overall_reliable', False)},"
        f"needs_ai_triage:{brief.get('needs_ai_triage', False)},"
        f"manual_entry_check_count:{evaluation_summary.get('manual_entry_check_count', 0)}"
    )

    # AI normalization status (brief mention only)
    ai_norm = _get_ai_normalization_from_manifest(manifest)
    if ai_norm and ai_norm.get("enabled"):
        shadow = "shadow" if ai_norm.get("shadow") else "active"
        status = ai_norm.get("status", "unknown")
        ok = ai_norm.get("ok", 0)
        cached = ai_norm.get("cached", 0)
        failed = ai_norm.get("failed", 0)
        total = ai_norm.get("total_packs", 0)
        lines.append(f"ai_normalization={shadow} {status} total={total} ok={ok} cached={cached} failed={failed}")

    lines.append("strong_candidates:")
    if strong:
        for c in strong:
            lines.append(f"- {summarize_candidate(c)}")
    else:
        lines.append("- אין כרגע מועמדות חזקות")

    if followup_candidates or manual_entry:
        lines.append(
            "hidden_review_queue="
            f"followup:{len(followup_candidates)},"
            f"manual:{len(manual_entry)}"
        )
        lines.append("report_note=הוצגו רק דירות רלוונטיות; שאר המועמדים נשמרו לבדיקה פנימית")

    followup_questions = brief.get("standard_followup_questions", [])[:3]
    if followup_questions:
        lines.append("followup_questions:")
        for q in followup_questions:
            lines.append(f"- {q}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
