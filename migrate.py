#!/usr/bin/env python3
"""
migrate.py — One-shot backfill from Google Sheets + rides_cache.json → Neon.

Reads every weekly tab from the old "Week Accountability" spreadsheets
(March + April 2026) and inserts matching rows into daily_entries.
Reads rides_cache.json and bulk-inserts into rides.

Safe to re-run: uses ON CONFLICT DO UPDATE everywhere. Does NOT delete
anything. Keeps the source sheet intact as a cold archive.

Usage:
    python migrate.py              # backfill all months we find
    python migrate.py --month 2026-04   # just one month
    python migrate.py --rides-only
    python migrate.py --dry-run    # print what would be written, make no changes
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from googleapiclient.discovery import build

from constants import (
    ROW_NOTES, ROW_STEPS, ROW_SLEEP, ROW_CYCLE, ROW_NUTRITION,
    ROW_STRENGTH, ROW_CARDIO, ROW_SAUNA, ROW_STRETCH,
    ROW_MORNING_STAR, ROW_NIGHT_STAR, ROW_SEASON_PASS,
)
from sheets import get_google_creds, resolve_spreadsheet_id
from db import Db

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Sheet readers — built on top of the existing sheets.py helpers
# ═══════════════════════════════════════════════════════════════════

DAY_COLS = "CDEFGHI"  # Mon..Sun in the old sheet

# Tab names look like "Apr 13 - 19" or "Mar 30 – Apr 05" (en-dash variants).
_WEEK_TAB_PAT = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s*[-–—]\s*(?:[A-Z][a-z]{2}\s+)?\d{1,2}$")


def _list_week_tabs(service, spreadsheet_id: str) -> list[str]:
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties.title"
    ).execute()
    return [
        s["properties"]["title"]
        for s in meta.get("sheets", [])
        if _WEEK_TAB_PAT.match(s["properties"]["title"])
    ]


def _parse_tab_to_monday(tab_name: str, year: int) -> date | None:
    """'Apr 13 - 19' → date(year, 4, 13). 'Mar 30 – Apr 05' → date(year, 3, 30)."""
    m = re.match(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s*[-–—]", tab_name)
    if not m:
        return None
    month_name, day = m.group(1), int(m.group(2))
    try:
        return datetime.strptime(f"{month_name} {day} {year}", "%b %d %Y").date()
    except ValueError:
        return None


def _fetch_week_values(service, spreadsheet_id: str, tab_name: str) -> dict:
    """Single batchGet of every row we care about, returned as a dict of lists."""
    ranges = [
        f"'{tab_name}'!B{ROW_NOTES}",               # notes (col B only)
        f"'{tab_name}'!C{ROW_STRENGTH}:I{ROW_STRENGTH}",
        f"'{tab_name}'!C{ROW_CARDIO}:I{ROW_CARDIO}",
        f"'{tab_name}'!C{ROW_SAUNA}:I{ROW_SAUNA}",
        f"'{tab_name}'!C{ROW_STEPS}:I{ROW_STEPS}",
        f"'{tab_name}'!C{ROW_STRETCH}:I{ROW_STRETCH}",
        f"'{tab_name}'!C{ROW_NUTRITION}:I{ROW_NUTRITION}",
        f"'{tab_name}'!C{ROW_SLEEP}:I{ROW_SLEEP}",
        f"'{tab_name}'!C{ROW_CYCLE}:I{ROW_CYCLE}",
        f"'{tab_name}'!B{ROW_SEASON_PASS}",         # season pass (col B)
        f"'{tab_name}'!C{ROW_MORNING_STAR}:I{ROW_MORNING_STAR}",
        f"'{tab_name}'!C{ROW_NIGHT_STAR}:I{ROW_NIGHT_STAR}",
    ]
    resp = service.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id, ranges=ranges
    ).execute()

    def _row(i):
        vr = resp["valueRanges"][i]
        vals = vr.get("values", [[]])
        return vals[0] if vals else []

    def _scalar(i):
        r = _row(i)
        return r[0] if r else ""

    return {
        "notes": _scalar(0),
        "strength": _row(1),
        "cardio":   _row(2),
        "sauna":    _row(3),
        "steps":    _row(4),
        "stretch":  _row(5),
        "calories": _row(6),
        "sleep":    _row(7),
        "cycle":    _row(8),
        "season_pass": _scalar(9),
        "morning_star": _row(10),
        "night_star":   _row(11),
    }


# ═══════════════════════════════════════════════════════════════════
# Parsers for individual cell values
# ═══════════════════════════════════════════════════════════════════

def _cell(row: list, i: int) -> str:
    return str(row[i]).strip() if i < len(row) else ""

def _pint(s: str) -> int | None:
    """Parse int, allowing commas. Returns None on empty/invalid."""
    s = s.replace(",", "").strip()
    if not s: return None
    try: return int(s)
    except ValueError:
        # Some calorie cells may be like "274 (manual)" — extract first int
        m = re.search(r"-?\d+", s)
        return int(m.group()) if m else None

def _pfloat(s: str) -> float | None:
    s = s.strip()
    if not s: return None
    try: return float(s)
    except ValueError:
        m = re.search(r"-?\d+(\.\d+)?", s)
        return float(m.group()) if m else None

def _parse_cycle(s: str) -> tuple[str | None, int | None]:
    """'Follicular D12' → ('Follicular', 12). 'Luteal-EM D17' → ('Luteal-EM', 17)."""
    s = s.strip()
    if not s: return (None, None)
    m = re.match(r"^([A-Za-z\- ]+?)\s*D\s*(\d+)\s*$", s)
    if m:
        return (m.group(1).strip(), int(m.group(2)))
    return (s, None)

def _season_indices(raw: str) -> list[int]:
    out = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if tok.isdigit():
            out.append(int(tok))
    return sorted(set(out))


# ═══════════════════════════════════════════════════════════════════
# Migration driver
# ═══════════════════════════════════════════════════════════════════

def migrate_week(db: Db, service, spreadsheet_id: str, tab_name: str, *,
                 year: int,
                 season_seen: set[str],
                 dry_run: bool = False) -> int:
    """Return number of daily_entries rows upserted.

    `season_seen` is a caller-owned set of month_key strings we've already
    written season-pass indices for during THIS migrate_spreadsheet run.
    Tabs are processed newest-first (Apr 20-26 → Apr 13-19 → Apr 06-12),
    so the FIRST sighting per month is the freshest truth and later tabs
    get ignored — they'd otherwise overwrite with an older, shorter list.
    """
    monday = _parse_tab_to_monday(tab_name, year)
    if monday is None:
        log.warning("  skip tab %r: can't parse Monday", tab_name)
        return 0

    vals = _fetch_week_values(service, spreadsheet_id, tab_name)
    notes = vals["notes"]
    upserted = 0

    for i in range(7):  # Mon..Sun
        d = monday + timedelta(days=i)

        # Tri-state for stars/sauna: the sheet only stores ✓ or empty, so
        # we CANNOT distinguish "unchecked" from "no data yet". Be
        # conservative: only write True cells. False values leave the DB
        # column at its default FALSE (which is correct for new rows) and
        # don't overwrite a real user-set True if one ever existed.
        cell_to_true = lambda row: True if _cell(row, i) == "✓" else None

        entry = {
            "steps":         _pint(_cell(vals["steps"], i)),
            "sleep_hours":   _pfloat(_cell(vals["sleep"], i)),
            "calories":      _pint(_cell(vals["calories"], i)),
            "strength_note": _cell(vals["strength"], i) or None,
            "cardio_note":   _cell(vals["cardio"], i) or None,
            "stretch_note":  _cell(vals["stretch"], i) or None,
            "sauna":         cell_to_true(vals["sauna"]),
            "morning_star":  cell_to_true(vals["morning_star"]),
            "night_star":    cell_to_true(vals["night_star"]),
        }
        phase, cday = _parse_cycle(_cell(vals["cycle"], i))
        if phase: entry["cycle_phase"] = phase
        if cday:  entry["cycle_day"] = cday
        if i == 0 and notes:
            entry["notes"] = notes

        # Strip Nones so we don't NULL-out existing DB values (preserves
        # v1's "never overwrite something with nothing" semantic).
        clean = {k: v for k, v in entry.items() if v is not None and v != ""}
        if not clean:
            continue

        if dry_run:
            log.info("  DRY %s: %s", d, clean)
        else:
            db.upsert_entry(d, **clean)
        upserted += 1

    # Season pass — only the newest-seen value per month wins (see docstring).
    month_key = f"{monday.year:04d}-{monday.month:02d}"
    if month_key not in season_seen:
        indices = _season_indices(vals["season_pass"])
        if indices:
            if dry_run:
                log.info("  DRY season %s ← %s (newest tab)", month_key, indices)
            else:
                db.set_season_pass(month_key, indices)
                log.info("  season %s ← %s", month_key, indices)
        season_seen.add(month_key)

    log.info("  tab %r → %d days upserted", tab_name, upserted)
    return upserted


def migrate_spreadsheet(db: Db, month: int, year: int, dry_run: bool = False) -> int:
    creds = get_google_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    probe = date(year, month, 15)
    spreadsheet_id = resolve_spreadsheet_id(probe, creds)
    log.info("Migrating %s %d spreadsheet (%s)", probe.strftime("%B"), year, spreadsheet_id)

    tabs = _list_week_tabs(service, spreadsheet_id)
    log.info("  %d weekly tabs: %s", len(tabs), tabs)

    total = 0
    season_seen: set[str] = set()
    for tab in tabs:  # API returns newest first
        total += migrate_week(db, service, spreadsheet_id, tab, year=year,
                              season_seen=season_seen, dry_run=dry_run)
    return total


def migrate_rides(db: Db, dry_run: bool = False) -> int:
    cache = Path("rides_cache.json")
    if not cache.exists():
        log.info("No rides_cache.json — skipping rides migration.")
        return 0
    rides = json.loads(cache.read_text())
    payload = []
    for r in rides:
        try:
            d = datetime.strptime(r["date"], "%b %d, %Y").date()
        except (KeyError, ValueError):
            continue
        payload.append({
            "strava_id":    int(r["id"]),
            "date":         d,
            "distance_mi":  float(r.get("distance") or 0),
            "elevation_ft": int(r.get("elevation") or 0),
            "payload":      r,
        })
    if dry_run:
        log.info("  DRY rides: %d rides would be upserted", len(payload))
        return len(payload)
    n = db.upsert_rides_bulk(payload)
    log.info("  rides: %d upserted", n)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="e.g. 2026-04. Default: all months we find.")
    ap.add_argument("--rides-only", action="store_true")
    ap.add_argument("--sheets-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Db()
    log.info("DB health: %s", db.health())

    total_days = 0
    if not args.rides_only:
        if args.month:
            year, month = args.month.split("-")
            total_days = migrate_spreadsheet(db, int(month), int(year), args.dry_run)
        else:
            # Walk back from current month through March 2026 (first recorded month)
            today = date.today()
            for dt in [date(today.year, today.month, 15),
                       date(today.year, today.month - 1 if today.month > 1 else 12,
                            15) if today.month > 1 else None]:
                if dt is None:
                    continue
                try:
                    total_days += migrate_spreadsheet(db, dt.month, dt.year, args.dry_run)
                except Exception as e:
                    log.warning("  skip %s: %s", dt.strftime("%Y-%m"), e)

    total_rides = 0
    if not args.sheets_only:
        total_rides = migrate_rides(db, args.dry_run)

    log.info("\nDONE — %d days, %d rides %s", total_days, total_rides,
             "(DRY RUN)" if args.dry_run else "committed")
    if not args.dry_run:
        log.info("Final health: %s", db.health())


if __name__ == "__main__":
    main()
