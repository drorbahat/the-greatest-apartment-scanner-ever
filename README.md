# 🏠 Apartment Scanner

**ערכת כלים לסריקת דירות — מופעלת על ידי סוכן AI.**

זה לא שירות standalone. זה ריפו שנותנים לסוכן AI (Claude Code, Pi, Hermes, OpenClaw, וכו') — והוא קורא את ההוראות, מפעיל את הסקריפטים, ומדווח על דירות.

---

## 🧠 איך זה עובד (לחבר — תן לסוכן שלך את זה)

```bash
git clone https://github.com/YOUR_USER/apartment-scanner.git
cd apartment-scanner
```

ואז תגיד לסוכן שלך:

> "תקרא את AGENTS.md ואת CLAUDE.md, תבין מה לעשות, ותריץ סריקת דירות."

זהו. הסוכן יודע:
- איך להרים Chromium (CDP)
- איך להזריק עוגיות פייסבוק
- איך להריץ סריקה מלאה
- איך לקרוא את הדוח ולסכם

### פקודות מהירות (Makefile)

```bash
make check     # בדיקת סביבה — הכל מוכן?
make scan      # סריקה מלאה
make status    # מצב הסריקה הנוכחית
make report    # הצגת הדוח האחרון
make help      # כל הפקודות
```

### או — סקריפט setup אוטומטי

```bash
bash setup.sh   # מתקין תלויות, בודק Chromium, יוצר .env, מריץ smoke test
```

### למה סוכן AI?

- הוא קורא את הקוד, מבין אותו, ומתקן תקלות בזמן אמת
- הוא יודע לסנן תוצאות, לזהות דגלים אדומים, ולדווח בעברית
- הוא לא צריך cron — הוא מריץ סריקות לפי לוז שאתה מגדיר
- הוא לא תלוי בטלגרם — הוא מדווח ישירות בצ'אט

---

## 🚀 התקנה מהירה (Docker — אופציונלי)

אם אתה מעדיף בוט טלגרם שרץ לבד:

```bash
cp .env.example .env
# ערוך: TELEGRAM_BOT_TOKEN (מ-@BotFather)

cp /path/to/facebook_cookies.json data/facebook_cookies.json

docker compose up -d
```

פקודות טלגרם: `/start`, `/scan`, `/status`, `/report`, `/recent`, `/help`

---

## ⚙️ מה יש פה

```
├── AGENTS.md              ← 📖 תן לסוכן לקרוא (עברית, מלא)
├── CLAUDE.md              ← 📖 גרסה טכנית באנגלית
├── criteria.yaml          ← 🎯 קריטריונים: אזור, תקציב, חדרים
├── scripts/               ← 🔧 21 סקריפטים
│   ├── full_apartment_scan.py    ← אורקסטרטור
│   ├── yad2_broad_search.py      ← סורק Yad2 (Chromium CDP)
│   ├── facebook_*.py             ← סורקי + מנקי + טריאז' פייסבוק
│   └── evaluate_candidates.py    ← ניקוד + סינון + דוח
├── telegram_bot.py        ← בוט טלגרם (אופציונלי)
├── Dockerfile             ← תמונת Docker
├── docker-compose.yml     ← הרמה מהירה
└── requirements.txt       ← תלויות Python
```

---

## 🔧 הגדרת פייסבוק (עוגיות)

1. התקן תוסף לייצוא עוגיות (Get cookies.txt, EditThisCookie)
2. התחבר לפייסבוק, ייצא עוגיות מ-`facebook.com` כ-JSON
3. שמור כ-`data/facebook_cookies.json`

> ⚠️ עוגיות פגות תוקף כל כמה שבועות — צריך לייצא מחדש.

---

## 📝 הערות

- **Madlan** — מושבת (PerimeterX)
- **Yad2** — דרך Chromium CDP בלבד (לא HTTP ישיר)
- **Facebook** — דורש עוגיות. טריאז' AI דורש `GEMINI_API_KEY`
- **בלי מזהים אישיים** — הריפו נקי, גנרי, מוכן להפצה
