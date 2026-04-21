#!/usr/bin/env python3
"""
Sheet reader — CLI for reading season pass state from Google Sheet.

Usage:
    python3 sheet_reader.py season
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

from constants import ROW_SEASON_PASS
from sheets import get_google_creds, resolve_spreadsheet_id, read_cell, get_week_tab_name
from googleapiclient.discovery import build


def _find_tab(service, spreadsheet_id, expected_tab):
    """Find actual tab name handling en-dash vs plain dash."""
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    expected_norm = expected_tab.replace(" - ", " ").replace(" \u2013 ", " ").replace(" \u2014 ", " ")
    for s in metadata.get("sheets", []):
        title = s["properties"]["title"]
        title_norm = title.replace(" - ", " ").replace(" \u2013 ", " ").replace(" \u2014 ", " ")
        if title_norm == expected_norm:
            return title
    return expected_tab


def read_season():
    """Read season pass done indices from sheet."""
    creds = get_google_creds()
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    # Anchor on Monday so cross-month weeks stay in one spreadsheet.
    sid = resolve_spreadsheet_id(monday, creds)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    expected_tab = get_week_tab_name(monday, sunday)
    tab = _find_tab(service, sid, expected_tab)

    raw = read_cell(service, sid, tab, f"B{ROW_SEASON_PASS}") or ""
    indices = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    print(json.dumps({"ok": True, "indices": indices}))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "season":
        read_season()
    else:
        print(json.dumps({"ok": False, "error": "Usage: python3 sheet_reader.py season"}))
