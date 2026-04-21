#!/usr/bin/env python3
"""
Sheet writer — CLI for writing morning/night stars and season pass
to the Google Sheet from the MCP server.

Usage:
    python3 sheet_writer.py collect morning 2026-04-13
    python3 sheet_writer.py collect night 2026-04-13
    python3 sheet_writer.py season toggle 3 true
    python3 sheet_writer.py season toggle 3 false
"""

import json
import sys
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

from constants import (
    DAY_COL, ROW_MORNING_STAR, ROW_NIGHT_STAR, ROW_DAILY_TOTAL,
    ROW_SEASON_PASS, CORE_STAR_THRESHOLD,
    ROW_STEPS, ROW_SLEEP, ROW_NUTRITION, ROW_STRENGTH, ROW_CARDIO,
    ROW_SAUNA, ROW_STRETCH, DAILY_STEPS_GOAL,
    SLEEP_STAR_THRESHOLD_DEFAULT, SLEEP_STAR_THRESHOLD_LOW_ENERGY,
    LOW_ENERGY_PHASES, ROW_CYCLE,
)
from sheets import get_google_creds, resolve_spreadsheet_id, write_cell, read_cell, get_week_tab_name
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def _get_tab_and_col(target: date, creds, service):
    """Resolve the weekly tab name and column letter for a date.

    Handles both en-dash and plain dash tab names by searching
    for the actual tab name in the spreadsheet.
    """
    weekday = target.weekday()
    col = DAY_COL[weekday]
    monday = target - timedelta(days=weekday)
    sunday = monday + timedelta(days=6)
    # Anchor spreadsheet on the week's Monday so cross-month weeks stay in
    # one spreadsheet (matches oura_sheets_sync.py).
    spreadsheet_id = resolve_spreadsheet_id(monday, creds)
    expected_tab = get_week_tab_name(monday, sunday)

    # Search for actual tab name (might use en-dash – or plain dash -)
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    actual_tab = expected_tab
    # Normalize: strip dashes to find a match
    expected_norm = expected_tab.replace(" - ", " ").replace(" – ", " ").replace(" — ", " ")
    for s in metadata.get("sheets", []):
        title = s["properties"]["title"]
        title_norm = title.replace(" - ", " ").replace(" – ", " ").replace(" — ", " ")
        if title_norm == expected_norm:
            actual_tab = title
            break

    return spreadsheet_id, actual_tab, col, weekday


def _count_core_items_from_sheet(service, spreadsheet_id, tab_name, col):
    """Count how many of the 7 core mission items are done for a given day column."""
    count = 0
    # Steps >= 8000
    val = read_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_STEPS}")
    if val:
        try:
            if int(str(val).replace(",", "")) >= DAILY_STEPS_GOAL:
                count += 1
        except ValueError:
            pass
    # Sleep >= threshold
    val = read_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_SLEEP}")
    if val:
        try:
            sl = float(str(val).strip())
            # Check cycle phase for threshold
            cycle_val = read_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_CYCLE}") or ""
            phase = ""
            for p in LOW_ENERGY_PHASES:
                if p in str(cycle_val):
                    phase = p
                    break
            threshold = SLEEP_STAR_THRESHOLD_LOW_ENERGY if phase in LOW_ENERGY_PHASES else SLEEP_STAR_THRESHOLD_DEFAULT
            if sl >= threshold:
                count += 1
        except ValueError:
            pass
    # Calories (count as done if value exists — consistent with html_report.py)
    # The actual goal check happens in scoring.py for the dashboard display
    val = read_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_NUTRITION}")
    if val and str(val).strip():
        try:
            cal = int(str(val).strip().split()[0].replace(",", ""))
            if cal > 0:
                count += 1
        except (ValueError, IndexError):
            pass
    # Strength
    val = read_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_STRENGTH}")
    if val and str(val).strip():
        count += 1
    # Cardio
    val = read_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_CARDIO}")
    if val and str(val).strip():
        count += 1
    # Stretch
    val = read_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_STRETCH}")
    if val and str(val).strip():
        count += 1
    # Sauna
    val = read_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_SAUNA}")
    if val and str(val).strip():
        count += 1
    return count


def _compute_daily_total(service, spreadsheet_id, tab_name, col):
    """Compute 0-3 daily star total for a column."""
    total = 0
    # Morning star
    val = read_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_MORNING_STAR}")
    if val and str(val).strip() == "✓":
        total += 1
    # Core star (4 of 7)
    core_count = _count_core_items_from_sheet(service, spreadsheet_id, tab_name, col)
    if core_count >= CORE_STAR_THRESHOLD:
        total += 1
    # Night star
    val = read_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_NIGHT_STAR}")
    if val and str(val).strip() == "✓":
        total += 1
    return total


def collect_star(action: str, date_str: str):
    """Write a morning or night star to the sheet.

    Args:
        action: "morning" or "night"
        date_str: ISO date string (YYYY-MM-DD)
    """
    target = datetime.strptime(date_str, "%Y-%m-%d").date()
    creds = get_google_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    spreadsheet_id, tab_name, col, weekday = _get_tab_and_col(target, creds, service)

    if action == "morning":
        row = ROW_MORNING_STAR
        label = "☀️ Morning Star"
    elif action == "night":
        row = ROW_NIGHT_STAR
        label = "🌙 Night Star"
    elif action == "core":
        # Core items are already in the sheet — just recompute daily total
        row = None
        label = None
    else:
        print(json.dumps({"ok": False, "error": f"Unknown action: {action}"}))
        return

    # Write checkmark (skip for core — items already in sheet)
    if row is not None:
        write_cell(service, spreadsheet_id, tab_name, f"{col}{row}", "✓")

    # Ensure row label exists (skip for core)
    if row is not None:
        current_label = read_cell(service, spreadsheet_id, tab_name, f"A{row}")
        if not current_label or current_label.strip() == "":
            write_cell(service, spreadsheet_id, tab_name, f"A{row}", label)

    # Recompute daily total
    daily_total = _compute_daily_total(service, spreadsheet_id, tab_name, col)
    write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_DAILY_TOTAL}", str(daily_total))

    # Ensure daily total row label
    total_label = read_cell(service, spreadsheet_id, tab_name, f"A{ROW_DAILY_TOTAL}")
    if not total_label or total_label.strip() == "":
        write_cell(service, spreadsheet_id, tab_name, f"A{ROW_DAILY_TOTAL}", "⭐ Daily Stars")

    print(json.dumps({"ok": True, "action": action, "date": date_str, "daily_total": daily_total}))


def season_toggle(index: int, done: bool):
    """Toggle a season pass item in the sheet.

    Stores done indices as comma-separated string in B14 of the current month's sheet.

    Args:
        index: 0-based index of the season item
        done: True to mark done, False to unmark
    """
    today = date.today()
    creds = get_google_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # Find current week's tab (use today's week). Anchor on Monday so
    # cross-month weeks live in one spreadsheet.
    weekday = today.weekday()
    monday = today - timedelta(days=weekday)
    sunday = monday + timedelta(days=6)
    spreadsheet_id = resolve_spreadsheet_id(monday, creds)
    tab_name = get_week_tab_name(monday, sunday)

    # Read current done indices
    current = read_cell(service, spreadsheet_id, tab_name, f"B{ROW_SEASON_PASS}") or ""
    indices = set()
    if current.strip():
        for s in current.split(","):
            s = s.strip()
            if s.isdigit():
                indices.add(int(s))

    if done:
        indices.add(index)
    else:
        indices.discard(index)

    # Write back
    new_val = ",".join(str(i) for i in sorted(indices))
    write_cell(service, spreadsheet_id, tab_name, f"B{ROW_SEASON_PASS}", new_val)

    # Ensure label
    label = read_cell(service, spreadsheet_id, tab_name, f"A{ROW_SEASON_PASS}")
    if not label or label.strip() == "":
        write_cell(service, spreadsheet_id, tab_name, f"A{ROW_SEASON_PASS}", "📅 Season Pass")

    print(json.dumps({"ok": True, "done_count": len(indices), "indices": sorted(indices)}))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 sheet_writer.py collect morning|night YYYY-MM-DD")
        print("       python3 sheet_writer.py season toggle INDEX true|false")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "collect":
        collect_star(sys.argv[2], sys.argv[3])
    elif cmd == "season":
        idx = int(sys.argv[3])
        done_val = sys.argv[4].lower() == "true"
        season_toggle(idx, done_val)
    else:
        print(json.dumps({"ok": False, "error": f"Unknown command: {cmd}"}))
