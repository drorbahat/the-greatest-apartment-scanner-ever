#!/usr/bin/env python3
import asyncio, base64, json, os, pathlib, re, sys, time, urllib.request
from datetime import datetime
import websockets

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_ID = os.environ.get('SCANNER_RUN_ID')
RUN_DATE = os.environ.get('SCANNER_SCAN_DATE') or datetime.now().strftime('%Y-%m-%d')
# Use run-specific directory if available, otherwise fall back to date-based directory
if RUN_ID:
    ART = ROOT / "artifacts" / f"broad_search_{RUN_ID}"
else:
    ART = ROOT / "artifacts" / f"broad_search_{RUN_DATE}"
ART.mkdir(parents=True, exist_ok=True)

TARGETS = [
    {
        "id": "rg_giv_25plus",
        "label": "רמת גן / גבעתיים — 2.5+ חדרים",
        "url": "https://www.yad2.co.il/realestate/rent/tel-aviv-area?area=3&property=1&minRooms=2.5",
        "max_details": 45,
        "scrolls": 16,
    },
    {
        "id": "yad_eliyahu_25plus",
        "label": "יד אליהו — 2.5+ חדרים",
        "url": "https://www.yad2.co.il/realestate/rent?city=5000&neighborhood=206&property=1&rooms=2.5--1",
        "max_details": 35,
        "scrolls": 12,
    },
    {
        "id": "bitzaron_ramat_israel_25plus",
        "label": "ביצרון / רמת ישראל — 2.5+ חדרים",
        "url": "https://www.yad2.co.il/realestate/rent?topArea=2&area=1&city=5000&neighborhood=486&property=1&rooms=2.5--1",
        "max_details": 25,
        "scrolls": 10,
    },
    {
        "id": "tel_aviv_rare_25plus",
        "label": "תל אביב — סריקה למציאות נדירות באזורים מועדפים",
        "url": "https://www.yad2.co.il/realestate/rent/tel-aviv-area?area=1&property=1&minRooms=2.5",
        "max_details": 25,
        "scrolls": 18,
        "rare_filter": True,
    },
]

RARE_TERMS = ["יד אליהו", "ביצרון", "רמת ישראל", "הצפון הישן", "בזל", "לב העיר", "לב תל אביב", "החשמונאים", "בוגרשוב", "דיזנגוף", "בן יהודה"]
BAD_AREAS = ["יפו", "פלורנטין", "שפירא", "נווה שאנן", "התקווה", "רמת אביב", "צהלה", "דרום תל אביב"]
MIN_BUDGET = 5500
MAX_BUDGET = 6500

BLOCK_TERMS = ["Are you for real", "ShieldSquare", "Captcha", "captcha", "אנו מניחים שגולשים", "validate.perfdrive"]


def get_json(url):
    return json.load(urllib.request.urlopen(url, timeout=8))

async def call(ws, counter, method, params=None):
    counter[0] += 1
    await ws.send(json.dumps({"id": counter[0], "method": method, "params": params or {}}))
    while True:
        data = json.loads(await ws.recv())
        if data.get("id") == counter[0]:
            return data

async def connect_browser():
    ver = get_json("http://127.0.0.1:9223/json/version")
    return await websockets.connect(ver["webSocketDebuggerUrl"])

async def new_page(browser_ws, url):
    c = [0]
    res = await call(browser_ws, c, "Target.createTarget", {"url": url, "newWindow": False, "background": False})
    tid = res["result"]["targetId"]
    target = None
    for _ in range(50):
        for t in get_json("http://127.0.0.1:9223/json"):
            if t.get("id") == tid:
                target = t
                break
        if target:
            break
        await asyncio.sleep(0.2)
    if not target:
        raise RuntimeError("target not found")
    ws = await websockets.connect(target["webSocketDebuggerUrl"])
    cc = [0]
    await call(ws, cc, "Page.enable")
    await call(ws, cc, "Runtime.enable")
    return tid, ws, cc

async def close_page(browser_ws, tid):
    c = [0]
    try:
        await call(browser_ws, c, "Target.closeTarget", {"targetId": tid})
    except Exception:
        pass

async def wait_text(ws, c, min_len=500, timeout_s=25):
    start = time.time()
    last = {}
    while time.time() - start < timeout_s:
        res = await call(ws, c, "Runtime.evaluate", {
            "expression": "(() => ({ready:document.readyState,url:location.href,title:document.title,text:document.body?document.body.innerText:''}))()",
            "returnByValue": True,
            "awaitPromise": True,
        })
        last = res.get("result", {}).get("result", {}).get("value") or {}
        txt = last.get("text") or ""
        if any(term in txt or term in (last.get("title") or "") or term in (last.get("url") or "") for term in BLOCK_TERMS):
            return last, True
        if len(txt) >= min_len:
            return last, False
        await asyncio.sleep(0.5)
    return last, False

async def extract_feed(ws, c, scrolls):
    # Initial wait
    state, blocked = await wait_text(ws, c)
    if blocked:
        return {"blocked": True, "state": state, "links": []}
    for _ in range(scrolls):
        await call(ws, c, "Runtime.evaluate", {"expression": "window.scrollBy(0, Math.floor(window.innerHeight*0.85))"})
        await asyncio.sleep(0.9)
        state, blocked = await wait_text(ws, c, min_len=300, timeout_s=3)
        if blocked:
            return {"blocked": True, "state": state, "links": []}
    expr = r'''
(() => {
 const out=[];
 for (const a of document.querySelectorAll('a[href]')){
   const txt=(a.innerText||'').trim().replace(/\s+/g,' ');
   const href=a.href;
   if (href.includes('/realestate/item/') && /₪|חדרים|מ״ר|קומה|דירה|גבעתיים|רמת גן|תל אביב/.test(txt)) {
     out.push({href, text:txt.slice(0,1200)});
   }
 }
 const uniq=[]; const seen=new Set();
 for (const x of out){
   const key=x.href.split('?')[0];
   if(!seen.has(key)) { seen.add(key); uniq.push(x); }
 }
 return {url:location.href,title:document.title,text:document.body.innerText.slice(0,4000),links:uniq};
})()
'''
    res = await call(ws, c, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    val = res.get("result", {}).get("result", {}).get("value") or {}
    if any(term in json.dumps(val, ensure_ascii=False) for term in BLOCK_TERMS):
        return {"blocked": True, "state": val, "links": []}
    return {"blocked": False, "state": val, "links": val.get("links", [])}

def clean_price(text):
    # Yad2 sometimes shows base rent (e.g., 6,400) and total payment (e.g., 6,820).
    # Prefer "סה״כ תשלום" / "סה"כ תשלום" / "מחיר כולל" if present.
    # Otherwise take the first reasonable price, but cap at MAX_BUDGET+500 to catch
    # total-payment prices that exceed the budget.
    text = text or ""
    # Look for total-payment context first
    total_match = re.search(r'סה[״"\']?כ\s+תשלום.*?([0-9]{1,2}(?:,[0-9]{3})|[0-9]{4,5})', text)
    if total_match:
        try:
            v = int(total_match.group(1).replace(',', ''))
            if 2500 <= v <= 25000:
                return v
        except Exception:
            pass
    # Fallback: first reasonable price near ₪ symbol
    nums = re.findall(r'(?:₪\s*)?([0-9]{1,2}(?:,[0-9]{3})|[0-9]{4,5})(?:\s*₪)?', text)
    vals = []
    for n in nums:
        try:
            v = int(n.replace(',', ''))
            if 2500 <= v <= 25000:
                vals.append(v)
        except Exception:
            pass
    return vals[0] if vals else None

def parse_rooms(text):
    m = re.search(r'([0-9](?:\.[0-9])?)\s*חדרים', text)
    return float(m.group(1)) if m else None

def parse_sqm(text):
    # Yad2 detail pages can compact floor+building floors+sqm as "קומה1/375מ״ר".
    # In that case the actual sqm is the trailing two digits (75), not 375.
    m = re.search(r'מ[״"\']?ר\s*בנוי\s*([0-9]{2,3})\s*מ[״"\']?ר', text)
    if m:
        return int(m.group(1))
    m = re.search(r'קומה\s*\d+\s*/\s*\d+([0-9]{2})\s*מ[״"\']?ר', text)
    if m:
        return int(m.group(1))
    candidates = []
    for m in re.finditer(r'(?<![/\d])([0-9]{2,3})\s*מ[״"\']?ר', text):
        v = int(m.group(1))
        if 25 <= v <= 250:
            candidates.append(v)
    return candidates[0] if candidates else None

def parse_floor(text):
    m = re.search(r'קומה\s*([^\s•]+)', text)
    return m.group(1) if m else None

def feed_score(link, target):
    txt = link.get('text','')
    price = clean_price(txt)
    rooms = parse_rooms(txt)
    sqm = parse_sqm(txt)
    score = 0
    notes=[]
    if rooms and rooms >= 2.5: score += 8
    if rooms and 2.5 <= rooms <= 4: score += 5
    if sqm and sqm >= 70: score += 7
    elif sqm and sqm >= 60: score += 3
    if price:
        # Budget is an upper ceiling, not a hard lower range. Cheap full apartments
        # should surface, with downstream review checking if they are suspicious.
        if price <= MAX_BUDGET: score += 12
        else: score -= 15
    # Area preference
    if any(x in txt for x in ["גבעתיים", "יד אליהו", "ביצרון", "רמת ישראל"]): score += 8
    if "רמת גן" in txt: score += 5
    if any(x in txt for x in ["הצפון הישן", "בזל", "לב העיר", "לב תל אביב"]): score += 3
    if any(x in txt for x in BAD_AREAS): score -= 20
    # Text cues
    for w in ["שקט", "עורפי", "עורפית", "מוארת", "נוף פתוח", "משופץ", "בניין משופץ", "מטבח גדול", "חניה", "ממ\"ד", "ממד"]:
        if w in txt: score += 2
    for w in ["חדש מקבלן", "נכס חדש"]:
        if w in txt: score += 1
    # very large/expensive family homes lower priority
    if rooms and rooms >= 5 and price and price >= 9000: score -= 8
    # rare target should only pass if target terms or really attractive
    if target.get('rare_filter'):
        if not any(t in txt for t in RARE_TERMS):
            score -= 15
        if price and price <= 8500:
            score += 3
    link['feed_price'] = price
    link['feed_rooms'] = rooms
    link['feed_sqm'] = sqm
    link['feed_floor'] = parse_floor(txt)
    link['feed_score'] = score
    return score

async def extract_detail(browser_ws, item):
    tid, ws, c = await new_page(browser_ws, item["href"])
    try:
        state, blocked = await wait_text(ws, c, min_len=900, timeout_s=25)
        if blocked:
            return {**item, "blocked": True, "detail_text": state.get('text',''), "detail_url": state.get('url',''), "detail_title": state.get('title','')}
        res = await call(ws, c, "Runtime.evaluate", {"expression": r'''(() => {
 const metas={};
 for (const m of document.querySelectorAll('meta[property],meta[name]')) metas[m.getAttribute('property')||m.getAttribute('name')]=m.content;
 const imgs=[...document.images].map(img=>img.src).filter(Boolean).slice(0,25);
 return {url:location.href,title:document.title,text:document.body.innerText,metas,imgs};
})()''', "returnByValue": True, "awaitPromise": True})
        val = res.get("result", {}).get("result", {}).get("value") or {}
        text = val.get("text", "")
        if any(term in text or term in val.get('title','') or term in val.get('url','') for term in BLOCK_TERMS):
            return {**item, "blocked": True, "detail_text": text[:10000], "detail_url": val.get('url',''), "detail_title": val.get('title','')}
        return {**item, "blocked": False, "detail_text": text[:14000], "detail_url": val.get('url',''), "detail_title": val.get('title',''), "metas": val.get('metas',{}), "imgs": val.get('imgs',[])}
    finally:
        try: await ws.close()
        except Exception: pass
        await close_page(browser_ws, tid)

def pick_candidates(links, target):
    for l in links:
        feed_score(l, target)
    # Keep main feed links first; remove obvious ads/recommendations outside target when query broad.
    filtered=[]
    for l in links:
        txt=l.get('text','')
        href=l.get('href','')
        if 'recommendation' in href and 'main_feed' not in href:
            continue
        if any(b in txt for b in BAD_AREAS):
            continue
        price=l.get('feed_price')
        rooms=l.get('feed_rooms')
        if rooms and rooms < 2.5:
            continue
        # Hard budget is an upper ceiling (~6,500 NIS), not a minimum.
        if price and price > MAX_BUDGET:
            continue
        if target.get('rare_filter') and not any(t in txt for t in RARE_TERMS):
            continue
        if l['feed_score'] >= 10:
            filtered.append(l)
    filtered.sort(key=lambda x: x.get('feed_score',0), reverse=True)
    return filtered[:target['max_details']]

async def main():
    browser_ws = await connect_browser()
    all_results=[]
    summary={"started": datetime.now().isoformat(), "targets": []}
    seen_detail=set()
    try:
        for target in TARGETS:
            print(f"TARGET {target['label']} {target['url']}", flush=True)
            tid, ws, c = await new_page(browser_ws, target['url'])
            try:
                feed = await extract_feed(ws, c, target['scrolls'])
                (ART / f"{target['id']}_feed.json").write_text(json.dumps(feed, ensure_ascii=False, indent=2))
                if feed.get('blocked'):
                    summary['blocked_at'] = target['id']
                    summary['blocked_state'] = feed.get('state')
                    summary['block_type'] = 'feed'
                    print('BLOCKED_IN_FEED', target['id'], flush=True)
                    (ART / "block_state.json").write_text(
                        json.dumps({
                            "blocked": True,
                            "block_type": "feed",
                            "blocked_target": target['id'],
                            "url": target.get('url', ''),
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    break
                links=feed.get('links',[])
                picks=pick_candidates(links,target)
                print(f"  collected={len(links)} picks={len(picks)}", flush=True)
                summary['targets'].append({"id":target['id'], "label":target['label'], "url":feed.get('state',{}).get('url'), "collected":len(links), "picked":len(picks)})
            finally:
                try: await ws.close()
                except Exception: pass
                await close_page(browser_ws, tid)
            for idx,item in enumerate(picks,1):
                key=item['href'].split('?')[0]
                if key in seen_detail:
                    continue
                seen_detail.add(key)
                item={**item, "search_id":target['id'], "search_label":target['label'], "pick_index":idx}
                print(f"  OPEN {target['id']} #{idx} score={item.get('feed_score')} {item.get('text','')[:120]}", flush=True)
                detail=await extract_detail(browser_ws,item)
                all_results.append(detail)
                (ART / "yad2_broad_details.json").write_text(json.dumps(all_results, ensure_ascii=False, indent=2))
                if detail.get('blocked'):
                    summary['blocked_at_detail'] = detail.get('href')
                    summary['partial'] = True
                    summary['completed_items'] = len(all_results)
                    print('BLOCKED_IN_DETAIL', detail.get('href'), flush=True)
                    (ART / "block_state.json").write_text(
                        json.dumps({
                            "blocked": True,
                            "block_type": "detail",
                            "blocked_href": detail.get('href', ''),
                            "completed_items": len(all_results),
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    (ART / "yad2_broad_details.json").write_text(
                        json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    summary['finished'] = datetime.now().isoformat()
                    (ART / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
                    sys.exit(2)
                await asyncio.sleep(0.6)
    finally:
        try: await browser_ws.close()
        except Exception: pass
        summary['finished'] = datetime.now().isoformat()
        summary.setdefault('partial', False)
        summary.setdefault('completed_items', len(all_results))
        (ART / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary.get('blocked_at'):
        sys.exit(2)
    print(f"DONE details={len(all_results)}", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
