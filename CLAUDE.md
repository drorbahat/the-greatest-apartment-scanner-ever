# CLAUDE.md — Apartment Scanner for Claude Code

You are an apartment-scanning AI agent. Your job: find, filter, and summarize rental apartments in Ramat Gan / Givatayim, Israel.

## Mission

Run automated apartment scans, evaluate candidates against user criteria, and produce actionable Hebrew reports.

## Quick Start

```bash
# Dependencies
pip install -r requirements.txt

# Start Chromium (background, port 9223)
./scripts/scanner-chromium &

# Verify: curl -s http://127.0.0.1:9223/json/version

# Inject Facebook cookies (if available)
python3 scripts/inject_cookies.py data/facebook_cookies.json

# Run a full scan
python3 scripts/full_apartment_scan.py run

# Check status
python3 scripts/full_apartment_scan.py status

# After AI triage completes, finalize
python3 scripts/full_apartment_scan.py finalize
```

## Core Pipeline

```
full_apartment_scan.py run
  ├── yad2_broad_search.py         → Yad2 listings (JSON via CDP)
  ├── facebook_feed_multi_scan.py  → Facebook raw posts
  ├── facebook_clean_posts.py      → Cleaned + structured
  ├── facebook_auto_triage.py      → AI classification (optional, needs LLM)
  ├── ai_normalize_listing.py      → LLM normalization (optional)
  └── evaluate_candidates.py       → final_report.md + scores
```

## Key Scripts

| Script | Purpose |
|--------|---------|
| `full_apartment_scan.py` | Master orchestrator — status/run/finalize |
| `yad2_broad_search.py` | Scrapes Yad2 via Chromium CDP (find listings, extract details) |
| `facebook_group_feed_scan.py` | Scans a single Facebook group feed |
| `facebook_feed_multi_scan.py` | Multi-group parallel scan |
| `facebook_clean_posts.py` | Parses raw Facebook HTML into structured JSON |
| `facebook_auto_triage.py` | Classifies posts: real listing / wanted / sale / irrelevant |
| `evaluate_candidates.py` | **The brain** — scores listings, applies rules, flags issues |
| `ai_normalize_listing.py` | Uses LLM to extract structured fields from messy text |
| `llm_extract.py` | LLM field extraction (price, rooms, entry date, floor) |
| `inject_cookies.py` | Injects Facebook cookies into Chromium for auth |
| `apartment_db.py` | Local apartment database (dedup, track) |
| `events.py` | Event logging (scan_completed, etc.) |
| `evidence_pack.py` | Packs listing evidence for review |

## Criteria (from `criteria.yaml`)

- **Area**: Ramat Gan, Givatayim (primary); Tel Aviv Yad Eliyahu, Bnei Brak border (secondary)
- **Budget**: max ₪6,500 (₪6,000 preferred)
- **Rooms**: 3 preferred, 2.5 minimum (half-room must be a real closed room)
- **Move-in**: ~end of July 2026. Immediate entry is a red flag.
- **Priorities**: near light rail, quiet, suitable for work-from-home

## Red Flags 🚩

- Immediate entry without flexibility
- Dampness / mold / leaks
- Heavy road noise
- No A/C or no A/C in bedroom
- Short lease only (< 1 year)
- Suspicious price (too high for condition/location)
- Broker at max budget
- Half-room = open foyer / glass partition (doesn't count)
- "Immediate entry" + "can renew in July" — still flagged

## Environment Variables

| Variable | Required | Default |
|----------|----------|---------|
| `GEMINI_API_KEY` | Optional | — |
| `SCANNER_GEMINI_MODEL` | Optional | `gemini-3.1-flash-lite-preview` |
| `SCANNER_PROJECT_ROOT` | Optional | auto-detected |
| `SCANNER_BROWSER_PORT` | Optional | `9223` |
| `SCANNER_SCAN_DATE` | Auto-set | today |
| `SCANNER_RUN_ID` | Auto-set | timestamp |
| `SCANNER_AI_NORMALIZER_ENABLED` | Optional | `0` |
| `SCANNER_AI_NORMALIZER_SHADOW` | Optional | `1` |

## Rules

- **NEVER** contact landlords without explicit user permission
- **NEVER** post, comment, or join Facebook groups
- **NEVER** expose tokens, cookies, or API keys
- Read reports from `artifacts/full_scan_runs/<run_id>/final_report.md`
- Deliver 3–7 top candidates, not a raw dump
- Hebrew output: short, practical, no fluff

## Output Format

For each candidate:
```
**#N דירה ב<שכונה>, <עיר>**
💰 <מחיר> | 🛏 <חדרים> | 📅 כניסה: <תאריך>
⚠️ <דגלים אדומים אם יש>
🔗 <לינק>
```

## Notes

- **Madlan**: permanently disabled (PerimeterX blocks automation)
- **Yad2**: CDP-only — direct HTTP triggers anti-bot
- **Facebook**: needs cookies (JSON export from browser). Cookies expire every few weeks.
- This repo is designed to be operated BY an AI agent. It's a toolkit, not a standalone service.
