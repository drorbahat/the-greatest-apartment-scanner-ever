#!/usr/bin/env python3
"""Simple robust apartment observations database for Yogev.

Design principle: every listing appearance is an observation. We only dedupe by
exact run_id + URL/source key; no fuzzy matching and no pretending different URLs
are the same apartment.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
from datetime import datetime
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts'
DEFAULT_DB = ART / 'apartments.db'
FULL_RUNS = ART / 'full_scan_runs'


def now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def load_json(path: pathlib.Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def connect(db_path: pathlib.Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: pathlib.Path | str = DEFAULT_DB) -> None:
    db_path = pathlib.Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as con:
        con.executescript(
            """
            create table if not exists scan_runs (
                run_id text primary key,
                scan_date text,
                generated_at text,
                status text,
                run_dir text,
                final_report text,
                assistant_brief text,
                manifest text,
                ingested_at text not null,
                raw_json text
            );

            create table if not exists observations (
                observation_key text primary key,
                run_id text not null,
                seen_at text not null,
                source text,
                source_key text,
                url text,
                title text,
                price real,
                rooms real,
                sqm real,
                floor text,
                entry text,
                verdict text,
                score real,
                pros_json text,
                flags_json text,
                missing_json text,
                followup_json text,
                description text,
                duplicate_policy text not null default 'exact_only',
                raw_json text not null,
                foreign key(run_id) references scan_runs(run_id)
            );

            create index if not exists idx_observations_run on observations(run_id);
            create index if not exists idx_observations_url on observations(url);
            create index if not exists idx_observations_source on observations(source);
            create index if not exists idx_observations_verdict on observations(verdict);
            create index if not exists idx_observations_price on observations(price);

            create table if not exists watchlist (
                url text primary key,
                status text not null,
                note text,
                created_at text not null,
                updated_at text not null
            );
            """
        )
        _ensure_column(con, 'scan_runs', 'generated_at', 'text')


def _ensure_column(con: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in con.execute(f'pragma table_info({table})')}
    if column not in cols:
        con.execute(f'alter table {table} add column {column} {decl}')


def _source_key(item: dict[str, Any]) -> str | None:
    explicit = item.get('source_key')
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    url = item.get('url')
    if isinstance(url, str) and url.strip():
        clean_url = url.strip()
        yad2_match = re.search(r'/realestate/item/[^/?#]+/([^/?#]+)', clean_url)
        if yad2_match:
            return f"yad2:{yad2_match.group(1)}"
        facebook_post_match = re.search(r'facebook\.com/groups/\d+/posts/(\d+)', clean_url)
        if facebook_post_match:
            return f"facebook:post:{facebook_post_match.group(1)}"
        return clean_url
    source = item.get('source') or 'unknown'
    title = item.get('title') or ''
    price = item.get('price')
    rooms = item.get('rooms')
    # Fallback key is only for idempotency inside the same run, not cross-run matching.
    return f"fallback::{source}::{title}::{price}::{rooms}"


def _observation_key(run_id: str, item: dict[str, Any], ordinal: int) -> str:
    key = _source_key(item)
    return f"{run_id}::{key or ordinal}"


def _as_json(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def _brief_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / 'assistant_brief.json'


def _manifest_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / 'manifest.json'


def _load_run_payload(run_dir: pathlib.Path) -> tuple[dict[str, Any], dict[str, Any]]:
    brief = load_json(_brief_path(run_dir), {}) or {}
    manifest = load_json(_manifest_path(run_dir), {}) or {}
    return brief, manifest


def _run_id_from_dir(run_dir: pathlib.Path, brief: dict[str, Any], manifest: dict[str, Any]) -> str:
    return str(manifest.get('run_id') or brief.get('run_id') or run_dir.name)


def _to_float(value: Any) -> float | None:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _items_from_brief(brief: dict[str, Any]) -> list[dict[str, Any]]:
    items = brief.get('top_candidates') or []
    return [x for x in items if isinstance(x, dict)]


def _evaluated_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir / 'evaluated_candidates.json'


def _items_from_evaluation(run_dir: pathlib.Path) -> list[dict[str, Any]] | None:
    """Load evaluated_candidates.json and map to DB-compatible items.
    Returns None if file doesn't exist (fallback to brief)."""
    path = _evaluated_path(run_dir)
    if not path.exists():
        return None
    data = load_json(path, {}) or {}
    items = data.get('items') or []
    if not items:
        return None
    mapped = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mapped.append({
            'source': item.get('source'),
            'source_key': item.get('source_key'),
            'url': item.get('canonical_url'),
            'title': item.get('title', ''),
            'price': item.get('price'),
            'rooms': item.get('rooms'),
            'sqm': item.get('sqm'),
            'entry': item.get('entry_raw') or item.get('entry_status'),
            'verdict': item.get('recommended_action', 'unknown'),
            'score': item.get('score', 0),
            'flags': item.get('flags', []),
            'missing': item.get('missing', []),
            'followup_needed': item.get('followup_question'),
            'description': item.get('processing_status'),
            'quality_status': item.get('quality_status'),
            'listing_type': item.get('listing_type'),
            'broker_status': item.get('broker_status'),
            'entry_status': item.get('entry_status'),
            'url_status': item.get('url_status'),
            'reject_reasons': item.get('reject_reasons', []),
            'recommended_action': item.get('recommended_action'),
            'raw_text': item.get('raw_text'),
        })
    return mapped


def ingest_run(run_dir: pathlib.Path | str, db_path: pathlib.Path | str = DEFAULT_DB) -> dict[str, Any]:
    run_dir = pathlib.Path(run_dir)
    db_path = pathlib.Path(db_path)
    init_db(db_path)
    brief, manifest = _load_run_payload(run_dir)
    run_id = _run_id_from_dir(run_dir, brief, manifest)
    scan_date = brief.get('scan_date') or manifest.get('scan_date')
    status = brief.get('status') or manifest.get('overall_status')
    # Prefer evaluation data over brief
    items = _items_from_evaluation(run_dir)
    source = 'evaluation'
    if items is None:
        items = _items_from_brief(brief)
        source = 'brief'
    inserted = 0

    with connect(db_path) as con:
        con.execute(
            """
            insert into scan_runs(run_id, scan_date, generated_at, status, run_dir, final_report, assistant_brief, manifest, ingested_at, raw_json)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(run_id) do update set
                scan_date=excluded.scan_date,
                generated_at=excluded.generated_at,
                status=excluded.status,
                run_dir=excluded.run_dir,
                final_report=excluded.final_report,
                assistant_brief=excluded.assistant_brief,
                manifest=excluded.manifest,
                raw_json=excluded.raw_json
            """,
            (
                run_id,
                scan_date,
                brief.get('generated_at') or manifest.get('finalized_at') or manifest.get('created_at'),
                status,
                str(run_dir),
                brief.get('final_report') or manifest.get('final_report'),
                str(_brief_path(run_dir)) if _brief_path(run_dir).exists() else None,
                str(_manifest_path(run_dir)) if _manifest_path(run_dir).exists() else None,
                now_iso(),
                json.dumps({'brief': brief, 'manifest': manifest}, ensure_ascii=False),
            ),
        )

        for ordinal, item in enumerate(items, start=1):
            source_key = _source_key(item)
            obs_key = _observation_key(run_id, item, ordinal)
            exists = con.execute('select 1 from observations where observation_key = ?', (obs_key,)).fetchone() is not None
            cur = con.execute(
                """
                insert into observations(
                    observation_key, run_id, seen_at, source, source_key, url, title,
                    price, rooms, sqm, floor, entry, verdict, score,
                    pros_json, flags_json, missing_json, followup_json, description,
                    duplicate_policy, raw_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'exact_only', ?)
                on conflict(observation_key) do update set
                    seen_at=excluded.seen_at,
                    source=excluded.source,
                    source_key=excluded.source_key,
                    url=excluded.url,
                    title=excluded.title,
                    price=excluded.price,
                    rooms=excluded.rooms,
                    sqm=excluded.sqm,
                    floor=excluded.floor,
                    entry=excluded.entry,
                    verdict=excluded.verdict,
                    score=excluded.score,
                    pros_json=excluded.pros_json,
                    flags_json=excluded.flags_json,
                    missing_json=excluded.missing_json,
                    followup_json=excluded.followup_json,
                    description=excluded.description,
                    duplicate_policy=excluded.duplicate_policy,
                    raw_json=excluded.raw_json
                """,
                (
                    obs_key,
                    run_id,
                    brief.get('generated_at') or now_iso(),
                    item.get('source'),
                    source_key,
                    item.get('url'),
                    item.get('title'),
                    _to_float(item.get('price')),
                    _to_float(item.get('rooms')),
                    _to_float(item.get('sqm')),
                    str(item.get('floor')) if item.get('floor') is not None else None,
                    item.get('entry'),
                    item.get('verdict'),
                    _to_float(item.get('score')),
                    _as_json(item.get('pros')),
                    _as_json(item.get('flags')),
                    _as_json(item.get('missing')),
                    _as_json(item.get('followup_needed')),
                    item.get('description'),
                    json.dumps(item, ensure_ascii=False),
                ),
            )
            inserted += 0 if exists else 1

    return {
        'run_id': run_id,
        'run_dir': str(run_dir),
        'observations_seen': len(items),
        'observations_inserted': inserted,
        'db': str(db_path),
        'ingest_source': source,
    }


def ingest_all(db_path: pathlib.Path | str = DEFAULT_DB, runs_dir: pathlib.Path | str = FULL_RUNS) -> dict[str, Any]:
    runs_dir = pathlib.Path(runs_dir)
    totals = []
    for run_dir in sorted([p for p in runs_dir.iterdir() if p.is_dir()] if runs_dir.exists() else []):
        if _brief_path(run_dir).exists():
            totals.append(ingest_run(run_dir, db_path))
    return {
        'runs_ingested': len(totals),
        'observations_inserted': sum(x['observations_inserted'] for x in totals),
        'runs': totals,
    }


def summary(db_path: pathlib.Path | str = DEFAULT_DB) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as con:
        runs_total = con.execute('select count(*) from scan_runs').fetchone()[0]
        observations_total = con.execute('select count(*) from observations').fetchone()[0]
        by_source = {r['source'] or 'unknown': r['n'] for r in con.execute('select source, count(*) n from observations group by source order by n desc')}
        by_verdict = {r['verdict'] or 'unknown': r['n'] for r in con.execute('select verdict, count(*) n from observations group by verdict order by n desc')}
        watchlist_total = con.execute('select count(*) from watchlist').fetchone()[0]
    return {
        'db': str(db_path),
        'runs_total': runs_total,
        'observations_total': observations_total,
        'watchlist_total': watchlist_total,
        'by_source': by_source,
        'by_verdict': by_verdict,
    }


def _ordered_run_ids(con: sqlite3.Connection) -> list[str]:
    return [
        r['run_id']
        for r in con.execute(
            """
            select run_id from scan_runs
            order by coalesce(generated_at, scan_date, ingested_at), run_id
            """
        )
    ]


def new_since_previous_run(db_path: pathlib.Path | str = DEFAULT_DB) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as con:
        run_ids = _ordered_run_ids(con)
        if len(run_ids) < 2:
            return []
        prev, latest = run_ids[-2], run_ids[-1]
        rows = con.execute(
            """
            select * from observations o
            where o.run_id = ?
              and coalesce(o.source_key, o.url) not in (
                select coalesce(source_key, url) from observations where run_id = ?
              )
            order by case verdict when 'כן' then 0 when 'אולי' then 1 else 2 end, score desc, price asc
            """,
            (latest, prev),
        ).fetchall()
    return [_row_to_observation_dict(r) for r in rows]


def _row_to_observation_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        'run_id': row['run_id'],
        'source': row['source'],
        'url': row['url'],
        'title': row['title'],
        'price': row['price'],
        'rooms': row['rooms'],
        'sqm': row['sqm'],
        'entry': row['entry'],
        'verdict': row['verdict'],
        'flags': json.loads(row['flags_json'] or '[]'),
        'missing': json.loads(row['missing_json'] or '[]'),
        'description': row['description'],
    }


def recent_observations(db_path: pathlib.Path | str = DEFAULT_DB, *, limit: int = 10) -> list[dict[str, Any]]:
    """Return latest observations, ordered by run time and practical interest."""
    init_db(db_path)
    with connect(db_path) as con:
        rows = con.execute(
            """
            select o.* from observations o
            join scan_runs r on r.run_id = o.run_id
            order by
                coalesce(r.generated_at, r.scan_date, r.ingested_at) desc,
                case o.verdict when 'כן' then 0 when 'אולי' then 1 else 2 end,
                o.score desc,
                o.price asc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_observation_dict(r) for r in rows]


def watch(db_path: pathlib.Path | str = DEFAULT_DB, *, url: str, status: str, note: str | None = None) -> None:
    init_db(db_path)
    ts = now_iso()
    with connect(db_path) as con:
        con.execute(
            """
            insert into watchlist(url, status, note, created_at, updated_at)
            values (?, ?, ?, ?, ?)
            on conflict(url) do update set
                status=excluded.status,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (url, status, note, ts, ts),
        )


def list_watchlist(db_path: pathlib.Path | str = DEFAULT_DB) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as con:
        rows = con.execute('select * from watchlist order by updated_at desc').fetchall()
    return [dict(r) for r in rows]


def write_delta_report(db_path: pathlib.Path | str = DEFAULT_DB, out_path: pathlib.Path | str | None = None) -> pathlib.Path:
    """Write a simple Markdown report of exact-URL-new observations only."""
    db_path = pathlib.Path(db_path)
    items = new_since_previous_run(db_path)
    if out_path is None:
        out_path = ART / 'db_delta_latest.md'
    out_path = pathlib.Path(out_path)
    lines = [
        '# דוח חדש מאז הריצה הקודמת',
        '',
        'מדיניות: חדש = URL/source-key שלא הופיע בריצה הקודמת. אין fuzzy matching.',
        '',
        f'- נצפו חדשים: {len(items)}',
        '',
    ]
    if not items:
        lines.append('אין פריטים חדשים ודאיים ביחס לריצה הקודמת.')
    for i, item in enumerate(items, 1):
        price = f"₪{int(item['price']):,}" if item.get('price') else 'לא צוין'
        rooms = item.get('rooms') if item.get('rooms') is not None else 'לא צוין'
        sqm = item.get('sqm') if item.get('sqm') is not None else 'לא צוין'
        flags = ', '.join(item.get('flags') or []) or 'אין דגל ברור'
        lines.extend([
            f"## {i}. {item.get('title') or 'ללא כותרת'}",
            f"- מקור: {item.get('source') or 'לא צוין'}",
            f"- לינק: {item.get('url') or 'אין לינק'}",
            f"- מחיר / חדרים / מ״ר: {price} / {rooms} / {sqm}",
            f"- כניסה: {item.get('entry') or 'לא צוין'}",
            f"- verdict: {item.get('verdict') or 'לא צוין'}",
            f"- דגלים: {flags}",
            '',
        ])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text('\n'.join(lines), encoding='utf-8')
    return out_path


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description='Simple robust apartment observations DB')
    ap.add_argument('--db', default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('init')

    ingest = sub.add_parser('ingest-run')
    ingest.add_argument('run_dir')

    sub.add_parser('ingest-all')
    sub.add_parser('summary')
    recent = sub.add_parser('recent')
    recent.add_argument('--limit', type=int, default=10)
    sub.add_parser('new-since-last-run')
    delta = sub.add_parser('delta-report')
    delta.add_argument('--out')

    w = sub.add_parser('watch')
    w.add_argument('url')
    w.add_argument('--status', required=True, choices=['interesting', 'contacted', 'visit_planned', 'rejected', 'too_early', 'broker'])
    w.add_argument('--note')

    sub.add_parser('watchlist')

    args = ap.parse_args()
    db_path = pathlib.Path(args.db)

    if args.cmd == 'init':
        init_db(db_path)
        print_json({'status': 'ok', 'db': str(db_path)})
    elif args.cmd == 'ingest-run':
        print_json(ingest_run(args.run_dir, db_path))
    elif args.cmd == 'ingest-all':
        print_json(ingest_all(db_path))
    elif args.cmd == 'summary':
        print_json(summary(db_path))
    elif args.cmd == 'recent':
        print_json(recent_observations(db_path, limit=args.limit))
    elif args.cmd == 'new-since-last-run':
        print_json(new_since_previous_run(db_path))
    elif args.cmd == 'delta-report':
        path = write_delta_report(db_path, args.out)
        print_json({'status': 'ok', 'report': str(path)})
    elif args.cmd == 'watch':
        watch(db_path, url=args.url, status=args.status, note=args.note)
        print_json({'status': 'ok', 'url': args.url})
    elif args.cmd == 'watchlist':
        print_json(list_watchlist(db_path))


if __name__ == '__main__':
    main()
