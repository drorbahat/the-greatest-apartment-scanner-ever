#!/usr/bin/env python3
"""Scrape Madlan public apartment listings for Apartment Scanner.

Uses the visible Chromium session via CDP. When Madlan asks for
verification, the scraper stops detail enrichment and leaves a tab open so Dror
can solve the CAPTCHA manually; it does not try to bypass the challenge.
Falls back to plain urllib if CDP is not available.

Architecture:
- 7 list pages fetched sequentially (CDP or urllib)
- Deduplication + candidate filtering
- Entry date enrichment: conservative few-listing CDP fetches with manual
  tab reuse when available.
- Partial save: writes results before enrichment so timeout doesn't lose data.
"""
import asyncio, html, json, os, pathlib, random, re, sys, time, urllib.parse, urllib.request
from collections import Counter
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_ID = os.environ.get('YOGEV_RUN_ID')
RUN_DATE = os.environ.get('YOGEV_SCAN_DATE') or datetime.now().strftime('%Y-%m-%d')
# Use run-specific directory if available, otherwise fall back to date-based directory
if RUN_ID:
    ART = ROOT / 'artifacts' / f'broad_search_{RUN_ID}'
else:
    ART = ROOT / 'artifacts' / f'broad_search_{RUN_DATE}'
ART.mkdir(parents=True, exist_ok=True)

URLS = [
    ('madlan_givataim', 'מדלן — גבעתיים', 'https://www.madlan.co.il/for-rent/%D7%92%D7%91%D7%A2%D7%AA%D7%99%D7%99%D7%9D-%D7%99%D7%A9%D7%A8%D7%90%D7%9C'),
    ('madlan_ramat_gan', 'מדלן — רמת גן', 'https://www.madlan.co.il/for-rent/%D7%A8%D7%9E%D7%AA-%D7%92%D7%9F-%D7%99%D7%A9%D7%A8%D7%90%D7%9C'),
    ('madlan_yad_eliyahu', 'מדלן — יד אליהו', 'https://www.madlan.co.il/for-rent/%D7%A9%D7%9B%D7%95%D7%A0%D7%94-%D7%99%D7%93-%D7%90%D7%9C%D7%99%D7%94%D7%95-%D7%AA%D7%9C-%D7%90%D7%91%D7%99%D7%91-%D7%99%D7%A4%D7%95-%D7%99%D7%A9%D7%A8%D7%90%D7%9C'),
    ('madlan_bitzaron', 'מדלן — ביצרון', 'https://www.madlan.co.il/for-rent/%D7%A9%D7%9B%D7%95%D7%A0%D7%94-%D7%91%D7%99%D7%A6%D7%A8%D7%95%D7%9F-%D7%AA%D7%9C-%D7%90%D7%91%D7%99%D7%91-%D7%99%D7%A4%D7%95-%D7%99%D7%A9%D7%A8%D7%90%D7%9C'),
    ('madlan_old_north_central', 'מדלן — הצפון הישן מרכזי', 'https://www.madlan.co.il/for-rent/%D7%A9%D7%9B%D7%95%D7%A0%D7%94-%D7%94%D7%A6%D7%A4%D7%95%D7%9F-%D7%94%D7%99%D7%A9%D7%9F-%D7%94%D7%97%D7%9C%D7%A7-%D7%94%D7%9E%D7%A8%D7%9B%D7%96%D7%99-%D7%AA%D7%9C-%D7%90%D7%91%D7%99%D7%91-%D7%99%D7%A4%D7%95-%D7%99%D7%A9%D7%A8%D7%90%D7%9C'),
    ('madlan_old_north_north', 'מדלן — הצפון הישן צפוני', 'https://www.madlan.co.il/for-rent/%D7%A9%D7%9B%D7%95%D7%A0%D7%94-%D7%94%D7%A6%D7%A4%D7%95%D7%9F-%D7%94%D7%99%D7%A9%D7%9F-%D7%94%D7%97%D7%9C%D7%A7-%D7%94%D7%A6%D7%A4%D7%95%D7%A0%D7%99-%D7%AA%D7%9C-%D7%90%D7%91%D7%99%D7%91-%D7%99%D7%A4%D7%95-%D7%99%D7%A9%D7%A8%D7%90%D7%9C'),
    ('madlan_lev_tel_aviv', 'מדלן — לב תל אביב', 'https://www.madlan.co.il/for-rent/%D7%A9%D7%9B%D7%95%D7%A0%D7%94-%D7%9C%D7%91-%D7%AA%D7%9C-%D7%90%D7%91%D7%99%D7%91-%D7%AA%D7%9C-%D7%90%D7%91%D7%99%D7%91-%D7%99%D7%A4%D7%95-%D7%99%D7%A9%D7%A8%D7%90%D7%9C'),
]

BAD_AREAS = ['דרום תל אביב', 'יפו', 'פלורנטין', 'שפירא', 'נווה שאנן', 'התקווה', 'רמת אביב', 'צהלה', 'נווה שרת', 'נווה אליעזר', 'כפר שלם']

_MADLAN_BLOCK_SIGNALS = (
    "access denied",
    "forbidden",
    "verify you are human",
    "are you a robot",
    "please enable javascript",
    "ddos protection",
    "checking your browser",
)
PREFERRED = ['גבעתיים','רמת גן','יד אליהו','ביצרון','רמת ישראל','הצפון הישן','בזל','לב תל אביב','לב העיר']
MIN_BUDGET = 5500
MAX_BUDGET = 6500

# Tunables for entry date enrichment
ENRICH_MAX_ITEMS = 3           # conservative: a few detail pages per run, stop on first block
ENRICH_CONCURRENCY = 1         # sequential — parallel triggers Madlan bot detection
ENRICH_DETAIL_WAIT_S = 8       # seconds to wait for detail page load
ENRICH_RATE_LIMIT_S = 35       # seconds between requests (slow to avoid blocks)
ENRICH_MAX_SECONDS = 160       # hard cap for enrichment phase

# Madlan block detection
MADLAN_BLOCKED_MIN_HTML_LEN = 50_000   # listing detail pages shorter than this are likely blocked
MADLAN_BLOCK_COOLDOWN_SECONDS = 60 * 60  # 1 hour cooldown after a block
MADLAN_BLOCK_STATE_PATH = ART / 'madlan_block_state.json'
MADLAN_BLOCK_MARKERS = [
    'סליחה על ההפרעה',
    'גרם לנו לחשוב שאתה רובוט',
    'אנא השלם את החידה',
    'captcha',
]
MADLAN_GENERIC_BLOCK_MARKERS = [
    'robot',
    'blocked',
]


def block_text(block):
    text = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', block, flags=re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def parse_price(text):
    # Try both formats: ₪1,850 (old) and 1,850 ₪ (new, with possible RTL marks)
    m = re.search(r'₪\s*([0-9]{1,2}(?:,[0-9]{3})|[0-9]{4,5})', text)
    if m: return int(m.group(1).replace(',',''))
    # New format: number followed by ₪ (with possible RTL marks ‏)
    m = re.search(r'([0-9]{1,2}(?:,[0-9]{3})|[0-9]{4,5})\s*‏?₪', text)
    if m: return int(m.group(1).replace(',',''))
    return None

def parse_rooms(text):
    m = re.search(r'([0-9](?:\.[0-9])?)\s*חד', text)
    if m: return float(m.group(1))
    if 'חדר אחד' in text: return 1.0
    return None

def parse_floor(text):
    m = re.search(r'קומה\s*([0-9]+|קרקע|מרתף)', text)
    return m.group(1) if m else None

def parse_sqm(text):
    matches = re.findall(r'([0-9]{2,3})\s*מ"ר', text)
    if matches:
        vals = [int(x) for x in matches]
        vals = [v for v in vals if 25 <= v <= 250]
        return vals[-1] if vals else None
    return None

def parse_address(text):
    m = re.search(r'(דירה|דירת גג|דירת גן|בית פרטי|דו משפחתי|יחידת דיור),\s*(.+)$', text)
    if m:
        return m.group(2).replace(' <div data-auto="listed-bulletin"','').strip()
    return text

def score_item(x):
    price=x.get('price'); rooms=x.get('rooms'); sqm=x.get('sqm'); text=x.get('text','')
    score=0; notes=[]
    if rooms and rooms >= 2.5: score += 8
    if rooms and 2.5 <= rooms <= 4: score += 5
    if sqm and sqm >= 70: score += 7
    elif sqm and sqm >= 60: score += 3
    if price:
        if price <= MAX_BUDGET: score += 12
        else: score -= 15; notes.append('מעל התקציב')
    if any(a in text for a in ['גבעתיים','יד אליהו','ביצרון']): score += 8
    if 'רמת גן' in text: score += 5
    if any(a in text for a in ['הצפון הישן','בזל','לב תל אביב','לב העיר']): score += 3
    if any(a in text for a in BAD_AREAS): score -= 20
    if 'תיווך' in text: notes.append('תיווך')
    if price and price >= 8500 and any(a in text for a in ['הצפון הישן','לב תל אביב']): notes.append('אזור נדיר אבל יקר יחסית')
    x['score']=score; x['notes']=notes
    return score

def parse_items_from_html(html_content, source_id, label):
    """Parse apartment items from Madlan HTML."""
    items = []
    ids = []
    for m in re.finditer(r'data-auto-bulletin-id="([^"]+)"', html_content):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    for bid in ids:
        m = re.search(r'data-auto-bulletin-id="%s".*?(?=data-auto-bulletin-id=|$)' % re.escape(bid), html_content, re.S)
        if not m: continue
        text = block_text(m.group(0))
        if '₪' not in text or 'מ"ר' not in text: continue
        x = {
            'source': 'מדלן', 'source_id': source_id, 'source_label': label,
            'id': bid, 'url': 'https://www.madlan.co.il/listings/' + bid,
            'text': text, 'price': parse_price(text), 'rooms': parse_rooms(text),
            'floor': parse_floor(text), 'sqm': parse_sqm(text), 'address': parse_address(text)
        }
        score_item(x)
        items.append(x)
    return items


async def fetch_via_cdp(url):
    """Fetch page HTML via CDP browser."""
    import websockets

    # Get browser WebSocket URL
    req = urllib.request.Request('http://127.0.0.1:9223/json/version')
    resp = urllib.request.urlopen(req, timeout=5)
    info = json.loads(resp.read())
    ws_url = info['webSocketDebuggerUrl']

    async with websockets.connect(ws_url, max_size=50*1024*1024) as ws:
        # Create new tab
        await ws.send(json.dumps({'id': 1, 'method': 'Target.createTarget', 'params': {'url': 'about:blank'}}))
        resp = json.loads(await ws.recv())
        target_id = resp['result']['targetId']

        # Find the new tab's WebSocket
        req2 = urllib.request.Request('http://127.0.0.1:9223/json')
        tabs = json.loads(urllib.request.urlopen(req2, timeout=5).read())
        page_ws = None
        for tab in tabs:
            if tab.get('id') == target_id:
                page_ws = tab['webSocketDebuggerUrl']
                break

        if not page_ws:
            raise RuntimeError('Could not find new tab')

        try:
            async with websockets.connect(page_ws, max_size=50*1024*1024) as pws:
                # Navigate
                await pws.send(json.dumps({'id': 1, 'method': 'Page.navigate', 'params': {'url': url}}))
                await pws.recv()

                # Wait for content to load + scroll to trigger lazy loading
                await asyncio.sleep(3)

                # Scroll down to trigger lazy loading
                for _ in range(3):
                    await pws.send(json.dumps({'id': 10, 'method': 'Runtime.evaluate', 'params': {'expression': 'window.scrollBy(0, 1500)'}}))
                    try:
                        await asyncio.wait_for(pws.recv(), timeout=3)
                    except Exception:
                        pass
                    await asyncio.sleep(1)

                # Quick check: if listings not found, wait a bit more
                try:
                    await pws.send(json.dumps({'id': 12, 'method': 'Runtime.evaluate', 'params': {'expression': '''document.querySelectorAll('[data-auto-bulletin-id]').length'''}}))
                    resp = json.loads(await asyncio.wait_for(pws.recv(), timeout=3))
                    count = int(resp['result']['result']['value'])
                    if count == 0:
                        await asyncio.sleep(3)
                except Exception:
                    pass

                # Get HTML
                await pws.send(json.dumps({'id': 2, 'method': 'Runtime.evaluate', 'params': {'expression': 'document.documentElement.outerHTML'}}))
                resp = json.loads(await pws.recv())
                return resp['result']['result']['value']
        finally:
            # Close tab
            try:
                await ws.send(json.dumps({'id': 99, 'method': 'Target.closeTarget', 'params': {'targetId': target_id}}))
                await ws.recv()
            except Exception:
                pass


def fetch_urllib(url):
    """Fallback: fetch via urllib (may be blocked by Madlan)."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
        'Accept-Language': 'he-IL,he;q=0.9,en;q=0.8',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    })
    return urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'ignore')


def cdp_available():
    """Check if CDP browser is accessible."""
    try:
        req = urllib.request.Request('http://127.0.0.1:9223/json/version')
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


def parse_entry_date_from_html(html_content):
    """Extract entry date from a Madlan listing detail page.

    Looks for patterns like:
      - 'תאריך כניסה: 06/2026'
      - 'תאריך כניסה: 06.2026'
      - 'תאריך כניסה: מיידית'
      - 'כניסה: 1/7/2026'
    Returns raw string or None.
    """
    text = block_text(html_content)
    # Try the structured field first: "תאריך כניסה: ..."
    # Use tighter boundary: stop at newline, comma, or common Hebrew words that follow
    m = re.search(r'תאריך כניסה[:\s]+([^\n,]{2,25}?)(?=\s*(?:קומות|ארנונה|בבניין|מ"ר|שטח|חדר|$))', text)
    if not m:
        # Fallback: stop at newline or comma
        m = re.search(r'תאריך כניסה[:\s]+([^\n,]{2,20})', text)
    if m:
        val = m.group(1).strip()
        # Clean up trailing noise
        val = re.sub(r'\s+קומות.*$', '', val)
        val = re.sub(r'\s+ארנונה.*$', '', val)
        val = re.sub(r'\s+בבניין.*$', '', val)
        if val:
            return val
    # Broader: "כניסה: ..." — only if it looks like a date
    m = re.search(r'כניסה[:\s]+(\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?|מיידית|גמיש)', text)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Madlan block detection & state
# ---------------------------------------------------------------------------

def detect_madlan_block(html_content=None, error=None, status_code=None):
    """Detect whether Madlan has blocked us (CAPTCHA / bot page / 403 / 429).

    Returns (blocked: bool, reason: str | None).
    """
    if status_code in (403, 429):
        return True, f'http_{status_code}'

    if error is not None:
        msg = str(error).lower()
        if 'http error 403' in msg:
            return True, 'http_403'
        if 'http error 429' in msg:
            return True, 'http_429'

    if not html_content:
        return False, None

    text = str(html_content)
    lower = text.lower()

    # Check for specific block markers first (works for any HTML length).
    # Do NOT treat generic words like "robot" as a hard block in large pages:
    # real Madlan pages can contain those strings in scripts/app chrome.
    for marker in MADLAN_BLOCK_MARKERS:
        if marker.lower() in lower:
            return True, f'blocked_marker:{marker}'

    # Madlan listing detail pages shorter than ~50K are almost always block pages.
    if len(text) < MADLAN_BLOCKED_MIN_HTML_LEN:
        for marker in MADLAN_GENERIC_BLOCK_MARKERS:
            if marker.lower() in lower:
                return True, f'blocked_marker:{marker}'
        return True, f'short_html:{len(text)}'

    return False, None


def load_madlan_block_state():
    """Load block state from disk. Returns dict or {}."""
    try:
        if MADLAN_BLOCK_STATE_PATH.exists():
            return json.loads(MADLAN_BLOCK_STATE_PATH.read_text())
    except Exception:
        pass
    return {}


def save_madlan_block_state(reason, blocked_for_seconds=MADLAN_BLOCK_COOLDOWN_SECONDS):
    """Persist block state so future runs know to skip enrichment."""
    now = time.time()
    state = {
        'blocked_at': now,
        'blocked_until': now + blocked_for_seconds,
        'reason': reason,
    }
    try:
        MADLAN_BLOCK_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2)
        )
    except Exception as e:
        print(f'  warn: failed writing madlan block state: {e}')
    return state


def is_madlan_block_cooldown_active():
    """Check if a previous block is still in cooldown.

    Returns (active: bool, state: dict).
    """
    state = load_madlan_block_state()
    until = state.get('blocked_until')
    if not until:
        return False, state
    if time.time() < float(until):
        return True, state
    return False, state


def clear_madlan_block_state(all_runs=False):
    """Clear the block cooldown state after the user manually solved a CAPTCHA.

    By default clears the current run/date artifact path. With all_runs=True,
    clears Madlan cooldown files from all broad_search_* artifact dirs, which is
    useful when the blocked run used a run-specific YOGEV_RUN_ID.
    """
    paths = [MADLAN_BLOCK_STATE_PATH]
    if all_runs:
        paths = sorted((ROOT / 'artifacts').glob('broad_search_*/madlan_block_state.json'))
        if MADLAN_BLOCK_STATE_PATH not in paths:
            paths.append(MADLAN_BLOCK_STATE_PATH)

    cleared = 0
    for path in paths:
        try:
            if path.exists():
                path.unlink()
                cleared += 1
        except Exception as e:
            print(f'  warn: failed to clear madlan block state at {path}: {e}')
    if cleared:
        print(f'  Madlan block state cleared ({cleared} file(s)) — enrichment will resume normally')
        return True
    print('  Madlan block state already clear')
    return False


def madlan_enrichment_summary(candidates):
    """Summarize detail-page enrichment status for downstream reports."""
    cooldown_active, block_state = is_madlan_block_cooldown_active()
    statuses = Counter(
        x.get('madlan_enrich_status')
        for x in candidates
        if x.get('madlan_enrich_status')
    )
    human_action_required = False
    if cooldown_active:
        status = 'blocked_cooldown'
        human_action_required = True
    elif statuses.get('blocked'):
        status = 'blocked'
        human_action_required = True
    elif statuses.get('skipped_block_in_run'):
        status = 'blocked'
        human_action_required = True
    elif statuses.get('skipped_block_cooldown'):
        status = 'blocked_cooldown'
        human_action_required = True
    elif not candidates:
        status = 'no_candidates'
    elif not statuses:
        status = 'not_attempted'
    elif statuses.get('ok') == len(candidates):
        status = 'ok'
    else:
        status = 'partial'
    return {
        'status': status,
        'status_counts': dict(statuses),
        'candidate_count': len(candidates),
        'enriched_ok_count': statuses.get('ok', 0),
        'attempted_count': sum(statuses.get(k, 0) for k in ('ok', 'no_entry_date', 'empty_html', 'error', 'blocked')),
        'cooldown_active': cooldown_active,
        'block_state': block_state,
        'human_action_required': human_action_required,
    }


def mark_enrichment_skipped(items, reason='blocked_cooldown'):
    """Mark items as skipped so reports can distinguish true misses from blocked enrichment."""
    for item in items:
        if not item.get('madlan_enrich_status'):
            item['madlan_enrich_status'] = reason
    return items


async def _read_html_from_page_ws(page_ws_url):
    """Read current outerHTML from an already-open page target."""
    import websockets
    async with websockets.connect(page_ws_url, max_size=50*1024*1024) as ws:
        await ws.send(json.dumps({
            'id': 1,
            'method': 'Runtime.evaluate',
            'params': {
                'expression': 'document.documentElement ? document.documentElement.outerHTML : ""',
                'returnByValue': True,
            },
        }))
        deadline = time.time() + 15
        while time.time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
            data = json.loads(raw)
            if data.get('id') == 1:
                return data.get('result', {}).get('result', {}).get('value', '') or ''
    return ''


async def fetch_existing_madlan_tab_html(url):
    """Prefer a human-opened Madlan tab for this listing when available.

    If Dror opened/verified a listing manually, that tab often has a valid
    session while a newly-created automated tab may receive a CAPTCHA.
    """
    try:
        tabs = json.loads(urllib.request.urlopen('http://127.0.0.1:9223/json', timeout=5).read())
    except Exception:
        return ''
    wanted = _listing_id_from_url(url)
    for tab in tabs:
        if tab.get('type') != 'page':
            continue
        tab_url = tab.get('url') or ''
        if not wanted or _listing_id_from_url(tab_url) != wanted:
            continue
        ws_url = tab.get('webSocketDebuggerUrl')
        if not ws_url:
            continue
        html_content = await _read_html_from_page_ws(ws_url)
        blocked, _ = detect_madlan_block(html_content=html_content)
        if html_content and not blocked and len(html_content) > MADLAN_BLOCKED_MIN_HTML_LEN:
            print(f'    using existing verified Madlan tab for {wanted}')
            return html_content
    return ''


def _listing_id_from_url(url):
    m = re.search(r'/listings/([^/?#]+)', str(url or ''))
    return m.group(1) if m else ''


def close_other_madlan_tabs(keep_url=None):
    """Close stale Madlan tabs while preserving the relevant listing tab.

    Manual observation (2026-05-09): Madlan CAPTCHA was solvable only after
    closing the other Madlan tabs. Before opening a fresh detail tab, keep the
    current listing tab (if any) and close old list/detail tabs from prior runs.
    """
    keep_listing_id = _listing_id_from_url(keep_url)
    closed = []
    try:
        tabs = json.loads(urllib.request.urlopen('http://127.0.0.1:9223/json', timeout=5).read())
    except Exception:
        return closed

    for tab in tabs:
        if tab.get('type') != 'page':
            continue
        tab_url = tab.get('url') or ''
        if 'madlan.co.il' not in tab_url:
            continue
        if keep_listing_id and _listing_id_from_url(tab_url) == keep_listing_id:
            continue
        try:
            tab_id = tab.get('id')
            if not tab_id:
                continue
            urllib.request.urlopen(f'http://127.0.0.1:9223/json/close/{tab_id}', timeout=3).read()
            closed.append(tab_url)
        except Exception:
            continue

    if closed:
        print(f'    closed {len(closed)} stale Madlan tab(s) before enrichment')
    return closed


# Track if we already hit a block in this run to avoid creating more tabs
_madlan_blocked_in_this_run = False

async def fetch_detail_via_cdp(url):
    """Fetch a single listing page HTML via CDP browser. Returns HTML string.

    First reuses an already-open verified listing tab if one exists.
    If no existing tab and we already hit a block in this run, skip creating
    new tabs (they'll just get CAPTCHA again).
    Otherwise creates a fresh tab per URL and reads CDP responses by exact message id.
    If the fresh tab gets a CAPTCHA, leaves it OPEN so the user can solve it manually.
    """
    global _madlan_blocked_in_this_run

    # 1. Try existing human-verified tab first
    existing_html = await fetch_existing_madlan_tab_html(url)
    if existing_html:
        return existing_html

    # 1b. Stale Madlan tabs appear to make CAPTCHA harder/more frequent.
    # Keep only the tab for this listing (if it exists) before opening a fresh one.
    close_other_madlan_tabs(keep_url=url)

    # 2. If we already hit a block this run, don't create new tabs — they'll just get CAPTCHA
    if _madlan_blocked_in_this_run:
        print(f'    skipping new tab for {_listing_id_from_url(url)} — block already detected this run')
        return None

    import websockets

    async def recv_id(ws, msg_id, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            data = json.loads(raw)
            if data.get('id') == msg_id:
                return data
        raise TimeoutError(f'CDP response id={msg_id} timed out')

    async def send_cmd(ws, msg_id, method, params=None, sid=None, timeout=20):
        msg = {'id': msg_id, 'method': method, 'params': params or {}}
        if sid:
            msg['sessionId'] = sid
        await ws.send(json.dumps(msg))
        return await recv_id(ws, msg_id, timeout=timeout)

    req = urllib.request.Request('http://127.0.0.1:9223/json/version')
    resp = urllib.request.urlopen(req, timeout=5)
    info = json.loads(resp.read())
    ws_url = info['webSocketDebuggerUrl']

    async with websockets.connect(ws_url, max_size=50*1024*1024) as ws:
        target_info = await send_cmd(ws, 1, 'Target.createTarget', {'url': 'about:blank'})
        target_id = target_info.get('result', {}).get('targetId')
        if not target_id:
            return None

        tab_should_stay_open = False  # Set to True if CAPTCHA detected so user can solve

        try:
            attach = await send_cmd(ws, 2, 'Target.attachToTarget', {'targetId': target_id, 'flatten': True})
            sid = attach.get('result', {}).get('sessionId', '')
            if not sid:
                return None

            await send_cmd(ws, 3, 'Page.enable', sid=sid)
            await send_cmd(ws, 4, 'Runtime.enable', sid=sid)
            await send_cmd(ws, 5, 'Page.navigate', {'url': url}, sid=sid)

            # Wait like a careful human: first enough wall time, then DOM readiness.
            await asyncio.sleep(ENRICH_DETAIL_WAIT_S + random.uniform(1.0, 3.5))
            for _ in range(10):
                ready = await send_cmd(
                    ws, 6, 'Runtime.evaluate',
                    {'expression': 'document.readyState', 'returnByValue': True},
                    sid=sid, timeout=10,
                )
                val = ready.get('result', {}).get('result', {}).get('value')
                if val in {'interactive', 'complete'}:
                    break
                await asyncio.sleep(1)

            # Gentle human-like scroll before reading HTML.
            await send_cmd(
                ws, 7, 'Runtime.evaluate',
                {'expression': 'window.scrollTo(0, Math.floor(document.body.scrollHeight * 0.35))'},
                sid=sid, timeout=10,
            )
            await asyncio.sleep(random.uniform(0.8, 1.8))
            await send_cmd(
                ws, 8, 'Runtime.evaluate',
                {'expression': 'window.scrollTo(0, Math.floor(document.body.scrollHeight * 0.75))'},
                sid=sid, timeout=10,
            )
            await asyncio.sleep(random.uniform(0.8, 1.8))

            # Retry outerHTML because SPA/detail pages can briefly return empty.
            html_result = ''
            for attempt in range(5):
                html_resp = await send_cmd(
                    ws, 9 + attempt, 'Runtime.evaluate',
                    {
                        'expression': 'document.documentElement ? document.documentElement.outerHTML : ""',
                        'returnByValue': True,
                    },
                    sid=sid, timeout=15,
                )
                result = html_resp.get('result', {}).get('result', {})
                html_result = result.get('value', '') or ''
                if len(html_result) > 1000:
                    break
                await asyncio.sleep(1.5 + random.uniform(0, 1.0))

            # Check if this new tab got a CAPTCHA / block page
            blocked, reason = detect_madlan_block(html_content=html_result)
            if blocked:
                _madlan_blocked_in_this_run = True
                tab_should_stay_open = True
                print(f'    ⚠️ New tab got CAPTCHA for {_listing_id_from_url(url)} — leaving tab OPEN for manual solve')
                print(f'       Tab URL: {url}')
                print(f'       Please solve the CAPTCHA in the browser, then the next run will reuse this tab.')

            return html_result
        finally:
            # Only close the tab if it was NOT blocked (CAPTCHA)
            if not tab_should_stay_open:
                try:
                    await send_cmd(ws, 99, 'Target.closeTarget', {'targetId': target_id}, timeout=5)
                except Exception:
                    pass


async def fetch_detail_via_cdp_reuse(ws, sid, url):
    """DEPRECATED: Tab reuse triggers bot detection on Madlan.

    Kept for reference but not used in production.
    """
    raise NotImplementedError("Tab reuse triggers bot detection — use fetch_detail_via_cdp instead")


def fetch_detail_urllib(url):
    """Fetch a single listing page via urllib (may be blocked)."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
        'Accept-Language': 'he-IL,he;q=0.9,en;q=0.8',
    })
    resp = urllib.request.urlopen(req, timeout=20)
    return resp.read().decode('utf-8', errors='replace')


async def _enrich_one(item, use_cdp, idx, total):
    """Enrich a single item with entry date. Returns (item, success).

    Sets madlan_enrich_status: "ok" | "blocked" | "no_entry_date" | "empty_html" | "missing_url" | "error"
    Sets madlan_blocked_reason when blocked.
    """
    url = item.get('url')
    if not url:
        item['madlan_enrich_status'] = 'missing_url'
        return item, False

    try:
        if use_cdp:
            html_content = await fetch_detail_via_cdp(url)
        else:
            html_content = fetch_detail_urllib(url)

        # If fetch_detail_via_cdp returned None because block already detected this run
        if html_content is None:
            item['madlan_enrich_status'] = 'skipped_block_in_run'
            item['madlan_blocked_reason'] = 'block already detected earlier in this run — tab creation skipped'
            print(f'    [{idx}/{total}] {url.split("/")[-1]} → SKIPPED (block already detected this run)')
            return item, False

        # Check for block signals in the response
        blocked, reason = detect_madlan_block(html_content=html_content)
        if blocked:
            item['madlan_enrich_status'] = 'blocked'
            item['madlan_blocked_reason'] = reason
            print(f'    [{idx}/{total}] {url.split("/")[-1]} → BLOCKED: {reason}')
            print('    ⚠️ Madlan blocked — please solve the CAPTCHA in the browser, then rerun or resume later.', flush=True)
            return item, False

        if html_content:
            entry_raw = parse_entry_date_from_html(html_content)
            if entry_raw:
                item['entry_date_raw'] = entry_raw
                item['madlan_enrich_status'] = 'ok'
                print(f'    [{idx}/{total}] {url.split("/")[-1]} → {entry_raw}')
                return item, True

            item['madlan_enrich_status'] = 'no_entry_date'
            print(f'    [{idx}/{total}] {url.split("/")[-1]} → no entry date found')
            return item, False

        item['madlan_enrich_status'] = 'empty_html'
        return item, False

    except Exception as e:
        # Check if the exception is a block signal
        blocked, reason = detect_madlan_block(error=e)
        if blocked:
            item['madlan_enrich_status'] = 'blocked'
            item['madlan_blocked_reason'] = reason
            print(f'    [{idx}/{total}] {url.split("/")[-1]} → BLOCKED error: {reason}')
            return item, False

        item['madlan_enrich_status'] = 'error'
        item['madlan_enrich_error'] = str(e)
        print(f'    [{idx}/{total}] {url.split("/")[-1]} → error: {e}')
        return item, False


async def _enrich_one_from_existing_tab_only(item, idx, total):
    """Use only an already-open verified Madlan tab; never creates a new tab."""
    url = item.get('url')
    if not url:
        item['madlan_enrich_status'] = 'missing_url'
        return item, False, False

    html_content = await fetch_existing_madlan_tab_html(url)
    if not html_content:
        return item, False, False

    # A valid manual tab means the user likely solved the challenge; remove
    # stale cooldown files so future runs are not blocked by old state.
    clear_madlan_block_state(all_runs=True)

    entry_raw = parse_entry_date_from_html(html_content)
    if entry_raw:
        item['entry_date_raw'] = entry_raw
        item['madlan_enrich_status'] = 'ok'
        print(f'    [{idx}/{total}] {url.split("/")[-1]} → {entry_raw} (manual tab)')
        return item, True, True

    item['madlan_enrich_status'] = 'no_entry_date'
    print(f'    [{idx}/{total}] {url.split("/")[-1]} → no entry date found (manual tab)')
    return item, False, True


async def enrich_entry_dates(items, use_cdp, max_items=ENRICH_MAX_ITEMS,
                              max_seconds=ENRICH_MAX_SECONDS,
                              concurrency=ENRICH_CONCURRENCY,
                              rate_limit_s=ENRICH_RATE_LIMIT_S):
    """Fetch entry dates from Madlan listing detail pages for top candidates.

    Sequential (not parallel) to avoid Madlan bot detection.
    Stops immediately if a block is detected.
    Uses cooldown state to avoid opening new tabs after a recent block, but a
    manually verified existing tab wins over cooldown.
    Time-capped so timeout doesn't kill the whole scan.

    Only enriches items that:
    - Don't already have an entry_date field
    - Have a price (i.e., are real candidates)
    - Score >= 15 (focus on the best ones)
    """
    # Sort by score descending, take top candidates.
    eligible = [x for x in items if x.get('price') and not x.get('entry_date') and x.get('score', 0) >= 15]
    eligible.sort(key=lambda x: x.get('score', 0), reverse=True)
    todo = eligible[:max_items]
    for skipped in eligible[max_items:]:
        skipped.setdefault('madlan_enrich_status', 'not_attempted_limit')

    if not todo:
        print(f'  enrich: no candidates need entry date enrichment')
        return items

    cooldown_active, block_state = is_madlan_block_cooldown_active()
    if cooldown_active:
        reason = block_state.get('reason', 'unknown')
        print(f'  enrich: cooldown active ({reason}) — checking for manually verified Madlan tab first')
        if use_cdp:
            found_manual_tab = False
            enriched = 0
            for i, item in enumerate(todo):
                item, ok, used_manual_tab = await _enrich_one_from_existing_tab_only(item, i + 1, len(todo))
                found_manual_tab = found_manual_tab or used_manual_tab
                if ok:
                    enriched += 1
            if found_manual_tab:
                print(f'  enrich: used manual tab during cooldown; got {enriched}/{len(todo)} entry dates')
                return items

        mark_enrichment_skipped(todo, 'skipped_block_cooldown')
        print('  enrich: skipping new Madlan tabs — cooldown active and no verified manual tab found')
        return items

    print(f'  enrich: fetching entry dates for {len(todo)} listings (max {max_seconds}s, rate limit {rate_limit_s}s)...')

    enriched = 0
    stopped_early = False
    start = time.time()

    for i, item in enumerate(todo):
        elapsed = time.time() - start
        if elapsed > max_seconds:
            remaining_items = todo[i:]
            remaining = len(remaining_items)
            mark_enrichment_skipped(remaining_items, 'skipped_time_budget')
            print(f'    SKIPPED {remaining} items — enrichment time budget exhausted ({elapsed:.0f}s > {max_seconds}s)')
            break

        item, ok = await _enrich_one(item, use_cdp, i + 1, len(todo))

        if ok:
            enriched += 1

        # If this item was blocked, save state and stop immediately
        if item.get('madlan_enrich_status') == 'blocked':
            reason = item.get('madlan_blocked_reason', 'unknown')
            save_madlan_block_state(reason)
            mark_enrichment_skipped(todo[i + 1:], 'skipped_block_in_run')
            print(f'  enrich: stopping Madlan enrichment due to block: {reason}')
            stopped_early = True
            break

        # Rate limit between items (but not after the last one)
        if i < len(todo) - 1 and rate_limit_s > 0:
            await asyncio.sleep(rate_limit_s)

    elapsed = time.time() - start
    if stopped_early:
        print(f'  enrich: got {enriched}/{i + 1} entry dates in {elapsed:.1f}s (stopped early — block detected)')
    else:
        print(f'  enrich: got {enriched}/{len(todo)} entry dates in {elapsed:.1f}s')
    return items


def _save_results(pages, all_items, candidates, all_blocked, blocked_pages):
    """Save results to JSON.  Called before AND after enrichment."""
    enrichment = madlan_enrichment_summary(candidates)
    alert = None
    if enrichment.get('human_action_required'):
        alert = (
            'Madlan requires manual verification — solve CAPTCHA in the open browser tab, '
            'then run: python3 scripts/scrape_madlan_public.py clear-block-state'
        )
    out = {
        'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'pages': pages,
        'blocked': all_blocked,
        'blocked_pages': [p['url'] for p in blocked_pages],
        'items': all_items,
        'candidates': candidates,
        'metadata': {
            'source': 'madlan',
            'madlan_block_state': load_madlan_block_state(),
            'madlan_enrichment': enrichment,
            'madlan_alert': alert,
            'madlan_enrichment_policy': {
                'max_items': ENRICH_MAX_ITEMS,
                'concurrency': ENRICH_CONCURRENCY,
                'rate_limit_s': ENRICH_RATE_LIMIT_S,
                'max_seconds': ENRICH_MAX_SECONDS,
            }
        }
    }
    (ART / 'madlan_public_scan.json').write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out


async def async_main():
    use_cdp = cdp_available()
    print(f'CDP browser: {"available" if use_cdp else "not available, using urllib fallback"}')

    all_items = []
    pages = []

    for source_id, label, url in URLS:
        print('fetch', label)
        try:
            if use_cdp:
                s = await fetch_via_cdp(url)
            else:
                s = fetch_urllib(url)
        except Exception as e:
            pages.append({'source_id': source_id, 'label': label, 'url': url, 'error': str(e)})
            continue

        s_lower = s.lower()
        is_blocked = any(sig in s_lower for sig in _MADLAN_BLOCK_SIGNALS)
        pages.append({'source_id': source_id, 'label': label, 'url': url, 'html_len': len(s), 'blocked': is_blocked})
        if is_blocked:
            print(f'  BLOCKED: {label} — block signal detected in HTML', flush=True)
            time.sleep(0.5)
            continue
        items = parse_items_from_html(s, source_id, label)
        all_items.extend(items)
        time.sleep(0.5)

    # Deduplicate by listing id
    uniq = []
    seen = set()
    for x in all_items:
        if x['id'] not in seen:
            seen.add(x['id'])
            uniq.append(x)

    # Filter candidates
    candidates = []
    for x in uniq:
        if x.get('rooms') is not None and x['rooms'] < 2.5: continue
        if x.get('sqm') is not None and x['sqm'] < 55: continue
        price = x.get('price')
        if price and price > MAX_BUDGET: continue
        if any(b in x['text'] for b in BAD_AREAS): continue
        if not any(p in x['text'] or p in x['source_label'] for p in PREFERRED): continue
        if x['score'] >= 10:
            candidates.append(x)

    candidates.sort(key=lambda x: x['score'], reverse=True)

    # Determine if all pages were blocked BEFORE using all_blocked
    blocked_pages = [p for p in pages if p.get('blocked')]
    all_blocked = len(URLS) > 0 and len(blocked_pages) == len(URLS)
    if all_blocked:
        print('MADLAN_BLOCKED: all pages returned block signals', flush=True)

    # === PARTIAL SAVE: write results before enrichment ===
    _save_results(pages, uniq, candidates, all_blocked, blocked_pages)
    print(f'  partial save: {len(uniq)} items, {len(candidates)} candidates')

    # Enrich top candidates with entry dates from detail pages
    if not all_blocked:
        candidates = await enrich_entry_dates(candidates, use_cdp)
        # Save again with enriched data
        _save_results(pages, uniq, candidates, all_blocked, blocked_pages)
    else:
        mark_enrichment_skipped(candidates, 'skipped_block_cooldown')
        _save_results(pages, uniq, candidates, all_blocked, blocked_pages)

    print('items', len(uniq), 'candidates', len(candidates))
    for i, x in enumerate(candidates[:35], 1):
        print(i, x['score'], x['price'], x['rooms'], x['sqm'], x['source_label'], x['address'][:120], x['url'])


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in {'clear-block-state', 'clear-cooldown', 'reset-block-state'}:
        clear_madlan_block_state(all_runs=True)
        sys.exit(0)
    asyncio.run(async_main())
