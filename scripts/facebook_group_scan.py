#!/usr/bin/env python3
"""Read-only Facebook group scanner for Yogev apartment research.

Uses the already-open dedicated Chromium profile/CDP session. It does not join groups,
post, comment, message, or bypass access controls. If Facebook asks for human input,
stop and let Dror handle it manually in the browser.

Example:
  python3 scripts/facebook_group_scan.py --group 564985183576779 --query 6500 --scrolls 3
"""
import argparse, asyncio, json, pathlib, re, time, urllib.parse, urllib.request
from datetime import datetime
import websockets

ROOT = pathlib.Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts' / 'facebook'
ART.mkdir(parents=True, exist_ok=True)

PORT = 9223
BLOCK_TERMS = [
    'Log in to Facebook', 'Facebook Login', 'You must log in', 'checkpoint',
    'Confirm your identity', 'Security Check', 'Suspicious activity', 'temporarily blocked'
]

KEYWORDS_POS = ['להשכרה','השכרה','דירה','חדרים','חדר','מ״ר','מר','כניסה','פינוי','מפנה','מתפנה','פנויה','פנוי','בעלים','ללא תיווך','גבעתיים','רמת גן','יד אליהו','ביצרון', 'for rent', 'apartment', 'room', 'rooms', 'entry', 'vacated', 'immediate', 'without brokerage', 'givatayim', 'ramat gan']
KEYWORDS_NEG = ['למכירה','שותפים','סאבלט','חניה בלבד','דרושים', 'for sale', 'asking price', 'for investment', 'investment or residence', 'price reduction', 'reduced from', 'million', 'taboo', 'טאבו', 'בטאבו', 'אטבו', 'מיליון', 'ירידת מחיר', 'roommates', 'sublet']
MIN_REASONABLE_RENT = 3500


def get_json(url):
    return json.load(urllib.request.urlopen(url, timeout=8))

async def call(ws, counter, method, params=None, timeout=60):
    counter[0] += 1
    await ws.send(json.dumps({'id': counter[0], 'method': method, 'params': params or {}}))
    start_time = time.time()
    while True:
        try:
            msg_str = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"CDP call {method} timed out after {timeout}s")
        msg = json.loads(msg_str)
        if msg.get('id') == counter[0]:
            return msg

async def connect_browser():
    ver = get_json(f'http://127.0.0.1:{PORT}/json/version')
    return await websockets.connect(ver['webSocketDebuggerUrl'])

async def new_page(browser_ws, url):
    c = [0]
    res = await call(browser_ws, c, 'Target.createTarget', {'url': url, 'newWindow': False, 'background': False})
    tid = res['result']['targetId']
    target = None
    for _ in range(60):
        for t in get_json(f'http://127.0.0.1:{PORT}/json/list'):
            if t.get('id') == tid:
                target = t; break
        if target: break
        await asyncio.sleep(0.2)
    if not target:
        raise RuntimeError('target not found')
    ws = await websockets.connect(target['webSocketDebuggerUrl'])
    cc = [0]
    await call(ws, cc, 'Page.enable')
    await call(ws, cc, 'Runtime.enable')
    return tid, ws, cc

async def close_page(browser_ws, tid):
    c = [0]
    try:
        await call(browser_ws, c, 'Target.closeTarget', {'targetId': tid})
    except Exception:
        pass

async def navigate_page(ws, c, url, timeout=20):
    """Navigate existing tab to new URL and wait for load."""
    await call(ws, c, 'Page.navigate', {'url': url})
    start = time.time()
    while time.time() - start < timeout:
        res = await call(ws, c, 'Runtime.evaluate', {
            'expression': "(() => ({ready:document.readyState,url:location.href,title:document.title}))()",
            'returnByValue': True,
            'awaitPromise': True,
        })
        val = res.get('result', {}).get('result', {}).get('value') or {}
        if val.get('ready') in ('interactive', 'complete') and url.split('?')[0] in val.get('url', ''):
            return val
        await asyncio.sleep(0.5)
    return val

async def wait_page(ws, c, timeout_s=25):
    start = time.time(); last = {}
    while time.time() - start < timeout_s:
        res = await call(ws, c, 'Runtime.evaluate', {
            'expression': "(() => ({ready:document.readyState,url:location.href,title:document.title,text:document.body?document.body.innerText:''}))()",
            'returnByValue': True,
            'awaitPromise': True,
        })
        last = res.get('result', {}).get('result', {}).get('value') or {}
        hay = ' '.join([last.get('url',''), last.get('title',''), (last.get('text') or '')[:3000]])
        if any(term in hay for term in BLOCK_TERMS):
            return last, True
        if len(last.get('text') or '') > 800 and last.get('ready') in ('interactive','complete'):
            return last, False
        await asyncio.sleep(0.5)
    return last, False

async def click_see_more(ws, c):
    # Read-only expansion of truncated visible posts. Avoids likes/comments/messages.
    expr = r'''
(() => {
 let n = 0;
 const terms = ['See more', 'עוד', 'ראה עוד', 'See More'];
 for (const el of document.querySelectorAll('[role="button"], a, span')) {
   const txt = (el.innerText || el.textContent || '').trim();
   if (terms.includes(txt)) {
     try { el.click(); n++; } catch(e) {}
     if (n >= 20) break;
   }
 }
 return n;
})()
'''
    try:
        await call(ws, c, 'Runtime.evaluate', {'expression': expr, 'returnByValue': True, 'awaitPromise': True})
    except Exception:
        pass

async def extract_articles(ws, c):
    expr = r'''
(() => {
 const clean = s => (s || '').replace(/\s+/g,' ').trim();
 const nodes = [...document.querySelectorAll('[role="article"], div[data-pagelet^="FeedUnit"], div[aria-posinset]')];
 const out = [];
 for (const el of nodes) {
   /* Try to extract only the post body, not comments/UI chrome */
   let postBody = '';
   const bodyCandidates = el.querySelectorAll('[data-ad-preview], [dir="auto"]');
   if (bodyCandidates.length > 0) {
     /* Collect text from dir=auto spans that look like post content (skip short ones) */
     const parts = [];
     for (const bc of bodyCandidates) {
       const t = clean(bc.innerText || bc.textContent || '');
       if (t.length > 20) parts.push(t);
     }
     if (parts.length > 0) postBody = parts.join(' ');
   }
   const text = postBody.length > 80 ? postBody : clean(el.innerText || el.textContent || '');
   if (text.length < 80) continue;
   const links = [...el.querySelectorAll('a[href]')].map(a => a.href).filter(h => /groups|posts|permalink|photo|pcb/.test(h));
   const images = [...el.querySelectorAll('img[src]')].map(img => ({src: img.src, alt: img.alt || ''})).slice(0,12);
   out.push({text, links: [...new Set(links)].slice(0,20), images});
 }
 return {url: location.href, title: document.title, text: clean(document.body ? document.body.innerText : '').slice(0,2000), articles: out.slice(0,80)};
})()
'''
    res = await call(ws, c, 'Runtime.evaluate', {'expression': expr, 'returnByValue': True, 'awaitPromise': True})
    return res.get('result', {}).get('result', {}).get('value') or {}


# Lines that are Facebook UI noise — stripped from post text
_NOISE_PATTERNS = [
    re.compile(r'\bWrite a public comment\b'),
    re.compile(r'\bLike\s*(·\s*\d+[hmd]?)?\s*(Reply|Share|Report)?\b'),
    re.compile(r'\bSee translation\b'),
    re.compile(r'\bSee original\b'),
    re.compile(r'\bRate this translation\b'),
    re.compile(r'\bSubmit your first comment\b'),
    re.compile(r'\bView all \d+ repl(?:y|ies)\b'),
    re.compile(r'\b\d+h?\s*(?:Like|Reply|Share)\b'),
    re.compile(r'\bFollow\s*$'),
    re.compile(r'·\s*Follow\b'),
    re.compile(r'\bAll-star contributor\b'),
    re.compile(r'\bTop contributor\b'),
]
# Pattern for author name + encoded garbage before · (e.g. "Name o S o n t d e p r s ... ·")
_GARBAGE_PREFIX = re.compile(r'^.*?\s(?:[a-zA-Z0-9]\s){10,}·\s*')


def clean_post_text(raw):
    """Remove Facebook UI noise and author-name encoding garbage from post text."""
    text = re.sub(r'\bFacebook\b', ' ', raw)
    text = re.sub(r'\s+', ' ', text).strip()
    # Strip author garbage at start (encoded name + · )
    text = _GARBAGE_PREFIX.sub('', text)
    # If there's still a "Name ·" at start (short garbage), strip it
    text = re.sub(r'^[^\n]{0,60}?·\s*', '', text)
    # Strip noise patterns
    for pat in _NOISE_PATTERNS:
        text = pat.sub(' ', text)
    # Clean up remaining artifacts
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def parse_price(text):
    vals = []
    low = (text or '').lower()
    sale_signals = (
        'למכירה', 'for sale', 'asking price', 'for investment', 'investment or residence',
        'written in the taboo', 'taboo', 'טאבו', 'למגורים או השקעה'
    )
    rent_signals = (
        'להשכרה', 'השכרה', 'שכירות', 'שכר דירה', 'for rent', 'rent', 'rental',
        'per month', 'לחודש', 'דירה להשכרה'
    )
    date_signals = (
        '2024', '2025', '2026', '2027',
        'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december',
        'ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני', 'יולי', 'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר',
        'aug', 'jul', 'jun', 'may', 'jun', 'jul'
    )
    # Sale posts often contain apartment-like facts plus a million-NIS asking price.
    # Do not mine incidental numbers from those texts as monthly rent.
    if any(s in low for s in sale_signals) and not any(s in low for s in rent_signals):
        return None
    if re.search(r'(?<![\d,.])(?:[1-9][0-9]{6,}|[1-9][,.][0-9]{3}[,.][0-9]{3})(?![\d,.])', text or '') and not any(s in low for s in rent_signals):
        return None
    # Handles 6500, 6,500, 6500₪, ב6500. Avoid phone-number substrings like 0585551715.
    money_words = r'(?:₪|שח|ש"ח|שקל|שקלים|NIS|nis|rent|rental|shekel|shekels|שכר דירה|שכירות|שכ["׳\']?ד|עלות)'
    amount_pattern = r'(?:[0-9]{1,2}[,.][0-9]{3}(?![\d,.])|[1-9][0-9]{3,4}(?!\d))'
    patterns = [
        rf'(?i)(?:{money_words})\s*[:：-]?\s*({amount_pattern})',
        rf'(?<![\d,.])({amount_pattern})\s*(?:{money_words})',
        rf'(?<![\d,.])({amount_pattern})\s*(?:₪|שח|ש"ח)',
    ]
    # Standalone 4-digit prices are allowed only when the post already looks like a rental.
    # If the text is date-heavy (year/month references), don't treat bare years as prices.
    if any(s in low for s in rent_signals) and not any(s in low for s in date_signals):
        patterns.append(r'(?<![\d,.])([1-9][0-9]{3})(?![\d,.])')
    for p in patterns:
        for m in re.finditer(p, text):
            try:
                raw = m.group(1)
                v = int(raw.replace(',', '').replace('.', ''))
                if MIN_REASONABLE_RENT <= v <= 25000:
                    around = text[max(0, m.start() - 12):m.end() + 12]
                    compact_digits = re.sub(r'\D', '', around)
                    if len(compact_digits) >= 7 or re.search(r'\b0(?:5\d|[23489])[-\s]?\d{3}', around):
                        continue
                    # Reject obvious years when no currency context is present.
                    if v in {2024, 2025, 2026, 2027} and not re.search(r'(₪|שח|ש"ח|NIS|nis|rent|rental|שכר דירה|שכירות|לחודש)', around, flags=re.I):
                        continue
                    vals.append(v)
            except Exception:
                pass
        if vals:
            return vals[0]
    return None


def parse_rooms(text):
    patterns = [
        # Hebrew: 3.5 חדרים, 3 חד'
        r'([0-9](?:[.,][0-9])?)\s*(?:חדרים|חד[׳\']|חדר)',
        # English: 3.5 room(s), bedroom(s)
        r'([0-9](?:[.,][0-9])?)\s*(?:room|rooms|bedroom|bedrooms)\b',
        # Spelled out: "three and a half room"
        r'\b(?:one|two|three|four|five)\s+and\s+a\s+half\s+(?:room|rooms|bedroom|bedrooms)\b',
        # "X and a half room"
        r'([0-9])\s+and\s+a\s+half\s+(?:room|rooms|bedroom|bedrooms)\b',
        # "X room in Givatayim" (misparse guard)
        r'\b([0-9](?:[.,][0-9])?)\s+in\s+(?:Givatayim|Ramat Gan)\b',
    ]
    word_to_num = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5}
    for i, p in enumerate(patterns):
        m = re.search(p, text, flags=re.I)
        if m:
            if i == 2:  # spelled out
                w = re.match(r'(one|two|three|four|five)', m.group(0), re.I).group(1).lower()
                return word_to_num[w] + 0.5
            try:
                return float(m.group(1).replace(',', '.'))
            except Exception:
                pass
    return None


def parse_sqm(text):
    vals=[]
    for m in re.finditer(r'([0-9]{2,3})\s*(?:מ[״"\']?ר|מר|מטר|sqm|sq\.?m|m2|meter|meters)', text, flags=re.I):
        v=int(m.group(1))
        if 20 <= v <= 250: vals.append(v)
    return vals[0] if vals else None


# Known neighborhoods in target area
_NEIGHBORHOODS = [
    # Ramat Gan
    'מרום נווה', 'נווה יהושע', 'קריית קריניצי', 'קריית בורוכוב', 'רמת ישראל',
    'רמת אפעל', 'רמת שקמה', 'רמת חן', 'רמת עמידר', 'תל בנימין', 'שכונת ו',
    'ביצרון', 'יד אליהו', 'נחלת גנים', 'שכונת הרצוג', 'גני אביב',
    # Givatayim
    'בורוכוב', 'כפר אזר', 'שכונת האלוף', 'שכונת הכוכב', 'רמת חן גבעתיים',
    'שכונת הצבר', 'ארלוזורוב', 'שכונת הגפן',
]

_STREETS = [
    'קצנלסון', 'בן גוריון', 'ביאליק', 'הרצל', 'סוקולוב', 'ירושלים', 'שדרות ירושלים',
    'הראשונים', 'העלייה', 'פנחס רוזן', 'אבא הלל', 'ז׳בוטינסקי', 'אמנון ותמר',
    'הגפן', 'התאנה', 'הסברון', 'הרב קוק', 'רמב״ם', 'נחשון', 'גולומב', 'השומר',
    'הפועלים', 'ברנר', 'דוד אלעזר', 'מנחם בגין', 'חיים לבנון',
    'Katznelson', 'Ben Gurion', 'Bialik', 'Herzl', 'Sokolov', 'Jerusalem',
    'Jabotinsky', 'Abba Hillel',
]


def parse_location(text):
    """Extract neighborhood and/or street from post text."""
    found_neighborhoods = []
    found_streets = []

    for n in _NEIGHBORHOODS:
        if n in text:
            found_neighborhoods.append(n)

    # Street matching: look for רחוב/Street prefix or standalone street name
    for s in _STREETS:
        # Hebrew streets: "רחוב קצנלסון" or "קצנלסון" near address context
        if s in text:
            # Check it's in an address-like context (near רחוב, Street, or number)
            idx = text.find(s)
            context = text[max(0, idx-30):idx+len(s)+30]
            if re.search(r'(?:רחוב|Street|St\.|ברחוב|on\s)', context, re.I) or re.search(r'\d', context):
                found_streets.append(s)

    result = {}
    if found_neighborhoods:
        result['neighborhood'] = found_neighborhoods[0]
    if found_streets:
        result['street'] = found_streets[0]
    return result if result else None


def parse_floor(text):
    """Extract floor number from post text."""
    patterns = [
        # Hebrew: קומה 3, קומה שלישית, קומת קרקע
        r'קומ[הת]\s*(?:בניין\s*)?(\d)',
        r'קומת?\s*קרקע',
        # English: 3rd floor, floor 3, ground floor
        r'(\d)(?:st|nd|rd|th)?\s+floor',
        r'floor\s*(\d)',
        r'ground\s+floor',
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            val = m.group(0)
            if 'קרקע' in val or 'ground' in val.lower():
                return 0
            try:
                return int(m.group(1))
            except (IndexError, ValueError):
                return 0
    return None


def parse_entry(text):
    # Entry keyword variants: כניסה, פינוי, entry, entrance
    kw = r'(?:כניסה|פינוי|entr(?:y|ance))'
    sep = r'[\s:：]*'
    be_on = r'(?:ב[- ]?|on\s+)?'
    patterns = [
        # Immediate
        rf'{kw}{sep}{be_on}(מיידי|מיידית|מידי|מידית|immediate)',
        # Flexible
        rf'{kw}{sep}{be_on}(גמיש|גמישה|flexible)',
        # Date: DD/MM/YYYY or DD.MM.YY or D.M etc.
        rf'{kw}{sep}{be_on}([0-9]{{1,2}}[./][0-9]{{1,2}}(?:[./][0-9]{{2,4}})?)',
        # Month names (Hebrew)
        rf'{kw}[^\n]{{0,30}}(יולי|July)',
        rf'{kw}[^\n]{{0,30}}(אוגוסט|August)',
        rf'{kw}[^\n]{{0,30}}(מאי|May)',
        rf'{kw}[^\n]{{0,30}}(יוני|June)',
        rf'{kw}[^\n]{{0,30}}(ספטמבר|September)',
        rf'{kw}[^\n]{{0,30}}(אוקטובר|October)',
        rf'{kw}[^\n]{{0,30}}(נובמבר|November)',
        rf'{kw}[^\n]{{0,30}}(דצמבר|December)',
        rf'{kw}[^\n]{{0,30}}(ינואר|January)',
        rf'{kw}[^\n]{{0,30}}(פברואר|February)',
        rf'{kw}[^\n]{{0,30}}(מרץ|March)',
        rf'{kw}[^\n]{{0,30}}(אפריל|April)',
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.I)
        if m:
            val = m.group(1)
            # Normalize date formats: "10.5" -> "1/5", "15/05/26" -> "15/5/26"
            if re.match(r'\d{1,2}[./]\d{1,2}', val):
                parts = re.split(r'[./]', val)
                val = f"{parts[0]}/{parts[1]}"
                if len(parts) > 2:
                    val += f"/{parts[2]}"
            return val
    return None


def candidate_score(item):
    text=item['text']
    score=0; reasons=[]; flags=[]
    price=item.get('price'); rooms=item.get('rooms'); sqm=item.get('sqm')
    is_listing=item.get('is_listing')
    
    # If LLM already marked this as a listing, give it a big boost
    if is_listing is True:
        score += 25; reasons.append('LLM סימן כדירה')
    elif is_listing is False:
        score -= 20; flags.append('LLM סימן כלא דירה')
    
    if price:
        if price <= 6500:
            score += 20; reasons.append('מחיר בתקציב')
        else:
            score -= 15; reasons.append('מחיר מעל התקציב')
    else:
        flags.append('מחיר לא צוין'); score += 5
    if sqm and sqm >= 55: score += 4
    for w in ['גבעתיים','רמת גן','יד אליהו','ביצרון','רמת ישראל']:
        if w in text: score += 5; reasons.append(w); break
    low = text.lower()
    for w in ['ללא תיווך','בעלים','לטווח ארוך','מזגן','מיזוג','שקט','עורפית','עורפי','משופצת','משופץ','מרווחת','מרווח','חדר עבודה', 'without brokerage', 'quiet', 'renovated', 'spacious', 'immediate', 'air conditioner', 'ac']:
        if w in text or w in low: score += 3
    for w in KEYWORDS_NEG:
        if w in text: score -= 10; flags.append(w)
    if not any(w in text for w in KEYWORDS_POS): score -= 8
    return score, reasons[:5], flags[:5]


def normalize_article(article, group, query):
    text = clean_post_text(article.get('text',''))
    location = parse_location(text)
    item = {
        'group': group,
        'query': query,
        'text': text[:3000],
        'links': article.get('links', []),
        'images': article.get('images', []),
        'price': parse_price(text),
        'rooms': parse_rooms(text),
        'sqm': parse_sqm(text),
        'entry': parse_entry(text),
        'floor': parse_floor(text),
        'location': location,
    }
    if location:
        if location.get('neighborhood'):
            item['neighborhood'] = location['neighborhood']
        if location.get('street'):
            item['street'] = location['street']
    item['score'], item['reasons'], item['flags'] = candidate_score(item)
    # stable-ish key from first post/photo link or text prefix
    key = None
    for link in item['links']:
        m = re.search(r'(?:posts|permalink|pcb|fbid=)[=/]?([0-9]{8,})', link)
        if m:
            key = m.group(1); break
    item['key'] = key or str(abs(hash(text[:500])))
    return item

async def scan_group(group, query, scrolls=4):
    url = f'https://www.facebook.com/groups/{group}/search/?q=' + urllib.parse.quote(query)
    browser_ws = await connect_browser()
    tid, ws, c = await new_page(browser_ws, url)
    try:
        state, blocked = await wait_page(ws, c)
        if blocked:
            return {'blocked': True, 'state': state, 'items': []}
        all_items=[]; seen=set()
        for s in range(scrolls + 1):
            await click_see_more(ws, c)
            val = await extract_articles(ws, c)
            for art in val.get('articles', []):
                item = normalize_article(art, group, query)
                if item['key'] in seen: continue
                seen.add(item['key']); all_items.append(item)
            await call(ws, c, 'Runtime.evaluate', {'expression': 'window.scrollBy(0, Math.floor(window.innerHeight*1.2))'})
            await asyncio.sleep(2.0)
        all_items.sort(key=lambda x: x.get('score',0), reverse=True)
        return {'blocked': False, 'url': url, 'group': group, 'query': query, 'items': all_items}
    finally:
        try: await ws.close()
        except Exception: pass
        await close_page(browser_ws, tid)
        try: await browser_ws.close()
        except Exception: pass

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--group', required=True, help='Facebook group id or slug')
    ap.add_argument('--query', required=True, help='Search query within the group, e.g. 6500 / 1.7 / גבעתיים 6500')
    ap.add_argument('--scrolls', type=int, default=3)
    ap.add_argument('--out')
    args = ap.parse_args()
    result = await scan_group(args.group, args.query, args.scrolls)
    result['generated_at'] = datetime.now().isoformat()
    out = pathlib.Path(args.out) if args.out else ART / f"group_{re.sub(r'[^A-Za-z0-9_-]+','_',args.group)}__{re.sub(r'[^0-9A-Za-zא-ת_-]+','_',args.query)}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('wrote', out, 'items', len(result.get('items', [])), 'blocked', result.get('blocked'))
    for i,item in enumerate((result.get('items') or [])[:10],1):
        print(i, 'score=', item.get('score'), 'price=', item.get('price'), 'rooms=', item.get('rooms'), 'sqm=', item.get('sqm'), 'entry=', item.get('entry'))
        print(' ', item.get('text','')[:220])

if __name__ == '__main__':
    asyncio.run(main())
