#!/usr/bin/env bash
set -e

echo "=== Yogev Scanner Starting ==="

# Ensure data directories exist (volume mounts may not auto-create)
mkdir -p /app/artifacts /app/artifacts/full_scan_runs /app/data/logs /app/data/browser-profile

# Ensure chat_id.txt exists (bot writes to it on /start)
touch /app/data/chat_id.txt

# ── Start Chromium ──────────────────────────────────────────────────────────
echo "Starting Chromium (headless, CDP port 9223)..."
/usr/bin/chromium \
  --no-first-run \
  --lang=he-IL \
  --disable-dev-shm-usage \
  --password-store=basic \
  --user-data-dir=/app/data/browser-profile \
  --remote-debugging-port=9223 \
  --disable-gpu \
  --disable-software-rasterizer \
  --disable-features=IsolateOrigins,site-per-process \
  --disable-site-isolation-trials \
  --no-sandbox \
  --headless=new \
  about:blank > /app/data/logs/chromium.log 2>&1 &
CHROME_PID=$!
echo "Chromium PID: $CHROME_PID"

# Wait for Chromium CDP port to be ready
for i in $(seq 1 15); do
  if curl -s http://127.0.0.1:9223/json/version > /dev/null 2>&1; then
    echo "Chromium CDP ready on port 9223"
    break
  fi
  sleep 1
done

# ── Inject Facebook cookies if available ────────────────────────────────────
COOKIE_FILE="/app/data/facebook_cookies.json"
if [ -f "$COOKIE_FILE" ]; then
  echo "Injecting Facebook cookies from $COOKIE_FILE..."
  python3 scripts/inject_cookies.py "$COOKIE_FILE" || echo "⚠️ Cookie injection had issues (non-fatal)"
else
  echo "No Facebook cookies file found at $COOKIE_FILE"
  echo "Facebook scans will fail until cookies are provided."
fi

# ── Setup cron jobs ─────────────────────────────────────────────────────────
echo "Setting up cron jobs..."
# Times in UTC. Israel summer (IDT) = UTC+3
#   scan: 4:10-19:10 UTC = 7:10-22:10 Israel
#   report: 5:00-20:00 UTC = 8:00-23:00 Israel
cat > /tmp/crontab <<'CRON'
10 4-19 * * * cd /app && python3 scripts/full_apartment_scan.py run >> /app/data/logs/scan.log 2>&1
0 5-20 * * * cd /app && python3 telegram_bot.py --send-report >> /app/data/logs/report.log 2>&1
CRON
crontab /tmp/crontab
rm /tmp/crontab

service cron start
echo "Cron started (scan :10, report :00)"

# ── Start Telegram bot (polling — stays alive) ─────────────────────────────
echo "Starting Telegram bot..."
echo ""
echo "  ╔═══════════════════════════════════════════╗"
echo "  ║  Bot is running!                          ║"
echo "  ║  Send /start in Telegram to begin.        ║"
echo "  ╚═══════════════════════════════════════════╝"
echo ""
exec python3 telegram_bot.py
