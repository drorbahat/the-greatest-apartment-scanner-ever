#!/usr/bin/env python3
"""AI normalizer for apartment listings.

Converts evidence packs into canonical normalized JSON.
Supports offline stub mode (no LLM) and optional Gemini backend.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

# ── Schema constants (no jsonschema dep) ────────────────────────────────────────

REQUIRED_KEYS = [
    "schema_version",
    "normalization_status",
    "source",
    "listing_id",
    "content_hash",
    "listing_type",
    "broker_status",
    "contract_type",
    "half_room_status",
    "features",
    "red_flags",
    "missing_questions",
    "confidence",
    "evidence_quotes",
]

ENUM_RULES: dict[str, set[str]] = {
    "normalization_status": {
        "ok", "skipped_prefilter", "skipped_cached",
        "failed_llm", "failed_invalid_json",
        "failed_schema_validation", "failed_no_backend",
    },
    "listing_type": {
        "rental_apartment", "sublet", "contract_transfer",
        "roommate", "sale", "office", "wanted",
        "not_listing", "unknown",
    },
    "broker_status": {"no_broker", "broker", "suspected_broker", "unknown_broker"},
    "contract_type": {
        "regular", "sublet", "contract_transfer",
        "short_term", "renewal_with_landlord", "unknown",
    },
    "half_room_status": {"closed", "open", "unclear", "not_relevant"},
    "entry_status_hint": {
        "ideal_july_august", "june_maybe_if_later",
        "immediate_hard_flag", "too_early",
        "may_bad", "bad_sublet_end", "unknown_entry",
    },
}

CONF_LEVELS = {"high", "medium", "low", "unknown"}
SOURCE_ENUM = {"Facebook", "Yad2", "Madlan", "unknown"}

# ── Prompt loading ────────────────────────────────────────────────────────────

def load_prompt(prompt_path: pathlib.Path | str | None = None) -> str:
    """Load the normalizer system prompt from disk."""
    if prompt_path is None:
        prompt_path = pathlib.Path(__file__).parent.parent / "prompts" / "listing_normalizer.md"
    p = pathlib.Path(prompt_path)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return _default_prompt()


def _default_prompt() -> str:
    return """you are an apartment's apartment listing normalizer.
You are not the final evaluator.
Your job is to translate evidence into strict JSON.

Return only valid JSON.
Do not use markdown fences or any other markup.
Do not guess unknown fields.
Use null/unknown when not explicitly stated.
For every important field, include evidence_quotes.

Target move date: late July 2026.
Budget: up to ₪6,500.
Preferred rooms: 3; 2.5 only if the half-room is a closed usable room.

Important semantic distinctions:
- "כניסה באוגוסט" means entry in August.
- "חוזה עד אוגוסט" means the contract ends in August, not entry in August.
- "כניסה מיידית" is immediate entry even if renewal in July is mentioned.
- "גמיש" without a date is unknown_entry, not automatically good.
- A half-room described as מבואה/open foyer/glass/loft/open space is open, not closed.
- Broker/agency signals include תיווך, מתווך, נדל"ן, RE/MAX, real estate, agency.
- Wanted/search posts are not listings.
- Sale and office/commercial listings are not rental apartments.

Return JSON with these fields:
{
  "schema_version": "1.0",
  "normalization_status": "ok",
  "normalization_error": null,
  "source": "Facebook|Yad2|Madlan|unknown",
  "source_url": "...",
  "listing_id": "...",
  "content_hash": "...",
  "listing_type": "rental_apartment|sublet|contract_transfer|roommate|sale|office|wanted|not_listing|unknown",
  "price_nis": number_or_null,
  "rooms": number_or_null,
  "sqm": number_or_null,
  "floor": "N/M" or null,
  "city": "...",
  "neighborhood": "...",
  "street": "...",
  "entry_date": "YYYY-MM-DD or null",
  "entry_raw": "...",
  "entry_status_hint": "ideal_july_august|june_maybe_if_later|immediate_hard_flag|too_early|may_bad|bad_sublet_end|unknown_entry",
  "broker_status": "no_broker|broker|suspected_broker|unknown_broker",
  "contract_type": "regular|sublet|contract_transfer|short_term|renewal_with_landlord|unknown",
  "half_room_status": "closed|open|unclear|not_relevant",
  "features": [],
  "red_flags": [],
  "missing_questions": [],
  "confidence": {},
  "evidence_quotes": {},
  "model": null,
  "normalized_at": "..."
}
"""


# ── Cache helpers ─────────────────────────────────────────────────────────────

def cache_key(pack: dict[str, Any]) -> str:
    """Build a stable cache key from listing_id and content_hash."""
    lid = pack.get("listing_id", "")
    ch = pack.get("content_hash", "")
    return f"{lid}@{ch}"


def load_cache(path: pathlib.Path) -> dict[str, Any]:
    """Load cache JSON from disk. Returns empty dict on failure."""
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_cache(path: pathlib.Path, cache: dict[str, Any]) -> None:
    """Write cache JSON to disk atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── Core functions ─────────────────────────────────────────────────────────────

def normalize_pack_offline_stub(pack: dict[str, Any]) -> dict[str, Any]:
    """Return a schema-valid normalized listing using only known fields.

    This is the no-LLM fallback. It copies deterministic known fields
    and marks everything else as unknown/conservative.
    """
    known = pack.get("known_fields", {})
    source = pack.get("source", "unknown")

    # Confidence based on field origin
    price_conf = "high" if known.get("price") is not None else "unknown"
    rooms_conf = "high" if known.get("rooms") is not None else "unknown"
    sqm_conf = "medium" if known.get("sqm") is not None else "unknown"

    # Evidence quotes — copy only real values
    eq: dict[str, str] = {}
    if known.get("price") is not None:
        eq["price_nis"] = f"{known['price']}"
    if known.get("rooms") is not None:
        eq["rooms"] = f"{known['rooms']}"
    if known.get("entry_raw"):
        eq["entry_raw"] = str(known["entry_raw"])

    # Offline stub must not infer policy-sensitive entry status.
    # Copy entry_raw as evidence only; deterministic evaluator decides later.
    entry_hint = "unknown_entry"

    result: dict[str, Any] = {
        "schema_version": "1.0",
        "normalization_status": "ok",
        "normalization_error": None,
        "source": source if source in SOURCE_ENUM else "unknown",
        "source_url": pack.get("source_url"),
        "listing_id": pack.get("listing_id", ""),
        "content_hash": pack.get("content_hash", ""),
        "listing_type": "unknown",
        "price_nis": known.get("price"),
        "rooms": known.get("rooms"),
        "sqm": known.get("sqm"),
        "floor": known.get("floor"),
        "city": known.get("address"),   # partial mapping
        "neighborhood": None,
        "street": None,
        "entry_date": None,
        "entry_raw": known.get("entry_raw"),
        "entry_status_hint": entry_hint,
        "broker_status": "unknown_broker",
        "contract_type": "unknown",
        "half_room_status": "not_relevant",
        "features": [],
        "red_flags": [],
        "missing_questions": [],
        "confidence": {
            "price_nis": price_conf,
            "rooms": rooms_conf,
            "sqm": sqm_conf,
            "entry_date": "unknown",
            "broker_status": "unknown",
            "contract_type": "unknown",
            "half_room_status": "unknown",
        },
        "evidence_quotes": eq,
        "model": None,
        "normalized_at": datetime.now(timezone.utc).isoformat(),
    }

    # Mark unusable evidence as prefiltered; normal no-LLM stub outputs are ok.
    sm = pack.get("source_metadata", {})
    if (
        sm.get("enrichment_status") in {"blocked", "skipped_block_in_run", "not_attempted_limit"}
        or sm.get("evidence_quality") in {"blocked", "list_card_only", "too_short"}
    ):
        result["normalization_status"] = "skipped_prefilter"
        if result["source"] == "Madlan":
            result["missing_questions"].append("כניסה — לא ניתן לאמת מפרסום חסום/חלקי")

    return result


def parse_llm_json(text: str) -> dict[str, Any]:
    """Parse LLM response text into JSON.

    Handles plain JSON, markdown-fenced JSON (```json ... ```),
    and common LLM JSON errors like unescaped quotes inside strings.
    Returns empty dict on parse failure.
    """
    text = text.strip()

    # Strip markdown fences
    fenced = re.match(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON from mixed content
    # Find first { and last }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        subset = text[first_brace : last_brace + 1]
        try:
            return json.loads(subset)
        except json.JSONDecodeError:
            pass

        # Fix common LLM JSON error: unescaped quotes inside Hebrew strings
        # Pattern: "text"more text" -> "text\"more text"
        # We need to be careful not to break legitimate JSON
        try:
            fixed = _fix_unescaped_quotes(subset)
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

    return {}


def _fix_unescaped_quotes(text: str) -> str:
    """Fix unescaped quotes inside JSON string values.

    Hebrew text often contains " (U+0022) inside strings, e.g.:
    "broker_status": "POOL- NADLAN - הפול נדל"ן משרד מוביל באזור"

    We use a heuristic: a quote that is followed by Hebrew text is likely
    an unescaped internal quote; a quote followed by JSON structural chars
    (,:}] or whitespace/newline) is a string delimiter.
    """
    result = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and not escaped:
            escaped = True
            result.append(ch)
        elif ch == '"' and not escaped:
            # Peek ahead to decide if this is a string delimiter or internal quote
            if in_string and i + 1 < len(text):
                next_ch = text[i + 1]
                # If next char is Hebrew letter, this is an internal unescaped quote
                if "\u0590" <= next_ch <= "\u05FF":
                    result.append('\\"')
                    i += 1
                    continue
            # Toggle string state
            in_string = not in_string
            result.append(ch)
        else:
            escaped = False
            result.append(ch)
        i += 1
    return "".join(result)


def validate_normalized_listing(obj: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate a normalized listing against the local schema rules.

    Returns (is_valid, list_of_errors).
    """
    errors: list[str] = []

    for key in REQUIRED_KEYS:
        if key not in obj:
            errors.append(f"missing:{key}")

    for key, allowed in ENUM_RULES.items():
        val = obj.get(key)
        if val is not None and val not in allowed:
            errors.append(f"bad_enum:{key}:{val}")

    # Confidence values
    conf = obj.get("confidence", {})
    if isinstance(conf, dict):
        for field, level in conf.items():
            if level not in CONF_LEVELS:
                errors.append(f"bad_confidence:{field}:{level}")

    # Source enum
    if obj.get("source") not in SOURCE_ENUM:
        errors.append(f"bad_source:{obj.get('source')}")

    # evidence_quotes must be dict
    if not isinstance(obj.get("evidence_quotes"), dict):
        errors.append("evidence_quotes_not_dict")

    # features/red_flags/missing_questions must be list
    for key in ("features", "red_flags", "missing_questions"):
        val = obj.get(key)
        if val is not None and not isinstance(val, list):
            errors.append(f"{key}_not_list")

    return not errors, errors


def normalize_pack(
    pack: dict[str, Any],
    *,
    use_llm: bool = False,
    dry_run: bool = False,
    cache_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Normalize a single evidence pack.

    Returns a normalized listing JSON. Caches when cache_path is provided.
    """
    # Check cache
    cache: dict[str, Any] = {}
    if cache_path:
        cache = load_cache(cache_path)
        key = cache_key(pack)
        if key in cache:
            cached = dict(cache[key])
            cached["normalization_status"] = "skipped_cached"
            return cached

    # Decide mode
    if not use_llm:
        result = normalize_pack_offline_stub(pack)
    else:
        result = _normalize_with_llm(pack, dry_run=dry_run)

    # Validate
    valid, errs = validate_normalized_listing(result)
    if not valid:
        result["normalization_status"] = "failed_schema_validation"
        result["normalization_error"] = "; ".join(errs)

    # Cache
    if cache_path and result.get("normalization_status") in {"ok", "skipped_cached", "skipped_prefilter"}:
        cache = load_cache(cache_path)
        cache[cache_key(pack)] = result
        save_cache(cache_path, cache)

    return result


def normalize_packs(
    packs: list[dict[str, Any]],
    *,
    max_items: int = 30,
    use_llm: bool = False,
    dry_run: bool = False,
    cache_path: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    """Normalize multiple evidence packs with rate limiting."""
    results: list[dict[str, Any]] = []
    total = min(len(packs), max_items)

    for i, pack in enumerate(packs[:total]):
        result = normalize_pack(pack, use_llm=use_llm, dry_run=dry_run, cache_path=cache_path)
        results.append(result)

        # Rate limit when LLM is enabled and not dry-run
        if use_llm and not dry_run and i < total - 1:
            time.sleep(0.8)

    return results


def _normalize_with_llm(pack: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Normalize using Gemini 3.1 Flash-Lite Preview.

    Reuses the same backend pattern as llm_extract.py:
    - Loads GEMINI_API_KEY from the environment
    - Uses google.generativeai with system_instruction + JSON mime_type
    - Retries with exponential backoff
    """
    import os

    # Build prompt from evidence pack
    raw_text = pack.get("raw_text", "")
    known = pack.get("known_fields", {})
    source = pack.get("source", "unknown")

    prompt = _build_normalizer_prompt(pack)

    if dry_run:
        print("\n=== DRY RUN — Prompt that would be sent to Gemini ===")
        print(prompt[:2000])
        print("=== END DRY RUN ===\n")
        return _failed_result(
            pack,
            "failed_no_backend",
            "dry_run: prompt printed, no API call made",
        )

    # Try Gemini backend
    model = _init_gemini_backend()
    if not model:
        return _failed_result(
            pack,
            "failed_no_backend",
            "Gemini backend not available (no API key or import error)",
        )

    import time
    import random

    max_retries = int(os.environ.get("SCANNER_LLM_MAX_RETRIES", "2"))
    retry_delay = float(os.environ.get("SCANNER_LLM_RETRY_DELAY", "2"))

    for attempt in range(max_retries + 1):
        try:
            resp = model.generate_content(
                prompt,
                request_options={"timeout": 45},
            )
            content = resp.text
            parsed = parse_llm_json(content)

            if not parsed:
                if attempt < max_retries:
                    time.sleep(retry_delay * (2 ** attempt) + random.uniform(0, 0.7))
                    continue
                return _failed_result(
                    pack,
                    "failed_invalid_json",
                    f"Gemini returned unparseable JSON after {max_retries + 1} attempts",
                )

            # Merge with pack metadata
            parsed["source"] = source if source in SOURCE_ENUM else "unknown"
            parsed["source_url"] = pack.get("source_url")
            parsed["listing_id"] = pack.get("listing_id", "")
            parsed["content_hash"] = pack.get("content_hash", "")
            parsed["model"] = "gemini-3.1-flash-lite-preview"
            parsed["normalized_at"] = datetime.now(timezone.utc).isoformat()

            # Ensure required keys exist (LLM may omit some)
            for key in REQUIRED_KEYS:
                if key not in parsed:
                    parsed[key] = None

            # Validate
            valid, errs = validate_normalized_listing(parsed)
            if not valid:
                parsed["normalization_status"] = "failed_schema_validation"
                parsed["normalization_error"] = "; ".join(errs)
                return parsed

            parsed["normalization_status"] = "ok"
            parsed["normalization_error"] = None
            return parsed

        except Exception as e:
            if attempt < max_retries:
                time.sleep(retry_delay * (2 ** attempt) + random.uniform(0, 0.7))
                continue
            return _failed_result(
                pack,
                "failed_llm",
                f"Gemini call failed after {max_retries + 1} attempts: {e}",
            )

    # Should never reach here
    return _failed_result(pack, "failed_llm", "Unexpected exit from retry loop")


def _build_normalizer_prompt(pack: dict[str, Any]) -> str:
    """Build the user prompt for the normalizer LLM from an evidence pack."""
    raw_text = pack.get("raw_text", "")
    known = pack.get("known_fields", {})
    source = pack.get("source", "unknown")

    known_lines = []
    if known.get("price") is not None:
        known_lines.append(f"- price: {known['price']} NIS")
    if known.get("rooms") is not None:
        known_lines.append(f"- rooms: {known['rooms']}")
    if known.get("sqm") is not None:
        known_lines.append(f"- sqm: {known['sqm']}")
    if known.get("floor") is not None:
        known_lines.append(f"- floor: {known['floor']}")
    if known.get("entry_raw"):
        known_lines.append(f"- entry_raw: {known['entry_raw']}")
    if known.get("address"):
        known_lines.append(f"- address hint: {known['address']}")

    known_block = "\n".join(known_lines) if known_lines else "(none)"

    prompt = f"""Source: {source}
Known fields from scraper:
{known_block}

Raw listing text:
---
{raw_text[:2500]}
---

Return ONLY valid JSON. Do not use markdown fences."""
    return prompt


# ── Gemini backend (mirrors llm_extract.py pattern) ───────────────────────────

_gemini_model = None


def _init_gemini_backend():
    """Initialize Gemini backend. Returns model object or None."""
    global _gemini_model
    if _gemini_model is not None:
        return _gemini_model

    import os

    # Load API key (same logic as llm_extract.py)
    key = os.environ.get("GEMINI_API_KEY")
    # 2. Common env files (absolute paths — $HOME may be remapped)
    candidates = []
    for env_file in candidates:
        if os.path.exists(env_file):
            for line in open(env_file):
                if line.startswith("GEMINI_API_KEY="):
                    key = line.strip().split("=", 1)[1]
                    break
            if key:
                break

    if not key:
        return None

    try:
        import google.generativeai as genai
        genai.configure(api_key=key)

        system_prompt = load_prompt()
        model_name = os.environ.get(
            "SCANNER_GEMINI_MODEL", "gemini-3.1-flash-lite-preview"
        )
        # Resolve legacy aliases
        aliases = {
            "gemini-3.1-flash-light": "gemini-3.1-flash-lite-preview",
            "gemini-2.5-flash-lite-preview-09-2025": "gemini-3.1-flash-lite-preview",
        }
        model_name = aliases.get(model_name, model_name)

        _gemini_model = genai.GenerativeModel(
            model_name,
            system_instruction=system_prompt,
            generation_config=genai.GenerationConfig(
                temperature=0,
                max_output_tokens=2048,
                response_mime_type="application/json",
            ),
        )
        return _gemini_model
    except ImportError:
        return None
    except Exception:
        return None


def _failed_result(pack: dict[str, Any], status: str, error: str) -> dict[str, Any]:
    """Build a failed normalization result."""
    return {
        "schema_version": "1.0",
        "normalization_status": status,
        "normalization_error": error,
        "source": pack.get("source", "unknown"),
        "source_url": pack.get("source_url"),
        "listing_id": pack.get("listing_id", ""),
        "content_hash": pack.get("content_hash", ""),
        "listing_type": "unknown",
        "broker_status": "unknown_broker",
        "contract_type": "unknown",
        "half_room_status": "not_relevant",
        "features": [],
        "red_flags": [],
        "missing_questions": [error] if error else [],
        "confidence": {},
        "evidence_quotes": {},
        "model": None,
        "normalized_at": datetime.now(timezone.utc).isoformat(),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Normalize apartment listing evidence packs.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    norm = sub.add_parser("normalize", help="Normalize a batch of evidence packs")
    norm.add_argument("--input", required=True, help="Input evidence_packs.json")
    norm.add_argument("--output", required=True, help="Output normalized_listings.json")
    norm.add_argument("--audit", help="Optional audit markdown path")
    norm.add_argument("--max-items", type=int, default=30, help="Max packs to normalize")
    norm.add_argument("--no-llm", action="store_true", help="Use offline stub only")
    norm.add_argument("--dry-run", action="store_true", help="Print prompt, do not call API")
    norm.add_argument("--llm-provider", choices=["gemini", "offline"], default="offline",
                      help="LLM provider (default: offline)")
    norm.add_argument(
        "--cache",
        default="artifacts/normalization/normalizer_cache.json",
        help="Cache file path",
    )

    args = parser.parse_args()

    if args.cmd == "normalize":
        inp = pathlib.Path(args.input)
        out = pathlib.Path(args.output)
        cache_path = pathlib.Path(args.cache) if args.cache else None
        audit_path = pathlib.Path(args.audit) if args.audit else None

        if not inp.exists():
            print(f"ERROR: Input file not found: {inp}")
            sys.exit(1)

        packs = json.loads(inp.read_text(encoding="utf-8"))

        # Determine LLM mode
        use_llm = False
        dry_run = False
        if args.llm_provider == "gemini":
            use_llm = True
            dry_run = args.dry_run
        elif not args.no_llm and args.llm_provider == "offline":
            # Default: no LLM
            use_llm = False

        results = normalize_packs(
            packs,
            max_items=args.max_items,
            use_llm=use_llm,
            dry_run=dry_run,
            cache_path=cache_path,
        )

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {len(results)} normalized listings to {out}")

        if audit_path:
            _write_audit(audit_path, results)
            print(f"Wrote audit to {audit_path}")


def _write_audit(path: pathlib.Path, results: list[dict[str, Any]]) -> None:
    """Write a markdown audit of normalization results."""
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.get("normalization_status", "?")] = by_status.get(r.get("normalization_status", "?"), 0) + 1

    lines = [
        "# Normalization Audit",
        "",
        f"**Total:** {len(results)}",
        "",
        "## By Status",
    ]
    for status, count in sorted(by_status.items()):
        lines.append(f"- {status}: {count}")

    ok_results = [r for r in results if r.get("normalization_status") == "ok"]
    if ok_results:
        lines.append("")
        lines.append("## Sample Normalized (status=ok)")
        for r in ok_results[:5]:
            lines.append(f"\n### {r.get('listing_id', '?')}")
            lines.append(f"- price: {r.get('price_nis')} | rooms: {r.get('rooms')} | entry: {r.get('entry_status_hint')}")
            lines.append(f"- broker: {r.get('broker_status')} | contract: {r.get('contract_type')}")
            if r.get("evidence_quotes"):
                lines.append(f"- quotes: {r['evidence_quotes']}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()