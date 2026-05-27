#!/usr/bin/env python3
"""Query and manage user rejections from the apartments.db.

Used by cron_load_results.py to filter out permanently rejected apartments
before presenting candidates to the user.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "artifacts" / "apartments.db"


USER_REJECTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_rejects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    source_platform TEXT,
    listing_id TEXT,
    reject_reason_text TEXT,
    classification TEXT NOT NULL CHECK(classification IN ('PERMANENT', 'TRANSIENT')),
    category TEXT NOT NULL,
    original_price REAL,
    original_entry TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP
)
"""


def ensure_schema(path: Path | None = None) -> None:
    """Create the user_rejects table when using a fresh DB path."""
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as con:
        con.execute(USER_REJECTS_SCHEMA)


def connect() -> sqlite3.Connection:
    ensure_schema(DB_PATH)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def load_rejections(classification: str | None = None) -> list[dict[str, Any]]:
    """Load all rejections from DB, optionally filtered by classification."""
    with connect() as con:
        if classification:
            rows = con.execute(
                "SELECT * FROM user_rejects WHERE classification = ?",
                (classification,),
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM user_rejects").fetchall()
    return [dict(r) for r in rows]


def _normalize_url(url: str) -> str:
    """Normalize URL: drop tracking params, sort query keys."""
    if not url:
        return ""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    url = str(url).strip()
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    # Keep only listing-identifying params
    keep_keys = {"story_fbid", "id", "fbid", "item"}
    keep = {k: v for k, v in qs.items() if k in keep_keys}

    query = urlencode(sorted(keep.items()), doseq=True)
    return urlunparse((
        parsed.scheme,
        parsed.netloc.lower(),
        parsed.path.rstrip("/"),
        "",
        query,
        "",
    ))


def _listing_key_from_url(url: str) -> str:
    """Extract stable listing key from URL."""
    if not url:
        return ""
    from urllib.parse import urlparse, parse_qs

    url = str(url).strip()
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    # Facebook permalink
    if "facebook.com" in parsed.netloc:
        story = qs.get("story_fbid", [""])[0]
        page_id = qs.get("id", [""])[0]
        if story and page_id:
            return f"facebook:story:{story}:page:{page_id}"
        # Group post fallback
        group_match = re.search(r"groups/(\d+)/posts/(\d+)", parsed.path)
        if group_match:
            return f"facebook:group:{group_match.group(1)}:post:{group_match.group(2)}"
        return f"facebook:url:{_normalize_url(url)}"

    # Madlan
    m = re.search(r"/listings/([^/?#]+)", parsed.path)
    if m:
        return f"madlan:{m.group(1)}"

    # Yad2
    m = re.search(r"/realestate/item/[^/?#]+/([^/?#]+)", parsed.path)
    if m:
        return f"yad2:{m.group(1)}"

    return f"url:{_normalize_url(url)}"


def _listing_key_from_row(row: dict) -> str:
    """Extract listing key from a DB row or item dict."""
    listing_id = row.get("listing_id")
    if listing_id:
        return listing_id
    url = row.get("source_url") or row.get("url") or row.get("link") or ""
    return _listing_key_from_url(url)


def is_rejected(item: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    """Check if an item matches a PERMANENT user rejection.

    Returns (rejected, rejection_record).
    """
    # Try multiple URL fields — canonical_url is what reports use, href is what Yad2 uses
    for url_field in ("canonical_url", "url", "link", "href"):
        item_url = item.get(url_field) or ""
        if not item_url:
            continue
        item_key = _listing_key_from_url(item_url)

        rejections = load_rejections(classification="PERMANENT")

        # Try item's listing_id field first
        item_listing_id = item.get("listing_id")
        if item_listing_id:
            for r in rejections:
                if r.get("listing_id") == item_listing_id:
                    return True, r

        # Try exact key match
        for r in rejections:
            r_url = r.get("source_url", "")
            r_key = _listing_key_from_url(r_url)
            if r_key and r_key == item_key:
                return True, r

        # Try normalized URL match
        item_norm = _normalize_url(item_url)
        for r in rejections:
            r_norm = _normalize_url(r.get("source_url", ""))
            if r_norm and r_norm == item_norm:
                return True, r

    return False, None


def filter_rejected(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter out PERMANENT rejected items. Returns (kept, dropped)."""
    kept = []
    dropped = []
    for item in items:
        rejected, record = is_rejected(item)
        if rejected:
            item["_user_rejected"] = True
            item["_rejection_reason"] = record.get("reject_reason_text", "")
            dropped.append(item)
        else:
            kept.append(item)
    return kept, dropped


def add_rejection(
    url: str,
    reason_text: str,
    classification: str = "PERMANENT",
    category: str = "location",
    platform: str = "",
    price: float | None = None,
    rooms: float | None = None,
    entry: str = "",
) -> dict[str, Any]:
    """Add or update a user rejection in the DB."""
    listing_id = _listing_key_from_url(url)
    if not platform:
        if "facebook.com" in url:
            platform = "Facebook"
        elif "madlan.co.il" in url:
            platform = "Madlan"
        elif "yad2.co.il" in url:
            platform = "Yad2"
        else:
            platform = "unknown"

    with connect() as con:
        # Upsert by exact URL first, then by stable listing key / normalized URL.
        existing = con.execute(
            "SELECT id FROM user_rejects WHERE source_url = ? OR listing_id = ?",
            (url, listing_id),
        ).fetchone()
        if not existing:
            url_norm = _normalize_url(url)
            for row in con.execute("SELECT id, source_url FROM user_rejects").fetchall():
                row_url = row["source_url"] or ""
                if _listing_key_from_url(row_url) == listing_id or _normalize_url(row_url) == url_norm:
                    existing = row
                    break

        if existing:
            con.execute(
                """UPDATE user_rejects
                SET reject_reason_text=?, classification=?, category=?,
                    source_platform=?, original_price=?, original_entry=?,
                    last_seen_at=CURRENT_TIMESTAMP
                WHERE id=?""",
                (reason_text, classification, category, platform,
                 price, entry, existing["id"]),
            )
            record_id = existing["id"]
        else:
            cur = con.execute(
                """INSERT INTO user_rejects
                (source_url, source_platform, listing_id, reject_reason_text,
                 classification, category, original_price, original_entry)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, platform, listing_id, reason_text, classification,
                 category, price, entry),
            )
            record_id = cur.lastrowid

    return {
        "id": record_id,
        "url": url,
        "listing_id": listing_id,
        "reason": reason_text,
        "classification": classification,
        "category": category,
    }


def seed_known_rejections() -> int:
    """Seed the known rejections from conversation. Returns count added."""
    known = []
    count = 0
    for row in known:
        try:
            add_rejection(
                url=row[0],
                source=row[1],
                source_id=row[2],
                reason_text=row[3],
                classification=row[4],
                category=row[5] if len(row) > 5 else None,
                price=row[6] if len(row) > 6 else None,
                entry_raw=row[7] if len(row) > 7 else None,
            )
            count += 1
        except Exception:
            pass
    _log.info("seeded %d known rejections", count)
    return count


def list_rejections(classification: str | None = None) -> None:
    rows = load_rejections(classification)
    print(f"Total: {len(rows)} rejection(s)")
    for r in rows:
        print(f"  #{r['id']} | {r['source_platform']} | {r['classification']} | {r['category']}")
        print(f"    reason: {r['reject_reason_text']}")
        print(f"    url: {r['source_url']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage user rejections")
    sub = parser.add_subparsers(dest="cmd", required=True)

    list_cmd = sub.add_parser("list", help="List rejections")
    list_cmd.add_argument("--classification", choices=["PERMANENT", "TRANSIENT"])

    seed_cmd = sub.add_parser("seed", help="Seed known rejections from conversation")

    add_cmd = sub.add_parser("add", help="Add a rejection")
    add_cmd.add_argument("--url", required=True, help="Full source URL")
    add_cmd.add_argument("--reason", required=True, help="Reason text")
    add_cmd.add_argument("--classification", default="PERMANENT", choices=["PERMANENT", "TRANSIENT"])
    add_cmd.add_argument("--category", default="location", help="Category")
    add_cmd.add_argument("--platform", default="", help="Platform override")
    add_cmd.add_argument("--price", type=float, help="Original price")
    add_cmd.add_argument("--rooms", type=float, help="Original rooms")
    add_cmd.add_argument("--entry", default="", help="Original entry date")

    args = parser.parse_args()

    if args.cmd == "list":
        list_rejections(args.classification)

    elif args.cmd == "seed":
        count = seed_known_rejections()
        print(f"Seeded {count} rejection(s)")
        list_rejections()

    elif args.cmd == "add":
        rec = add_rejection(
            args.url,
            args.reason,
            args.classification,
            args.category,
            args.platform,
            args.price,
            args.rooms,
            args.entry,
        )
        print(json.dumps(rec, ensure_ascii=False, indent=2))
