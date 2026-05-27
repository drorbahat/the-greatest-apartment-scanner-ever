# 🏠 Apartment Scanner

בוט טלגרם אוטומטי לסריקת דירות להשכרה מ-Yad2 וקבוצות פייסבוק.

## איך זה עובד

- **סריקה אוטומטית** כל שעה (7:10-22:10) — אוספת מודעות מ־Yad2 ומפייסבוק
- **דוח אוטומטי** נשלח לטלגרם כשיש תוצאות חדשות
- **פקודות ידניות** לביצוע סריקה / בדיקת סטטוס / קבלת דוח

## דרישות

- **Docker** ו־**Docker Compose** מותקנים
- **טלגרם**: בוט מ־[BotFather](https://t.me/BotFather) + ה־Token
- **פייסבוק**: עוגיות (cookies) מחשבון פייסבוק מחובר — [איך להוציא](#-הגדרת-פייסבוק-עוגיות)

---

## 🚀 התקנה מהירה (לחבר — תן לקלוד את זה)

```bash
# 1. clone
git clone https://github.com/YOUR_USER/apartment-scanner.git
cd apartment-scanner

# 2. צור קובץ .env
cp .env.example .env
# ערוך את .env: הכנס TELEGRAM_BOT_TOKEN

# 3. העתק עוגיות פייסבוק (ראה בהמשך)
cp /path/to/facebook_cookies.json data/facebook_cookies.json

# 4. הרם
docker compose up -d

# 5. פתח טלגרם, כתוב לבוט: /start
```

> 💡 **לקלוד**: פשוט תתן לו את הסקריפטים + README הזה, תבקש ממנו להרים Docker.

---

## 🐳 Docker

### הרמה ראשונה

```bash
docker compose up -d
```

### בדיקה שהכל עובד

```bash
docker compose logs -f --tail 50
```

צריך לראות:
- `Chromium CDP ready on port 9223`
- `Cron started`
- `Telegram bot started`

### עצירה

```bash
docker compose down
```

### עדכון אחרי שינוי

```bash
docker compose build --no-cache
docker compose up -d
```

---

## 🤖 פקודות טלגרם

| פקודה | מה קורה |
|--------|---------|
| `/start` | אתחול הצ'אט (שומר את ה־Chat ID) |
| `/scan` | הפעל סריקת דירות עכשיו |
| `/status` | מצב הריצה הנוכחית |
| `/report` | הדוח המלא האחרון |
| `/recent` | 5 המועמדויות המובילות |
| `/help` | עזרה |

---

## 🔧 הגדרת פייסבוק (עוגיות)

הסורק צריך גישה לפייסבוק דרך חשבון מחובר.

### שיטה 1: ייצוא עוגיות (מומלץ)

1. התקן תוסף לדפדפן לייצוא עוגיות —例如 [Get cookies.txt](https://chrome.google.com/webstore/detail/get-cookiestxt/bgaddhkoddajcdgocldbbfleckgcbcid) או [EditThisCookie](https://www.editthiscookie.com/)
2. התחבר לפייסבוק בדפדפן
3. ייצא עוגיות (cookies) מ־`facebook.com` לפורמט JSON
4. שמור כ־`data/facebook_cookies.json` בתיקיית הפרויקט
5. הרץ: `docker compose restart`

> ⚠️ עוגיות פייסבוק פגות תוקף. תצטרך לייצא מחדש בערך אחת לכמה שבועות.

### שיטה 2: התחברות דרך קונסול (חלופי)

```bash
# כנס לקונטיינר
docker exec -it apartment-scanner /bin/bash

# הפעל כרום עם ממשק (צריך X11)
# או פשוט בדוק שהעוגיות עובדות
python3 scripts/inject_cookies.py /app/data/facebook_cookies.json
```

### אימות שהעוגיות עובדות

```bash
docker compose logs | grep -i "inject"
```

אם רואים `✅ Success! Injected X cookies. Facebook: logged in` — הכל בסדר.

---

## ⚙️ קונפיגורציה

### criteria.yaml

פרמטרי החיפוש (אזור, תקציב, חדרים) מוגדרים ב־`criteria.yaml` בשורש הפרויקט.
הערכים האלה משמשים כתיעוד — הלוגיקה עצמה מוגדרת ב־`evaluate_candidates.py`.
אם רוצים לשנות פרמטרים, עורכים את הקובץ ואז `docker compose build --no-cache && docker compose up -d`.

```yaml
budget:
  max_nis: 6500     # תקציב מקסימלי
rooms:
  preferred: 3      # חדרים מועדפים
area:
  primary:
    - רמת גן
    - גבעתיים
```

### .env

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234...  # חובה
GEMINI_API_KEY=AIzaSy...                  # אופציונלי — העשרת מודעות ב־AI
```

---

## 🏗 מבנה התיקיות

```
apartment-scanner/
├── scripts/                   # סקריפטים של סריקה
│   ├── full_apartment_scan.py # אורקסטרטור ראשי
│   ├── yad2_broad_search.py   # סורק Yad2
│   ├── facebook_group_scan.py # סורק פייסבוק
│   ├── evaluate_candidates.py # ניקוד וסינון
│   └── inject_cookies.py      # הזרקת עוגיות פייסבוק
├── telegram_bot.py            # בוט טלגרם (polling)
├── Dockerfile                 # תמונת Docker
├── docker-compose.yml         # הגדרת הרצה
├── entrypoint.sh              # סקריפט התחלה
├── .env.example               # תבנית משתני סביבה
├── requirements.txt           # תלויות Python
├── criteria.yaml              # פרמטרי חיפוש (ערוך + בנה מחדש)
└── data/                     # מידע מתמיד — volume mounts
    ├── facebook_cookies.json  # ← שים כאן (קובץ עוגיות פייסבוק)
    ├── artifacts/             # ← תוצאות סריקה
    ├── browser-profile/       # ← פרופיל כרום (מתמיד)
    └── logs/                  # ← לוגים של cron + entrypoint
```

---

## 🆘 טריאז' (תקלות נפוצות)

### "הבוט לא עונה"
```bash
docker compose logs scanner     # בדוק שהבוט רץ
# ואז בטלגרם: /start
```

### "הסריקה לא מתחילה"
```bash
docker compose exec apartment-scanner python3 scripts/full_apartment_scan.py status
```

### "אין תוצאות מפייסבוק"
- העוגיות כנראה פגו. ייצא מחדש.

### "Chromium CDP not ready"
```bash
docker compose logs | grep Chromium
```

### "Permission denied" על data/
```bash
sudo chown -R 1000:1000 data/
```

---

## 📝 הערות

- **Madlan** — מושבת לצמיתות (PerimeterX חוסם)
- **הסריקה אוספת מ־Yad2 (דרך Chromium CDP) + קבוצות פייסבוק**
- **בלי Hermes Agent** — הבוט רץ עצמאית לגמרי
- **שעות פעילות**: 7:10-22:10 שעון ישראל (כתוב ב־UTC ב־entrypoint.sh)

---

*נבנה עבור דרור 🤝*
