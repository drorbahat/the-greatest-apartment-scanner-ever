#!/usr/bin/env python3
"""LLM-based apartment listing extractor for Apartment Scanner scanner.

Supports multiple backends:
  - gemini-3.1-flash-lite-preview (default, fast, cheap)

Usage:
  from llm_extract import extract_listing, extract_hybrid

  # Full LLM extraction:
  result = extract_listing(post_text)

  # Hybrid: regex first, LLM only fills gaps:
  result = extract_hybrid(post_text, regex_result)
"""
import hashlib
import json
import os
import pathlib
import random
import re
import time
import logging
from typing import Optional

log = logging.getLogger('scanner.llm')

# ---------------------------------------------------------------------------
# Config — auto-select best available backend
# ---------------------------------------------------------------------------
# Priority: Gemini 3.1 Flash-Lite Preview
# Gemini: low-latency, strong Hebrew, best fit for lightweight extraction

BACKEND = None
_gemini_model = None

_LEGACY_GEMINI_MODEL_ALIASES = {
    'gemini-3.1-flash-light': 'gemini-3.1-flash-lite-preview',
    'gemini-2.5-flash-lite-preview-09-2025': 'gemini-3.1-flash-lite-preview',
}

def _resolve_gemini_model_name() -> str:
    model = os.environ.get('SCANNER_GEMINI_MODEL', 'gemini-3.1-flash-lite-preview')
    return _LEGACY_GEMINI_MODEL_ALIASES.get(model, model)

GEMINI_MODEL_NAME = _resolve_gemini_model_name()

MAX_RETRIES = int(os.environ.get('SCANNER_LLM_MAX_RETRIES', '2'))
RETRY_DELAY = float(os.environ.get('SCANNER_LLM_RETRY_DELAY', '2'))
CALL_DELAY = float(os.environ.get('SCANNER_LLM_CALL_DELAY', '0.8'))  # conservative rate limiting
MAX_OUTPUT_TOKENS=2000  # Gemini sometimes needs more for full JSON response
LLM_CACHE_PATH = pathlib.Path(os.environ.get('SCANNER_LLM_CACHE', '/app/artifacts/facebook/llm_extract_cache.json'))
_llm_cache = None

SYSTEM_PROMPT = """You are an Israeli apartment rental listing parser.
Current date: May 2026. Assume future dates unless explicitly stated.

Extract structured data from the rental post below.

Fields (all optional - set to null if not mentioned):
- "price": number (NIS/month)
- "rooms": number
- "entrance": "YYYY-MM" or "immediate" or "flexible"
- "city": Hebrew city name
- "neighborhood": Hebrew neighborhood name
- "street": Hebrew street name (with number if given)
- "floor": number (0 = ground floor)
- "features": array of strings, e.g. ["ac","balcony","parking","elevator","renovated","pets_allowed","safe_room","storage"]
- "contact": name string
- "phone": phone string
- "is_listing": true if apartment IS FOR RENT, false if someone is SEARCHING

Do not guess. If a field is not mentioned, set it to null."""

USER_PROMPT = "Extract apartment rental info from the following post."


# ---------------------------------------------------------------------------
# Backend initialization
# ---------------------------------------------------------------------------
def _get_gemini_key():
    """Try multiple sources for Gemini API key."""
    # 1. Environment
    key = os.environ.get('GEMINI_API_KEY')
    if key:
        return key
    # 2. Common env files (absolute paths — $HOME may be remapped)
    candidates = []
    for env_file in candidates:
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    if line.startswith('GEMINI_API_KEY='):
                        return line.strip().split('=', 1)[1]
    return None


def _init_gemini():
    """Initialize Gemini backend. Returns model object or None."""
    global _gemini_model
    if _gemini_model is not None:
        return _gemini_model
    
    key = _get_gemini_key()
    if not key:
        return None
    
    try:
        from google import genai
        from google.genai import types
        _gemini_model = genai.Client(api_key=key)
        # Define schema for structured JSON output (all fields nullable for missing data)
        _response_schema = {
            "type": "object",
            "properties": {
                "price": {"type": "number", "nullable": True},
                "rooms": {"type": "number", "nullable": True},
                "entrance": {"type": "string", "nullable": True},
                "city": {"type": "string", "nullable": True},
                "neighborhood": {"type": "string", "nullable": True},
                "street": {"type": "string", "nullable": True},
                "floor": {"type": "number", "nullable": True},
                "features": {"type": "array", "items": {"type": "string"}, "nullable": True},
                "contact": {"type": "string", "nullable": True},
                "phone": {"type": "string", "nullable": True},
                "is_listing": {"type": "boolean", "nullable": True},
            },
        }
        # Store config for later use
        _gemini_model._config = types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            response_mime_type='application/json',
            response_schema=_response_schema,
            system_instruction=SYSTEM_PROMPT,
        )
        log.info(f"Gemini backend initialized: {GEMINI_MODEL_NAME}")
        return _gemini_model
    except ImportError:
        log.warning("google-genai not installed")
        return None
    except Exception as e:
        log.warning(f"Gemini init failed: {e}")
        return None


def _detect_backend():
    """Auto-detect best available backend."""
    global BACKEND
    if BACKEND:
        return BACKEND
    
    if _init_gemini():
        BACKEND = 'gemini'
    else:
        BACKEND = None
        log.error("No LLM backend available! Need GEMINI_API_KEY")
    
    return BACKEND


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------
_last_call = 0.0

def _cache_key(text: str) -> str:
    compact = re.sub(r'\s+', ' ', (text or '').strip())[:2000]
    return hashlib.sha1(compact.encode('utf-8')).hexdigest()


def _get_cache() -> dict:
    global _llm_cache
    if _llm_cache is not None:
        return _llm_cache
    try:
        if LLM_CACHE_PATH.exists():
            _llm_cache = json.loads(LLM_CACHE_PATH.read_text(encoding='utf-8'))
        else:
            _llm_cache = {'version': 1, 'items': {}}
    except Exception as e:
        log.warning(f"LLM cache load failed: {e}")
        _llm_cache = {'version': 1, 'items': {}}
    _llm_cache.setdefault('items', {})
    return _llm_cache


def _cache_get(text: str) -> Optional[dict]:
    entry = _get_cache().get('items', {}).get(_cache_key(text))
    if not entry:
        return None
    data = entry.get('data')
    return dict(data) if isinstance(data, dict) else None


def _cache_put(text: str, data: dict, backend: str) -> None:
    cache = _get_cache()
    cache.setdefault('items', {})[_cache_key(text)] = {
        'backend': backend,
        'cached_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'text_preview': re.sub(r'\s+', ' ', (text or '').strip())[:180],
        'data': data,
    }
    try:
        LLM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LLM_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        log.warning(f"LLM cache write failed: {e}")


def _retry_sleep(attempt: int) -> None:
    # Exponential backoff + jitter reduces repeated 429/API bursts.
    time.sleep(RETRY_DELAY * (2 ** attempt) + random.uniform(0, 0.7))


def _rate_limit():
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < CALL_DELAY:
        time.sleep(CALL_DELAY - elapsed)
    _last_call = time.time()


def _parse_response(content: str) -> Optional[dict]:
    """Parse LLM response, stripping markdown fences if present."""
    content = (content or '').strip()
    if not content:
        return None
    
    # Strip markdown code blocks
    if content.startswith('```'):
        lines = content.split('\n')
        # Remove first line (```json) and last line (```)
        if len(lines) > 2:
            content = '\n'.join(lines[1:])
            if content.rstrip().endswith('```'):
                content = content.rstrip()[:-3].rstrip()
        else:
            content = content.strip('`').strip()
            if content.startswith('json'):
                content = content[4:].strip()

    # Try direct parse
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the response
    brace_count = 0
    start = None
    for i, ch in enumerate(content):
        if ch == '{':
            if brace_count == 0:
                start = i
            brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if brace_count == 0 and start is not None:
                try:
                    return json.loads(content[start:i+1])
                except json.JSONDecodeError:
                    start = None

    return None


def extract_listing_gemini(text: str) -> Optional[dict]:
    """Extract using Gemini 3.1 Flash-Lite Preview."""
    client = _init_gemini()
    if not client:
        return None
    
    _rate_limit()
    
    prompt = f"""{USER_PROMPT}

Post:
{text[:2000]}"""

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL_NAME,
                contents=prompt,
                config=client._config,
            )
            content = resp.text
            parsed = _parse_response(content)
            if parsed:
                return _normalize(parsed)
            log.warning(f"Gemini returned unparseable response (attempt {attempt+1})")
        except Exception as e:
            log.warning(f"Gemini call failed (attempt {attempt+1}): {e}")
            if attempt < MAX_RETRIES:
                _retry_sleep(attempt)
    return None


def _normalize(parsed: dict) -> dict:
    """Normalize extracted data."""
    # Normalize entrance: "2026-08-01" -> "2026-08"
    if parsed.get('entrance') and isinstance(parsed['entrance'], str):
        ent = parsed['entrance']
        m = re.match(r'(\d{4}-\d{2})-\d{2}', ent)
        if m:
            parsed['entrance'] = m.group(1)
    return parsed


def extract_listing(text: str) -> Optional[dict]:
    """Extract structured listing data from post text.
    
    Uses Gemini for extraction. Falls back to regex-only if unavailable.
    """
    if not text or len(text.strip()) < 30:
        return None

    cached = _cache_get(text)
    if cached:
        cached['_llm_cache_hit'] = True
        return cached
    
    backend = _detect_backend()
    if not backend:
        return None
    
    result = extract_listing_gemini(text)
    if result:
        _cache_put(text, result, 'gemini')
    return result


# ---------------------------------------------------------------------------
# Hybrid: regex + LLM
# ---------------------------------------------------------------------------
def extract_hybrid(text: str, regex_result: dict) -> dict:
    """Fill in fields that regex missed using LLM.

    Regex runs first (fast, free). LLM fills only the gaps.
    """
    # Count missing fields
    missing = sum(1 for k in ['price', 'rooms', 'entry', 'floor'] if regex_result.get(k) is None)
    has_location = bool(regex_result.get('location') or regex_result.get('neighborhood') or regex_result.get('street'))
    if not has_location:
        missing += 1
    
    # Skip LLM if only 0-1 fields missing
    if missing <= 1:
        return regex_result
    
    llm_data = extract_listing(text)
    if not llm_data:
        return regex_result
    
    result = dict(regex_result)
    
    # Merge: only overwrite None fields
    if result.get('price') is None and llm_data.get('price') is not None:
        result['price'] = llm_data['price']
        result['price_source'] = 'llm'
    
    if result.get('rooms') is None and llm_data.get('rooms') is not None:
        result['rooms'] = llm_data['rooms']
        result['rooms_source'] = 'llm'
    
    if result.get('entry') is None and llm_data.get('entrance') is not None:
        result['entry'] = llm_data['entrance']
        result['entry_source'] = 'llm'
    
    if result.get('floor') is None and llm_data.get('floor') is not None:
        result['floor'] = llm_data['floor']
        result['floor_source'] = 'llm'
    
    # Location
    loc = result.get('location')
    llm_city = llm_data.get('city')
    llm_neighborhood = llm_data.get('neighborhood')
    llm_street = llm_data.get('street')
    
    if not loc and (llm_city or llm_neighborhood or llm_street):
        result['location'] = {
            'city': llm_city,
            'neighborhood': llm_neighborhood,
            'street': llm_street,
        }
        result['location_source'] = 'llm'
    elif loc:
        if not loc.get('city') and llm_city:
            result['location']['city'] = llm_city
            result['location_source'] = 'llm'
        if not loc.get('neighborhood') and llm_neighborhood:
            result['location']['neighborhood'] = llm_neighborhood
            if 'location_source' not in result:
                result['location_source'] = 'llm'
        if not loc.get('street') and llm_street:
            result['location']['street'] = llm_street
            if 'location_source' not in result:
                result['location_source'] = 'llm'
    
    if not result.get('neighborhood') and llm_neighborhood:
        result['neighborhood'] = llm_neighborhood
    if not result.get('street') and llm_street:
        result['street'] = llm_street
    
    if llm_data.get('features') and not result.get('features'):
        result['features'] = llm_data['features']
    
    if not result.get('contact') and llm_data.get('contact'):
        result['contact'] = llm_data['contact']
    if not result.get('phone') and llm_data.get('phone'):
        result['phone'] = llm_data['phone']
    
    if llm_data.get('is_listing') is not None:
        result['is_listing'] = llm_data['is_listing']
    
    return result


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------
def batch_extract_hybrid(items: list, progress_cb=None, checkpoint_path=None, checkpoint_meta=None) -> list:
    """Process a list of items through hybrid extraction.

    If checkpoint_path is provided, writes partial enriched results after every item
    and resumes from that checkpoint on the next run when the batch size matches.
    This prevents re-paying Gemini latency and rate-limit cost after a crash.
    """
    total = len(items)
    results = []
    checkpoint_path = pathlib.Path(checkpoint_path) if checkpoint_path else None

    # Resume completed prefix from checkpoint, but only when it clearly belongs to
    # the same batch size. If inputs changed, ignore the old checkpoint safely.
    if checkpoint_path and checkpoint_path.exists():
        try:
            payload = json.loads(checkpoint_path.read_text(encoding='utf-8'))
            saved_items = payload.get('items') or []
            if payload.get('items_total') == total and isinstance(saved_items, list):
                results = saved_items[:total]
                if results:
                    log.info(f"Resuming LLM checkpoint: {len(results)}/{total} already completed")
            else:
                log.info("Ignoring stale LLM checkpoint: batch size changed")
        except Exception as e:
            log.warning(f"Could not read LLM checkpoint, starting fresh: {e}")
            results = []

    start_idx = len(results)
    for idx in range(start_idx, total):
        item = items[idx]
        text = item.get('text', '')
        updated = extract_hybrid(text, item)
        results.append(updated)
        
        if progress_cb:
            progress_cb(idx + 1, total, updated)

        if checkpoint_path:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'completed': idx + 1,
                'items_total': total,
                **(checkpoint_meta or {}),
                'items': results,
            }
            checkpoint_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    
    return results


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format='%(message)s')

    ap = argparse.ArgumentParser(description='LLM extractor test')
    ap.add_argument('--input', help='JSON file with items (from scan)')
    ap.add_argument('--text', help='Direct text to extract')
    ap.add_argument('--full', action='store_true', help='Full LLM extraction (not hybrid)')
    ap.add_argument('--backend', choices=['gemini', 'auto'], default='auto')
    ap.add_argument('--limit', type=int, default=10)
    ap.add_argument('--out', help='Write enriched results JSON to this file')
    args = ap.parse_args()
    
    # Force backend if specified
    if args.backend != 'auto':
        BACKEND = args.backend

    if args.text:
        result = extract_listing(args.text)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.input:
        data = json.loads(open(args.input).read())
        items = data if isinstance(data, list) else data.get('all_items', data.get('items', []))
        
        backend = _detect_backend()
        print(f"Backend: {backend}")
        print(f"Processing {min(len(items), args.limit)} items...")
        
        def progress(i, total, item):
            p = item.get('price', '?')
            r = item.get('rooms', '?')
            e = item.get('entry', '?')
            src = item.get('price_source', 'regex')
            print(f"  [{i}/{total}] price={p}({src}) rooms={r} entry={e}", flush=True)

        t0 = time.time()
        
        if args.full:
            results = []
            for item in items[:args.limit]:
                text = item.get('text', '')
                llm_data = extract_listing(text)
                if llm_data:
                    llm_data['_original_text_preview'] = text[:200]
                results.append(llm_data)
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            checkpoint = pathlib.Path(args.out) if args.out else None
            results = batch_extract_hybrid(
                items[:args.limit],
                progress,
                checkpoint_path=checkpoint,
                checkpoint_meta={'backend': backend, 'source': args.input, 'limit': args.limit},
            )
            if args.out:
                out_path = pathlib.Path(args.out)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                    'completed': len(results),
                    'backend': backend,
                    'source': args.input,
                    'limit': args.limit,
                    'items_total': len(results),
                    'items': results,
                }
                out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                print(f"WROTE {out_path}")
            # Summary
            regex_price = sum(1 for i in results if i.get('price') is not None and i.get('price_source') != 'llm')
            llm_price = sum(1 for i in results if i.get('price') is not None and i.get('price_source') == 'llm')
            regex_rooms = sum(1 for i in results if i.get('rooms') is not None and i.get('rooms_source') != 'llm')
            llm_rooms = sum(1 for i in results if i.get('rooms') is not None and i.get('rooms_source') == 'llm')
            llm_entry = sum(1 for i in results if i.get('entry') is not None and i.get('entry_source') == 'llm')
            llm_floor = sum(1 for i in results if i.get('floor') is not None and i.get('floor_source') == 'llm')
            llm_loc = sum(1 for i in results if i.get('location_source') == 'llm')
            
            elapsed = time.time() - t0
            print(f"\n--- Summary ({elapsed:.0f}s, {elapsed/len(results):.1f}s/item) ---")
            print(f"Backend: {backend}")
            print(f"Items: {len(results)}")
            print(f"Price: {regex_price} regex + {llm_price} LLM = {regex_price + llm_price} total")
            print(f"Rooms: {regex_rooms} regex + {llm_rooms} LLM = {regex_rooms + llm_rooms} total")
            print(f"Entry: {llm_entry} LLM fills")
            print(f"Floor: {llm_floor} LLM fills")
            print(f"Location: {llm_loc} LLM fills")
    else:
        print("Use --text 'post text' or --input file.json")
