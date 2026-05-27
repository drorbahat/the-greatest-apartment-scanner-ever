# 🏠 Apartment Scanner — Agent Operating Manual

## מי אני

אני סורק דירות אוטומטי. תפקידי: לחפש, לסנן ולסכם דירות להשכרה באזור רמת גן / גבעתיים.
אני רץ כ**סוכן AI** — לא כשירות standalone. אני מקבל את הריפו הזה כהקשר, מבין את המשימה, ומפעיל את הסקריפטים.

## המשימה

למצוא דירה טובה להשכרה. המשתמש מגדיר קריטריונים, אני סורק, מדווח, ומסמן דגלים אדומים.

## איך אני עובד

1. **סריקה** — אוסף מודעות מ-Yad2 (דרך Chromium CDP) ומקבוצות פייסבוק
2. **ניקוי** — מנקה פוסטים, מחלץ שדות, מסנן לא רלוונטי
3. **טריאז' AI** — מסווג מודעות פייסבוק (דורש LLM / API key)
4. **הערכה** — ניקוד וסינון לפי קריטריונים (evaluate_candidates.py)
5. **דוח** — הפקת final_report.md בעברית עם מועמדות מובילות

## מחזור סריקה

```bash
# סריקה מלאה
python3 scripts/full_apartment_scan.py run

# בדיקת סטטוס
python3 scripts/full_apartment_scan.py status

# סגירת סריקה אחרי טריאז'
python3 scripts/full_apartment_scan.py finalize
```

## סקריפטים — מה כל אחד עושה

**ליבת הסריקה:**
- `full_apartment_scan.py` — אורקסטרטור ראשי: מריץ איסוף, מרכז, מפיק דוח
- `yad2_broad_search.py` — סורק את Yad2 דרך Chromium CDP (פורט 9223)
- `evaluate_candidates.py` — **הליבה** — ניקוד, סינון, דגלים אדומים, הפקת final_report.md

**פייסבוק:**
- `facebook_group_feed_scan.py` — סורק פיד של קבוצת פייסבוק (גלילה, פריסת פוסטים)
- `facebook_feed_multi_scan.py` — סורק מספר קבוצות במקביל
- `facebook_group_scan.py` — חיפוש בקבוצה לפי מילות מפתח (משלים)
- `facebook_clean_posts.py` — מנקה ומבנה פוסטים גולמיים ← JSON + MD
- `facebook_auto_triage.py` — טריאז' אוטומטי: זיהוי מודעות רלוונטיות
- `facebook_ai_triage_prepare.py` — הכנת מודעות לטריאז' AI
- `facebook_ai_triage_cache_update.py` — עדכון cache טריאז' אחרי סיווג
- `facebook_url_utils.py` — חילוץ, נרמול וסיווג קישורים מפוסטים

**AI / נרמול:**
- `ai_normalize_listing.py` — נרמול מודעה באמצעות LLM (Gemini)
- `llm_extract.py` — חילוץ שדות (מחיר, חדרים, כניסה) מטקסט חופשי
- `normalization_pipeline.py` — צינור הנרמול: הרצה על סט מודעות + הפקת audit
- `normalization_audit.py` — השוואת תוצאות נרמול מול evidence packs

**תשתית:**
- `apartment_db.py` — מסד נתונים SQLite של דירות (מעקב, dedup)
- `user_rejections.py` — ניהול דירות שנפסלו (DB, סינון)
- `events.py` — רישום אירועי סריקה (scan_completed, errors)
- `evidence_pack.py` — אריזת ראיות לדירה (ללא LLM, דטרמיניסטי)
- `scan_quality.py` — מדדי איכות סריקה (כיסוי, שלמות, אמינות)
- `cron_load_results.py` — טעינת תוצאות אחרונות למעקב
- `daily_scan.py` — הרצת סריקה יומית + טריאז' AI

**תפעול:**
- `smoke_test.py` — **בדיקת סביבה** — תלויות, Chromium, עוגיות, API keys
- `inject_cookies.py` — הזרקת עוגיות פייסבוק ל-Chromium
- `scanner-chromium` — סקריפט launch ל-Chromium עם CDP (פורט 9223)
- `scrape_madlan_public.py` — סורק Madlan (מושבת — PerimeterX)

## צינור (Pipeline) — מה קורה בלחיצת run

```
make scan  ← או: python3 scripts/full_apartment_scan.py run
```

**זמנים צפויים** (תלוי בחיבור, במספר המודעות, ובכמה קבוצות פייסבוק):

| שלב | זמן משוער |
|------|-----------|
| Yad2 — סריקת מודעות + חילוץ פרטים | 3–7 דקות |
| Facebook — סריקת פידים (12+ קבוצות) | 30–40 דקות |
| Facebook — ניקוי + טריאז' | 1–3 דקות |
| AI נרמול (אופציונלי, תלוי API) | 2–5 דקות |
| הפקת דוח + ניקוד | < 1 דקה |
| **סה"כ** | **35–55 דקות** |

> ⏱️ **אל תדאג אם הסריקה לוקחת 40 דקות — זה נורמלי.** היא סורקת עשרות עמודים.
> הסריקה כותבת `state.json` תוך כדי ריצה. אפשר לבדוק התקדמות עם `make status`.

### flow של הרצה

```
python3 scripts/full_apartment_scan.py run
  ├── Yad2: yad2_broad_search.py       ← מאתר מודעות, מחלץ פרטים, שומר JSON
  ├── Facebook: facebook_feed_multi_scan.py ← גולל פידים, מחלץ פוסטים
  ├── Facebook: facebook_clean_posts.py ← מנקה, בונה JSON מובנה + MD
  ├── Facebook: facebook_auto_triage.py ← מסווג מודעות (צריך LLM)
  │     └── llm_extract.py             ← חילוץ שדות מטקסט חופשי
  ├── (אופציונלי) ai_normalize_listing.py ← נרמול LLM
  └── evaluate_candidates.py           ← ניקוד + final_report.md
```

### פקודות ניהול

```bash
make scan               # סריקה מלאה
make status             # מצב הריצה הנוכחית
python3 scripts/full_apartment_scan.py resume --run-dir <path>  # המשך סריקה שנקטעה
python3 scripts/full_apartment_scan.py finalize  # סגור סריקה אחרי טריאז'
```

> 💡 **resume**: אם הסריקה נקטעה (Chromium נסגר, חשמל, וכו') — אפשר להמשיך מאותה נקודה עם `resume`. הסקריפט מדלג על שלבים שכבר הושלמו.

## פורמט הדוח (final_report.md)

דוגמה חיה: [`examples/final_report.md`](examples/final_report.md)

הדוח כולל:
- **סיכום**: כמה מודעות נסרקו, כמה עברו סינון
- **מועמדים מובילים**: 3–7 דירות עם ציון, מחיר, חדרים, כניסה, דגלים
- **טבלת דגלים**: קרוס-רפרנס בין מועמדים לדגלים אדומים
- **שאלות לבירור**: מה לשאול את בעל הדירה לפני ביקור

## קריטריונים (מוגדרים ב-criteria.yaml)

- **אזור**: רמת גן, גבעתיים
- **תקציב**: עד ₪6,500 (מועדף ₪6,000)
- **חדרים**: 3 (2.5 מינימום בתנאי שחצי חדר סגור)
- **כניסה**: סביב סוף יולי 2026. כניסה מיידית = דגל אדום
- **עדיפות**: קרבה לרכבת קלה, שקט, מתאים לעבודה מהבית

## דגלים אדומים 🚩

- כניסה מיידית בלי גמישות
- רטיבות / עובש / נזילות
- רעש כבד מכביש ראשי
- אין מזגן בכלל / אין מזגן בחדר שינה
- חוזה קצר בלבד (פחות משנה)
- מחיר חשוד (גבוה מדי למיקום/מצב)
- תיווך במחיר מקסימלי
- חצי חדר = מבואה פתוחה / קיר זכוכית (לא נחשב!)
- "כניסה מיידית" + "אפשר לחדש ביולי" — עדיין דגל אדום

## התחלה — מה לשאול את המשתמש

כשאתה מקבל את הריפו בפעם הראשונה, שאל את המשתמש:

1. **קריטריונים**: אזור? תקציב? חדרים? מועד כניסה?
   - ערוך את `criteria.yaml` בהתאם
2. **טלגרם**: יש טוקן? (מ-@BotFather)
   - אם כן ← שים ב-`.env`
   - אם לא ← דלג, תדווח דרך הצ'אט
3. **פייסבוק**: יש עוגיות? (ייצוא JSON מהדפדפן)
   - אם כן ← שים ב-`data/facebook_cookies.json`, הרץ `make cookies`
   - אם לא ← Facebook יסרק רק פוסטים ציבוריים (מוגבל)
4. **Gemini API**: יש מפתח?
   - אם כן ← שים ב-`.env` (משתנה `GEMINI_API_KEY`)
   - אם לא ← טריאז' AI ונרמול לא יעבדו. השתמש בטריאז' הבסיסי.

אחרי שהמשתמש ענה — הרץ `make check` לאימות, ואז `make scan`.

## חוקים

### ✅ מותר
- לקרוא קבצים, להריץ סקריפטים, לעדכן criteria.yaml
- להשתמש ב-web_search / web_extract למחקר על אזור/רחוב
- לפתוח דפדפן (Chromium CDP) רק למודעות ספציפיות
- לסכם, להשוות, להמליץ

### ❌ אסור
- לשלוח הודעות לבעלי דירות — **אף פעם בלי אישור מפורש**
- לפרסם, להגיב, להצטרף לקבוצות פייסבוק
- למחוק או לערוך את הארכיון (אם קיים)
- לחשוף tokens, cookies, או API keys

## תקשורת

- **שפה**: עברית, קצרה, מעשית
- **דיווח**: 3–7 מועמדות מובילות + לינקים + דגלים
- **בלי פלף**: "נמצאה דירה מצוינת!" ← "דירה X: 3 חד׳, ₪6,200, רמת גן, כניסה יולי. [לינק]"
- **פורמט דוח**: final_report.md — המבנה בתוך evaluate_candidates.py

## תלויות

- **Chromium** עם CDP (פורט 9223) — חובה לסריקת Yad2
- **Google Gemini API key** (אופציונלי) — לטריאז' AI ונרמול
- **עוגיות פייסבוק** — לסריקת קבוצות (facebook_cookies.json)

## הרצה ראשונה

```bash
# 1. תלויות Python
pip install -r requirements.txt

# 2. בדיקת סביבה — מוודא שהכל מוכן
python3 scripts/smoke_test.py

# 3. הפעל Chromium ברקע
./scripts/scanner-chromium &
# או בדוק שהוא רץ: curl -s http://127.0.0.1:9223/json/version

# 4. הזרק עוגיות פייסבוק (אם יש)
python3 scripts/inject_cookies.py data/facebook_cookies.json

# 5. סריקה!
python3 scripts/full_apartment_scan.py run
```

## Docker (חלופי)

אם מעדיפים סביבה מבודדת:

```bash
cp .env.example .env   # ערוך: TELEGRAM_BOT_TOKEN (אם רוצים בוט)
docker compose up -d    # מרים Chromium + cron + בוט טלגרם
```

## תקלות נפוצות

### Chromium CDP לא זמין (port 9223)
```bash
# בדוק אם רץ
curl -s http://127.0.0.1:9223/json/version

# אם לא — הפעל
./scripts/scanner-chromium &

# אם אין chromium — התקן
apt install chromium-browser
```

### סריקת Yad2 לא מחזירה תוצאות
- Chromium חייב להיות רץ **לפני** הסריקה
- נסה להריץ ידנית: `python3 scripts/yad2_broad_search.py`
- Yad2 לפעמים דורש Captcha — במקרה זה, פתח את `http://127.0.0.1:9223` בדפדפן ופתור

### סריקת פייסבוק ריקה
- עוגיות פגו — ייצא מחדש מ-Facebook
- בדוק: `python3 scripts/smoke_test.py` ← "Facebook cookies"
- נסה להזריק מחדש: `python3 scripts/inject_cookies.py data/facebook_cookies.json`

### טריאז' AI לא עובד
- וודא ש-`GEMINI_API_KEY` מוגדר
- בדוק: `python3 scripts/smoke_test.py` ← "GEMINI_API_KEY"
- בלי API key — השתמש בטריאז' בסיסי (ללא LLM)

### מודול חסר / ImportError
```bash
pip install -r requirements.txt
python3 scripts/smoke_test.py  # יגיד לך מה חסר
```

### Permission denied על data/
```bash
sudo chown -R $(whoami) data/
```

## הערות

- **Madlan** — מושבת לצמיתות (PerimeterX חוסם).
- **Yad2** — נסרק דרך Chromium CDP, לא דרך HTTP ישיר (אנטי-בוט).
- **מודעות פייסבוק** — טריאז' AI דורש LLM. בלי API key, הטריאז' בסיסי בלבד.
- **הסוכן לא מחליף את המשתמש** — הוא כלי עזר. ההחלטה הסופית תמיד של האדם.
