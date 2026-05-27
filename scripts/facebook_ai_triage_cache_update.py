#!/usr/bin/env python3
"""Update persistent Facebook AI-triage cache from a triage output JSON.

The cache prevents resending already-reviewed posts to low-cost AI in future runs.
"""
import argparse, json, pathlib
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts' / 'facebook'
DEFAULT_CACHE = ART / 'triage_cache.json'
CACHE_VERSION = 1


def load_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))


def load_cache(path):
    path = pathlib.Path(path)
    if not path.exists():
        return {'version': CACHE_VERSION, 'created_at': datetime.now().isoformat(), 'updated_at': None, 'items': {}}
    data = load_json(path)
    data.setdefault('version', CACHE_VERSION)
    data.setdefault('items', {})
    return data


def input_index(input_json):
    if not input_json:
        return {}
    p = pathlib.Path(input_json)
    if not p.exists():
        return {}
    data = load_json(p)
    return {post.get('id'): post for post in data.get('posts') or []}


def stable_cache_key(src):
    import hashlib
    if src.get('cache_key'):
        return src['cache_key']
    if src.get('dedupe_key'):
        return src['dedupe_key']
    if src.get('post_url'):
        return hashlib.sha1(src['post_url'].encode('utf-8')).hexdigest()[:16]
    text = src.get('similarity_signature') or src.get('text') or ''
    if text:
        return hashlib.sha1(text[:500].encode('utf-8')).hexdigest()[:16]
    return None


def normalize_summary(data):
    s = data.get('summary') or {}
    return {
        'posts_reviewed': s.get('posts_reviewed') or s.get('reviewed'),
        'yes': s.get('yes') or s.get('relevant_yes'),
        'maybe': s.get('maybe') or s.get('relevant_maybe'),
        'no': s.get('no') or s.get('relevant_no'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('triage_json', help='AI triage output JSON')
    ap.add_argument('--input-json', help='AI triage input JSON; used for cache_key/source metadata')
    ap.add_argument('--cache', default=str(DEFAULT_CACHE))
    ap.add_argument('--model', default='glm-5.1')
    ap.add_argument('--out-md', default=str(ART / 'triage_cache_summary.md'))
    args = ap.parse_args()

    triage = load_json(args.triage_json)
    posts_by_id = input_index(args.input_json)
    cache = load_cache(args.cache)
    items = cache.setdefault('items', {})
    now = datetime.now().isoformat()
    added=0; updated=0; skipped=0
    for item in triage.get('items') or []:
        pid = item.get('id')
        src = posts_by_id.get(pid) or {}
        key = item.get('cache_key') or stable_cache_key(src)
        if not key:
            skipped += 1
            continue
        entry = {
            'cache_key': key,
            'source_post_id': pid,
            'post_url': item.get('post_url') or src.get('post_url'),
            'desktop_post_url': item.get('desktop_post_url') or src.get('desktop_post_url'),
            'mobile_post_url': item.get('mobile_post_url') or src.get('mobile_post_url'),
            'permalink_url': item.get('permalink_url') or src.get('permalink_url'),
            'mobile_permalink_url': item.get('mobile_permalink_url') or src.get('mobile_permalink_url'),
            'universal_post_url': item.get('universal_post_url') or src.get('universal_post_url'),
            'url_status': item.get('url_status') or src.get('url_status'),
            'crosspost_urls': src.get('crosspost_urls') or [],
            'seen_in_groups': src.get('seen_in_groups') or [],
            'model': args.model,
            'triaged_at': now,
            'verdict': item.get('verdict'),
            'confidence': item.get('confidence'),
            'is_real_listing': item.get('is_real_listing'),
            'reason_short': item.get('reason_short'),
            'extracted': item.get('extracted') or {},
            'pros': item.get('pros') or [],
            'cons': item.get('cons') or [],
            'missing': item.get('missing') or [],
            'followup_needed': item.get('followup_needed') or [],
            'text_excerpt': (src.get('text') or '')[:500],
        }
        if key in items:
            updated += 1
        else:
            added += 1
        items[key] = entry
    cache['updated_at'] = now
    cache['last_update'] = {
        'triage_json': str(args.triage_json),
        'input_json': str(args.input_json) if args.input_json else None,
        'model': args.model,
        'summary': normalize_summary(triage),
        'added': added,
        'updated': updated,
        'skipped': skipped,
    }
    path = pathlib.Path(args.cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')

    counts = {'yes': 0, 'maybe': 0, 'no': 0, 'other': 0}
    for e in items.values():
        v = e.get('verdict')
        counts[v if v in counts else 'other'] += 1
    md = [
        '# Facebook AI triage cache', '',
        f'Updated: {now}',
        f'Total cached items: {len(items)}',
        f'Counts: yes={counts["yes"]}, maybe={counts["maybe"]}, no={counts["no"]}, other={counts["other"]}',
        f'Last update: added={added}, updated={updated}, skipped={skipped}', '',
        '## Yes / Maybe cached leads', ''
    ]
    top = [e for e in items.values() if e.get('verdict') in ('yes', 'maybe')]
    top.sort(key=lambda e: (e.get('verdict') != 'yes', e.get('post_url') or ''))
    for e in top[:80]:
        ex=e.get('extracted') or {}
        md += [
            f'- **{e.get("verdict")}** {e.get("reason_short") or ""}',
            f'  - price={ex.get("price")}, rooms={ex.get("rooms")}, sqm={ex.get("sqm")}, area={ex.get("area")}, entry={ex.get("entry")}',
            f'  - url: {e.get("universal_post_url") or "אין לינק ישיר לפוסט"}',
        ]
    pathlib.Path(args.out_md).write_text('\n'.join(md), encoding='utf-8')
    print('cache', path, 'total', len(items), 'added', added, 'updated', updated, 'skipped', skipped)
    print('counts', counts)
    print('summary_md', args.out_md)

if __name__ == '__main__':
    main()
