#!/usr/bin/env python3
"""
Daily data-sync orchestrator.

Pulls fitness data from Oura, Garmin Connect, Strava, and Google
Calendar (cycle + Week Agenda events) and writes it into Postgres.
Scheduled 4 times a day by GitHub Actions; also runnable by hand for
backfills and one-off refreshes:

    python sync.py                    # sync yesterday
    python sync.py --date 2026-04-20  # sync a specific date
    python sync.py --morning          # backfill every day since last sync
    python sync.py --morning --force  # re-sync today even if up-to-date
    python sync.py --rides            # also refresh the Strava rides table
    python sync.py --last-sync        # print the stored last-sync date
    python sync.py --health           # print DB row counts

The Habit Tracker Google Doc (read via `habit_source.py`) is fetched
lazily at dashboard-render time, not here — it changes rarely and
has its own 24h disk cache.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from api_clients import (
    fetch_sleep, fetch_steps, fetch_cycle_day,
    fetch_nutrition, fetch_garmin_activities,
    fetch_week_calendar_notes,
)
from cycle import get_cycle_phase
from db import Db
from tz import local_today

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Sync state (lives in Neon, not local disk — so Actions + Render see
# the same value).
# ═══════════════════════════════════════════════════════════════════

LAST_SYNC_KEY = "last_sync_date"


def read_last_sync(db: Db) -> date | None:
    raw = db.get_state(LAST_SYNC_KEY)
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def write_last_sync(db: Db, d: date) -> None:
    db.set_state(LAST_SYNC_KEY, d.isoformat())


# ═══════════════════════════════════════════════════════════════════
# Google Calendar (for cycle detection + weekly notes). Keeps using
# existing api_clients helpers; they need `creds` from Google OAuth.
# This is read-only — Sheets-as-storage is gone, but GCal reads stay.
# ═══════════════════════════════════════════════════════════════════

def _google_creds_optional():
    """Best-effort: return Google creds or None so cycle/notes can run.

    In the cloud (Render/Actions) we'll ship a service-account JSON in
    GOOGLE_CREDS_JSON env var. On Mac we have a user-OAuth token.json.
    If neither is available, we skip cycle/notes and carry on.
    """
    try:
        from google_auth import get_google_creds
        return get_google_creds()
    except SystemExit:
        # google_auth calls sys.exit on missing credentials.json — swallow
        # so the sync doesn't abort for everyone when a single field fails.
        log.warning("Google creds unavailable — skipping GCal-dependent fields")
        return None
    except Exception as exc:
        log.warning("Google creds error (%s) — skipping GCal-dependent fields", exc)
        return None


# ═══════════════════════════════════════════════════════════════════
# Single-day sync
# ═══════════════════════════════════════════════════════════════════

def sync_single_day(db: Db, target: date, creds) -> bool:
    """Fetch one day's data from every source and upsert into daily_entries.

    Returns True if anything was written.
    """
    day_str = target.isoformat()
    log.info("Syncing %s (%s)", day_str, target.strftime("%A"))

    # Oura
    sleep_hrs = fetch_sleep(day_str)
    steps = fetch_steps(day_str)
    cycle_day = fetch_cycle_day(day_str, creds) if creds else None

    if sleep_hrs is None and steps is None and cycle_day is None:
        log.warning("  No Oura data for %s — skipping.", day_str)
        return False

    entry: dict = {}
    if sleep_hrs is not None:
        entry["sleep_hours"] = sleep_hrs
    if steps is not None:
        entry["steps"] = steps
    if cycle_day is not None:
        entry["cycle_day"] = cycle_day
        entry["cycle_phase"] = get_cycle_phase(cycle_day)

    # Garmin activities (strength / cardio). Stretch is intentionally
    # NOT auto-detected — it's a manual one-tap toggle in the dashboard.
    activities = fetch_garmin_activities(target)
    if activities["strength"]:
        parts = [f"💪 {s['duration_min']}m" for s in activities["strength"]]
        entry["strength_note"] = " + ".join(parts)
    if activities["cardio"]:
        parts = []
        for c in activities["cardio"]:
            type_key = c.get("name", "").lower()
            icon = "🚴" if any(k in type_key for k in ["cycling", "biking", "bike"]) else "🏃‍♀️"
            mi = c.get("distance_mi")
            parts.append(f"{icon} {mi}mi" if mi else f"{icon} {c['duration_min']}m")
        entry["cardio_note"] = " + ".join(parts)

    # Nutrition (Garmin/MFP)
    nutrition = fetch_nutrition(target)
    if nutrition:
        if nutrition.get("calories") is not None:
            entry["calories"] = nutrition["calories"]
        if nutrition.get("goal") is not None:
            entry["calorie_goal"] = nutrition["goal"]

    # Notes — only updated on the FIRST day of the week so we don't
    # rewrite the same text 7 times.
    if target.weekday() == 0 and creds:
        monday = target
        sunday = target + timedelta(days=6)
        notes = fetch_week_calendar_notes(monday, sunday, creds)
        if notes:
            entry["notes"] = notes

    if not entry:
        log.warning("  Nothing to write for %s", day_str)
        return False

    db.upsert_entry(target, **entry)
    log.info("  ✓ upserted %s: %s", day_str, list(entry.keys()))
    return True


# ═══════════════════════════════════════════════════════════════════
# Strava ride sync (optional per-invocation flag; also has own cron)
# ═══════════════════════════════════════════════════════════════════

def sync_rides(db: Db) -> int:
    """Fetch all Strava rides and upsert into rides table. Returns count."""
    # strava_fetch.py currently writes to rides_cache.json. Reuse its
    # fetch function (returns list of dicts) and upsert here.
    import strava_fetch as _sf
    rides = _sf.fetch_all_rides()  # network: minutes
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
    n = db.upsert_rides_bulk(payload)
    log.info("  rides: %d upserted", n)
    return n


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def _backfill(db: Db, force: bool) -> int:
    last = read_last_sync(db)
    today = local_today()

    if last is None:
        start_date = today - timedelta(days=1)
        log.info("First run (no sync history) — syncing yesterday only")
    else:
        yesterday = today - timedelta(days=1)
        start_date = min(last + timedelta(days=1), yesterday)
        if start_date > today:
            if force:
                start_date = today
                log.info("Force re-sync for today (%s)", today)
            else:
                log.info("Already synced through %s — nothing new", last)
                return 0
        else:
            log.info("Last sync: %s — backfilling %s → %s", last, start_date, today)

    creds = _google_creds_optional()
    days_synced = 0
    current = start_date
    while current <= today:
        sync_single_day(db, current, creds)
        days_synced += 1
        if current < today:
            time.sleep(0.5)  # be kind to APIs
        current += timedelta(days=1)

    write_last_sync(db, today)
    return days_synced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="Sync a specific date (YYYY-MM-DD)")
    ap.add_argument("--morning", action="store_true",
                    help="Backfill from last-sync through today")
    ap.add_argument("--force", action="store_true",
                    help="With --morning, re-sync today even if already done")
    ap.add_argument("--rides", action="store_true",
                    help="Also fetch Strava rides after daily sync")
    ap.add_argument("--last-sync", action="store_true",
                    help="Print the stored last-sync date and exit")
    ap.add_argument("--health", action="store_true",
                    help="Print DB row counts and exit")
    args = ap.parse_args()

    if not os.getenv("OURA_TOKEN"):
        log.error("OURA_TOKEN not set — can't fetch sleep/steps")
        sys.exit(1)

    db = Db()

    if args.health:
        import json
        print(json.dumps(db.health(), default=str, indent=2))
        return

    if args.last_sync:
        print(read_last_sync(db))
        return

    log.info("=" * 50)

    if args.morning:
        log.info("☀️  Good morning! Starting backfill sync")
        n = _backfill(db, args.force)
        log.info("☀️  Backfill complete. Synced %d day(s)", n)
    elif args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d").date()
        creds = _google_creds_optional()
        sync_single_day(db, target, creds)
        write_last_sync(db, target)
    else:
        # Default: sync yesterday
        target = local_today() - timedelta(days=1)
        creds = _google_creds_optional()
        sync_single_day(db, target, creds)
        write_last_sync(db, target)

    if args.rides:
        log.info("Fetching Strava rides…")
        sync_rides(db)

    log.info("DONE. Health: %s", db.health())


if __name__ == "__main__":
    main()
