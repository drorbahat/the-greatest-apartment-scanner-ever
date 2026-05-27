#!/usr/bin/env python3
"""Normalization pipeline — helper module for shadow-mode AI normalization.

This module is the integration layer between the scanner's finalize flow
and the AI normalizer. It is non-blocking: failures are caught and logged,
never failing the main scan.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import traceback
from typing import Any

# Ensure scripts/ is on path for imports
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import evidence_pack as _ep
import ai_normalize_listing as _anl


def _env_flag(name: str, default: str = "0") -> str:
    return os.environ.get(name, default)


def normalization_enabled() -> bool:
    return _env_flag("YOGEV_AI_NORMALIZER_ENABLED") == "1"


def normalization_shadow_enabled() -> bool:
    return _env_flag("YOGEV_AI_NORMALIZER_SHADOW", "1") == "1"


def _max_items() -> int:
    try:
        return int(_env_flag("YOGEV_AI_NORMALIZER_MAX_ITEMS", "30"))
    except ValueError:
        return 30


def _use_llm() -> bool:
    return _env_flag("YOGEV_AI_NORMALIZER_USE_LLM", "0") == "1"


def _max_text_chars() -> int:
    try:
        return int(_env_flag("YOGEV_AI_NORMALIZER_MAX_TEXT_CHARS", "6000"))
    except ValueError:
        return 6000


def _cache_path() -> pathlib.Path:
    raw = _env_flag(
        "YOGEV_AI_NORMALIZER_CACHE",
        "/app/artifacts/normalization/normalizer_cache.json",
    )
    return pathlib.Path(raw)


def _try_load_user_rejections() -> Any:
    """Lazy import user_rejections to avoid circular deps / startup cost."""
    try:
        import user_rejections as _ur
        return _ur
    except Exception:
        return None


def _is_permanently_rejected(obs: dict[str, Any]) -> bool:
    ur = _try_load_user_rejections()
    if ur is None:
        return False
    url = obs.get("canonical_url") or obs.get("post_url") or obs.get("url")
    if not url:
        return False
    try:
        rejected, _record = ur.is_rejected(obs)
        return bool(rejected)
    except Exception:
        return False


def _load_raw_observations(run_dir: pathlib.Path) -> list[dict[str, Any]]:
    """Load raw observations from run_dir, preferring the JSON artifact."""
    raw_path = run_dir / "raw_observations.json"
    if raw_path.exists():
        data = json.loads(raw_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return data if isinstance(data, list) else []

    # Fallback: use evaluate_candidates loader
    try:
        import evaluate_candidates as _ec
        return _ec.load_raw_observations(run_dir)
    except Exception:
        return []


def run_shadow_normalization(
    run_dir: pathlib.Path | str,
    *,
    max_items: int | None = None,
    use_llm: bool | None = None,
) -> dict[str, Any]:
    """Run shadow normalization for a completed scan run.

    Returns a summary dict suitable for manifest['ai_normalization'].
    Never raises — failures are captured in the returned dict.
    """
    run_dir = pathlib.Path(run_dir)
    if not normalization_enabled():
        return {"enabled": False}

    max_items = max_items if max_items is not None else _max_items()
    use_llm = use_llm if use_llm is not None else _use_llm()
    shadow = normalization_shadow_enabled()
    cache_path = _cache_path()

    # Ensure output dirs exist
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    log_path = logs_dir / "normalization.log"
    evidence_path = run_dir / "evidence_packs.json"
    normalized_path = run_dir / "normalized_listings.json"
    audit_path = run_dir / "normalization_audit.md"

    def _log(msg: str) -> None:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        line = f"[{ts}] {msg}\n"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    try:
        _log("START shadow normalization")
        _log(f"run_dir={run_dir} max_items={max_items} use_llm={use_llm} shadow={shadow}")

        raw_obs = _load_raw_observations(run_dir)
        _log(f"raw_observations_loaded={len(raw_obs)}")

        if not raw_obs:
            return {
                "enabled": True,
                "shadow": shadow,
                "status": "completed",
                "total_packs": 0,
                "normalized": 0,
                "failed": 0,
                "llm_enabled": use_llm,
                "error": None,
            }

        # Build evidence packs with prefilter
        packs: list[dict[str, Any]] = []
        skipped = 0
        for obs in raw_obs:
            if _is_permanently_rejected(obs):
                skipped += 1
                continue
            eligible, reason = _ep.should_send_to_normalizer(obs)
            if not eligible:
                skipped += 1
                continue
            packs.append(_ep.build_evidence_pack(obs, max_text_chars=_max_text_chars()))

        _log(f"evidence_packs_built={len(packs)} skipped={skipped}")

        # Limit to max_items (deterministic: first N after filtering)
        if len(packs) > max_items:
            packs = packs[:max_items]
            _log(f"limited_to_max_items={max_items}")

        # Write evidence packs
        evidence_path.write_text(
            json.dumps(packs, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        _log(f"evidence_packs_written={evidence_path}")

        # Normalize
        normalized = _anl.normalize_packs(
            packs,
            max_items=max_items,
            use_llm=use_llm,
            cache_path=cache_path,
        )

        from collections import Counter

        status_counts = Counter(n.get("normalization_status", "unknown") for n in normalized)
        ok_count = status_counts.get("ok", 0)
        cached_count = status_counts.get("skipped_cached", 0)
        failed_count = sum(count for status, count in status_counts.items() if str(status).startswith("failed_"))

        _log(f"normalized_total={len(normalized)} ok={ok_count} cached={cached_count} failed={failed_count}")

        # Write normalized listings
        normalized_path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        _log(f"normalized_listings_written={normalized_path}")

        # Write audit via normalization_audit if available
        try:
            import normalization_audit as _na
            audit_summary = _na.build_audit(
                raw_observations=raw_obs,
                evidence_packs=packs,
                normalized_listings=normalized,
                run_dir=run_dir,
            )
            _na.write_audit_markdown(
                audit_summary,
                path=audit_path,
                run_dir=run_dir,
            )
            _log(f"audit_written={audit_path}")
        except Exception as exc:
            _log(f"audit_write_failed={exc}")
            # Write minimal fallback audit
            audit_path.write_text(
                f"# Normalization Audit (fallback)\n\n"
                f"- Total normalized: {len(normalized)}\n"
                f"- OK: {ok_count}\n"
                f"- Failed: {failed_count}\n"
                f"- Audit module error: {exc}\n",
                encoding="utf-8",
            )

        return {
            "enabled": True,
            "shadow": shadow,
            "status": "completed",
            "evidence_packs": str(evidence_path),
            "normalized_listings": str(normalized_path),
            "audit": str(audit_path),
            "total_packs": len(packs),
            "normalized": len(normalized),
            "ok": ok_count,
            "cached": cached_count,
            "failed": failed_count,
            "status_counts": dict(status_counts),
            "skipped_prefilter": skipped,
            "llm_enabled": use_llm,
            "error": None,
        }

    except Exception as exc:
        err = traceback.format_exc()
        _log(f"FATAL {err}")
        return {
            "enabled": True,
            "shadow": shadow,
            "status": "failed_non_blocking",
            "error": str(exc),
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Normalization pipeline (shadow mode)")
    parser.add_argument("--run-dir", required=True, help="Path to completed scan run directory")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--no-llm", action="store_true", dest="no_llm")
    parser.add_argument("--llm", action="store_true", dest="llm")
    args = parser.parse_args()

    # Allow CLI override of env
    use_llm = _use_llm()
    if args.no_llm:
        use_llm = False
    if args.llm:
        use_llm = True

    result = run_shadow_normalization(args.run_dir, max_items=args.max_items, use_llm=use_llm)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
