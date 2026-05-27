#!/usr/bin/env python3
"""Prepare compact Facebook posts for low-cost AI semantic triage.

This is the bridge between non-AI scraping/cleaning and a cheap LLM sub-agent.
It reads `facebook_clean_posts.py` output, skips posts already present in a
persistent triage cache, and writes a small JSON input plus a ready-to-copy prompt.

Default model target: glm-5.1 via Hermes/Z.ai.
"""
import argparse, hashlib, json, pathlib, re
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts' / 'facebook'
DEFAULT_CACHE = ART / 'triage_cache.json'
DEFAULT_MODEL = 'auto-triage'
DEFAULT_LIMIT = 50
DEFAULT_CACHE_TTL_DAYS = 7
CACHE_VERSION = 1

CRITERIA = {
    'budget_nis': 'up to 6500 hard max',
    'areas': ['Givatayim', 'good/quiet Ramat Gan', 'Yad Eliyahu', 'Bitzaron/Ramat Israel'],
    'rooms': '2.5+ preferred 3',
    'must_have': ['usable work/studio room', 'avoid wasted Jerusalem trip'],
    'important': ['quiet', 'maintained', 'AC in key rooms or option', 'long-term option'],
    'entry': 'July 1 - August 30, 2026; immediate/flexible entries are maybe if potentially flexible to July; May/June entries flagged for follow-up',
    'entry_accepts': ['1.7', '1/7', '1.8', '1/8', 'יולי', 'אוגוסט', 'July', 'August', 'flexible', 'גמיש'],
    'entry_followup': ['immediate', 'מיידי', 'May', 'מאי', 'June', 'יוני'],
    'contact_policy': 'read-only scan; do not contact anyone without explicit approval',
}


def load_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))


def load_cache(path):
    path = pathlib.Path(path)
    if not path.exists():
        return {'version': CACHE_VERSION, 'created_at': datetime.now().isoformat(), 'updated_at': None, 'items': {}}
    data = load_json(path)
    data.setdefault('version', CACHE_VERSION)
    data.setdefault('items', {})
    data.setdefault('created_at', datetime.now().isoformat())
    data.setdefault('updated_at', None)
    return data


def prune_cache(cache, ttl_days):
    items = cache.get('items') or {}
    if ttl_days is None or ttl_days <= 0 or not items:
        return 0
    cutoff = datetime.now().timestamp() - (ttl_days * 24 * 60 * 60)
    removed = 0
    kept = {}
    for key, item in items.items():
        ts_text = item.get('triaged_at') or item.get('updated_at') or item.get('created_at')
        ts = None
        if isinstance(ts_text, str):
            try:
                ts = datetime.fromisoformat(ts_text).timestamp()
            except Exception:
                ts = None
        if ts is not None and ts < cutoff:
            removed += 1
            continue
        kept[key] = item
    cache['items'] = kept
    if removed:
        cache['updated_at'] = datetime.now().isoformat()
    return removed


def stable_cache_key(post):
    if post.get('dedupe_key'):
        return post['dedupe_key']
    if post.get('post_url'):
        return hashlib.sha1(post['post_url'].encode('utf-8')).hexdigest()[:16]
    sig = post.get('similarity_signature') or post.get('text', '')[:500]
    return hashlib.sha1(sig.encode('utf-8')).hexdigest()[:16]


def should_send(post, mode='smart'):
    h = post.get('heuristic') or {}
    if mode == 'all':
        return True, 'mode_all'
    if h.get('is_wanted_like'):
        return False, 'skip_wanted_like'
    if h.get('has_negative_terms'):
        return False, 'skip_negative_terms'
    price = h.get('price')
    rooms = h.get('rooms')
    sqm = h.get('sqm')
    score = h.get('score') or 0
    text = (post.get('text') or '').lower()

    if mode == 'strict':
        if price is not None and price > 6500:
            return False, 'skip_price_out_of_budget'
        if rooms is not None and rooms < 2.5:
            return False, 'skip_too_few_rooms'
        if score < 10 and price is None:
            return False, 'skip_low_signal_no_price'
        return True, 'strict_candidate'

    # smart default: include budget candidates and ambiguous posts with useful signals.
    if price is not None:
        if price <= 6500:
            return True, 'budget_candidate'
        # include near-budget / parser-suspicious posts only if otherwise useful.
        if price <= 7000 and (rooms is None or rooms >= 2.5) and score >= 8:
            return True, 'near_budget_check'
        return False, 'skip_price_out_of_scope'
    if rooms is not None and rooms < 2.5:
        return False, 'skip_too_few_rooms'
    if sqm is not None and sqm < 50:
        return False, 'skip_too_small'
    important_terms = ['1.7', 'יולי', 'כניסה', 'entry', 'גבעתיים', 'רמת גן', 'יד אליהו', 'ביצרון', 'ללא תיווך', 'for rent']
    if score >= 8 or any(t in text for t in important_terms):
        return True, 'ambiguous_candidate'
    return False, 'skip_low_signal'


def compact_for_ai(post, max_text=1100):
    h = post.get('heuristic') or {}
    return {
        'id': post.get('id'),
        'cache_key': stable_cache_key(post),
        'group_name': post.get('group_name'),
        'post_url': post.get('post_url'),
        'desktop_post_url': post.get('desktop_post_url'),
        'mobile_post_url': post.get('mobile_post_url'),
        'permalink_url': post.get('permalink_url'),
        'mobile_permalink_url': post.get('mobile_permalink_url'),
        'universal_post_url': post.get('universal_post_url'),
        'url_status': post.get('url_status'),
        'crosspost_urls': post.get('crosspost_urls') or [],
        'seen_in_groups': post.get('seen_in_groups') or [],
        'heuristic': {
            'price': h.get('price'),
            'rooms': h.get('rooms'),
            'sqm': h.get('sqm'),
            'entry': h.get('entry'),
            'floor': h.get('floor'),
            'location': h.get('location'),
            'street': h.get('street'),
            'neighborhood': h.get('neighborhood'),
            'features': h.get('features') or [],
            'is_listing': h.get('is_listing'),
            'sources': h.get('sources') or {},
            'score': h.get('score'),
            'reasons': h.get('reasons') or [],
            'flags': h.get('flags') or [],
            'is_wanted_like': h.get('is_wanted_like'),
            'has_negative_terms': h.get('has_negative_terms'),
        },
        'photo_count': post.get('photo_count'),
        'image_alts': (post.get('image_alts') or [])[:3],
        'text': (post.get('text') or '')[:max_text],
    }


def prompt_text(input_path, output_json, output_md):
    return f"""Low-cost semantic triage for Facebook apartment-search posts.\n\nRead `{input_path}`. Classify each post using the criteria inside the file. Be conservative and practical: distinguish real apartment listings from wanted posts, sale posts, irrelevant/studio/roommate/sublet, over-budget, too-small, or missing critical info.\n\nWrite:\n1) `{output_json}`\n2) `{output_md}`\n\nJSON shape:\n{{\n  \"summary\": {{\"posts_reviewed\": number, \"relevant_yes\": number, \"relevant_maybe\": number, \"relevant_no\": number, \"quality_notes\": [\"...\"]}},\n  \"items\": [\n    {{\n      \"id\": \"post_...\",\n      \"cache_key\": \"...\",\n      \"verdict\": \"yes|maybe|no\",\n      \"confidence\": \"high|medium|low\",\n      \"is_real_listing\": true,\n      \"reason_short\": \"Hebrew short reason\",\n      \"extracted\": {{\"price\": number|null, \"rooms\": number|null, \"sqm\": number|null, \"area\": string|null, \"entry\": string|null, \"address\": string|null}},\n      \"pros\": [\"...\"],\n      \"cons\": [\"...\"],\n      \"missing\": [\"...\"],\n      \"followup_needed\": [\"...\"],\n      \"post_url\": \"...\"\n    }}\n  ]\n}}\n\nMD should be a concise Hebrew shortlist ordered yes, maybe, no. Do not contact anyone or browse Facebook. Only read/write files.\n"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('clean_json', help='Output JSON from facebook_clean_posts.py')
    ap.add_argument('--cache', default=str(DEFAULT_CACHE))
    ap.add_argument('--out', default=str(ART / 'ai_triage_input_next.json'))
    ap.add_argument('--output-json', default=str(ART / 'ai_triage_next.json'))
    ap.add_argument('--output-md', default=str(ART / 'ai_triage_next.md'))
    ap.add_argument('--prompt-out', default=str(ART / 'ai_triage_next_prompt.md'))
    ap.add_argument('--model', default=DEFAULT_MODEL)
    ap.add_argument('--mode', choices=['smart', 'strict', 'all'], default='smart')
    ap.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    ap.add_argument('--max-text', type=int, default=1100)
    ap.add_argument('--cache-ttl-days', type=int, default=DEFAULT_CACHE_TTL_DAYS, help='Drop cache entries older than this many days before skipping cached posts')
    ap.add_argument('--include-cached', action='store_true')
    ap.add_argument('--batch', type=int, default=0, help='Batch number (1-based); 0 means all up to limit')
    args = ap.parse_args()

    clean = load_json(args.clean_json)
    posts = clean.get('posts') or []
    cache = load_cache(args.cache)
    pruned = prune_cache(cache, args.cache_ttl_days)
    cache_items = cache.get('items') or {}

    eligible=[]; skipped=[]
    for post in posts:
        key = stable_cache_key(post)
        ok, reason = should_send(post, args.mode)
        if not ok:
            skipped.append({'id': post.get('id'), 'cache_key': key, 'reason': reason})
            continue
        if key in cache_items and not args.include_cached:
            skipped.append({'id': post.get('id'), 'cache_key': key, 'reason': 'skip_cached'})
            continue
        eligible.append(compact_for_ai(post, args.max_text))

    # Batch selection: if --batch N (1-based), take only that slice. Otherwise
    # take the first --limit candidates. Earlier versions applied --limit before
    # batching, which made batch >1 unreachable.
    batch_size = args.limit or DEFAULT_LIMIT
    if args.batch > 0:
        start = (args.batch - 1) * batch_size
        end = start + batch_size
        total_eligible = len(eligible)
        selected = eligible[start:end]
        if not selected:
            print(f'Batch {args.batch} is empty (total {total_eligible} eligible, start={start})')
        else:
            print(f'Batch {args.batch}: taking items {start+1}-{min(end, total_eligible)} of {total_eligible}')
    else:
        selected = eligible[:args.limit] if args.limit else eligible

    total_batches = max(1, (len(eligible) + batch_size - 1) // batch_size) if batch_size else 1

    payload = {
        'generated_at': datetime.now().isoformat(),
        'source': str(args.clean_json),
        'cache': str(args.cache),
        'cache_ttl_days': args.cache_ttl_days,
        'cache_pruned': pruned,
        'target_model': args.model,
        'purpose': 'Semantic AI triage of cleaned Facebook apartment posts for apartment search',
        'criteria': CRITERIA,
        'prompt_out': str(args.prompt_out),
        'output_json': str(args.output_json),
        'output_md': str(args.output_md),
        'selection': {
            'mode': args.mode,
            'raw_clean_posts': len(posts),
            'eligible_for_ai': len(eligible),
            'selected_for_ai': len(selected),
            'skipped': len(skipped),
            'cached_items_available': len(cache_items),
            'include_cached': args.include_cached,
            'batch': args.batch,
            'batch_size': batch_size,
            'total_batches': total_batches,
        },
        'posts': selected,
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    prompt = prompt_text(out, pathlib.Path(args.output_json), pathlib.Path(args.output_md))
    pathlib.Path(args.prompt_out).write_text(prompt, encoding='utf-8')
    print('wrote', out)
    print('wrote', args.prompt_out)
    print('model', args.model, 'mode', args.mode, 'selected_for_ai', len(selected), 'skipped', len(skipped), 'cache_items', len(cache_items))
    for p in selected[:12]:
        h=p.get('heuristic') or {}
        print(p.get('id'), p.get('cache_key'), 'price', h.get('price'), 'rooms', h.get('rooms'), 'score', h.get('score'), (p.get('text') or '')[:90].replace('\n',' '))

if __name__ == '__main__':
    main()
