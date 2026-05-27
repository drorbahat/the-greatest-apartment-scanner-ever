#!/usr/bin/env python3
"""Automatic conservative triage for prepared Facebook apartment posts.

Reads facebook_ai_triage_prepare.py JSON and writes the same JSON/MD shape that
facebook_ai_triage_cache_update.py expects. It is deliberately conservative:
only clear fits get "yes"; early entry, missing price, brokerage, or incomplete
info becomes "maybe"; wrong format/too small/over budget becomes "no".
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
from datetime import datetime
from typing import Any


def load_json(path: str | pathlib.Path) -> dict[str, Any]:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def write_json(path: str | pathlib.Path, data: Any) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def location_label(h: dict[str, Any], text: str) -> tuple[str | None, str | None]:
    loc = h.get("location") if isinstance(h.get("location"), dict) else {}
    street = h.get("street") or loc.get("street")
    neighborhood = h.get("neighborhood") or loc.get("neighborhood")
    city = loc.get("city")
    if not city:
        if "גבעתיים" in text or "givatayim" in text.lower():
            city = "גבעתיים"
        elif "רמת גן" in text or "ramat gan" in text.lower() or "rg" in text.lower():
            city = "רמת גן"
        elif "יד אליהו" in text:
            city = "תל אביב - יד אליהו"
    parts = [p for p in [city, neighborhood, street] if p]
    return (", ".join(parts) if parts else None), street


def entry_kind(entry: Any, text: str) -> str:
    s = (norm_text(entry) + " " + text[:300]).lower()
    target_terms = ["2026-07", "2026-08", "1.7", "1/7", "01/07", "1.8", "1/8", "01/08", "יולי", "אוגוסט", "july", "august"]
    early_terms = ["immediate", "מייד", "מיד", "מאי", "may", "יוני", "june", "2026-04", "2026-05", "2026-06", "1.6", "1/6", "01/06"]
    # Current search timing: immediate/May/June is a hard reject even if the
    # post also says "flexible". Reconsider only once closer to June.
    if any(t in s for t in early_terms):
        return "early"
    if any(t in s for t in target_terms):
        return "target"
    if "גמיש" in s or "flexible" in s:
        return "flexible"
    if entry:
        return "other"
    return "missing"


def is_bad_format(text: str, h: dict[str, Any]) -> tuple[bool, list[str]]:
    low = text.lower()
    cons = []
    if h.get("is_wanted_like") or re.search(r"\bמחפש(?:ים|ת)?\b|looking for an apartment", low):
        cons.append("פוסט חיפוש ולא מודעת השכרה")
    if re.search(r"שותפ(?:ים|ה)?|roommates?|חדר בדירה|split apartment|דירה מפוצלת", low):
        cons.append("נראה כמו שותפים/חדר/דירה מפוצלת")
    if re.search(r"סאבלט|sublet", low):
        cons.append("סאבלט/טווח קצר")
    if re.search(r"למכירה|for sale|price reduction|reduced from|\bmillion\b|מיליון|ירידת מחיר|בטאבו|אטבו", low):
        cons.append("למכירה ולא להשכרה")
    if re.search(r"תמ[״\"]?א|tma", low):
        cons.append("תמ״א/עבודות בבניין")
    if h.get("is_listing") is False:
        cons.append("LLM סימן שזה לא listing אמיתי")
    return bool(cons[:4] and any("פוסט חיפוש" in c or "שותפים" in c or "סאבלט" in c or "למכירה" in c for c in cons)), cons


def triage_post(post: dict[str, Any]) -> dict[str, Any]:
    h = post.get("extracted") or post.get("heuristic") or {}
    text = norm_text(post.get("text") or post.get("clean_text"))
    price = h.get("price")
    rooms = h.get("rooms")
    sqm = h.get("sqm")
    entry = h.get("entry")
    area, address = location_label(h, text)
    bad_format, format_cons = is_bad_format(text, h)
    ek = entry_kind(entry, text)

    pros: list[str] = []
    cons: list[str] = []
    missing: list[str] = []
    followup: list[str] = []
    if price:
        if price <= 6500:
            pros.append("מחיר בתקציב")
        else:
            cons.append("מחיר מעל התקציב")
    else:
        missing.append("מחיר")

    if rooms:
        if rooms >= 3:
            pros.append(f"{rooms:g} חדרים")
        elif rooms >= 2.5:
            pros.append(f"{rooms:g} חדרים — גבולי אבל אפשרי")
        else:
            cons.append("פחות מ־2.5 חדרים")
    else:
        missing.append("מספר חדרים")

    if sqm:
        if sqm >= 70:
            pros.append(f"{sqm:g} מ״ר")
        elif sqm < 50:
            cons.append("קטנה מדי")
    else:
        missing.append("מ״ר")

    if area:
        if any(t in area for t in ["גבעתיים", "רמת גן", "יד אליהו", "ביצרון", "רמת ישראל"]):
            pros.append("אזור רלוונטי")
        else:
            cons.append("אזור לא מרכזי לחיפוש")
    else:
        missing.append("מיקום מדויק")

    if ek == "target":
        pros.append("כניסה ביולי/אוגוסט")
    elif ek == "flexible":
        pros.append("כניסה גמישה")
        followup.append("לוודא שאפשר סוף יולי/אוגוסט")
    elif ek == "early":
        cons.append("כניסה מוקדמת מדי")
    else:
        missing.append("תאריך כניסה")
        followup.append("מה תאריך הכניסה והאם יש גמישות ליולי/אוגוסט?")

    low = text.lower()
    if "מעלית" in text or "elevator" in low:
        pros.append("מעלית/אינדיקציה למעלית")
    elif re.search(r"קומה\s*[3-9]|3rd floor|4th floor", low):
        cons.append("קומה גבוהה — לבדוק מעלית")
    if re.search(r"מזגן|מיזוג|air conditioner|ac", low):
        pros.append("אינדיקציה למזגן")
    else:
        missing.append("מזגנים")
    if re.search(r"מקלט|ממ[״\"]?ד|shelter|safe room", low):
        pros.append("מקלט/מרחב מוגן")
    # Check for broker mentions — but exclude "ללא תיווך" / "without brokerage" etc.
    has_no_broker = re.search(r'(?:ללא|בלי|no|without)\s*(?:a\s+)?(?:עמלת\s*)?(?:תיווך|broker(?:age)?|realtor|דמי תיווך)', low)
    if has_no_broker:
        pros.append("ללא תיווך")
    elif re.search(r'תיווך|broker(?:age)?|מתווך', low):
        cons.append("ייתכן תיווך")
        followup.append("לוודא אם יש דמי תיווך")

    cons = format_cons + cons
    missing.extend(["רטיבות/עובש", "רעש בפועל", "אפשרות חוזה ארוך"])

    if bad_format or ek == "early" or (rooms is not None and rooms < 2.5) or (price is not None and price > 7000) or (sqm is not None and sqm < 45):
        verdict = "no"
        confidence = "high"
    elif price is not None and price <= 6500 and rooms is not None and rooms >= 3 and ek in {"target", "flexible"} and area:
        verdict = "yes"
        confidence = "medium" if missing else "high"
    elif (price is None or price <= 7000) and (rooms is None or rooms >= 2.5):
        verdict = "maybe"
        confidence = "medium"
    else:
        verdict = "no"
        confidence = "medium"

    if verdict == "yes":
        reason = "מתאים לפרופיל הבסיסי: מחיר, חדרים, אזור ותאריך כניסה נראים רלוונטיים."
    elif verdict == "maybe":
        reason = "יש פוטנציאל, אבל חסרים פרטים או שיש דגל כניסה/תיווך/מחיר שדורש בדיקה."
    else:
        reason = "לא מתאים מספיק לפרופיל: " + (", ".join(cons[:3]) if cons else "חסר התאמה בסיסית")

    return {
        "id": post.get("id"),
        "cache_key": post.get("cache_key"),
        "verdict": verdict,
        "confidence": confidence,
        "is_real_listing": not bad_format,
        "reason_short": reason,
        "extracted": {
            "price": price,
            "rooms": rooms,
            "sqm": sqm,
            "area": area,
            "entry": entry,
            "address": address,
        },
        "pros": list(dict.fromkeys(pros))[:8],
        "cons": list(dict.fromkeys(cons))[:8],
        "missing": list(dict.fromkeys(missing))[:8],
        "followup_needed": list(dict.fromkeys(followup))[:6],
        "post_url": post.get("post_url"),
        "desktop_post_url": post.get("desktop_post_url"),
        "mobile_post_url": post.get("mobile_post_url"),
        "permalink_url": post.get("permalink_url"),
        "mobile_permalink_url": post.get("mobile_permalink_url"),
        "universal_post_url": post.get("universal_post_url"),
        "url_status": post.get("url_status"),
    }


def write_md(path: str | pathlib.Path, items: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    p = pathlib.Path(path)
    lines = ["# Facebook auto triage", "", f"Updated: {datetime.now().isoformat(timespec='seconds')}", "", f"Reviewed: {summary['posts_reviewed']} | yes={summary['relevant_yes']} | maybe={summary['relevant_maybe']} | no={summary['relevant_no']}", ""]
    order = {"yes": 0, "maybe": 1, "no": 2}
    for item in sorted(items, key=lambda x: (order.get(x.get("verdict"), 9), x.get("extracted", {}).get("price") or 999999)):
        ex = item.get("extracted") or {}
        lines += [
            f"## {item.get('verdict')} — {item.get('id')}",
            f"- {item.get('reason_short')}",
            f"- מחיר: {ex.get('price') or '?'} | חדרים: {ex.get('rooms') or '?'} | מ״ר: {ex.get('sqm') or '?'} | כניסה: {ex.get('entry') or '?'}",
            f"- אזור: {ex.get('area') or '?'}",
            f"- לינק: {item.get('universal_post_url') or 'אין לינק ישיר לפוסט'}",
            "",
        ]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input_json", help="ai_triage_input_*.json from facebook_ai_triage_prepare.py")
    ap.add_argument("--out", help="triage output JSON; defaults to input's output_json")
    ap.add_argument("--out-md", help="triage output MD; defaults to input's output_md")
    args = ap.parse_args()

    data = load_json(args.input_json)
    posts = data.get("posts") or []
    items = [triage_post(p) for p in posts]
    summary = {
        "posts_reviewed": len(items),
        "relevant_yes": sum(1 for i in items if i.get("verdict") == "yes"),
        "relevant_maybe": sum(1 for i in items if i.get("verdict") == "maybe"),
        "relevant_no": sum(1 for i in items if i.get("verdict") == "no"),
        "quality_notes": [
            "Auto-triage is conservative; yes means clear fit, maybe means needs follow-up.",
            "Immediate/May/June entry is rejected for now, even if marked flexible; reconsider only closer to June.",
        ],
    }
    payload = {"summary": summary, "items": items}
    out_json = args.out or data.get("output_json")
    out_md = args.out_md or data.get("output_md")
    if not out_json or not out_md:
        raise SystemExit("Missing output path; pass --out and --out-md")
    write_json(out_json, payload)
    write_md(out_md, items, summary)
    print("wrote", out_json)
    print("wrote", out_md)
    print("summary", summary)


if __name__ == "__main__":
    main()
