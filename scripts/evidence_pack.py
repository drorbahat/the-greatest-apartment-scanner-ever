#!/usr/bin/env python3
"""Evidence pack builder for the AI normalizer layer.

Converts raw observations into compact, safe, token-controlled evidence packs.
This module is deterministic, has no side effects, and has no LLM dependencies.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

DEFAULT_MAX_TEXT_CHARS = 6000
DEFAULT_MAX_JSON_EXCERPT_CHARS = 1500

SECRETISH_KEYS = {
    "cookie", "cookies", "authorization", "token", "access_token", "password",
    "secret", "api_key", "apikey", "session", "sessionid",
}


def _compact_text(text: str | None, limit: int) -> str:
    """Strip whitespace and truncate to limit."""
    s = re.sub(r"\s+", " ", text or "").strip()
    return s[:limit]


def _safe_excerpt(value: Any, limit: int = DEFAULT_MAX_JSON_EXCERPT_CHARS) -> dict[str, Any]:
    """Extract only non-sensitive fields from raw_json for debugging/audit."""
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in [
        "id", "post_id", "post_url", "group_name", "address", "city",
        "neighborhood", "price", "rooms", "entry", "entry_date",
        "madlan_enrich_status",
    ]:
        if key in value and key.lower() not in SECRETISH_KEYS:
            safe[key] = value.get(key)
    encoded = json.dumps(safe, ensure_ascii=False, default=str)
    if len(encoded) > limit:
        return {"truncated": True, "preview": encoded[:limit]}
    return safe


def stable_content_hash(text: str, known_fields: dict[str, Any]) -> str:
    """Stable SHA-256 hash independent of dict key ordering."""
    payload = {
        "text": re.sub(r"\s+", " ", text or "").strip(),
        "known_fields": known_fields,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def listing_id_for_observation(obs: dict[str, Any]) -> str:
    """Build a listing_id from the observation. Must not be empty."""
    source = str(obs.get("source") or "unknown")
    for key in ("source_item_id", "source_key", "post_url", "url", "canonical_url"):
        value = obs.get(key)
        if value:
            return f"{source}:{value}"
    text = str(obs.get("text") or "")
    return f"{source}:hash:{hashlib.sha1(text.encode('utf-8')).hexdigest()}"


def infer_evidence_quality(obs: dict[str, Any], text: str) -> str:
    """Classify evidence quality for routing decisions."""
    source = str(obs.get("source") or "").lower()
    status = str(obs.get("madlan_enrich_status") or obs.get("enrichment_status") or "").lower()
    if not text or len(text.strip()) < 80:
        return "too_short"
    if source == "madlan":
        if status == "ok" and (
            obs.get("detail_text") or obs.get("entry_date_raw") or obs.get("entry_raw")
        ):
            return "full_detail"
        if status in {"blocked", "not_attempted_limit"}:
            return "blocked"
        return "list_card_only"
    return "full_detail"


def build_evidence_pack(
    obs: dict[str, Any],
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> dict[str, Any]:
    """Convert a raw observation into a compact evidence pack."""
    text = _compact_text(obs.get("text") or obs.get("raw_text"), max_text_chars)
    known_fields = {
        "price": obs.get("price"),
        "rooms": obs.get("rooms"),
        "sqm": obs.get("sqm"),
        "floor": obs.get("floor"),
        "entry_raw": obs.get("entry_raw") or obs.get("entry_date_raw") or obs.get("entry"),
        "address": obs.get("address"),
        "group_name": obs.get("group_name"),
    }
    return {
        "schema_version": "1.0",
        "source": obs.get("source") or "unknown",
        "source_url": obs.get("canonical_url") or obs.get("post_url") or obs.get("url"),
        "listing_id": listing_id_for_observation(obs),
        "content_hash": stable_content_hash(text, known_fields),
        "raw_text": text,
        "known_fields": known_fields,
        "source_metadata": {
            "source_item_id": obs.get("source_item_id"),
            "source_key": obs.get("source_key"),
            "url_status": obs.get("url_status"),
            "collected_at": obs.get("collected_at"),
            "scrape_status": obs.get("scrape_status"),
            "enrichment_status": (
                obs.get("madlan_enrich_status")
                or obs.get("enrichment_status")
                or "unknown"
            ),
            "human_action_required": bool(obs.get("human_action_required")),
            "evidence_quality": infer_evidence_quality(obs, text),
        },
        "raw_json_excerpt": _safe_excerpt(obs.get("raw_json")),
    }


def build_evidence_packs(
    observations: list[dict[str, Any]],
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> list[dict[str, Any]]:
    """Convert a list of raw observations to evidence packs."""
    return [
        build_evidence_pack(obs, max_text_chars=max_text_chars)
        for obs in observations
    ]


def should_send_to_normalizer(
    obs: dict[str, Any],
    *,
    include_reject_examples: bool = False,
) -> tuple[bool, str]:
    """Determine whether a given observation should be sent to the LLM normalizer.

    Conservative: returns True only when evidence looks meaningful and within budget.
    Returns (eligible, reason) tuple.
    """
    text = str(obs.get("text") or "").strip()
    if not text:
        return False, "empty_text"
    price = obs.get("price")
    if isinstance(price, (int, float)) and price > 7500 and not include_reject_examples:
        return False, "price_far_over_budget"
    rooms = obs.get("rooms")
    if isinstance(rooms, (int, float)) and rooms < 2.5 and not include_reject_examples:
        return False, "too_few_rooms"
    source = str(obs.get("source") or "").lower()
    if source == "madlan":
        quality = infer_evidence_quality(obs, text)
        if quality in {"blocked", "list_card_only", "too_short"} and not include_reject_examples:
            return False, f"madlan_{quality}"
    return True, "eligible"