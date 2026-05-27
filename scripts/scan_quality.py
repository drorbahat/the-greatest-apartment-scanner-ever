#!/usr/bin/env python3
"""Scan quality analysis utilities.

Computes coverage, completeness, and reliability metrics for each source
so the report can include a "scan quality" section.
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Any


def load_json(path: pathlib.Path | str, default: Any = None) -> Any:
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _count_fields(items: list[dict[str, Any]], fields: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    total = max(len(items), 1)
    for f in fields:
        counts[f] = sum(1 for i in items if i.get(f) not in (None, "", []))
    counts["_total"] = total
    return counts


def _field_presence_rate(counts: dict[str, int], field: str) -> float:
    total = counts.get("_total", 1)
    return round((counts.get(field, 0) / total) * 100, 1)


def _url_health(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = max(len(items), 1)
    has_url = 0
    missing_url = 0
    profile_url = 0
    group_url = 0
    other_bad = 0
    for i in items:
        # Yad2 uses href, Facebook/Madlan use url
        url = i.get("url") or i.get("href") or i.get("post_url") or ""
        if not url:
            missing_url += 1
            continue
        if "/user/" in url:
            profile_url += 1
        elif url.rstrip("/").endswith("/groups") or ("/groups/" in url and "/posts/" not in url and "/user/" not in url and "permalink" not in url):
            group_url += 1
        else:
            has_url += 1
    return {
        "total": total,
        "has_valid_url": has_url,
        "missing_url": missing_url,
        "profile_url": profile_url,
        "group_url": group_url,
        "other_bad": other_bad,
        "valid_rate": round((has_url / total) * 100, 1),
    }


def _source_overall_status(health: dict[str, Any], completeness: dict[str, float]) -> str:
    if health.get("total", 0) == 0:
        return "❌ לא נאסף מידע"
    if health.get("valid_rate", 0) < 30:
        return "⚠️ חלקי — בעיית URLs"
    # Check key fields with various possible names
    price_ok = any(completeness.get(k, 0) >= 20 for k in ("price", "feed_price"))
    rooms_ok = any(completeness.get(k, 0) >= 20 for k in ("rooms", "feed_rooms"))
    if not price_ok or not rooms_ok:
        return "⚠️ חלקי — חסרים שדות קריטיים"
    return "✅ תקין"


def analyze_yad2(broad_dir: pathlib.Path) -> dict[str, Any]:
    raw = load_json(broad_dir / "yad2_broad_details.json", []) or []
    items = [x for x in raw if not x.get("blocked")]
    fields = ["feed_price", "feed_rooms", "feed_sqm", "feed_floor", "detail_text", "href"]
    counts = _count_fields(items, fields)
    health = _url_health(items)
    completeness = {f: _field_presence_rate(counts, f) for f in fields}
    return {
        "source": "Yad2",
        "total_items": len(raw),
        "valid_items": len(items),
        "blocked": len(raw) - len(items),
        "field_completeness": completeness,
        "url_health": health,
        "status": _source_overall_status(health, completeness),
    }


def _madlan_enrichment_health(data: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    metadata = data.get("metadata") or {}
    enrichment = metadata.get("madlan_enrichment") or {}
    status = enrichment.get("status")
    status_counts = enrichment.get("status_counts") or {}
    block_state = enrichment.get("block_state") or metadata.get("madlan_block_state") or {}
    if not status:
        status_counts = {}
        for c in candidates:
            s = c.get("madlan_enrich_status")
            if s:
                status_counts[s] = status_counts.get(s, 0) + 1
        blocked_until = block_state.get("blocked_until")
        cooldown_active = bool(blocked_until and time.time() < float(blocked_until))
        if cooldown_active:
            status = "blocked_cooldown"
        elif status_counts.get("blocked"):
            status = "blocked"
        elif not candidates:
            status = "no_candidates"
        elif not status_counts:
            status = "not_attempted"
        elif status_counts.get("ok") == len(candidates):
            status = "ok"
        else:
            status = "partial"
    return {
        "status": status,
        "status_counts": status_counts,
        "block_state": block_state,
    }


def analyze_madlan(broad_dir: pathlib.Path) -> dict[str, Any]:
    data = load_json(broad_dir / "madlan_public_scan.json", {}) or {}
    items = data.get("items") or []
    candidates = data.get("candidates") or []
    pages = data.get("pages") or []
    blocked_pages = data.get("blocked_pages") or []
    all_blocked = bool(data.get("blocked"))
    blocked_page_count = len(blocked_pages) or sum(1 for p in pages if p.get("blocked"))
    enrichment = _madlan_enrichment_health(data, candidates)

    if not items and not candidates:
        if all_blocked or blocked_page_count:
            status = "⚠️ חלקי — מדלן חסם חלק מהעמודים"
        else:
            status = "❌ לא נאסף מידע"
        return {
            "source": "Madlan",
            "total_items": 0,
            "candidates": 0,
            "blocked_pages": blocked_page_count,
            "all_blocked": all_blocked,
            "enrichment": enrichment,
            "field_completeness": {},
            "url_health": {"total": 0, "has_valid_url": 0, "missing_url": 0, "valid_rate": 0},
            "status": status,
        }

    fields = ["price", "rooms", "sqm", "floor", "url", "address"]
    counts = _count_fields(candidates, fields)
    health = _url_health(candidates)
    completeness = {f: _field_presence_rate(counts, f) for f in fields}
    status = _source_overall_status(health, completeness)
    if enrichment.get("status") in {"blocked", "blocked_cooldown"}:
        status = "⚠️ איסוף בסיסי תקין, enrichment חסום"
    elif blocked_page_count:
        status = "⚠️ חלקי — חלק מעמודי מדלן חסומים"
    return {
        "source": "Madlan",
        "total_items": len(items),
        "candidates": len(candidates),
        "blocked_pages": blocked_page_count,
        "all_blocked": all_blocked,
        "enrichment": enrichment,
        "field_completeness": completeness,
        "url_health": health,
        "status": status,
    }


def analyze_facebook(
    manifest: dict[str, Any],
    fb_input_path: pathlib.Path | None,
    triage_path: pathlib.Path | None,
) -> dict[str, Any]:
    fb_last = manifest.get("facebook_last_run") or {}
    clean_file = fb_last.get("clean_file")
    clean = load_json(clean_file, {}) if clean_file else {}
    posts = clean.get("posts") or []

    # URL health from clean posts
    total = max(len(posts), 1)
    valid_post = 0
    missing_post_id = 0
    profile_url = 0
    group_url = 0
    other = 0
    for p in posts:
        st = p.get("url_status")
        if st is None:
            # Fallback: old clean file without url_status — classify from post_url
            from facebook_url_utils import classify_facebook_url
            st = classify_facebook_url(p.get("post_url"))
        if st == "valid_post":
            valid_post += 1
        elif st == "missing_post_id":
            missing_post_id += 1
        elif st == "profile_url":
            profile_url += 1
        elif st == "group_url":
            group_url += 1
        else:
            other += 1

    # Triage summary
    triage = load_json(triage_path, {}) if triage_path else {}
    summary = triage.get("summary") or {}
    triage_items = triage.get("items") or []

    # Group health from manifest
    fb_health = manifest.get("facebook_health") or {}
    has_health = bool(fb_health)

    status = "✅ תקין"
    if fb_health.get("error_groups", 0) > 0 or fb_health.get("blocked_groups", 0) > 0:
        status = "⚠️ חלקי"
    elif not has_health and not posts:
        status = "❌ לא נאסף מידע"
    elif not has_health:
        status = "⚠️ חלקי — חסר health data"

    return {
        "source": "Facebook",
        "total_groups_scanned": fb_health.get("total_groups", 0),
        "ok_groups": fb_health.get("ok_groups", 0),
        "error_groups": fb_health.get("error_groups", 0),
        "blocked_groups": fb_health.get("blocked_groups", 0),
        "raw_posts": clean.get("raw_items", len(posts)),
        "clean_posts": len(posts),
        "duplicates_collapsed": clean.get("duplicates_collapsed", 0),
        "url_status": {
            "valid_post": valid_post,
            "missing_post_id": missing_post_id,
            "profile_url": profile_url,
            "group_url": group_url,
            "other": other,
        },
        "valid_post_rate": round((valid_post / total) * 100, 1),
        "triage": {
            "posts_reviewed": summary.get("posts_reviewed", len(triage_items)),
            "yes": summary.get("relevant_yes", summary.get("yes", 0)),
            "maybe": summary.get("relevant_maybe", summary.get("maybe", 0)),
            "no": summary.get("relevant_no", summary.get("no", 0)),
        },
        "status": status,
    }


def scan_quality_section(
    manifest: dict[str, Any],
    broad_dir: pathlib.Path,
    fb_input_path: pathlib.Path | None,
    triage_path: pathlib.Path | None,
) -> list[str]:
    yad2 = analyze_yad2(broad_dir)
    madlan = analyze_madlan(broad_dir)
    facebook = analyze_facebook(manifest, fb_input_path, triage_path)

    lines: list[str] = []
    lines.append("## איכות הסריקה")
    lines.append("")

    # Yad2
    lines.append(f"### Yad2")
    lines.append(f"- סטטוס: {yad2['status']}")
    lines.append(f"- מודעות שנאספו: {yad2['total_items']}")
    if yad2["blocked"]:
        lines.append(f"- חסומות / כשל פתיחה: {yad2['blocked']}")
    lines.append(f"- מועמדות תקינות: {yad2['valid_items']}")
    yc = yad2["field_completeness"]
    lines.append(f"- שלמות שדות: מחיר {yc.get('feed_price', 0)}%, חדרים {yc.get('feed_rooms', 0)}%, מ״ר {yc.get('feed_sqm', 0)}%, קומה {yc.get('feed_floor', 0)}%, טקסט {yc.get('detail_text', 0)}%, לינק {yc.get('href', 0)}%")
    lines.append("")

    # Madlan
    lines.append(f"### Madlan")
    lines.append(f"- סטטוס: {madlan['status']}")
    lines.append(f"- כרטיסים שנאספו: {madlan['total_items']}")
    lines.append(f"- מועמדות: {madlan['candidates']}")
    if madlan.get("blocked_pages"):
        lines.append(f"- עמודים חסומים: {madlan['blocked_pages']}")
    enrichment = madlan.get("enrichment") or {}
    if enrichment.get("status") in {"blocked", "blocked_cooldown"}:
        reason = (enrichment.get("block_state") or {}).get("reason")
        suffix = f" ({reason})" if reason else ""
        lines.append(f"- enrichment: חסום{suffix}")
    mc = madlan["field_completeness"]
    lines.append(f"- שלמות שדות: מחיר {mc.get('price', 0)}%, חדרים {mc.get('rooms', 0)}%, מ״ר {mc.get('sqm', 0)}%, קומה {mc.get('floor', 0)}%, כתובת {mc.get('address', 0)}%, לינק {mc.get('url', 0)}%")
    lines.append("")

    # Facebook
    lines.append(f"### Facebook")
    lines.append(f"- סטטוס: {facebook['status']}")
    lines.append(f"- קבוצות שנסרקו: {facebook['total_groups_scanned']} / הצליחו {facebook['ok_groups']} / שגיאות {facebook['error_groups']} / חסומות {facebook['blocked_groups']}")
    lines.append(f"- פוסטים גולמיים: {facebook['raw_posts']}")
    lines.append(f"- אחרי ניקוי: {facebook['clean_posts']} (כפילויות מקובצות: {facebook['duplicates_collapsed']})")
    lines.append(f"- לינקי פוסט תקינים: {facebook['url_status']['valid_post']} ({facebook['valid_post_rate']}%)")
    if facebook["url_status"]["missing_post_id"]:
        lines.append(f"- בלי לינק ישיר: {facebook['url_status']['missing_post_id']}")
    if facebook["url_status"]["profile_url"]:
        lines.append(f"- לינקי פרופיל במקום פוסט: {facebook['url_status']['profile_url']}")
    if facebook["url_status"]["group_url"]:
        lines.append(f"- לינקי קבוצה בלבד: {facebook['url_status']['group_url']}")
    if facebook["triage"]["posts_reviewed"]:
        lines.append(f"- AI triage: {facebook['triage']['posts_reviewed']} נבדקו — כן {facebook['triage']['yes']}, אולי {facebook['triage']['maybe']}, לא {facebook['triage']['no']}")
    lines.append("")

    # Overall
    all_ok = all(s["status"].startswith("✅") for s in (yad2, madlan, facebook))
    lines.append("### סיכום אמינות")
    if all_ok:
        lines.append("✅ כל המקורות נסרקו בהצלחה. ניתן לסמוך על הדוח כמקיף.")
    else:
        lines.append("⚠️ חלק מהמקורות חלקיים — ייתכן שחסר מידע. לבדוק פרטים לפני פעולה.")
    lines.append("")

    return lines
