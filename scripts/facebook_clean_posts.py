#!/usr/bin/env python3
"""Clean and compact Facebook feed-scan artifacts for low-token semantic triage.

Input: artifacts produced by facebook_feed_multi_scan.py / facebook_group_feed_scan.py.
Output:
  - compact JSONL: one cleaned post per line, short enough for AI triage
  - compact JSON: same records plus summary
  - review MD: human-readable excerpts

This script is deliberately non-AI: it removes obvious Facebook chrome/noise,
deduplicates cross-posts/reposts, redacts phone-ish numbers, and preserves links.
"""
import argparse, json, pathlib, re, hashlib
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from facebook_group_scan import parse_price, parse_rooms, parse_sqm, parse_entry, candidate_score
from facebook_url_utils import normalize_facebook_item_urls

ROOT = pathlib.Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts' / 'facebook'
ART.mkdir(parents=True, exist_ok=True)

NOISE_PHRASES = [
    'See original', 'Rate this translation', 'See translation', 'Write a public comment',
    'Submit your first comment', 'View more comments', 'Like Reply', 'Share', 'Commenting has been turned off for this post',
    'הצג מקור', 'ראה תרגום', 'דרג את התרגום הזה', 'כתוב תגובה ציבורית',
]

WANTED_PATTERNS = [
    r'\bמחפש(?:ים|ת)?\s+(?:דירה|בית)\b',
    r'\bמחפשים\b',
    r'\bמחפשת\b',
    r'בעלי ואני מחפשים',
    r'looking for an apartment',
]

NEGATIVE_PATTERNS = [
    r'למכירה', r'שותפים', r'סאבלט', r'for sale', r'roommates', r'sublet',
    r'asking price', r'for investment', r'investment or residence', r'written in the taboo',
    r'\btaboo\b', r'טאבו', r'בטאבו', r'אטבו', r'למגורים או השקעה',
    r'price reduction', r'reduced from', r'\bmillion\b', r'מיליון', r'ירידת מחיר',
    r'the master of the real estate',
]


def remove_facebook_noise(text: str) -> str:
    """Remove Facebook anti-scraping noise and UI chrome from post text."""
    # 0. Split on '·' to separate noise segments from content
    segments = text.split('·')
    clean_segments = []
    for seg in segments:
        seg = seg.strip()
        if not seg or len(seg) < 3:
            continue
        # Check if this segment is noise (mostly 1-2 char tokens)
        tokens = seg.split()
        if len(tokens) >= 6:
            short = sum(1 for t in tokens if len(t) <= 2)
            if short / len(tokens) > 0.65:
                continue  # skip this noise segment
        clean_segments.append(seg)
    text = ' · '.join(clean_segments)
    
    # 1. Remove remaining noise lines
    def is_noise_line(line):
        stripped = line.strip()
        if not stripped or len(stripped) < 5:
            return True
        tokens = stripped.split()
        if len(tokens) >= 8:
            short = sum(1 for t in tokens if len(t) <= 2)
            if short / len(tokens) > 0.70:
                return True
        return False
    
    lines = text.split('\n')
    lines = [l for l in lines if not is_noise_line(l)]
    text = '\n'.join(lines)
    
    # 2. Remove repeated "Facebook" tokens (more than 2 consecutive)
    text = re.sub(r'(?:\bFacebook\b\s*){3,}', '', text)
    
    # 3. Remove noise phrases
    for phrase in NOISE_PHRASES:
        text = text.replace(phrase, ' ')
    
    # 4. Remove common UI chrome
    chrome = [
        r'\d+\s*(?:Most relevant|Relevant|Newest|All comments)',
        r'Like\s+Reply\s+Share',
        r'\d+\s*(?:h|m|min|hr|hours?|minutes?)\s*(?:ago|·)?',
        r'^\s*(?:Edited|Edited ·)\s*$',
        r'^\s*(?:Follow|Follow ·)\s*$',
    ]
    for pat in chrome:
        text = re.sub(pat, ' ', text, flags=re.MULTILINE)
    
    # 5. Collapse whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def strip_tracking_url(url: str) -> str:
    if not url:
        return url
    # Prefer canonical post URL from pcb/photo links when possible.
    m = re.search(r'set=pcb\.([0-9]+)', url)
    if m:
        return m.group(1)
    m = re.search(r'/posts/([0-9]+)', url)
    if m:
        return m.group(1)
    return url.split('?')[0]


def canonical_post_url(item):
    return normalize_facebook_item_urls(item).get('desktop_post_url') or ''


def remove_fb_letter_noise(text):
    # Facebook sometimes exposes tracking-ish sequences as spaced single chars/digits.
    return re.sub(r'\b(?:[a-z0-9]\s+){8,}[a-z0-9]\b', ' ', text, flags=re.I)


def remove_facebook_chrome(text):
    """Aggressively remove Facebook anti-scraping noise and UI chrome."""
    text = text or ''

    # Anti-scraping noise: lines of mostly single chars separated by spaces (20+ chars)
    text = re.sub(r'(?m)^(?:[a-zA-Z0-9\u0590-\u05FF]\s+){15,}[a-zA-Z0-9\u0590-\u05FF]\s*$', ' ', text)

    # "See translation", "Rate this translation" and Hebrew equivalents
    text = re.sub(r'(?:See|View)\s+(?:original|translation)', ' ', text, flags=re.I)
    text = re.sub(r'Rate\s+this\s+translation', ' ', text, flags=re.I)
    text = re.sub(r'[הצ]ג\s+מקור', ' ', text)
    text = re.sub(r'(?:ראה|הצג)\s+(?:תרגום|עוד)', ' ', text)
    text = re.sub(r'(?:דרג|הערך)\s+(?:את\s+)?התרגום', ' ', text)

    # Comment UI chrome
    text = re.sub(r'Write\s+(?:a\s+)?public\s+comment', ' ', text, flags=re.I)
    text = re.sub(r'Submit\s+your\s+first\s+comment', ' ', text, flags=re.I)
    text = re.sub(r'View\s+more\s+comments', ' ', text, flags=re.I)
    text = re.sub(r'(?:Most\s+relevant|Top\s+comments)', ' ', text, flags=re.I)
    text = re.sub(r'(?:Like|Reply|Share|Comment)(?:\s+\w+)*\s*buttons?', ' ', text, flags=re.I)
    text = re.sub(r'Commenting\s+has\s+been\s+turned\s+off', ' ', text, flags=re.I)

    # Remove repeated "Facebook" tokens (more than 2 consecutive)
    text = re.sub(r'(?:Facebook\s+){3,}', 'Facebook ', text, flags=re.I)

    # Remove empty lines created by cleanup
    text = re.sub(r'\n\s*\n+', '\n\n', text)

    return text


def clean_text(text):
    text = text or ''
    # Apply the new aggressive noise removal first
    text = remove_facebook_noise(text)
    text = remove_fb_letter_noise(text)
    text = remove_facebook_chrome(text)
    text = re.sub(r'\bFacebook\b', ' ', text)
    for phrase in NOISE_PHRASES:
        text = text.replace(phrase, ' ')
    # Redact Israeli-ish phone numbers after extraction. Preserve prices.
    text = re.sub(r'(?<!\d)0\d{1,2}[-\s]?\d{6,7}(?!\d)', '[PHONE]', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def signature_text(text):
    t = text.lower()
    t = re.sub(r'\[phone\]', '', t)
    t = re.sub(r'https?://\S+', '', t)
    t = re.sub(r'[^0-9a-zא-ת ]+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # Use a middle slice after author if possible.
    parts = t.split(' · ', 1)
    if len(parts) == 2:
        t = parts[1]
    return t[:500]


def is_wanted(text):
    low = text.lower()
    return any(re.search(p, low) for p in WANTED_PATTERNS)


def has_negative(text):
    low = text.lower()
    return any(re.search(p, low) for p in NEGATIVE_PATTERNS)


def compact_item(item, idx):
    raw_text = item.get('text') or ''
    text = clean_text(raw_text)
    # Prefer already-extracted fields from the feed scan (including LLM fills),
    # and only fall back to regex recomputation when a field is missing. Earlier
    # versions discarded LLM fields here, so AI triage saw weaker heuristics than
    # the feed scan had already produced.
    parsed = {
        'price': item.get('price') if item.get('price') is not None else parse_price(raw_text),
        'rooms': item.get('rooms') if item.get('rooms') is not None else parse_rooms(raw_text),
        'sqm': item.get('sqm') if item.get('sqm') is not None else parse_sqm(raw_text),
        'entry': item.get('entry') if item.get('entry') is not None else parse_entry(raw_text),
        'floor': item.get('floor'),
        'location': item.get('location'),
        'street': item.get('street'),
        'neighborhood': item.get('neighborhood'),
        'features': item.get('features') or [],
        'is_listing': item.get('is_listing'),
        'price_source': item.get('price_source'),
        'rooms_source': item.get('rooms_source'),
        'entry_source': item.get('entry_source'),
        'floor_source': item.get('floor_source'),
        'location_source': item.get('location_source'),
    }
    scored_item = {'text': raw_text, **parsed}
    parsed['score'], parsed['reasons'], parsed['flags'] = candidate_score(scored_item)
    url_bundle = normalize_facebook_item_urls(item)
    post_url = url_bundle.get('desktop_post_url') or ''
    images = item.get('images') or []
    photo_count = sum(1 for im in images if im.get('src'))
    image_alts = []
    for im in images:
        alt = clean_text(im.get('alt') or '')
        if alt and alt not in image_alts:
            image_alts.append(alt[:180])
        if len(image_alts) >= 3:
            break
    links = []
    for l in item.get('links') or []:
        s = strip_tracking_url(l)
        if s and s not in links:
            links.append(s)
        if len(links) >= 8:
            break
    compact = {
        'id': f'post_{idx:04d}',
        'group': item.get('group'),
        'group_name': item.get('group_name') or item.get('group'),
        'post_url': post_url,
        'desktop_post_url': url_bundle.get('desktop_post_url'),
        'mobile_post_url': url_bundle.get('mobile_post_url'),
        'permalink_url': url_bundle.get('permalink_url'),
        'mobile_permalink_url': url_bundle.get('mobile_permalink_url'),
        'universal_post_url': url_bundle.get('universal_post_url'),
        'url_status': url_bundle.get('url_status'),
        'original_url': url_bundle.get('original_url'),
        'original_url_status': url_bundle.get('original_url_status'),
        'source_mode': item.get('source_mode'),
        'scroll_index': item.get('scroll_index'),
        'heuristic': {
            'price': parsed.get('price'),
            'rooms': parsed.get('rooms'),
            'sqm': parsed.get('sqm'),
            'entry': parsed.get('entry'),
            'floor': parsed.get('floor'),
            'location': parsed.get('location'),
            'street': parsed.get('street'),
            'neighborhood': parsed.get('neighborhood'),
            'features': parsed.get('features') or [],
            'is_listing': parsed.get('is_listing'),
            'sources': {
                'price': parsed.get('price_source'),
                'rooms': parsed.get('rooms_source'),
                'entry': parsed.get('entry_source'),
                'floor': parsed.get('floor_source'),
                'location': parsed.get('location_source'),
            },
            'score': parsed.get('score'),
            'reasons': parsed.get('reasons') or [],
            'flags': parsed.get('flags') or [],
            'is_wanted_like': is_wanted(text),
            'has_negative_terms': has_negative(text),
        },
        'text': text[:1400],
        'text_len': len(text),
        'photo_count': photo_count,
        'image_alts': image_alts,
        'links': links,
    }
    sig_src = post_url or signature_text(text)
    compact['dedupe_key'] = hashlib.sha1(sig_src.encode('utf-8')).hexdigest()[:16]
    compact['similarity_signature'] = signature_text(text)[:220]
    return compact


def load_items(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(data, list):
        return data, {'source_shape': 'list'}
    # Prefer candidates (LLM-filtered) over all_items (raw everything)
    if 'candidates' in data:
        return data.get('candidates') or [], {'source_shape': 'candidates', 'runs': data.get('runs'), 'groups': data.get('groups')}
    if 'items' in data:
        return data.get('items') or [], {'source_shape': 'single_feed'}
    if 'all_items' in data:
        return data.get('all_items') or [], {'source_shape': 'multi_feed', 'runs': data.get('runs'), 'groups': data.get('groups')}
    raise SystemExit(f'Unsupported input shape: {path}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input', help='scan JSON artifact')
    ap.add_argument('--out-prefix', default=None)
    ap.add_argument('--keep-wanted', action='store_true')
    ap.add_argument('--max-text', type=int, default=1400)
    args = ap.parse_args()

    inp = pathlib.Path(args.input)
    items, meta = load_items(inp)
    compact=[]; seen={}; duplicate_count=0
    for idx, raw in enumerate(items, 1):
        c = compact_item(raw, idx)
        if args.max_text and len(c['text']) > args.max_text:
            c['text'] = c['text'][:args.max_text]
        # Skip items that LLM explicitly marked as NOT listings (unless keep-wanted)
        if not args.keep_wanted and c['heuristic']['is_listing'] is False:
            continue
        if not args.keep_wanted and c['heuristic']['is_wanted_like']:
            c['triage_hint'] = 'skip_probably_wanted_post'
        key = c['dedupe_key']
        sim = c['similarity_signature']
        sim_key = hashlib.sha1(sim.encode('utf-8')).hexdigest()[:16] if sim else key
        # Cross-posted apartment ads often have different post URLs in different groups.
        # Use the semantic-ish text signature first; keep all post URLs under crosspost_urls.
        final_key = sim_key or key
        if final_key in seen:
            duplicate_count += 1
            seen[final_key]['crosspost_urls'].append(c.get('post_url'))
            seen[final_key]['seen_in_groups'].append(c.get('group_name'))
            # Prefer richer text/heuristics.
            if len(c['text']) > len(seen[final_key]['text']):
                base = seen[final_key]
                c['crosspost_urls'] = base.get('crosspost_urls', [])
                c['seen_in_groups'] = base.get('seen_in_groups', [])
                seen[final_key] = c
            continue
        c['crosspost_urls'] = [c.get('post_url')] if c.get('post_url') else []
        c['seen_in_groups'] = [c.get('group_name')]
        seen[final_key] = c
    compact = list(seen.values())
    # Sort: likely useful first, but preserve maybes.
    compact.sort(key=lambda x: (
        x['heuristic']['is_wanted_like'],
        x['heuristic']['has_negative_terms'],
        x['heuristic']['price'] is None,
        -(x['heuristic']['score'] or 0),
    ))

    prefix = pathlib.Path(args.out_prefix) if args.out_prefix else ART / f'clean_posts_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    prefix.parent.mkdir(parents=True, exist_ok=True)
    out_json = prefix.with_suffix('.json')
    out_jsonl = prefix.with_suffix('.jsonl')
    out_md = prefix.with_suffix('.md')
    payload = {
        'generated_at': datetime.now().isoformat(),
        'source': str(inp),
        'source_meta': meta,
        'raw_items': len(items),
        'clean_items': len(compact),
        'duplicates_collapsed': duplicate_count,
        'posts': compact,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    out_jsonl.write_text('\n'.join(json.dumps(x, ensure_ascii=False) for x in compact) + '\n', encoding='utf-8')
    md = [f'# Clean Facebook posts for semantic triage', '', f'Source: `{inp}`', f'Raw items: {len(items)}', f'Clean items: {len(compact)}', f'Duplicates collapsed: {duplicate_count}', '']
    for i,p in enumerate(compact[:80],1):
        h=p['heuristic']
        md += [
            f'## {i}. {p["id"]} — {p.get("group_name")}',
            f'- Link: {p.get("universal_post_url") or p.get("post_url") or "אין לינק ישיר לפוסט"}',
            f'- Heuristic: price={h.get("price")}, rooms={h.get("rooms")}, sqm={h.get("sqm")}, entry={h.get("entry")}, score={h.get("score")}',
            f'- Hints: wanted={h.get("is_wanted_like")}, negative={h.get("has_negative_terms")}, photos={p.get("photo_count")}',
            f'- Text: {p.get("text")[:900]}',
            ''
        ]
    out_md.write_text('\n'.join(md), encoding='utf-8')
    print('wrote', out_json, out_jsonl, out_md)
    print('raw_items', len(items), 'clean_items', len(compact), 'duplicates_collapsed', duplicate_count)
    useful = [p for p in compact if not p['heuristic']['is_wanted_like'] and not p['heuristic']['has_negative_terms']]
    print('usefulish', len(useful), 'priced_budget', sum(1 for p in useful if p['heuristic']['price'] and p['heuristic']['price'] <= 6500))

if __name__ == '__main__':
    main()
