#!/usr/bin/env python3
"""Human-like read-only Facebook group feed scanner.

Use this as the primary Facebook workflow: enter a group, prefer recent posts,
scroll the discussion feed, expand "See more", and parse every visible post.
Search queries are optional follow-up tools, not the main discovery method.

No joins, posts, comments, reactions, or messages.
"""
import argparse, asyncio, json, pathlib, re
from datetime import datetime

from facebook_group_scan import (
    ART, PORT, call, click_see_more, close_page, connect_browser, extract_articles,
    navigate_page, new_page, normalize_article, wait_page,
)

async def try_sort_recent(ws, c):
    """Best-effort read-only UI interaction: choose Recent/New posts if menu exists."""
    click_menu = r'''
(() => {
 const norm = s => (s || '').replace(/\s+/g,' ').trim();
 const candidates = [...document.querySelectorAll('[role="button"], span, div, a')]
   .map(el => ({el, t: norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '')}))
   .filter(x => x.t === 'Most relevant' || x.t.includes('sort group feed by') || x.t.includes('Most relevant'))
   .sort((a,b) => a.t.length - b.t.length);
 for (const {el,t} of candidates) {
   try { el.click(); return t.slice(0,120); } catch(e) {}
 }
 return null;
})()
'''
    choose_recent = r'''
(() => {
 const norm = s => (s || '').replace(/\s+/g,' ').trim();
 const terms = ['Recent posts', 'New posts', 'Most recent', 'Newest activity', 'פוסטים אחרונים', 'הכי חדשים'];
 const candidates = [...document.querySelectorAll('[role="menuitem"], [role="option"], [role="button"], span, div')];
 for (const el of candidates) {
   const t = norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
   if (terms.some(term => t.includes(term))) {
     try { el.click(); return t.slice(0,120); } catch(e) {}
   }
 }
 return null;
})()
'''
    try:
        await call(ws, c, 'Runtime.evaluate', {'expression': click_menu, 'returnByValue': True, 'awaitPromise': True})
        await asyncio.sleep(0.8)
        res = await call(ws, c, 'Runtime.evaluate', {'expression': choose_recent, 'returnByValue': True, 'awaitPromise': True})
        return res.get('result', {}).get('result', {}).get('value')
    except Exception:
        return None

async def scan_feed(group, scrolls=8, delay=2.2, sort_recent=True, ws=None, c=None, browser_ws=None, tid=None, keep_open=False):
    url = f'https://www.facebook.com/groups/{group}/?sorting_setting=CHRONOLOGICAL'
    created_tab = False
    if ws is None:
        browser_ws = await connect_browser()
        tid, ws, c = await new_page(browser_ws, url)
        created_tab = True
    else:
        await navigate_page(ws, c, url)
    try:
        state, blocked = await wait_page(ws, c)
        if blocked:
            return {'blocked': True, 'state': state, 'items': [], 'ws': ws, 'c': c, 'browser_ws': browser_ws, 'tid': tid}
        chosen_sort = None
        if sort_recent:
            chosen_sort = await try_sort_recent(ws, c)
            await asyncio.sleep(1.2)
        all_items=[]; seen=set()
        for s in range(scrolls + 1):
            await click_see_more(ws, c)
            val = await extract_articles(ws, c)
            for art in val.get('articles', []):
                item = normalize_article(art, group, '__feed__')
                item['source_mode'] = 'feed_scroll'
                item['scroll_index'] = s
                key = item.get('key') or (item.get('text') or '')[:500]
                if key in seen:
                    continue
                seen.add(key)
                all_items.append(item)
            await call(ws, c, 'Runtime.evaluate', {'expression': 'window.scrollBy(0, Math.floor(window.innerHeight*1.15))'})
            await asyncio.sleep(delay)
        all_items.sort(key=lambda x: (-(x.get('score') or 0), x.get('price') or 999999))
        return {
            'blocked': False,
            'url': url,
            'group': group,
            'sort_recent_attempt': chosen_sort,
            'strategy_note': 'Feed-first human-like scan: scroll group discussion, expand See more, parse every visible post; search is only follow-up.',
            'items': all_items,
            'ws': ws,
            'c': c,
            'browser_ws': browser_ws,
            'tid': tid,
        }
    except Exception:
        if created_tab and not keep_open:
            try: await close_page(browser_ws, tid)
            except Exception: pass
            try: await browser_ws.close()
            except Exception: pass
        raise
    finally:
        # Close only for standalone usage. Multi-scan passes keep_open=True and closes/recycles itself.
        if created_tab and not keep_open:
            try: await ws.close()
            except Exception: pass
            try: await close_page(browser_ws, tid)
            except Exception: pass
            # Clean up any leftover tabs beyond the first one to prevent tab leaks
            try:
                import urllib.request as _ur
                tabs = json.loads(_ur.urlopen(f'http://127.0.0.1:{PORT}/json/list', timeout=5).read())
                if len(tabs) > 3:
                    for t in tabs[:-1]:  # keep the last tab alive
                        try: await call(browser_ws, [0], 'Target.closeTarget', {'targetId': t['id']})
                        except Exception: pass
            except Exception: pass
            try: await browser_ws.close()
            except Exception: pass

def clean_filename(s):
    return re.sub(r'[^0-9A-Za-zא-ת_-]+', '_', s).strip('_')[:80] or 'group'

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--group', required=True)
    ap.add_argument('--scrolls', type=int, default=8)
    ap.add_argument('--delay', type=float, default=2.2)
    ap.add_argument('--no-sort-recent', action='store_true')
    ap.add_argument('--out')
    args = ap.parse_args()
    res = await scan_feed(args.group, args.scrolls, args.delay, not args.no_sort_recent)
    res['generated_at'] = datetime.now().isoformat()
    path = pathlib.Path(args.out) if args.out else ART / f"feed_{clean_filename(args.group)}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding='utf-8')
    items = res.get('items') or []
    budget = [x for x in items if x.get('price') is not None and x['price'] <= 6500]
    print('wrote', path, 'items', len(items), 'budget', len(budget), 'blocked', res.get('blocked'), 'sort', res.get('sort_recent_attempt'))
    for i,item in enumerate(budget[:12],1):
        print(i, 'score=', item.get('score'), 'price=', item.get('price'), 'rooms=', item.get('rooms'), 'sqm=', item.get('sqm'), 'entry=', item.get('entry'))
        print(' ', (item.get('text') or '')[:240])

if __name__ == '__main__':
    asyncio.run(main())
