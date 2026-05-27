#!/usr/bin/env python3
"""Orchestrate the full daily Facebook apartment-scan pipeline.

Run this script to:
1. Scan multiple Facebook groups for new posts
2. Clean and deduplicate the posts
3. Prepare for AI triage (check cache)
4. Report on new posts needing triage
5. Generate summary report

Can be called from cron or imported for programmatic use.
"""
import argparse, json, pathlib, subprocess, sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts' / 'facebook'
ART.mkdir(parents=True, exist_ok=True)
RUN_HISTORY = ART / 'run_history.json'

DEFAULT_MODEL = 'auto-triage'
FB_TRIAGE_LIMIT = 50


def load_run_history():
    if not RUN_HISTORY.exists():
        return {'runs': []}
    return json.loads(RUN_HISTORY.read_text(encoding='utf-8'))


def save_run_history(entry):
    history = load_run_history()
    history['runs'].append(entry)
    history['last_run'] = entry.get('timestamp')
    RUN_HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')


def get_latest_clean_output():
    """Find the most recent clean_posts JSON file."""
    pattern = 'clean_posts_*.json'
    matches = sorted(ART.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def get_latest_triage_cache():
    cache_path = ART / 'triage_cache.json'
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding='utf-8'))
    return None


def run_feed_scan(scrolls=15, delay=2.6, use_llm=True):
    """Step 1: Run facebook_feed_multi_scan.py"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = ART / f'multi_feed_scan_{timestamp}.json'
    cmd = [
        sys.executable,
        str(ROOT / 'scripts' / 'facebook_feed_multi_scan.py'),
        '--scrolls', str(scrolls),
        '--delay', str(delay),
        '--out', str(out_path),
    ]
    if use_llm:
        cmd.append('--llm')
    print(f"STEP 1: Scanning feeds (scrolls={scrolls}, llm={use_llm})...", flush=True)
    with open(out_path.with_suffix('.log'), 'w') as log_file:
        result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr, cwd=ROOT)
    if result.returncode != 0:
        raise RuntimeError(f"Feed scan failed: {result.returncode}")
    return out_path


def run_clean_posts(input_path):
    """Step 2: Run facebook_clean_posts.py"""
    prefix = input_path.stem.replace('multi_feed_scan_', 'clean_posts_')
    out_prefix = ART / prefix
    cmd = [
        sys.executable,
        str(ROOT / 'scripts' / 'facebook_clean_posts.py'),
        str(input_path),
        '--out-prefix', str(out_prefix),
    ]
    print(f"STEP 2: Cleaning posts...", flush=True)
    result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr, cwd=ROOT)
    if result.returncode != 0:
        raise RuntimeError(f"Clean posts failed: {result.returncode}")
    return out_prefix.with_suffix('.json')


def run_prepare_ai(clean_json_path, model=DEFAULT_MODEL, *, batch=0, limit=FB_TRIAGE_LIMIT):
    """Step 3: Run facebook_ai_triage_prepare.py"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    batch_suffix = f'_b{batch}' if batch else ''
    out_path = ART / f'ai_triage_input_{timestamp}{batch_suffix}.json'
    prompt_out = ART / f'ai_triage_prompt_{timestamp}{batch_suffix}.md'
    output_json = ART / f'ai_triage_next_{timestamp}{batch_suffix}.json'
    output_md = ART / f'ai_triage_next_{timestamp}{batch_suffix}.md'

    cmd = [
        sys.executable,
        str(ROOT / 'scripts' / 'facebook_ai_triage_prepare.py'),
        str(clean_json_path),
        '--model', model,
        '--out', str(out_path),
        '--prompt-out', str(prompt_out),
        '--output-json', str(output_json),
        '--output-md', str(output_md),
        '--mode', 'smart',
        '--limit', str(limit),
    ]
    if batch > 0:
        cmd += ['--batch', str(batch)]
    print(f"STEP 3: Preparing AI triage (model={model}, batch={batch or 'all'}, limit={limit})...", flush=True)
    result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr, cwd=ROOT)
    if result.returncode != 0:
        raise RuntimeError(f"Prepare AI triage failed: {result.returncode}")
    return out_path, prompt_out, output_json, output_md


def run_ai_triage_batches(clean_json_path, model=DEFAULT_MODEL, skip_ai=False):
    """Prepare and process all Facebook AI triage batches."""
    first_input_path, first_prompt_path, first_output_json, first_output_md = run_prepare_ai(
        clean_json_path,
        model=model,
        batch=1,
        limit=FB_TRIAGE_LIMIT,
    )
    first_data = json.loads(first_input_path.read_text(encoding='utf-8'))
    selection = first_data.get('selection', {})
    total_batches = max(1, int(selection.get('total_batches') or 1))

    batch_runs = []
    total_selected = 0
    total_skipped = int(selection.get('skipped') or 0)
    total_eligible = int(selection.get('eligible_for_ai') or 0)
    raw_clean_posts = int(selection.get('raw_clean_posts') or 0)
    cached_items_available = int(selection.get('cached_items_available') or 0)
    last_prompt_path = first_prompt_path
    last_output_json = first_output_json
    last_output_md = first_output_md
    last_input_path = first_input_path

    for batch in range(1, total_batches + 1):
        if batch == 1:
            ai_input_path = first_input_path
            prompt_path = first_prompt_path
            output_json = first_output_json
            output_md = first_output_md
            data = first_data
        else:
            ai_input_path, prompt_path, output_json, output_md = run_prepare_ai(
                clean_json_path,
                model=model,
                batch=batch,
                limit=FB_TRIAGE_LIMIT,
            )
            data = json.loads(ai_input_path.read_text(encoding='utf-8'))

        sel = data.get('selection', {})
        selected = int(sel.get('selected_for_ai') or 0)
        skipped = int(sel.get('skipped') or 0)
        total_selected += selected
        last_prompt_path = prompt_path
        last_output_json = output_json
        last_output_md = output_md
        last_input_path = ai_input_path
        batch_runs.append({
            'batch': batch,
            'input_json': str(ai_input_path),
            'prompt_out': str(prompt_path),
            'output_json': str(output_json),
            'output_md': str(output_md),
            'selection': sel,
        })

        if skip_ai:
            continue

        if selected == 0:
            print(f"STEP 4: Batch {batch}/{total_batches} has no posts for triage.", flush=True)
            continue

        print(f"STEP 4: Auto-triaging batch {batch}/{total_batches} with {selected} posts...", flush=True)
        triage_cmd = [
            sys.executable,
            str(ROOT / 'scripts' / 'facebook_auto_triage.py'),
            str(ai_input_path),
            '--out', str(output_json),
            '--out-md', str(output_md),
        ]
        triage_result = subprocess.run(triage_cmd, stdout=sys.stdout, stderr=sys.stderr, cwd=ROOT)
        if triage_result.returncode != 0:
            raise RuntimeError(f"Auto triage failed for batch {batch}: {triage_result.returncode}")
        run_update_cache(output_json, ai_input_path, model='auto-triage')

    aggregate_path = ART / f'ai_triage_aggregate_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    aggregate_payload = {
        'generated_at': datetime.now().isoformat(),
        'source': str(clean_json_path),
        'cache': str(ART / 'triage_cache.json'),
        'target_model': model,
        'purpose': 'Semantic AI triage of cleaned Facebook apartment posts for apartment search',
        'criteria': first_data.get('criteria') or {},
        'prompt_out': str(last_prompt_path),
        'output_json': str(last_output_json),
        'output_md': str(last_output_md),
        'selection': {
            'mode': selection.get('mode', 'smart'),
            'raw_clean_posts': raw_clean_posts,
            'eligible_for_ai': total_eligible,
            'selected_for_ai': total_selected,
            'skipped': total_skipped,
            'cached_items_available': cached_items_available,
            'include_cached': bool(selection.get('include_cached')),
            'batch': 'all',
            'batch_size': FB_TRIAGE_LIMIT,
            'total_batches': total_batches,
        },
        'batches': batch_runs,
        'posts': [],
    }
    aggregate_path.write_text(json.dumps(aggregate_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return aggregate_path, last_prompt_path, last_output_json, last_output_md, aggregate_payload


def run_update_cache(triage_json_path, input_json_path, model=DEFAULT_MODEL):
    """Step 5: Run facebook_ai_triage_cache_update.py if triage output exists"""
    cmd = [
        sys.executable,
        str(ROOT / 'scripts' / 'facebook_ai_triage_cache_update.py'),
        str(triage_json_path),
        '--input-json', str(input_json_path),
        '--model', model,
    ]
    print(f"STEP 5: Updating triage cache...", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    print(result.stdout, flush=True)
    if result.stderr:
        print("STDERR:", result.stderr, flush=True)
    if result.returncode != 0:
        raise RuntimeError(f"Cache update failed: {result.returncode}")


def generate_summary_report(scan_json, clean_json, ai_input_json, report_path):
    """Step 6: Generate daily summary report in markdown"""
    scan_data = json.loads(scan_json.read_text(encoding='utf-8'))
    clean_data = json.loads(clean_json.read_text(encoding='utf-8'))
    ai_data = json.loads(ai_input_json.read_text(encoding='utf-8'))

    cache = get_latest_triage_cache()
    yes_maybe_count = 0
    if cache:
        for item in cache.get('items', {}).values():
            if item.get('verdict') in ('yes', 'maybe'):
                yes_maybe_count += 1

    md_lines = [
        f'# Facebook Apartment Scan - Daily Report',
        f'',
        f'**Date/Time:** {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
        f'',
        f'## Scan Summary',
        f'- Groups scanned: {len(scan_data.get("groups", []))}',
        f'- Total posts found: {scan_data.get("items_total", 0)}',
        f'- Candidates after filtering: {scan_data.get("candidates_total", 0)}',
        f'- Clean posts (deduped): {clean_data.get("clean_items", 0)}',
        f'- Duplicates collapsed: {clean_data.get("duplicates_collapsed", 0)}',
        f'',
        f'## AI Triage',
        f'- Posts selected for AI: {ai_data.get("selection", {}).get("selected_for_ai", 0)}',
        f'- Posts skipped (cached/filtered): {ai_data.get("selection", {}).get("skipped", 0)}',
        f'- Cached items: {ai_data.get("selection", {}).get("cached_items_available", 0)}',
        f'- Cached yes/maybe leads: {yes_maybe_count}',
        f'',
        f'## Top Leads (from cache)',
        f'',
    ]

    if cache:
        top_leads = [item for item in cache.get('items', {}).values() if item.get('verdict') in ('yes', 'maybe')]
        top_leads.sort(key=lambda x: (x.get('verdict') != 'yes', x.get('universal_post_url') or x.get('post_url') or ''))
        for lead in top_leads[:15]:
            ex = lead.get('extracted', {})
            md_lines.append(f"- **{lead.get('verdict').upper()}** {lead.get('reason_short', '')}")
            md_lines.append(f"  - Price: {ex.get('price') or '?'} | Rooms: {ex.get('rooms') or '?'} | Sqm: {ex.get('sqm') or '?'} | Area: {ex.get('area') or '?'}")
            md_lines.append(f"  - URL: {lead.get('universal_post_url') or 'אין לינק ישיר לפוסט'}")
            md_lines.append('')
    else:
        md_lines.append('No cached leads yet.', '')

    md_lines.extend([
        f'## Recommended Actions',
        f'',
    ])

    needs_triage = ai_data.get('selection', {}).get('selected_for_ai', 0)
    if needs_triage > 0:
        md_lines.append(f"- ⚠️ {needs_triage} posts need AI triage. Run sub-agent with model `{ai_data.get('target_model', DEFAULT_MODEL)}` using `{ai_data.get('prompt_out', 'N/A')}`.")
    else:
        md_lines.append(f"- ✅ No new posts need AI triage.")

    if yes_maybe_count > 0:
        md_lines.append(f"- 📋 Review {yes_maybe_count} yes/maybe leads from cache.")
    md_lines.append(f"- Check `{ai_data.get('prompt_out', 'N/A')}` for full AI triage prompt.")

    report_path.write_text('\n'.join(md_lines), encoding='utf-8')
    return report_path


def daily_run(scrolls=15, delay=2.6, model=DEFAULT_MODEL, skip_ai=False, use_llm=True):
    """Run the full daily pipeline."""
    start = datetime.now()
    timestamp = start.strftime('%Y%m%d_%H%M%S')

    print("=" * 60, flush=True)
    print(f"Daily Scan Pipeline - {start.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("=" * 60, flush=True)

    try:
        scan_path = run_feed_scan(scrolls=scrolls, delay=delay, use_llm=use_llm)
        clean_path = run_clean_posts(scan_path)

        ai_input_path, prompt_path, output_json, output_md, ai_summary = run_ai_triage_batches(
            clean_path,
            model=model,
            skip_ai=skip_ai,
        )
        needs_triage = int(ai_summary.get('selection', {}).get('selected_for_ai', 0) or 0)
        report_path = ART / f'daily_report_{start.strftime("%Y%m%d")}.md'

        # Always write a report; the summary object is already aggregated across batches.
        report_path = generate_summary_report(scan_path, clean_path, ai_input_path, report_path)

        if needs_triage > 0 and skip_ai:
            print(f"STEP 4: ⚠️ {needs_triage} posts collected and prepared, but AI triage was skipped.", flush=True)
            print(f"   Prompt: {prompt_path}", flush=True)
            print(f"   Output: {output_json}, {output_md}", flush=True)

        status = 'awaiting_ai_triage' if needs_triage > 0 and skip_ai else 'completed'

        run_entry = {
            'timestamp': start.isoformat(),
            'scan_file': str(scan_path),
            'clean_file': str(clean_path),
            'ai_input_file': str(ai_input_path),
            'prompt_file': str(prompt_path),
            'triage_output_json': str(output_json),
            'triage_output_md': str(output_md),
            'report_file': str(report_path),
            'model': model,
            'needs_triage': needs_triage,
            'skip_ai': skip_ai,
            'status': status,
            'ai_summary': ai_summary,
        }
        save_run_history(run_entry)

        print("=" * 60, flush=True)
        print(f"Pipeline completed. Report: {report_path}", flush=True)
        if needs_triage > 0 and skip_ai:
            print("⚠️ ACTION REQUIRED: Run AI triage sub-agent.", flush=True)
        print("=" * 60, flush=True)

        return run_entry

    except Exception as e:
        print(f"ERROR: {repr(e)}", flush=True)
        run_entry = {
            'timestamp': start.isoformat(),
            'status': 'failed',
            'error': str(e),
        }
        save_run_history(run_entry)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scrolls', type=int, default=15, help='Number of scrolls per group')
    ap.add_argument('--delay', type=float, default=2.6, help='Delay between scrolls')
    ap.add_argument('--model', default=DEFAULT_MODEL, help='AI model for triage')
    ap.add_argument('--skip-ai', action='store_true', help='Skip AI triage step (no-op if no new posts)')
    ap.add_argument('--no-llm', action='store_true', help='Disable LLM field extraction (regex only)')
    args = ap.parse_args()

    result = daily_run(scrolls=args.scrolls, delay=args.delay, model=args.model, skip_ai=args.skip_ai, use_llm=not args.no_llm)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
