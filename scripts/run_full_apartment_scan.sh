#!/usr/bin/env bash
# Compatibility wrapper. The reliable implementation lives in full_apartment_scan.py
# and writes state.json, manifest.json, logs, and a unified final_report.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/full_apartment_scan.py" run "$@"
