#!/usr/bin/env bash
# Apartment Scanner — First-time setup script
# Usage: bash setup.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "╔══════════════════════════════════════════╗"
echo "║   🏠 Apartment Scanner — Setup           ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Python check ──────────────────────────────────────────────────────────
echo "→ Checking Python..."
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" --version 2>&1 | grep -oP '\d+\.\d+')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    echo "❌ Python 3.10+ required. Install: apt install python3"
    exit 1
fi
echo "   ✅ $PYTHON ($($PYTHON --version))"

# ── Dependencies ──────────────────────────────────────────────────────────
echo "→ Installing Python dependencies..."
$PYTHON -m pip install -r requirements.txt --quiet 2>&1 | tail -1
echo "   ✅ Dependencies installed"

# ── Chromium ──────────────────────────────────────────────────────────────
echo "→ Checking Chromium..."
CHROMIUM=""
for path in /usr/bin/chromium /usr/bin/chromium-browser /usr/bin/google-chrome /usr/bin/google-chrome-stable /snap/bin/chromium; do
    if [ -x "$path" ]; then
        CHROMIUM="$path"
        break
    fi
done
if [ -z "$CHROMIUM" ]; then
    echo "   ⚠️  Chromium not found."
    echo "   Install: apt install chromium-browser"
    echo "   Or set CHROMIUM_BIN=/path/to/chromium in your environment."
else
    echo "   ✅ Found: $CHROMIUM"
fi

# ── .env ──────────────────────────────────────────────────────────────────
echo "→ Setting up .env..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "   ✅ Created .env from .env.example"
    echo "   ⚠️  Edit .env and add your TELEGRAM_BOT_TOKEN (optional) and GEMINI_API_KEY (optional)"
else
    echo "   ✅ .env already exists"
fi

# ── Directories ───────────────────────────────────────────────────────────
echo "→ Creating data directories..."
mkdir -p data/artifacts/full_scan_runs data/artifacts/facebook data/browser-profile data/logs
echo "   ✅ Done"

# ── Facebook cookies ──────────────────────────────────────────────────────
echo "→ Checking Facebook cookies..."
if [ -f data/facebook_cookies.json ]; then
    cookie_count=$($PYTHON -c "
import json
try:
    data = json.load(open('data/facebook_cookies.json'))
    print(len(data) if isinstance(data, list) else 0)
except: print(0)
" 2>/dev/null || echo 0)
    if [ "$cookie_count" -gt 0 ]; then
        echo "   ✅ Found ($cookie_count cookies)"
    else
        echo "   ⚠️  File exists but appears empty. Export cookies from your browser."
    fi
else
    echo "   ⚠️  No Facebook cookies found."
    echo "   To scan Facebook groups:"
    echo "   1. Install a cookie exporter browser extension"
    echo "   2. Log into Facebook in your browser"
    echo "   3. Export cookies as JSON → data/facebook_cookies.json"
fi

# ── Smoke test ────────────────────────────────────────────────────────────
echo ""
echo "→ Running smoke test..."
$PYTHON scripts/smoke_test.py --quick
echo ""

# ── Next steps ────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════╗"
echo "║   Setup complete!                        ║"
echo "║                                          ║"
echo "║   Next steps:                            ║"
echo "║   1. Edit criteria.yaml (your prefs)     ║"
echo "║   2. Start Chromium: make chromium       ║"
echo "║   3. Inject cookies: make cookies        ║"
echo "║   4. Run a scan:    make scan            ║"
echo "╚══════════════════════════════════════════╝"
