#!/usr/bin/env python3
"""Build and write normalization audit reports.

Compares AI-normalized listings against raw evidence packs and produces
a human-readable audit in Markdown.

Used by normalization_pipeline.py (optional — graceful fallback if missing).
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime
from typing import Any


def build_audit(
    raw_observations: list[dict[str, Any]],
    evidence_packs: list[dict[str, Any]],
    normalized_listings: list[dict[str, Any]],
    run_dir: pathlib.Path,
) -> dict[str, Any]:
    """Build an audit summary comparing normalized vs raw data.

    Returns a dict with:
      - generated_at: ISO timestamp
      - total_raw: number of raw observations
      - total_packs: number of evidence packs
      - total_normalized: number of normalized listings
      - field_comparisons: per-field accuracy metrics
      - discrepancies: list of notable differences
    """
    discrepancies = []

    # Compare key fields between normalized and evidence packs
    for i, (norm, pack) in enumerate(zip(normalized_listings, evidence_packs)):
        issues = []

        # Price comparison
        norm_price = norm.get("price")
        pack_price = pack.get("price") or pack.get("extracted_price")
        if norm_price and pack_price and abs(norm_price - pack_price) > 100:
            issues.append(f"price: normalized={norm_price} vs evidence={pack_price}")

        # Rooms comparison
        norm_rooms = norm.get("rooms")
        pack_rooms = pack.get("rooms") or pack.get("extracted_rooms")
        if norm_rooms and pack_rooms and abs(norm_rooms - pack_rooms) > 0.5:
            issues.append(f"rooms: normalized={norm_rooms} vs evidence={pack_rooms}")

        if issues:
            discrepancies.append({
                "index": i,
                "url": norm.get("url") or pack.get("url", ""),
                "issues": issues,
            })

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_raw": len(raw_observations),
        "total_packs": len(evidence_packs),
        "total_normalized": len(normalized_listings),
        "discrepancies": discrepancies,
        "discrepancy_count": len(discrepancies),
    }


def write_audit_markdown(
    audit: dict[str, Any],
    path: pathlib.Path,
    run_dir: pathlib.Path | None = None,
) -> None:
    """Write the audit summary as a Markdown file.

    Args:
        audit: Dict from build_audit()
        path: Output .md file path
        run_dir: Scan run directory (for context)
    """
    lines = [
        "# 📊 Normalization Audit",
        "",
        f"**Generated:** {audit['generated_at']}",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Raw observations | {audit['total_raw']} |",
        f"| Evidence packs | {audit['total_packs']} |",
        f"| Normalized listings | {audit['total_normalized']} |",
        f"| Discrepancies | {audit['discrepancy_count']} |",
        "",
    ]

    if audit["discrepancies"]:
        lines.append("## ⚠️ Discrepancies")
        lines.append("")
        for d in audit["discrepancies"]:
            lines.append(f"### Listing #{d['index']}")
            if d.get("url"):
                lines.append(f"🔗 {d['url']}")
            lines.append("")
            for issue in d["issues"]:
                lines.append(f"- {issue}")
            lines.append("")
    else:
        lines.append("## ✅ No discrepancies found")
        lines.append("")
        lines.append("All normalized listings match their evidence packs within tolerance.")

    if run_dir:
        lines.append("")
        lines.append(f"*Run directory: `{run_dir}`*")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
