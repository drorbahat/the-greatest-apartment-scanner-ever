#!/usr/bin/env python3
"""Deterministic evaluation layer for apartment scan observations.

Pure functions. No DB, no network, no AI. Every input gets one output with a status.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any

from facebook_url_utils import classify_facebook_url, normalize_facebook_item_urls

# ---------------------------------------------------------------------------
# Normalized fields integration (Task 7 — guarded, disabled by default)
# ---------------------------------------------------------------------------

def _use_normalized() -> bool:
    return os.environ.get("SCANNER_AI_NORMALIZER_USE_IN_EVALUATOR") == "1"


def _normalized_field(
    norm: dict[str, Any],
    field: str,
    *,
    require_quote: bool = True,
    min_confidence: set[str] | None = None,
) -> Any:
    """Safely extract a field from normalized listing with guards.

    Returns the value only if normalization_status is ok, confidence is sufficient,
    and (for critical fields) an evidence quote exists.
    """
    if min_confidence is None:
        min_confidence = {"high", "medium"}

    if norm.get("normalization_status") != "ok":
        return None

    conf = (norm.get("confidence") or {}).get(field, "unknown")
    if conf not in min_confidence:
        return None

    val = norm.get(field)
    if val is None:
        return None

    if require_quote:
        quote = (norm.get("evidence_quotes") or {}).get(field)
        if not quote:
            return None

    return val


def _load_normalized_by_listing_id(run_dir: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Load normalized_listings.json and index by listing_id."""
    path = run_dir / "normalized_listings.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return {}
    return {n.get("listing_id"): n for n in data if n.get("listing_id")}


def _listing_id_for_observation(item: dict[str, Any]) -> str:
    """Build listing_id consistent with evidence_pack.py logic."""
    try:
        import evidence_pack as _ep
        return _ep.listing_id_for_observation(item)
    except Exception:
        source = str(item.get("source") or "unknown")
        for key in ("source_item_id", "source_key", "post_url", "url", "canonical_url"):
            val = item.get(key)
            if val:
                return f"{source}:{val}"
        return f"{source}:hash:{hash(str(item.get('text') or ''))}"


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def facebook_url_status(url: str | None) -> str:
    """Classify a Facebook URL."""
    return classify_facebook_url(url)


def choose_canonical_url(item: dict[str, Any]) -> tuple[str | None, str]:
    """Return (canonical_url, url_status). Prefer universal permalink for Facebook posts."""
    if item.get("source") == "Facebook" or item.get("post_url") or item.get("crosspost_urls"):
        bundle = normalize_facebook_item_urls(item)
        if bundle.get("universal_post_url"):
            return bundle.get("universal_post_url"), "valid_post"
        # Avoid surfacing profile/group URLs as post links.
        return None, str(bundle.get("url_status") or "missing")
    url = item.get("url")
    return url, "valid_post" if url else "missing"


# ---------------------------------------------------------------------------
# Listing-type classifier
# ---------------------------------------------------------------------------

_OFFICE_SIGNALS = [
    "משרד", "קליניקה", "עורכי דין", "רואי חשבון", "+ מע״מ", "+מע״מ",
    "משרדים", "office", "clinic", "מרפאה", "סטודיו לאמן",
]

# Page chrome containing signal words that are not listing content.
# Yad2: nav categories and broker links. Facebook: broker agency footer signatures.
_CHROME_STRIP = [
    # Yad2 top nav
    "עסקים למכירה",                # nav link — triggers "למכירה" sale signal
    "משרד וריהוט",                 # nav category — triggers "משרד" office signal
    # Yad2 bottom search promo block
    "מחפשים דירה להשכרה",          # search CTA — triggers "מחפשים דירה" wanted signal
    "מחפשים דירות להשכרה",         # search CTA plural form
    # Yad2 footer category links
    "דירות למכירה",                 # footer link — triggers "למכירה" sale signal
    "בתים למכירה",                  # footer link — triggers "למכירה" sale signal
    # Yad2 broker agency pages
    "לאתר המשרד",                   # broker link — triggers "משרד" office signal
    # Facebook broker agency footer signatures
    "פרופיל המשרד",                 # broker footer — triggers "משרד" office signal
    "לדירוג המשרדים",               # broker footer — triggers "משרד" office signal
    "לפרטים",                      # prevent 'פרטי' (no broker) signal from matching 'for details'
]


def _strip_chrome(text: str) -> str:
    for pat in _CHROME_STRIP:
        text = text.replace(pat, "")
    return text

_SALE_SIGNALS = [
    "למכירה", "for sale", "marketing price", "seller", "property tour",
    "asking price", "for investment", "investment or residence", "taboo", "written in the taboo",
    "price reduction", "reduced from", "million", "the master of the real estate",
    "מחיר שיווק", "פרויקט חדש", "קבלן", "new construction",
    "טרום השקה", "השקעה", "טאבו", "בטאבו", "אטבו", "מיליון", "ירידת מחיר", "למגורים או השקעה",
]

_ROOMMATE_SIGNALS = [
    "שותף", "שותפה", "חדר בדירת", "roommate", "מחפש שותף", "מחפשת שותפה",
]

_WANTED_SIGNALS = [
    "מחפשים דירה", "looking for apartment", "מחפש דירה", "מחפשת דירה",
    "דרושה דירה", "רוצים לשכור",
]

_SUBLET_SIGNALS = [
    "סאבלט", "sublet", "לתקופה קצרה",
    "חוזה עד", "העברת חוזה", "מסתיים ב",
    "contract lasts until", "entry into the contract lasts until",
    "contract expires", "contract transfer", "swapping the contract",
]

# Signals that the "half room" is not a real closed room.
# Feedback: open foyer / glass-walled space does NOT count as 2.5 rooms.
_OPEN_HALF_ROOM_SIGNALS = [
    "מבואה", "מבואה פתוחה", "מבואה גדולה", "מבואה מרווחת",
    "חלל פתוח", "חלל מואר", "זכוכית", "חלונות זכוכית",
    "מחיצת זכוכית", "גג זכוכית", "סקיילייט",
    "open foyer", "glass wall", "glass partition", "loft",
]

_PRIMARY_LOCATION_SIGNALS = ["גבעתיים", "רמת גן", "יד אליהו", "ביצרון", "רמת ישראל"]
_SECONDARY_LOCATION_SIGNALS = ["הצפון הישן", "בזל", "לב העיר", "לב תל אביב"]
_OUTSIDE_LOCATION_SIGNALS = [
    "פלורנטין", "שפירא", "נווה שאנן", "התקווה", "רמת אביב",
    "צהלה", "נווה שרת", "כפר שלם", "בני ברק", "דרום תל אביב",
]


def _first_matching_signal(text: str, signals: list[str]) -> str | None:
    for sig in signals:
        if sig.lower() in text:
            return sig
    return None


def _location_haystack(item: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("text", "address", "group_name", "city", "neighborhood", "area"):
        value = item.get(key)
        if value:
            pieces.append(str(value))
    raw_json = item.get("raw_json")
    if isinstance(raw_json, dict):
        loc = raw_json.get("location") if isinstance(raw_json.get("location"), dict) else {}
        for key in ("city", "neighborhood", "area", "street"):
            value = raw_json.get(key)
            if value:
                pieces.append(str(value))
            value = loc.get(key)
            if value:
                pieces.append(str(value))
    return " ".join(pieces).strip()


def classify_location(item: dict[str, Any]) -> dict[str, Any]:
    """Return a coarse area status for deterministic gating."""
    hay = _location_haystack(item).lower()
    if not hay:
        return {"location_status": "unknown", "location_evidence": ""}

    matched = _first_matching_signal(hay, _PRIMARY_LOCATION_SIGNALS)
    if matched:
        return {"location_status": "primary", "location_evidence": matched}

    matched = _first_matching_signal(hay, _SECONDARY_LOCATION_SIGNALS)
    if matched:
        return {"location_status": "secondary", "location_evidence": matched}

    matched = _first_matching_signal(hay, _OUTSIDE_LOCATION_SIGNALS)
    if matched:
        return {"location_status": "outside", "location_evidence": matched}

    return {"location_status": "unknown", "location_evidence": ""}


def classify_listing_type(text: str) -> str:
    """Return listing_type: rental_apartment | office | sale | roommate | wanted | sublet | unknown."""
    t = _strip_chrome(text or "").lower()
    for sig in _OFFICE_SIGNALS:
        if sig.lower() in t:
            return "office"
    for sig in _SALE_SIGNALS:
        if sig.lower() in t:
            return "sale"
    for sig in _ROOMMATE_SIGNALS:
        if sig.lower() in t:
            return "roommate"
    for sig in _WANTED_SIGNALS:
        if sig.lower() in t:
            return "wanted"
    for sig in _SUBLET_SIGNALS:
        if sig.lower() in t:
            return "sublet"
    return "rental_apartment"


# ---------------------------------------------------------------------------
# Broker classifier
# ---------------------------------------------------------------------------

_BROKER_YES = [
    "re/max", "remax", "re max", "תיווך", "מתווך", "מתווכת", "broker",
    "realtor", "real estate", "נדל״ן", "נדל\"ן", "סוכנות", "agency", "תיווך:",
    "anglo saxon", "אנגלו סקסון", "קורין נדלן", "בן עמי", "ארד נדלן",
    "משרד תיווך",
]

_BROKER_NO = [
    "ללא תיווך", "ללא עמלת תיווך", "בלי תיווך", "לא תיווך",
    "without mediation", "without a brokerage", "without brokerage",
    "no broker", "no brokerage", "no realtor",
    "without realtor", "no realtor fees",
    "בעל הדירה", "בעלת הדירה", "פרטי",
]


def classify_broker(text: str) -> str:
    """Return: no_broker | broker | suspected_broker | unknown_broker."""
    t = _strip_chrome(text or "").lower()
    for sig in _BROKER_NO:
        if sig.lower() in t:
            return "no_broker"
    for sig in _BROKER_YES:
        if sig.lower() in t:
            return "broker"
    return "unknown_broker"


# ---------------------------------------------------------------------------
# Entry-date classifier
# ---------------------------------------------------------------------------

_IMMEDIATE = ["מיידית", "מידית", "מידי", "פנויה עכשיו", "available now",
              "immediate", "מעבר מיידי", "כניסה עכשיו"]

_MAY_WORDS = ["מאי", " 5/", "/5/", "1.5", "15.5", "1/5", "15/5", "may "]

# April and earlier months — always too early, never relevant
_APRIL_WORDS = ["אפריל", " 4/", "/4/", "1.4", "1/4", "april", " 04/"]
# Generic early-month date patterns: 01/MM/2026 or 1/MM/26 where MM is 01-05
_EARLY_MONTH_RE = re.compile(
    r'(?:0[1-9]|[12][0-9]|[3][01])[/\.\-](?:0[1-5]|[1-5])[/\.\-](?:2026|26)'
)

# "until july/august" — short-term sublet whose availability ENDS in July/August.
# "עד אוגוסט" ≠ "כניסה באוגוסט". Must be checked before the july_aug ideal block.
_UNTIL_JULY_AUG = re.compile(
    r'(?<!לא )עד\s+(?:סוף\s+)?(?:יולי|אוגוסט|07|08)'
    r'|מסתיים\s+(?:ב|ב-)?(?:יולי|אוגוסט)'
    r'|פנוי\s+עד\s+(?:יולי|אוגוסט)'
    r'|until\s+(?:july|august|aug|jul)',
    re.IGNORECASE,
)


def classify_entry(text: str, extracted_entry: str | None) -> dict[str, Any]:
    """Return entry classification with direction awareness."""
    raw = extracted_entry or ""
    combined = f"{raw} {text or ''}".lower()

    # "immediate" also catches "כניסה מיידית + חידוש חוזה ביולי"
    # Note: even if they say "can renew in July", immediate entry is a hard flag.
    for sig in _IMMEDIATE:
        if sig in combined:
            return {"entry_status": "immediate_hard_flag", "entry_raw": raw,
                    "needs_followup": False, "followup_question": ""}

    # April and earlier — always rejected
    for sig in _APRIL_WORDS:
        if sig in combined:
            return {"entry_status": "too_early", "entry_raw": raw,
                    "needs_followup": False, "followup_question": ""}
    # Hebrew date format: DD/MM or DD/MM/YY(YY) — e.g., "7/5" = May 7, "01/04/2026" = April 1
    _HEBREW_DATE = re.search(r'(?<!\d)([0-9]{1,2})/([0-9]{1,2})(?:/([0-9]{2,4}))?(?!\d)', combined)
    if _HEBREW_DATE:
        day_s, month_s, year_s = _HEBREW_DATE.group(1), _HEBREW_DATE.group(2), _HEBREW_DATE.group(3)
        month = int(month_s)
        year = int(year_s) if year_s else 2026  # assume current target year
        # Only classify if year is 2026 or not specified
        if year_s is None or year == 2026 or year == 26:
            if month in (1, 2, 3, 4):
                return {"entry_status": "too_early", "entry_raw": raw,
                        "needs_followup": False, "followup_question": ""}
            elif month == 5:
                return {"entry_status": "may_bad", "entry_raw": raw,
                        "needs_followup": False, "followup_question": ""}
            elif month == 6:
                # June — always ask if late July possible
                return {"entry_status": "june_maybe_if_later", "entry_raw": raw,
                        "needs_followup": True, "followup_question": "לשאול: האם אפשר סוף יולי / תחילת אוגוסט?"}
            elif month in (7, 8):
                return {"entry_status": "ideal_july_august", "entry_raw": raw,
                        "needs_followup": False, "followup_question": ""}

    # Early month date regex (covers formats the Hebrew regex might miss)
    if _EARLY_MONTH_RE.search(combined):
        return {"entry_status": "too_early", "entry_raw": raw,
                "needs_followup": False, "followup_question": ""}

    # May — word-based fallback (covers "מאי", "may" etc.)
    for sig in _MAY_WORDS:
        if sig in combined:
            return {"entry_status": "may_bad", "entry_raw": raw,
                    "needs_followup": False, "followup_question": ""}

    # "עד אוגוסט/יולי" — sublet/short-term that ENDS in July/August, not starts.
    # Must precede the july_aug ideal check or "עד אוגוסט" would pass as ideal.
    if _UNTIL_JULY_AUG.search(combined):
        return {"entry_status": "bad_sublet_end", "entry_raw": raw,
                "needs_followup": False, "followup_question": ""}

    # June — check BEFORE july/august, because "יוני, אפשר גם ביולי" is june-maybe-if-later
    june_words = ["יוני", " 6/", "1.6", "1/6", "15.6", "june"]
    has_june = any(sig in combined for sig in june_words)
    if has_june:
        later_signals = ["אחרי", "אחר כך", "לאחר", "גם ביולי", "גם באוגוסט",
                         "ביולי", "באוגוסט", "מאוחר יותר", "ניתן מאוחר"]
        earlier_signals = ["לפני", "מוקדם", "מוקדמת", "אפשר לפני"]

        has_later = any(s in combined for s in later_signals)
        has_earlier = any(s in combined for s in earlier_signals)

        if has_later and not has_earlier:
            return {"entry_status": "june_maybe_if_later", "entry_raw": raw,
                    "needs_followup": True, "followup_question": "לשאול: האם אפשר להתחיל חוזה בסוף יולי?"}
        if has_earlier and not has_later:
            return {"entry_status": "bad_too_early", "entry_raw": raw,
                    "needs_followup": False, "followup_question": ""}
        if has_later and has_earlier:
            return {"entry_status": "june_maybe_if_later", "entry_raw": raw,
                    "needs_followup": True, "followup_question": "לשאול: האם אפשר סוף יולי?"}
        # June without direction
        return {"entry_status": "june_maybe_if_later", "entry_raw": raw,
                "needs_followup": True, "followup_question": "לשאול: האם אפשר סוף יולי / תחילת אוגוסט?"}

    # ISO date format YYYY-MM (e.g., "2026-06" = June 2026) — must check BEFORE July/August
    # Covers entry_raw values like "2026-06", "2026-07", "2026-08"
    _ISO_DATE = re.search(r'(?<![\d/])([0-9]{4})-([0-9]{2})(?![\d/])', combined)
    if _ISO_DATE:
        iso_year = int(_ISO_DATE.group(1))
        iso_month = int(_ISO_DATE.group(2))
        if iso_year in (2026, 26):
            if iso_month == 6:
                return {"entry_status": "june_maybe_if_later", "entry_raw": raw,
                        "needs_followup": True, "followup_question": "לשאול: האם אפשר סוף יולי / תחילת אוגוסט?"}
            elif iso_month in (7, 8):
                return {"entry_status": "ideal_july_august", "entry_raw": raw,
                        "needs_followup": False, "followup_question": ""}

    # July/August — ideal (checked after June so "יוני, אפשר גם ביולי" goes to june path)
    july_aug = ["יולי", "אוגוסט", " 7/", " 8/", "1.7", "1.8", "1/7", "1/8",
                "15.7", "15.8", "july", "august", "סוף יולי", "תחילת אוגוסט"]
    for sig in july_aug:
        if sig in combined:
            return {"entry_status": "ideal_july_august", "entry_raw": raw,
                    "needs_followup": False, "followup_question": ""}

    # Flexible without date
    if "גמיש" in combined or "flexib" in combined:
        return {"entry_status": "unknown_entry", "entry_raw": raw,
                "needs_followup": True, "followup_question": "לשאול: מה תאריך הכניסה? האפשר סוף יולי?"}

    return {"entry_status": "unknown_entry", "entry_raw": raw,
            "needs_followup": True, "followup_question": "לשאול: מה תאריך הכניסה?"}


# ---------------------------------------------------------------------------
# Main evaluator
# ---------------------------------------------------------------------------

def evaluate_item(item: dict[str, Any], *, use_normalized: bool = False) -> dict[str, Any]:
    """Evaluate a single normalized raw item. Returns a full evaluated candidate dict.

    If use_normalized=True and item has an attached 'normalized_listing', the evaluator
    may use normalized fields to fill gaps or detect conflicts. Deterministic fields
    remain primary; conflicts route to needs_review.
    """
    text = item.get("text") or ""
    price = item.get("price")
    rooms = item.get("rooms")
    entry_raw = item.get("entry_raw") or item.get("entry_date_raw") or item.get("entry") or ""

    # Normalized overlay (guarded)
    norm = item.get("normalized_listing") if use_normalized else None
    norm_price = None
    norm_rooms = None
    norm_entry_raw = None
    norm_entry_date = None
    norm_listing_type = None
    norm_broker_status = None
    norm_contract_type = None
    norm_half_room_status = None
    norm_features = None
    norm_red_flags = None

    if norm:
        norm_price = _normalized_field(norm, "price_nis")
        norm_rooms = _normalized_field(norm, "rooms")
        norm_entry_raw = _normalized_field(norm, "entry_raw")
        norm_entry_date = _normalized_field(norm, "entry_date")
        norm_listing_type = _normalized_field(norm, "listing_type", require_quote=False)
        norm_broker_status = _normalized_field(norm, "broker_status")
        norm_contract_type = _normalized_field(norm, "contract_type", require_quote=False)
        norm_half_room_status = _normalized_field(norm, "half_room_status")
        norm_features = norm.get("features")
        norm_red_flags = norm.get("red_flags")

    # Conflict detection helpers
    flags: list[str] = []

    # Price conflict
    if isinstance(price, (int, float)) and isinstance(norm_price, (int, float)):
        if abs(price - norm_price) > 300:
            flags.append(f"סתירה במחיר: סקרייפר ₪{price} vs AI ₪{norm_price}")

    # Rooms conflict
    if isinstance(rooms, (int, float)) and isinstance(norm_rooms, (int, float)):
        if abs(rooms - norm_rooms) >= 0.5:
            flags.append(f"סתירה בחדרים: סקרייפר {rooms} vs AI {norm_rooms}")

    # Use normalized to fill gaps only when raw is missing
    if price is None and norm_price is not None:
        price = norm_price
    if rooms is None and norm_rooms is not None:
        rooms = norm_rooms
    if not entry_raw and (norm_entry_raw or norm_entry_date):
        entry_raw = norm_entry_raw or norm_entry_date

    # URL
    canonical_url, url_status = choose_canonical_url(item)

    # Location
    location = classify_location(item)
    location_status = location["location_status"]
    location_evidence = location["location_evidence"]

    # Listing type
    listing_type = classify_listing_type(text)

    # Broker — prefer normalized only if it provides a clear signal and raw is unknown
    broker_status = classify_broker(text)
    if broker_status == "unknown_broker" and norm_broker_status in ("broker", "no_broker", "suspected_broker"):
        broker_status = norm_broker_status
    elif broker_status != "unknown_broker" and norm_broker_status and broker_status != norm_broker_status:
        flags.append(f"סתירה בתיווך: סקרייפר={broker_status} vs AI={norm_broker_status}")

    # Entry
    entry = classify_entry(text, entry_raw)
    entry_status = entry["entry_status"]

    # If deterministic entry is unknown and normalized gives ideal with quote, allow it
    if entry_status == "unknown_entry" and norm:
        evidence_quotes = norm.get("evidence_quotes") or {}
        has_entry_quote = bool(evidence_quotes.get("entry_raw") or evidence_quotes.get("entry_date"))
        entry_hint = norm.get("entry_status_hint") if has_entry_quote else None
        if entry_hint in ("ideal_july_august", "june_maybe_if_later") and (norm_entry_raw or norm_entry_date):
            entry_status = entry_hint
            entry["entry_status"] = entry_status

    # Facts — only from explicit sources, no inference
    facts: list[dict[str, Any]] = []
    if price is not None:
        facts.append({"field": "price", "value": price, "source": "extracted", "confidence": "high"})
    if rooms is not None:
        facts.append({"field": "rooms", "value": rooms, "source": "extracted", "confidence": "high"})

    # Reject reasons and flags
    reject_reasons: list[str] = []
    missing: list[str] = []
    manual_review_reasons: list[str] = []

    # Technical failure: bad URL
    if url_status in ("profile_url", "missing"):
        reject_reasons.append("bad_url_extraction")

    # Non-rental → invalid
    if listing_type != "rental_apartment":
        return {
            "source": item.get("source", "unknown"),
            "canonical_url": canonical_url,
            "url_status": url_status,
            "processing_status": "evaluated",
            "listing_type": listing_type,
            "price": price,
            "rooms": rooms,
            "entry_status": entry_status,
            "entry_raw": entry_raw,
            "broker_status": broker_status,
            "location_status": location_status,
            "location_evidence": location_evidence,
            "quality_status": "invalid",
            "recommended_action": "reject",
            "reject_reasons": reject_reasons or [f"not_rental_{listing_type}"],
            "flags": [],
            "missing": [],
            "facts": facts,
            "score": -1000,
        }

    # Missing price
    if price is None:
        missing.append("price")

    # Over budget — hard reject
    if price is not None and price > 6500:
        reject_reasons.append("over_budget")

    # Suspiciously low price (parsing error)
    if price is not None and price < 1500:
        reject_reasons.append("suspiciously_low_price")
        flags.append("מחיר נמוך באופן חשוד — כנראה טעות חילוץ")

    # Price too low for room count (parsing error)
    if price is not None and price < 4000 and rooms is not None and rooms >= 3:
        reject_reasons.append("price_too_low_for_rooms")
        flags.append("מחיר נמוך מאוד לרמת החדרים — טעות חילוץ כנראה")

    # Broker at ceiling
    if broker_status == "broker" and price is not None and price >= 6500:
        reject_reasons.append("broker_at_budget_ceiling")
        flags.append("תיווך במחיר 6,500 — דיל ברייקר")

    # Entry hard flags
    if entry_status == "immediate_hard_flag":
        reject_reasons.append("immediate_entry")
        flags.append("כניסה מיידית — לא רלוונטי ליולי/אוגוסט")
    elif entry_status == "bad_sublet_end":
        reject_reasons.append("sublet_ends_july_august")
    elif entry_status == "too_early":
        reject_reasons.append("too_early")
        flags.append(f"כניסה מוקדמת מדי ({entry_raw}) — לא יולי/אוגוסט")
    elif entry_status == "may_bad":
        reject_reasons.append("entry_may")
        flags.append("כניסה במאי — מוקדם מדי")
    elif entry_status == "bad_too_early":
        reject_reasons.append("entry_too_early")
        flags.append(f"כניסה מוקדמת ({entry_raw}) — לא רלוונטי")

    # Room-size gate — 2 חדרים ומטה לא רלוונטיים, ו-2.5 רק אם החצי-חדר סגור באמת.
    if isinstance(rooms, (int, float)):
        if rooms < 2.5:
            reject_reasons.append("too_few_rooms")
            flags.append("פחות מ־2.5 חדרים — לא רלוונטי")
        elif rooms < 3:
            text_lower = (text or "").lower()
            has_open_half = any(sig.lower() in text_lower for sig in _OPEN_HALF_ROOM_SIGNALS)
            if has_open_half:
                reject_reasons.append("open_half_room_not_counted")
                flags.append("חצי חדר פתוח/זכוכית — לא נחשב כחדר סגור")
            elif norm_half_room_status == "open":
                reject_reasons.append("open_half_room_not_counted")
                flags.append("חצי חדר פתוח (AI) — לא נחשב כחדר סגור")
            elif norm_half_room_status == "unclear":
                manual_review_reasons.append("half_room_unclear")
                flags.append("חצי חדר לא ברור — לבדוק אם סגור עם דלת")
            elif norm_half_room_status == "closed":
                pass
            else:
                manual_review_reasons.append("half_room_uncertain")
                flags.append("2.5 חדרים — צריך לוודא שחצי החדר סגור")

    # Contract type from normalized — affects routing but not automatic open
    if norm_contract_type == "short_term":
        reject_reasons.append("short_term_contract")
    elif norm_contract_type == "sublet":
        if entry_status == "ideal_july_august":
            # Sublet that says ideal entry is suspicious — probably ends then
            entry_status = "bad_sublet_end"
            entry["entry_status"] = entry_status
            reject_reasons.append("sublet_ends_july_august")
        else:
            manual_review_reasons.append("sublet_contract")
            flags.append("סאבלט — לבדיקה ידנית")
    elif norm_contract_type == "contract_transfer":
        manual_review_reasons.append("contract_transfer")
        flags.append("העברת חוזה — לבדיקה ידנית, לא מועמדת חזקה אוטומטית")

    # Deterministic location gating
    if location_status == "outside":
        reject_reasons.append("out_of_target_area")
        flags.append(f"אזור מחוץ לטווח המועדף — {location_evidence}")

    # Determine quality status and recommended action
    quality_status = "candidate"
    processing_status = "evaluated"
    recommended_action = "open"

    # Price conflict with normalized → needs_review
    price_conflict = any("סתירה במחיר" in f for f in flags)
    rooms_conflict = any("סתירה בחדרים" in f for f in flags)
    broker_conflict = any("סתירה בתיווך" in f for f in flags)

    if url_status in ("profile_url", "missing"):
        quality_status = "technical_failure"
        processing_status = "technical_failure"
        recommended_action = "recover_url_before_showing"
    elif price_conflict:
        quality_status = "needs_review"
        recommended_action = "manual_check_price_with_source"
    elif rooms_conflict:
        quality_status = "needs_review"
        recommended_action = "manual_check_rooms_with_source"
    elif broker_conflict:
        quality_status = "needs_review"
        recommended_action = "manual_check_broker_with_source"
    elif reject_reasons:
        quality_status = "rejected"
        if "immediate_entry" in reject_reasons and len(reject_reasons) == 1:
            recommended_action = "ignore_unless_exceptional"
        elif "broker_at_budget_ceiling" in reject_reasons:
            recommended_action = "reject"
        else:
            recommended_action = "reject"
    elif "contract_transfer" in manual_review_reasons:
        quality_status = "needs_review"
        recommended_action = "manual_check_contract_transfer"
    elif "sublet_contract" in manual_review_reasons:
        quality_status = "needs_review"
        recommended_action = "manual_check_contract_type"
    elif "half_room_unclear" in manual_review_reasons or "half_room_uncertain" in manual_review_reasons:
        quality_status = "needs_review"
        recommended_action = "manual_check_half_room"
    elif entry_status == "unknown_entry":
        quality_status = "needs_review"
        recommended_action = "manual_check_entry_with_source"
        flags.append("תאריך כניסה לא צוין — לבדיקה ידנית")
    elif entry_status == "ideal_july_august" and "price" in missing:
        quality_status = "needs_review"
        recommended_action = "ask_price"
    elif entry_status in ("june_maybe_if_later",) and "price" not in missing:
        quality_status = "needs_review"
        recommended_action = "ask_if_late_july_possible"
    elif entry_status in ("june_maybe_if_later",) and "price" in missing:
        quality_status = "needs_review"
        recommended_action = "ask_price"
    elif entry_status == "may_bad":
        # Already handled above in reject_reasons — just set action
        quality_status = "rejected"
        recommended_action = "ignore_unless_exceptional"
    elif entry_status == "too_early":
        quality_status = "rejected"
        recommended_action = "ignore_unless_exceptional"
    elif entry_status == "bad_too_early":
        quality_status = "rejected"
        recommended_action = "ignore_unless_exceptional"

    # Broker at lower price — flag but maybe acceptable
    if broker_status == "broker" and (price is None or price < 6500) and quality_status == "candidate":
        recommended_action = "ask_if_broker_acceptable"

    # Red flags from normalized (informational only, do not override deterministic decisions)
    if norm_red_flags and isinstance(norm_red_flags, list):
        for rf in norm_red_flags:
            if rf and rf not in flags:
                flags.append(rf)

    return {
        "source": item.get("source", "unknown"),
        "canonical_url": canonical_url,
        "url_status": url_status,
        "processing_status": processing_status,
        "listing_type": listing_type,
        "price": price,
        "rooms": rooms,
        "entry_status": entry_status,
        "entry_raw": entry_raw,
        "broker_status": broker_status,
        "location_status": location_status,
        "location_evidence": location_evidence,
        "quality_status": quality_status,
        "recommended_action": recommended_action,
        "reject_reasons": reject_reasons,
        "flags": flags,
        "missing": missing,
        "facts": facts,
        "score": 0,
    }


def evaluate_items(items: list[dict[str, Any]], *, use_normalized: bool = False) -> list[dict[str, Any]]:
    """Evaluate all items. One output per input."""
    return [evaluate_item(item, use_normalized=use_normalized) for item in items]


# ---------------------------------------------------------------------------
# Load raw observations from run artifacts
# ---------------------------------------------------------------------------

def _load_json(path: pathlib.Path) -> dict | list | None:
    if not path.exists():
        return None
    import json as _json
    return _json.loads(path.read_text(encoding="utf-8"))


def load_raw_observations(run_dir: pathlib.Path | str, *, attach_normalized: bool = False) -> list[dict[str, Any]]:
    """Read manifest + source artifacts and return normalized raw observations.

    If attach_normalized=True and normalized_listings.json exists, attach
    matching normalized objects under 'normalized_listing' key.
    """
    run_dir = pathlib.Path(run_dir)
    manifest_path = run_dir / "manifest.json"
    manifest = _load_json(manifest_path) or {}
    observations: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def _add(obs: dict[str, Any]) -> None:
        key = f"{obs.get('source')}:{obs.get('source_item_id', '')}:{obs.get('source_key', '')}"
        if key not in seen_keys:
            seen_keys.add(key)
            observations.append(obs)

    # Facebook — clean posts (all posts, not just triage)
    fb_run = manifest.get("facebook_last_run") or {}
    clean_file = fb_run.get("clean_file")
    if clean_file:
        clean_data = _load_json(pathlib.Path(clean_file))
        # clean_posts is a dict with 'posts' list and 'clean_items' as count
        if isinstance(clean_data, dict):
            clean_items = clean_data.get("posts") or clean_data.get("clean_items") or []
        elif isinstance(clean_data, list):
            clean_items = clean_data
        else:
            clean_items = []
        if not isinstance(clean_items, list):
            clean_items = []
        for post in clean_items:
            _add({
                "source": "Facebook",
                "source_item_id": post.get("id") or post.get("post_id", ""),
                "source_key": post.get("post_url") or post.get("dedupe_key", ""),
                "post_url": post.get("post_url"),
                "crosspost_urls": post.get("crosspost_urls") or [],
                "text": post.get("text") or post.get("clean_text") or "",
                "group_name": post.get("group_name"),
                "price": post.get("price") or (post.get("heuristic") or {}).get("price"),
                "rooms": post.get("rooms") or (post.get("heuristic") or {}).get("rooms"),
                "entry_raw": post.get("entry") or (post.get("heuristic") or {}).get("entry"),
                "collected_at": fb_run.get("timestamp", ""),
                "scrape_status": "scraped",
                "raw_json": post,
            })

    # Yad2 — details file
    yad2_run = manifest.get("yad2") or manifest.get("yad2_broad_search") or manifest.get("yad2_last_run") or {}
    yad2_file = yad2_run.get("details_file") or yad2_run.get("output_file")
    if yad2_file:
        yad2_data = _load_json(pathlib.Path(yad2_file))
        if isinstance(yad2_data, list):
            for item in yad2_data:
                href = item.get("href") or item.get("detail_url") or item.get("url")
                item_id = item.get("id") or item.get("item_id")
                if not item_id and href:
                    m = __import__("re").search(r"/item/[^/]+/([a-zA-Z0-9]+)", href)
                    item_id = m.group(1) if m else ""
                # Extract entry date from detail_text
                detail_text = item.get("detail_text") or ""
                entry_raw = item.get("entry_date") or item.get("entry")
                if not entry_raw and detail_text:
                    em = __import__("re").search(r"\u05ea\u05d0\u05e8\u05d9\u05da \u05db\u05e0\u05d9\u05e1\u05d4[\s\u200c\u200b]*([^\n]{1,30})", detail_text)
                    if em:
                        entry_raw = em.group(1).strip().replace("\u200c", "").replace("\u200b", "").strip()
                _add({
                    "source": "Yad2",
                    "source_item_id": item_id or "",
                    "source_key": href or item_id or "",
                    "url": href,
                    "text": item.get("detail_text") or item.get("text") or item.get("description") or "",
                    "price": item.get("feed_price") or item.get("price"),
                    "rooms": item.get("feed_rooms") or item.get("rooms"),
                    "sqm": item.get("feed_sqm") or item.get("sqm") or item.get("area_sqm"),
                    "entry_raw": entry_raw,
                    "address": item.get("detail_title") or item.get("address") or item.get("street"),
                    "collected_at": yad2_run.get("timestamp", ""),
                    "scrape_status": "scraped",
                    "raw_json": item,
                })

    # Madlan — scan file
    madlan_run = manifest.get("madlan") or manifest.get("madlan_last_run") or manifest.get("madlan_broad_search") or {}
    madlan_file = madlan_run.get("file") or madlan_run.get("output_file") or madlan_run.get("scan_file")
    if madlan_file:
        madlan_data = _load_json(pathlib.Path(madlan_file))
        if isinstance(madlan_data, dict):
            madlan_data = madlan_data.get("items") or madlan_data.get("listings") or []
        if isinstance(madlan_data, list):
            for item in madlan_data:
                _add({
                    "source": "Madlan",
                    "source_item_id": item.get("id") or item.get("listing_id", ""),
                    "source_key": item.get("url") or item.get("id", ""),
                    "url": item.get("url"),
                    "text": item.get("text") or item.get("description") or "",
                    "price": item.get("price"),
                    "rooms": item.get("rooms"),
                    "sqm": item.get("sqm") or item.get("area"),
                    "entry_raw": item.get("entry") or item.get("entry_date"),
                    "address": item.get("address"),
                    "collected_at": madlan_run.get("timestamp") or madlan_run.get("fetched_at", ""),
                    "scrape_status": "scraped",
                    "raw_json": item,
                })

    # Attach normalized listings if requested
    if attach_normalized:
        normalized_by_id = _load_normalized_by_listing_id(run_dir)
        if normalized_by_id:
            for obs in observations:
                lid = _listing_id_for_observation(obs)
                if lid in normalized_by_id:
                    obs["normalized_listing"] = normalized_by_id[lid]
                else:
                    # Fallback: match by URL
                    url = obs.get("canonical_url") or obs.get("post_url") or obs.get("url")
                    if url:
                        for n in normalized_by_id.values():
                            if n.get("source_url") == url:
                                obs["normalized_listing"] = n
                                break

    return observations


# ---------------------------------------------------------------------------
# Batch evaluation + artifact generation
# ---------------------------------------------------------------------------

def evaluate_run(run_dir: pathlib.Path | str, *, use_normalized: bool | None = None) -> dict[str, Any]:
    """Load raw observations, evaluate all, write artifacts.

    use_normalized defaults from SCANNER_AI_NORMALIZER_USE_IN_EVALUATOR env var.
    """
    if use_normalized is None:
        use_normalized = _use_normalized()

    run_dir = pathlib.Path(run_dir)
    raw = load_raw_observations(run_dir, attach_normalized=use_normalized)
    evaluated = evaluate_items(raw, use_normalized=use_normalized)

    # Statistics
    stats: dict[str, Any] = {
        "total_raw": len(raw),
        "total_evaluated": len(evaluated),
        "by_source": {},
        "by_quality_status": {},
        "by_listing_type": {},
        "by_recommended_action": {},
    }
    for ev in evaluated:
        src = ev.get("source", "unknown")
        stats["by_source"][src] = stats["by_source"].get(src, 0) + 1
        qs = ev.get("quality_status", "unknown")
        stats["by_quality_status"][qs] = stats["by_quality_status"].get(qs, 0) + 1
        lt = ev.get("listing_type", "unknown")
        stats["by_listing_type"][lt] = stats["by_listing_type"].get(lt, 0) + 1
        ra = ev.get("recommended_action", "unknown")
        stats["by_recommended_action"][ra] = stats["by_recommended_action"].get(ra, 0) + 1

    # Write raw_observations.json
    _write_json(run_dir / "raw_observations.json", raw)

    # Write evaluated_candidates.json
    _write_json(run_dir / "evaluated_candidates.json", {"statistics": stats, "items": evaluated})

    # Write evaluation_audit.md
    audit_lines = [
        f"# Evaluation Audit",
        f"",
        f"- **Run:** {run_dir.name}",
        f"- **Raw observations:** {len(raw)}",
        f"- **Evaluated:** {len(evaluated)}",
        f"",
        f"## By Quality Status",
        f"",
    ]
    for status, count in sorted(stats["by_quality_status"].items()):
        audit_lines.append(f"- {status}: {count}")
    audit_lines.extend(["", "## By Listing Type", ""])
    for lt, count in sorted(stats["by_listing_type"].items()):
        audit_lines.append(f"- {lt}: {count}")
    audit_lines.extend(["", "## By Recommended Action", ""])
    for ra, count in sorted(stats["by_recommended_action"].items()):
        audit_lines.append(f"- {ra}: {count}")

    # Candidates to show
    candidates = [e for e in evaluated if e["recommended_action"] == "open"]
    asks = [e for e in evaluated if e["recommended_action"].startswith("ask_")]
    needs_review = [e for e in evaluated if e["recommended_action"] == "needs_review"]
    rejected = [e for e in evaluated if e["recommended_action"] == "reject"]
    tech_failures = [e for e in evaluated if e["processing_status"] == "technical_failure"]

    if candidates:
        audit_lines.extend(["", "## Strong Candidates", ""])
        for c in candidates:
            audit_lines.append(f"- [{c.get('canonical_url','')}] {c.get('source')} — "
                              f"₪{c.get('price','?')} — {c.get('rooms','?')} חד' — "
                              f"{c.get('entry_status','')}")

    if asks:
        audit_lines.extend(["", "## Worth Asking", ""])
        for a in asks:
            audit_lines.append(f"- [{a.get('canonical_url','')}] {a.get('source')} — "
                              f"₪{a.get('price','?')} — {a.get('recommended_action')}")

    if tech_failures:
        audit_lines.extend(["", "## Technical Failures", ""])
        for t in tech_failures:
            audit_lines.append(f"- {t.get('source')} — {t.get('url_status')} — {t.get('source_item_id','')}")

    if rejected:
        audit_lines.extend(["", f"## Rejected ({len(rejected)})", ""])
        reasons: dict[str, int] = {}
        for r in rejected:
            for reason in r.get("reject_reasons", []):
                reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            audit_lines.append(f"- {reason}: {count}")

    (run_dir / "evaluation_audit.md").write_text("\n".join(audit_lines), encoding="utf-8")

    return {
        "raw_count": len(raw),
        "evaluated_count": len(evaluated),
        "candidates": len(candidates),
        "asks": len(asks),
        "needs_review": len(needs_review),
        "rejected": len(rejected),
        "technical_failures": len(tech_failures),
        "stats": stats,
    }


def _write_json(path: pathlib.Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Evaluate candidates from a scan run")
    parser.add_argument("run_dir", help="Path to scan run directory")
    parser.add_argument("--json", action="store_true", help="Output summary as JSON")
    parser.add_argument("--use-normalized", action="store_true", dest="use_normalized",
                        help="Use normalized fields from normalized_listings.json")
    parser.add_argument("--no-normalized", action="store_true", dest="no_normalized",
                        help="Ignore normalized fields even if env flag is set")
    args = parser.parse_args()

    use_norm = _use_normalized()
    if args.use_normalized:
        use_norm = True
    if args.no_normalized:
        use_norm = False

    result = evaluate_run(args.run_dir, use_normalized=use_norm)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Raw: {result['raw_count']} → Evaluated: {result['evaluated_count']}")
        print(f"  Candidates: {result['candidates']}")
        print(f"  Worth asking: {result['asks']}")
        print(f"  Needs review: {result['needs_review']}")
        print(f"  Rejected: {result['rejected']}")
        print(f"  Technical failures: {result['technical_failures']}")


if __name__ == "__main__":
    main()
