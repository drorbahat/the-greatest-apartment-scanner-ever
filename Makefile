# Apartment Scanner — Makefile
# Usage: make <target>

.PHONY: test check scan status report setup clean help

# ── Default ──────────────────────────────────────────────────────────────
help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ── Environment ──────────────────────────────────────────────────────────
setup:  ## Install dependencies
	pip install -r requirements.txt
	@echo "✅ Dependencies installed. Run 'make test' to verify."

test: check  ## Alias for check
check:  ## Run smoke test — verify environment is ready
	python3 scripts/smoke_test.py

# ── Chromium ─────────────────────────────────────────────────────────────
chromium:  ## Start Chromium with CDP (port 9223)
	./scripts/scanner-chromium &

chromium-status:  ## Check if Chromium CDP is running
	@curl -s http://127.0.0.1:9223/json/version | python3 -m json.tool || \
		echo "❌ Chromium not running. Run: make chromium"

# ── Scan ─────────────────────────────────────────────────────────────────
scan:  ## Run a full apartment scan
	python3 scripts/full_apartment_scan.py run

scan-status: status  ## Alias for status
status:  ## Check scan status
	python3 scripts/full_apartment_scan.py status

finalize:  ## Finalize scan after AI triage
	python3 scripts/full_apartment_scan.py finalize

# ── Reports ──────────────────────────────────────────────────────────────
report:  ## Show latest scan report
	@ls -t artifacts/full_scan_runs/*/final_report.md 2>/dev/null | head -1 | xargs cat || \
		echo "No reports found. Run 'make scan' first."

recent:  ## Show most recent run directory
	@ls -dt artifacts/full_scan_runs/*/ 2>/dev/null | head -1 || \
		echo "No runs found."

# ── Facebook ─────────────────────────────────────────────────────────────
cookies:  ## Inject Facebook cookies
	python3 scripts/inject_cookies.py data/facebook_cookies.json

# ── Maintenance ──────────────────────────────────────────────────────────
clean:  ## Clean all artifacts and logs
	rm -rf artifacts/full_scan_runs/* artifacts/facebook/* data/logs/*
	@echo "✅ Cleaned."

clean-all: clean  ## Clean everything including browser profile
	rm -rf data/browser-profile data/artifacts data/logs
	@echo "✅ All data cleaned."
