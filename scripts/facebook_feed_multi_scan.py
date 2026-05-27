#!/usr/bin/env python3
"""Run feed-first scans over a curated set of accessible Facebook apartment groups."""
import argparse, asyncio, json, logging, os, pathlib, re, time
from datetime import datetime
from facebook_group_feed_scan import scan_feed
from facebook_group_scan import close_page

log = logging.getLogger('scanner.multi')

# --- LLM Enhancement ---
# After regex extraction, run LLM to fill in missing fields.
# Controlled by --llm flag or YOGEV_LLM=1 env var.
_LLM_AVAILABLE = False
try:
    import llm_extract
    _LLM_AVAILABLE = True
except ImportError:
    pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts' / 'facebook'
ART.mkdir(parents=True, exist_ok=True)

DEFAULT_GROUPS = [
    # Deep daily scan: all historically accessible/productive apartment groups.
    # Dror prefers maximum coverage every run because good apartments disappear daily.
    {'id': '1456553661265604', 'name': 'דירות בשושו רמת גן גבעתיים תל אביב - אין כניסה למתווכים'},
    {'id': '564985183576779', 'name': 'דירות להשכרה בגבעתיים'},
    {'id': '1380680752778760', 'name': 'דירות להשכרה בגבעתיים'},
    {'id': '399396623465240', 'name': 'דירות להשכרה בגבעתיים'},
    {'id': '210828101191729', 'name': 'דירות להשכרה בגבעתיים'},
    {'id': '464974649987268', 'name': 'דירות להשכרה בגבעתיים בלבד'},
    {'id': '1998122560446744', 'name': 'דירות להשכרה בלבד! | גבעתיים בלבד | ללא תיווך.'},
    {'id': '115046608513246', 'name': 'דירות מפה לאוזן בגבעתיים'},
    {'id': '1386194455009158', 'name': 'דירות במחירים שפויים גבעתיים רמת גן והסביבה'},
    {'id': '625075606357799', 'name': 'דירות להשכרה ברמת גן וגבעתיים ללא תיווך עד 6000₪'},
    {'id': '203160775078548', 'name': 'דירות להשכרה ברמת גן'},
    {'id': '253957624766723', 'name': 'דירות להשכרה ברמת גן'},
    {'id': '1431533310402418', 'name': 'דירות להשכרה ברמת גן'},
    {'id': '192850633573', 'name': 'דירות מפה לאוזן ברמת גן'},
    {'id': 'DIRARAMATGAN', 'name': 'דירה להשכרה ברמת גן - ללא תיווך'},
    {'id': '1013405926610334', 'name': 'השכרת דירות ברמת גן ללא תיווך'},
    {'id': '434949249990596', 'name': 'דירה להשכרה ברמת גן'},
    {'id': '3822798808021263', 'name': 'דירות ללא תיווך ברמת גן וגבעתיים - ישירות מהבעלים'},
    {'id': '175757842565733', 'name': 'דירות להשכרה ברמת גן וגבעתיים'},
    {'id': '1870209196564360', 'name': 'דירות להשכרה רמת גן גבעתיים'},
    {'id': '1774413905909921', 'name': 'דירות להשכרה רמת גן גבעתיים'},
    {'id': '647901439404148', 'name': 'דירות להשכרה רמת גן/גבעתיים במחיר שפוי'},
    {'id': '1068642559922565', 'name': 'דירות ברמת גן - גבעתיים להשכרה בעוד חודש +'},
    {'id': '1642168479433463', 'name': 'דירות בדקה ה90 להשכרה ברמת גן גבעתיים'},
    {'id': '1040608103788431', 'name': 'דירות שוות ברמת גן והסביבה'},
    {'id': '170304031901994', 'name': 'דירות רמת גן גבעתיים'},
    {'id': '441654752934426', 'name': 'דירות להשכרה ברמת גן, גבעתיים והסביבה ללא תיווך עד 5200 שקל'},
    {'id': '2098391913533248', 'name': 'דירות להשכרה בגבעתיים ר"ג ותל אביב'},
    {'id': '186810449287215', 'name': 'דירות להשכרה גבעתיים - רמת גן - תל אביב'},
    {'id': '618612590371717', 'name': 'דירות להשכרה בתל אביב, רמת גן, גבעתיים והסביבה'},
    {'id': '692882024975122', 'name': 'דירות להשכרה תל אביב - רמת גן - גבעתיים'},
    {'id': '265003707247852', 'name': 'דירות להשכרה תל אביב רמת גן וגבעתיים + סבלאט. ללא תיווך'},
    {'id': '190589595080054', 'name': 'דירות מזרח תל אביב רמת גן גבעתיים זה כאן'},
    {'id': '2160088270869724', 'name': 'דירות להשכרה מכירה ברמת גן גבעתיים'},
    {'id': '2844599122523289', 'name': 'דירות להשכרה ומכירה ר״ג גבעתיים'},
    {'id': '895215257226969', 'name': 'דירות להשכרה ומכירה בגבעתיים ורמת גן - ללא תיווך'},
    {'id': '848566722447073', 'name': 'דירות להשכרה ומכירה תל אביב רמת גן גבעתיים'},
    {'id': '184664888737746', 'name': 'דירות רמת גן גבעתיים מכירה/השכרה'},
    {'id': '1632065176901483', 'name': 'דירות למכירה והשכרה רמת גן גבעתיים'},
    {'id': '641604913946062', 'name': 'דירות למכירה/השכרה רמת גן,גבעתיים,תל אביב'},
    {'id': '356656767106352', 'name': 'דירות תל אביב - רמת גן - גבעתיים : מכירה , השכרה , ת״א, ר״ג להשכרה למכירה'},
    {'id': '681450437482761', 'name': 'דירות להשכרה רמת גן גבעתיים בני ברק מתעדכון כול רגע'},
    {'id': '515084613733054', 'name': 'להשכרה ומכירה דירות ברמת גן גבעתיים'},
    {'id': '1446203822077011', 'name': 'לוח נדלן גבעתיים רמת גן וכל גוש דן'},
    {'id': '2950391991846523', 'name': 'שוק הדירות דירות להשכרה ומכירה בת"א גבעתיים ור"ג'},
]

MIN_BUDGET = 5500
MAX_BUDGET = 6500

BAD_TERMS = [
    'למכירה', 'שותפים', 'סאבלט', 'מחסן', 'for sale', 'roommates', 'sublet',
    'asking price', 'for investment', 'investment or residence', 'written in the taboo',
    'taboo', 'טאבו', 'בטאבו', 'אטבו', 'למגורים או השקעה',
    'price reduction', 'reduced from', 'million', 'מיליון', 'ירידת מחיר',
    'the master of the real estate',
]
WANTED_TERMS = ['מחפשים דירה', 'מחפש דירה', 'מחפשת דירה', 'בעלי ואני מחפשים', 'looking for an apartment']
GOOD_AREAS = ['גבעתיים', 'רמת גן', 'יד אליהו', 'ביצרון', 'רמת ישראל', 'Givatayim', 'Ramat Gan']


def post_url(item):
    from facebook_url_utils import normalize_facebook_item_urls
    # Do not fall back to links[0]: Facebook often exposes the author profile or
    # group URL first, which opens badly on iPhone and is not a post permalink.
    return normalize_facebook_item_urls(item).get('desktop_post_url') or ''


def relevant(item):
    text = item.get('text') or ''
    low = text.lower()
    is_listing = item.get('is_listing')
    
    # If LLM explicitly marked this as NOT a listing, filter it out
    if is_listing is False:
        return False
    
    # If LLM explicitly marked this as a listing, keep it (unless bad terms)
    if is_listing is True:
        if any(t.lower() in low for t in BAD_TERMS):
            return False
        return True
    
    # Fallback to heuristics for items without LLM classification
    if any(t.lower() in low for t in BAD_TERMS):
        return False
    if any(t.lower() in low for t in WANTED_TERMS):
        return False
    price = item.get('price')
    # Budget is an upper ceiling. Low-priced full-apartment posts should not be
    # discarded here; downstream review can flag suspicious/roommate cases.
    if price is not None and price > MAX_BUDGET:
        return False
    rooms = item.get('rooms')
    if rooms is not None and rooms < 2.5:
        return False
    # If price is missing but text looks like an apartment, keep as maybe only if area is relevant.
    if price is None and not any(a in text for a in GOOD_AREAS):
        return False
    return True


def _content_key(text):
    import hashlib
    cleaned = re.sub(r'\s+', ' ', (text or '').strip())[:700]
    return hashlib.sha1(cleaned.encode('utf-8')).hexdigest() if cleaned else None


def dedupe(items):
    seen_urls=set(); seen_content=set(); out=[]
    for item in items:
        url = post_url(item)
        content_key = _content_key(item.get('text') or '')
        raw_key = item.get('key')
        # Dedupe by URL OR content signature. The old tuple-based check missed
        # cross-posts where the same content appeared under different URLs/groups.
        if url and url in seen_urls:
            continue
        if content_key and content_key in seen_content:
            continue
        if raw_key and raw_key in seen_content:
            continue
        if url:
            seen_urls.add(url)
        if content_key:
            seen_content.add(content_key)
        if raw_key:
            seen_content.add(raw_key)
        out.append(item)
    return out


def likely_candidate_for_llm(item):
    """Cheap prefilter before expensive LLM extraction."""
    text = item.get('text') or ''
    low = text.lower()
    if any(t.lower() in low for t in BAD_TERMS + WANTED_TERMS):
        return False
    price = item.get('price')
    if price is not None and not (5000 <= price <= 7000):
        return False
    rooms = item.get('rooms')
    if rooms is not None and rooms < 2.5:
        return False
    if any(a in text for a in GOOD_AREAS):
        return True
    if price is not None or rooms is not None:
        return True
    return False


def enhance_with_llm(items, checkpoint_path=None):
    """Run LLM extraction on items to fill fields that regex missed.
    
    Only processes items that have 2+ missing key fields (price, rooms, entry, floor, location).
    Skips items that are already well-extracted by regex.
    """
    if not _LLM_AVAILABLE:
        log.warning("LLM module not available, skipping enhancement")
        return items
    
    if not llm_extract._detect_backend():
        log.warning("No LLM backend available, skipping LLM enhancement")
        return items
    
    # Filter: only enhance plausible relevant items that need it (2+ missing key fields).
    # This avoids spending Gemini/GLM calls on sale posts, wanted posts, tiny rooms, etc.
    need_llm = []
    need_indices = []
    for idx, item in enumerate(items):
        if not likely_candidate_for_llm(item):
            continue
        missing = sum(1 for k in ['price', 'rooms', 'entry', 'floor'] if item.get(k) is None)
        has_location = bool(item.get('location') or item.get('neighborhood') or item.get('street'))
        if not has_location:
            missing += 1
        if missing >= 2:
            need_indices.append(idx)
            need_llm.append(item)
    
    if not need_llm:
        log.info("No items need LLM enhancement")
        return items
    
    log.info(f"Enhancing {len(need_llm)}/{len(items)} items with LLM...")
    
    enhanced = llm_extract.batch_extract_hybrid(
        need_llm,
        progress_cb=lambda i, t, item: log.info(
            f"  [{i}/{t}] price={item.get('price', '?')}"
            f"({item.get('price_source', 'regex')}) "
            f"rooms={item.get('rooms', '?')} entry={item.get('entry', '?')}"
        ) if i % 10 == 0 or i == t else None,
        checkpoint_path=checkpoint_path,
        checkpoint_meta={'source': 'facebook_feed_multi_scan', 'input_items': len(items)},
    )
    
    # Merge back by original position. Do not rely on object identity: when LLM
    # fills fields, extract_hybrid returns a new dict, and resumed checkpoint
    # items are also new objects loaded from JSON.
    result = list(items)
    for original_idx, updated in zip(need_indices, enhanced):
        result[original_idx] = updated
    
    # Stats
    llm_fills = {
        'price': sum(1 for i in result if i.get('price_source') == 'llm'),
        'rooms': sum(1 for i in result if i.get('rooms_source') == 'llm'),
        'entry': sum(1 for i in result if i.get('entry_source') == 'llm'),
        'floor': sum(1 for i in result if i.get('floor_source') == 'llm'),
        'location': sum(1 for i in result if i.get('location_source') == 'llm'),
    }
    log.info(f"LLM fills: {llm_fills}")
    
    return result

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scrolls', type=int, default=8)
    ap.add_argument('--delay', type=float, default=2.5)
    ap.add_argument('--groups', nargs='*', help='Override group ids only')
    ap.add_argument('--llm', action='store_true', default=bool(os.environ.get('YOGEV_LLM')),
                    help='Enable LLM enhancement for missing fields (default: YOGEV_LLM env)')
    ap.add_argument('--out')
    ap.add_argument('--llm-checkpoint', help='Checkpoint path for incremental LLM enrichment output')
    args = ap.parse_args()

    groups = [{'id': g, 'name': g} for g in args.groups] if args.groups else DEFAULT_GROUPS
    runs=[]; all_items=[]
    # Reuse a single browser tab across groups; recycle every TAB_RECYCLE_INTERVAL groups to prevent memory bloat.
    TAB_RECYCLE_INTERVAL = 5
    ws = c = browser_ws = tid = None
    for idx, g in enumerate(groups):
        # Recycle tab every N groups to prevent Chromium memory bloat
        if idx > 0 and idx % TAB_RECYCLE_INTERVAL == 0 and ws is not None:
            print(f'  [recycle] Closing tab after {TAB_RECYCLE_INTERVAL} groups to free memory', flush=True)
            try: await ws.close()
            except Exception: pass
            try: await close_page(browser_ws, tid)
            except Exception: pass
            try: await browser_ws.close()
            except Exception: pass
            ws = c = browser_ws = tid = None
            await asyncio.sleep(2.0)  # Let Chromium GC before opening next tab

        print(f"SCAN {g['id']} {g['name']}", flush=True)
        try:
            res = await scan_feed(g['id'], scrolls=args.scrolls, delay=args.delay, sort_recent=True, ws=ws, c=c, browser_ws=browser_ws, tid=tid, keep_open=True)
        except Exception as e:
            print('  ERROR', repr(e), flush=True)
            runs.append({'group': g, 'error': repr(e)})
            continue
        # Reuse the returned tab handles for the next group
        ws = res.get('ws')
        c = res.get('c')
        browser_ws = res.get('browser_ws')
        tid = res.get('tid')
        items = res.get('items') or []
        for item in items:
            item['group_name'] = g['name']
            item['post_url'] = post_url(item)
        budget = [x for x in items if relevant(x) and x.get('price') is not None]
        maybe = [x for x in items if relevant(x) and x.get('price') is None]
        print('  items', len(items), 'budget', len(budget), 'maybe_no_price', len(maybe), 'blocked', res.get('blocked'), flush=True)
        runs.append({'group': g, 'blocked': res.get('blocked'), 'items': len(items), 'budget': len(budget), 'maybe_no_price': len(maybe), 'sort': res.get('sort_recent_attempt')})
        if res.get('blocked'):
            break
        all_items.extend(items)

    # Final cleanup: close the reused tab
    if tid is not None:
        print('[cleanup] Closing final reused tab', flush=True)
        try: await ws.close()
        except Exception: pass
        try: await close_page(browser_ws, tid)
        except Exception: pass
        try: await browser_ws.close()
        except Exception: pass

    all_items = dedupe(all_items)
    
    # LLM enhancement: fill missing fields
    if args.llm and _LLM_AVAILABLE:
        print(f"LLM enhancement enabled. Enhancing {len(all_items)} items...", flush=True)
        t0 = time.time()
        checkpoint = args.llm_checkpoint
        if not checkpoint and args.out:
            checkpoint = str(pathlib.Path(args.out).with_suffix('.llm_checkpoint.json'))
        all_items = enhance_with_llm(all_items, checkpoint_path=checkpoint)
        print(f"LLM enhancement done in {time.time()-t0:.0f}s", flush=True)
    
    candidates = [x for x in all_items if relevant(x)]
    candidates.sort(key=lambda x: (x.get('price') is None, -(x.get('score') or 0), x.get('price') or 999999))
    out = {
        'generated_at': datetime.now().isoformat(),
        'strategy': 'feed-first human-like scan over group discussion feeds; search not used for discovery',
        'groups': groups,
        'runs': runs,
        'items_total': len(all_items),
        'candidates_total': len(candidates),
        'candidates': candidates,
        'all_items': all_items,
    }
    path = pathlib.Path(args.out) if args.out else ART / f"multi_feed_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print('WROTE', path, 'items', len(all_items), 'candidates', len(candidates), flush=True)
    for i,x in enumerate(candidates[:20],1):
        print(i, x.get('group_name'), 'score', x.get('score'), 'price', x.get('price'), 'rooms', x.get('rooms'), 'sqm', x.get('sqm'), 'entry', x.get('entry'), flush=True)
        print(' ', (x.get('text') or '')[:240], flush=True)
        print(' ', x.get('post_url'), flush=True)

if __name__ == '__main__':
    asyncio.run(main())
