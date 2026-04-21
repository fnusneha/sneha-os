#!/usr/bin/env python3
"""
Oura Ring → Google Sheets daily sync — orchestrator.

This is the CLI entry point. It parses arguments, drives the backfill /
single-date / steps-left flows, and delegates to focused modules:

    constants.py   — shared constants (rows, goals, colors, types)
    api_clients.py — Oura, Garmin, Google Calendar API calls
    sheets.py      — Google Sheets read/write/create operations
    cycle.py       — menstrual cycle phase logic & highlighting
    scoring.py     — weekly challenge star calculation
    report.py      — morning report & steps report generation
    html_report.py — Quest Hub HTML report builder

Usage:
    python oura_sheets_sync.py                        # sync yesterday
    python oura_sheets_sync.py --date 2026-03-10      # sync specific date
    python oura_sheets_sync.py --morning              # backfill all missed days
    python oura_sheets_sync.py --morning --force      # force re-sync today
    python oura_sheets_sync.py --morning --emit-html  # MCP server path
    python oura_sheets_sync.py --steps-left           # weekly steps report
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, timedelta, datetime

from dotenv import load_dotenv
from googleapiclient.discovery import build

from constants import (
    SCRIPT_DIR, LOG_DIR, LOG_FILE, LAST_SYNC_FILE,
    ROW_DATE_NUM, ROW_STRENGTH, ROW_CARDIO, ROW_STRETCH, ROW_STEPS,
    ROW_SLEEP, ROW_NUTRITION, ROW_CYCLE, ROW_NOTES,
    DAY_COL,
)
from api_clients import (
    fetch_sleep, fetch_steps, fetch_cycle_day,
    fetch_nutrition, fetch_garmin_activities,
    fetch_week_calendar_notes,
)
from sheets import (
    get_google_creds, resolve_spreadsheet_id,
    find_or_create_tab, write_cell, set_cell_font_size,
    ensure_nutrition_row_label, ensure_cycle_row_label,
)
from cycle import get_cycle_phase, highlight_active_phase, get_dominant_cycle_day
from report import generate_morning_report, steps_left_report
from html_report import generate_html_report

# ═══════════════════════════════════════════════════════════════════
# Bootstrap (paths, env, logging)
# ═══════════════════════════════════════════════════════════════════
load_dotenv(SCRIPT_DIR / ".env")

LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

OURA_TOKEN = os.getenv("OURA_TOKEN")


# ═══════════════════════════════════════════════════════════════════
# Last-sync state persistence
# ═══════════════════════════════════════════════════════════════════

def read_last_sync() -> date | None:
    """Read the last synced date from the state file.

    Returns:
        The last synced date, or None if no state file exists.
    """
    if not LAST_SYNC_FILE.exists():
        return None
    try:
        data = json.loads(LAST_SYNC_FILE.read_text())
        return datetime.strptime(data["last_sync_date"], "%Y-%m-%d").date()
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def write_last_sync(d: date) -> None:
    """Write the last synced date to the state file.

    Args:
        d: The date to record.
    """
    LAST_SYNC_FILE.write_text(json.dumps({"last_sync_date": d.isoformat()}))


# ═══════════════════════════════════════════════════════════════════
# Single-day sync
# ═══════════════════════════════════════════════════════════════════

def sync_single_day(target: date, service, creds) -> bool:
    """Sync one day's Oura/Garmin data to the Google Sheet.

    Fetches sleep, steps, cycle, nutrition, and activity data, then
    writes each to the appropriate cell in the weekly tab. Also
    computes the daily challenge star count.

    Args:
        target: The date to sync.
        service: Google Sheets API service.
        creds: Google OAuth2 credentials.
        skip_scoreboard: If True, skip writing the challenge scoreboard
            (used during backfill to avoid redundant writes).

    Returns:
        True if any data was written, False if no data available.
    """
    day_str = target.isoformat()
    weekday = target.weekday()

    col = DAY_COL[weekday]
    log.info("Syncing %s (%s → column %s)", day_str, target.strftime("%A"), col)

    # Fetch Oura data
    sleep_hrs = fetch_sleep(day_str)
    steps = fetch_steps(day_str)
    cycle_day = fetch_cycle_day(day_str, creds)

    if sleep_hrs is None and steps is None and cycle_day is None:
        log.warning("No Oura data for %s — skipping.", day_str)
        return False

    # Find or create the weekly tab. Anchor the spreadsheet on the week's
    # Monday so cross-month weeks (e.g. Apr 27 – May 03) live in ONE
    # spreadsheet instead of being split across two.
    monday = target - timedelta(days=weekday)
    sunday = monday + timedelta(days=6)
    spreadsheet_id = resolve_spreadsheet_id(monday, creds)
    tab_name = find_or_create_tab(service, spreadsheet_id, monday, sunday)

    # Write date number
    write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_DATE_NUM}", target.day)

    # Write sleep
    if sleep_hrs is not None:
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_SLEEP}", sleep_hrs)
    else:
        log.info("No sleep data — skipping row %d", ROW_SLEEP)

    # Write steps
    if steps is not None:
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_STEPS}", steps)
    else:
        log.info("No step data — skipping row %d", ROW_STEPS)

    # Write strength & cardio activities (Garmin)
    activities = fetch_garmin_activities(target)
    # Write activities (concatenate if multiple in one day)
    if activities["strength"]:
        parts = [f"💪 {s['duration_min']}m" for s in activities["strength"]]
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_STRENGTH}", " + ".join(parts))
    else:
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_STRENGTH}", "")
    if activities["cardio"]:
        parts = []
        for c in activities["cardio"]:
            type_key = c.get("name", "").lower()
            icon = "🚴" if any(k in type_key for k in ["cycling", "biking", "bike"]) else "🏃‍♀️"
            mi = c["distance_mi"]
            parts.append(f"{icon} {mi}mi" if mi else f"{icon} {c['duration_min']}m")
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_CARDIO}", " + ".join(parts))
    else:
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_CARDIO}", "")
    if activities["stretch"]:
        parts = [f"🧘 {st['duration_min']}m" for st in activities["stretch"]]
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_STRETCH}", " + ".join(parts))
    else:
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_STRETCH}", "")

    # Write nutrition (Garmin/MFP)
    nutrition = fetch_nutrition(target)
    if nutrition:
        ensure_nutrition_row_label(service, spreadsheet_id, tab_name)
        nutr_text = f"{nutrition['calories']}"
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_NUTRITION}", nutr_text)
    else:
        log.info("No nutrition data — skipping row %d", ROW_NUTRITION)

    # Write cycle phase + highlight guide
    if cycle_day is not None:
        ensure_cycle_row_label(service, spreadsheet_id, tab_name)
        phase = get_cycle_phase(cycle_day)
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_CYCLE}",
                   f"{phase} D{cycle_day}")
        dominant_day = get_dominant_cycle_day(service, spreadsheet_id, tab_name)
        if dominant_day is not None:
            highlight_active_phase(service, spreadsheet_id, tab_name, dominant_day)
    else:
        log.info("No cycle data — skipping row %d", ROW_CYCLE)

    # Write weekly calendar notes
    notes = fetch_week_calendar_notes(monday, sunday, creds)
    if notes:
        write_cell(service, spreadsheet_id, tab_name, f"B{ROW_NOTES}", notes)
        set_cell_font_size(service, spreadsheet_id, tab_name,
                           ROW_NOTES, 1, 2, 9)
    else:
        log.info("No notable calendar events for the week")

    log.info("✓ Synced %s → tab '%s'", day_str, tab_name)
    return True


# ═══════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════

def main():
    if not OURA_TOKEN:
        log.error("OURA_TOKEN not set in .env")
        sys.exit(1)

    parser = argparse.ArgumentParser(
        description="Oura Ring → Google Sheets daily sync"
    )
    parser.add_argument("--date", help="Sync a specific date (YYYY-MM-DD)")
    parser.add_argument("--morning", action="store_true",
                        help="Good morning! Backfill all days since last sync")
    parser.add_argument("--steps-left", action="store_true",
                        help="Show steps remaining for this week")
    parser.add_argument("--force", action="store_true",
                        help="Force re-sync today even if already up to date")
    parser.add_argument("--emit-html", action="store_true",
                        help="Print HTML report to stdout (for MCP server / mobile)")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Force fresh pulls from Google Doc + Travel Sheet (bust cache)")
    args = parser.parse_args()

    # ── refresh-cache: clear cached data ──
    if args.refresh_cache:
        from habit_source import clear_cache as clear_habits
        from travel_source import clear_cache as clear_travel
        clear_habits()
        clear_travel()
        log.info("🔄 Cache cleared — next sync will fetch fresh data")
        if not args.morning and not args.date:
            print("\n  🔄 Cache cleared! Run with --morning --force to sync fresh.\n")
            return

    # ── steps-left: read-only report ──
    if args.steps_left:
        steps_left_report()
        return

    # ── morning: backfill mode ──
    if args.morning:
        log.info("=" * 50)
        log.info("☀️  Good morning! Starting backfill sync")

        last = read_last_sync()
        today = date.today()

        if last is None:
            start_date = today - timedelta(days=1)
            log.info("First run (no sync history) — syncing yesterday only")
        else:
            yesterday = today - timedelta(days=1)
            start_date = min(last + timedelta(days=1), yesterday)
            if start_date > today:
                if args.force:
                    start_date = today
                    log.info("Force re-sync for today (%s)", today)
                else:
                    log.info("Already synced through %s — nothing new!", last)
                    print(f"\n  ✅ Already up to date! (last sync: {last})\n")
                    return
            log.info("Last sync: %s — backfilling %s → %s",
                     last, start_date, today)

        creds = get_google_creds()
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        days_synced = 0
        current = start_date

        while current <= today:
            sync_single_day(current, service, creds)
            days_synced += 1
            if current < today:
                time.sleep(0.5)  # be kind to APIs
            current += timedelta(days=1)

        write_last_sync(today)
        log.info("☀️  Backfill complete! Synced %d day(s)", days_synced)

        # Generate the pretty morning report. Anchor on Monday so cross-month
        # weeks still find their data in one spreadsheet.
        monday = today - timedelta(days=today.weekday())
        spreadsheet_id = resolve_spreadsheet_id(monday, creds)
        result = generate_morning_report(service, spreadsheet_id, creds)
        if result:
            report, report_data = result
            if args.emit_html:
                os.environ["OURA_EMIT_HTML"] = "1"
                html = generate_html_report(report_data)
                print(report)
                print("<!--HTML_REPORT_START-->")
                print(html)
            else:
                print(report)
                generate_html_report(report_data)
        else:
            print(f"\n  ☀️  Good morning! Synced {days_synced} day(s) "
                  f"({start_date} → {today})\n")
        return

    # ── single date mode (default: yesterday) ──
    log.info("=" * 50)
    log.info("Oura → Sheets sync starting")

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target = date.today() - timedelta(days=1)

    creds = get_google_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    sync_single_day(target, service, creds)
    write_last_sync(target)


if __name__ == "__main__":
    main()
