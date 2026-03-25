#!/usr/bin/env python3
"""
Oura Ring → Google Sheets daily sync.

Pulls sleep, steps, and cycle data from Oura API v2,
finds (or creates) the correct weekly tab in the accountability
spreadsheet, maps the day to a column, and writes values.

Usage:
    python oura_sheets_sync.py                  # sync yesterday
    python oura_sheets_sync.py --date 2026-03-10  # sync specific date
    python oura_sheets_sync.py --morning        # backfill all missed days
    python oura_sheets_sync.py --steps-left     # weekly steps report
"""

import argparse
import json
import os
import sys
import time
import logging
from datetime import date, timedelta, datetime
from pathlib import Path

import re
import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── paths ──────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "sync.log"
LAST_SYNC_FILE = SCRIPT_DIR / ".last_sync.json"

# ── logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── config ─────────────────────────────────────────────────────────
OURA_TOKEN = os.getenv("OURA_TOKEN")
TEMPLATE_SPREADSHEET_ID = os.getenv(
    "SPREADSHEET_ID", "1xTqB26-HdeNSqPdNT-Bs8qSmGeAyPf0wlQTz68Mj3ds"
)
DRIVE_PARENT_FOLDER_ID = os.getenv("DRIVE_PARENT_FOLDER_ID")

# Garmin Connect (nutrition/calories via MFP sync)
GARMIN_EMAIL = os.getenv("GARMIN_EMAIL")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD")
GARMIN_TOKEN_DIR = SCRIPT_DIR / ".garmin_tokens"

# Cache: "YYYY-MM" → spreadsheet_id (avoids repeated Drive lookups during backfill)
_spreadsheet_cache: dict[str, str] = {}
OAUTH_CREDENTIALS_FILE = SCRIPT_DIR / os.getenv(
    "OAUTH_CREDENTIALS_FILE", "credentials.json"
)
OAUTH_TOKEN_FILE = SCRIPT_DIR / "token.json"


OURA_BASE = "https://api.ouraring.com/v2/usercollection"

# Sheet layout: row numbers (1-indexed)
ROW_DATE_NUM = 3
ROW_STRENGTH = 5
ROW_CARDIO = 6
ROW_STEPS = 8
ROW_NUTRITION = 11
ROW_SLEEP = 12
ROW_CYCLE = 13
ROW_NOTES = 2  # "Special Notes / Trips:" row

# Weekly goals
WEEKLY_STEPS_GOAL = 48000
WEEKLY_STRENGTH_GOAL = 3
WEEKLY_CARDIO_GOAL = 1

# ── Scoring system ──
# Daily stars (max 3/day):
#   🚶 Steps     → ⭐ if steps >= 8,000
#   😴 Sleep     → ⭐ if sleep >= 7h (6h during Menstrual/Luteal-PMS)
#   🍽️ Calories  → ⭐ if calories <= daily goal (from Garmin)
# Weekly stars:
#   💪 Strength  → 1⭐ per session (max 3/week)
#   🚴 Cardio    → 1⭐ per session (max 1/week, run or bike)
# Max possible: (3/day × 6 days) + 3 strength + 1 cardio = 22/week
# Tiers: 🥉 Good = 14, 🥈 Great = 18, 🥇 Perfect = 22
ROW_CHALLENGE_HEADER = 21  # "⭐ WEEKLY CHALLENGE" header
ROW_CHALLENGE = 22  # Daily star scores
DAILY_STEPS_GOAL = 8000
SLEEP_STAR_THRESHOLD_DEFAULT = 7.0
SLEEP_STAR_THRESHOLD_LOW_ENERGY = 6.0  # Luteal-PMS & Menstrual phases
LOW_ENERGY_PHASES = {"Menstrual", "Luteal-PMS"}
TIER_PERFECT, TIER_GREAT, TIER_GOOD = 22, 18, 14

# Garmin activity types
STRENGTH_TYPES = {"strength_training"}
CARDIO_TYPES = {"road_biking", "cycling", "running", "trail_running",
                "treadmill_running", "indoor_cycling"}

# Column mapping: weekday (0=Mon) → column letter
DAY_COL = {0: "C", 1: "D", 2: "E", 3: "F", 4: "G", 5: "H"}

# Cycle phase lookup (day-of-cycle → phase label)
# Each entry: (start_day, end_day, label, guide_row)
# guide_row = row in the PMS Quick Guide section of the sheet
CYCLE_PHASES = [
    (1, 3, "Menstrual", 16),
    (4, 13, "Follicular", 17),
    (14, 16, "Ovulation", 18),
    (17, 23, "Luteal-EM", 19),
    (24, 28, "Luteal-PMS", 20),
]

# Highlight color for the active phase in the PMS Quick Guide (light yellow)
PHASE_HIGHLIGHT = {"red": 1.0, "green": 0.95, "blue": 0.6}
# Default background (white) for non-active phases
PHASE_DEFAULT_BG = {"red": 1.0, "green": 1.0, "blue": 1.0}


def get_cycle_phase(cycle_day: int) -> str:
    """Return the phase label for a given day of the menstrual cycle."""
    for start, end, label, _row in CYCLE_PHASES:
        if start <= cycle_day <= end:
            return label
    if cycle_day > 28:
        return "Luteal (PMS)"
    return "Unknown"


def get_cycle_phase_guide_row(cycle_day: int) -> int | None:
    """Return the PMS Quick Guide row number for the current cycle phase."""
    for start, end, _label, row in CYCLE_PHASES:
        if start <= cycle_day <= end:
            return row
    if cycle_day > 28:
        return 20  # Luteal (PMS) row
    return None


def highlight_active_phase(service, spreadsheet_id: str, tab_name: str, cycle_day: int) -> None:
    """Highlight the active cycle phase row in the PMS Quick Guide."""
    active_row = get_cycle_phase_guide_row(cycle_day)
    if active_row is None:
        return

    # Get the sheet ID for this tab
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    sheet_id = None
    for s in metadata.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            sheet_id = s["properties"]["sheetId"]
            break
    if sheet_id is None:
        return

    requests = []
    for _start, _end, _label, row in CYCLE_PHASES:
        # 0-indexed row for API
        row_idx = row - 1
        is_active = (row == active_row)
        bg = PHASE_HIGHLIGHT if is_active else PHASE_DEFAULT_BG
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_idx,
                    "endRowIndex": row_idx + 1,
                    "startColumnIndex": 3,  # column D
                    "endColumnIndex": 8,    # through column H
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": bg,
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    # Also set font size 9 on cycle value cells (C13:H13) — keeps them from
    # overflowing the column width.  Only touches ROW_CYCLE, not the guide rows.
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": ROW_CYCLE - 1,
                "endRowIndex": ROW_CYCLE,
                "startColumnIndex": 2,   # column C
                "endColumnIndex": 8,     # through column H
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"fontSize": 9}
                }
            },
            "fields": "userEnteredFormat.textFormat.fontSize",
        }
    })

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()
    log.info("Highlighted phase row %d in PMS Quick Guide", active_row)


# ── Calendar notes (Special Notes / Trips) ────────────────────────
# Skip events whose summary starts with (case-insensitive) any of these:
NOTES_SKIP_STARTS = [
    "office", "habit:", "reminder", "task", "strength training",
    "cardio", "sprint", "commute", "get ready", "bike", "wash",
    "sauna", "potential", "weatherbug", "attending:", "holiday",
]


# Events that are trip logistics — skipped when a "Trip"/"Travel:" event exists
NOTES_TRIP_LOGISTICS = [
    "drive", "checkin", "check in", "arrange", "airbnb", "pack", "commute",
]


def _should_skip_event(summary: str) -> bool:
    """Return True if the event should be excluded from weekly notes."""
    lower = summary.lower().strip()
    # Keep appointment-tagged habits (e.g. "Habit<appointment>: Dentist")
    if "<appointment>" in lower:
        return False
    # Strip "sneha " prefix before checking (events like "Sneha Office Holiday")
    check = lower[len("sneha "):] if lower.startswith("sneha ") else lower
    for pat in NOTES_SKIP_STARTS:
        if lower.startswith(pat) or check.startswith(pat):
            return True
    return False


def _is_trip_logistics(summary: str) -> bool:
    """Return True if the event is trip logistics (drive, airbnb, packing, etc.)."""
    lower = summary.lower().strip()
    for pat in NOTES_TRIP_LOGISTICS:
        if lower.startswith(pat) or pat in lower:
            return True
    return False


def _is_monthly_quarterly_habit(summary: str) -> bool:
    """Return True if the event is a monthly or quarterly habit."""
    lower = summary.lower()
    return "habit: monthly" in lower or "habit: quarterly" in lower or "habit:  submit forma" in lower


def _shorten_event_name(summary: str) -> str:
    """Strip prefixes, tags, parentheticals, and trim to a concise label."""
    s = summary.strip()
    # Strip known prefixes
    for prefix in ["Appt:", "Appointment:", "Habit<appointment>:", "Travel:"]:
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    if s.startswith("Sneha "):
        s = s[len("Sneha "):]
    # Strip tags like <optional>
    s = re.sub(r"<[^>]+>", "", s).strip()
    # Remove parenthetical content: "(Green Card)", "(2026)", etc.
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    # Remove filler words at the start
    for filler in ["BiAnnualy ", "BiAnnually "]:
        if s.startswith(filler):
            s = s[len(filler):]
    # Strip subtitle after colon for movie/show-like names ("Dhurandhar: The Revenge")
    if ": " in s and not s.lower().startswith(("appt", "task")):
        s = s.split(":")[0].strip()
    # Strip wordy suffixes
    for suffix in ["Before Temple", "Photo & Reels Meetup", "Adjustment of Status Interview",
                    "Adjustment of Status"]:
        s = s.replace(suffix, "").strip()
    # Collapse "USCIS  Interview" → "USCIS interview"
    s = re.sub(r"\s{2,}", " ", s).strip()
    # If still long (>30 chars), keep first 4 words
    if len(s) > 30:
        s = " ".join(s.split()[:4])
    return s.strip()


def fetch_week_calendar_notes(monday: date, saturday: date, creds) -> str | None:
    """Fetch notable calendar events for the week and return a '+ '-joined summary.

    Filters out office work, routines, reminders, tasks, workouts, and
    tentative ('Potential') events.  Collapses clusters of monthly/quarterly
    habits into 'Month end habits'.
    """
    try:
        cal = build("calendar", "v3", credentials=creds, cache_discovery=False)

        time_min = monday.isoformat() + "T00:00:00Z"
        time_max = (saturday + timedelta(days=1)).isoformat() + "T00:00:00Z"

        events_result = cal.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        # First pass: collect valid events and detect trip events
        monthly_habit_count = 0
        timed_events: list[str] = []   # events with a specific time
        allday_events: list[str] = []  # all-day calendar markers
        has_trip = False

        for ev in events_result.get("items", []):
            summary = (ev.get("summary") or "").strip()
            if not summary:
                continue

            start = ev.get("start", {})
            is_allday = "date" in start and "dateTime" not in start

            # For all-day events that span multiple days, only include if
            # the event starts within this week (avoids duplication)
            if is_allday:
                ev_start = datetime.strptime(start["date"], "%Y-%m-%d").date()
                if ev_start < monday:
                    continue

            # Count monthly/quarterly habits (normally skipped by Habit: rule)
            if _is_monthly_quarterly_habit(summary):
                monthly_habit_count += 1
                continue

            if _should_skip_event(summary):
                continue

            # Detect trip/travel events
            lower = summary.lower()
            if "trip" in lower or lower.startswith("travel:"):
                has_trip = True

            if is_allday:
                allday_events.append(summary)
            else:
                timed_events.append(summary)

        # Build candidate list: prefer timed events over all-day markers.
        # If a timed event shares the same first word as an all-day event
        # (e.g. "Ugadi Celebration @ temple" vs "Ugadi"), drop the all-day one.
        timed_first_words = set()
        for s in timed_events:
            short = _shorten_event_name(s)
            word = short.lower().split()[0] if short.split() else ""
            if word:
                timed_first_words.add(word)

        candidates: list[str] = list(timed_events)
        for s in allday_events:
            short = _shorten_event_name(s)
            word = short.lower().split()[0] if short.split() else ""
            if word and word in timed_first_words:
                continue  # skip all-day marker, timed event covers it
            candidates.append(s)

        # Second pass: filter logistics if a trip exists, deduplicate similar names
        kept: list[str] = []
        seen: set[str] = set()

        for summary in candidates:
            if has_trip and _is_trip_logistics(summary):
                continue

            short = _shorten_event_name(summary)
            key = short.lower()
            # Deduplicate: skip if a kept entry already starts with the same word
            # (e.g. "Ugadi" and "Ugadi Celebration @ Sunnyvale" → keep only first)
            first_word = key.split()[0] if key.split() else key
            if first_word in seen:
                continue
            seen.add(first_word)
            seen.add(key)
            kept.append(short)

        if monthly_habit_count >= 2:
            kept.append("Month end habits")

        if not kept:
            return None

        return " + ".join(kept)

    except Exception as exc:
        log.warning("Calendar notes fetch failed: %s", exc)
        return None



# ── Garmin nutrition (calories via MFP sync) ────────────────────
_garmin_client_cache = None

def _get_garmin_client():
    """Return an authenticated Garmin Connect client (cached per run)."""
    global _garmin_client_cache
    if _garmin_client_cache is not None:
        return _garmin_client_cache

    from garminconnect import Garmin

    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        log.warning("Garmin credentials not set in .env")
        return None

    garmin = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    token_dir = str(GARMIN_TOKEN_DIR)

    # Try loading saved session first
    if GARMIN_TOKEN_DIR.exists():
        try:
            garmin.login(token_dir)
            log.info("Garmin: resumed saved session")
            _garmin_client_cache = garmin
            return garmin
        except Exception:
            pass  # token expired — fall through to fresh login

    garmin.login()
    GARMIN_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    garmin.garth.dump(token_dir)
    log.info("Garmin: fresh login, tokens saved")
    _garmin_client_cache = garmin
    return garmin


def fetch_nutrition(day: date) -> dict | None:
    """Fetch calorie data from Garmin Connect (synced from MFP).

    Returns dict with keys: calories, goal
    or None if no data / error.
    """
    try:
        garmin = _get_garmin_client()
        if garmin is None:
            return None

        nutrition = garmin.get_nutrition_daily_food_log(day.isoformat())
        content = nutrition.get("dailyNutritionContent", {})
        calories = content.get("calories", 0)

        if not calories:
            log.info("No Garmin nutrition data for %s", day)
            return None

        goal = nutrition.get("dailyNutritionGoals", {}).get("calories", 0)
        result = {"calories": int(calories), "goal": int(goal)}
        log.info("Garmin nutrition for %s: %s", day, result)
        return result

    except Exception as exc:
        log.warning("Garmin nutrition fetch failed: %s", exc)
        return None


def fetch_garmin_activities(day: date) -> dict:
    """Fetch strength and cardio activities from Garmin for a given day.

    Returns dict with keys: strength (list), cardio (list).
    Each entry: {duration_min, calories, avg_hr, name}
    """
    result = {"strength": [], "cardio": []}
    try:
        garmin = _get_garmin_client()
        if garmin is None:
            return result

        day_str = day.isoformat()
        activities = garmin.get_activities_by_date(day_str, day_str)

        for a in activities:
            type_key = a.get("activityType", {}).get("typeKey", "")
            dist_m = a.get("distance", 0) or 0
            entry = {
                "duration_min": int(a.get("duration", 0) / 60),
                "calories": int(a.get("calories", 0)),
                "avg_hr": int(a.get("averageHR", 0)) if a.get("averageHR") else 0,
                "name": a.get("activityName", type_key),
                "distance_mi": round(dist_m / 1609.34, 1) if dist_m else 0,
            }
            if type_key in STRENGTH_TYPES:
                result["strength"].append(entry)
            elif type_key in CARDIO_TYPES:
                result["cardio"].append(entry)

        if result["strength"] or result["cardio"]:
            log.info("Garmin activities for %s: %d strength, %d cardio",
                     day, len(result["strength"]), len(result["cardio"]))
    except Exception as exc:
        log.warning("Garmin activities fetch failed: %s", exc)

    return result


def fetch_weekly_strength_count(monday: date) -> int:
    """Count strength sessions Mon–Sat for the given week."""
    try:
        garmin = _get_garmin_client()
        if garmin is None:
            return 0
        saturday = monday + timedelta(days=5)
        activities = garmin.get_activities_by_date(monday.isoformat(), saturday.isoformat())
        return sum(1 for a in activities
                   if a.get("activityType", {}).get("typeKey", "") in STRENGTH_TYPES)
    except Exception as exc:
        log.warning("Weekly strength count failed: %s", exc)
        return 0


def fetch_weekly_cardio_count(monday: date) -> int:
    """Count cardio sessions (run/bike) Mon–Sat for the given week."""
    try:
        garmin = _get_garmin_client()
        if garmin is None:
            return 0
        saturday = monday + timedelta(days=5)
        activities = garmin.get_activities_by_date(monday.isoformat(), saturday.isoformat())
        return sum(1 for a in activities
                   if a.get("activityType", {}).get("typeKey", "") in CARDIO_TYPES)
    except Exception as exc:
        log.warning("Weekly cardio count failed: %s", exc)
        return 0


# ── Last sync state ───────────────────────────────────────────────
def read_last_sync() -> date | None:
    """Read the last synced date from the state file."""
    if not LAST_SYNC_FILE.exists():
        return None
    try:
        data = json.loads(LAST_SYNC_FILE.read_text())
        return datetime.strptime(data["last_sync_date"], "%Y-%m-%d").date()
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def write_last_sync(d: date) -> None:
    """Write the last synced date to the state file."""
    LAST_SYNC_FILE.write_text(json.dumps({"last_sync_date": d.isoformat()}))


# ── Oura helpers ───────────────────────────────────────────────────
def oura_get(endpoint: str, params: dict) -> dict | None:
    """GET from Oura API; returns parsed JSON or None on failure."""
    headers = {"Authorization": f"Bearer {OURA_TOKEN}"}
    try:
        resp = requests.get(
            f"{OURA_BASE}/{endpoint}", headers=headers, params=params, timeout=30
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        log.warning("Oura API error (%s): %s", endpoint, exc)
        return None


def _next_day(day: str) -> str:
    """Return the day after `day` as YYYY-MM-DD (Oura end_date is exclusive)."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    return (d + timedelta(days=1)).isoformat()


def fetch_sleep(day: str) -> float | None:
    """Return total sleep duration in hours for the given date, or None."""
    end = _next_day(day)
    data = oura_get("sleep", {"start_date": day, "end_date": end})
    if not data or not data.get("data"):
        return None
    total_seconds = 0
    for period in data["data"]:
        duration = period.get("total_sleep_duration")
        if duration is not None:
            total_seconds += duration
    if total_seconds == 0:
        return None
    hours = round(total_seconds / 3600, 1)
    log.info("Sleep on %s: %.1f hrs", day, hours)
    return hours


def fetch_steps(day: str) -> int | None:
    """Return step count for the given date, or None."""
    end = _next_day(day)
    data = oura_get("daily_activity", {"start_date": day, "end_date": end})
    if not data or not data.get("data"):
        return None
    steps = data["data"][0].get("steps")
    if steps is not None:
        log.info("Steps on %s: %d", day, steps)
    return steps


def fetch_cycle_day(day: str, creds=None) -> int | None:
    """Return the current cycle day by finding 'Periods' events in Google Calendar.

    Searches the past 90 days for events named 'Periods' (any color),
    takes the most recent one's start date, and calculates cycle day
    assuming a CYCLE_LENGTH-day cycle.
    """
    if creds is None:
        log.info("No Google creds for calendar lookup — skipping cycle")
        return None

    try:
        cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        target = datetime.strptime(day, "%Y-%m-%d").date()

        # Search 90 days back for "Periods" events
        time_min = (target - timedelta(days=90)).isoformat() + "T00:00:00Z"
        time_max = (target + timedelta(days=1)).isoformat() + "T00:00:00Z"

        # Search twice — "Periods" and "Period" are separate words to Google
        all_events = []
        for keyword in ("Periods", "Period"):
            result = cal.events().list(
                calendarId=CALENDAR_ID,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                q=keyword,
            ).execute()
            all_events.extend(result.get("items", []))

        # Deduplicate by event ID
        seen = set()
        events = []
        for ev in all_events:
            eid = ev.get("id")
            if eid not in seen:
                seen.add(eid)
                events.append(ev)

        # Filter for events whose summary matches "period" (case-insensitive)
        period_starts = []
        for ev in events:
            summary = (ev.get("summary") or "").lower()
            if "period" in summary:
                start = ev.get("start", {})
                ev_date = start.get("date") or start.get("dateTime", "")[:10]
                period_starts.append(datetime.strptime(ev_date, "%Y-%m-%d").date())

        if not period_starts:
            log.info("No 'Periods' calendar events found in past 90 days")
            return None

        # Use the most recent period start date
        period_starts.sort()
        latest_period_start = period_starts[-1]

        cycle_day = (target - latest_period_start).days + 1
        if cycle_day > CYCLE_LENGTH:
            # Predict next period start based on cycle length
            periods_passed = (target - latest_period_start).days // CYCLE_LENGTH
            predicted_start = latest_period_start + timedelta(days=periods_passed * CYCLE_LENGTH)
            cycle_day = (target - predicted_start).days + 1

        log.info("Cycle day %d on %s (period started %s, from Google Calendar)",
                 cycle_day, day, latest_period_start)
        return cycle_day

    except Exception as exc:
        log.warning("Calendar cycle lookup failed: %s", exc)
        return None


# ── Google Auth (OAuth2) ──────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

# Cycle config
CYCLE_LENGTH = 28  # default cycle length in days
CALENDAR_ID = "fnu.sneha@gmail.com"  # calendar with period events


def get_google_creds() -> Credentials:
    """Get OAuth2 credentials, refreshing or prompting login as needed."""
    creds = None

    # Load saved token if it exists
    if OAUTH_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(OAUTH_TOKEN_FILE), SCOPES)

    # Refresh or re-authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                log.info("Refreshing OAuth2 token...")
                creds.refresh(Request())
            except Exception as exc:
                log.warning("Token refresh failed (%s) — re-authenticating...", exc)
                creds = None  # fall through to browser login
        if not creds or not creds.valid:
            if not OAUTH_CREDENTIALS_FILE.exists():
                log.error("OAuth credentials file not found: %s", OAUTH_CREDENTIALS_FILE)
                log.error("Download it from GCP Console → APIs → Credentials → OAuth 2.0 Client IDs")
                sys.exit(1)
            log.info("Opening browser for Google login...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(OAUTH_CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Save token for next run
        OAUTH_TOKEN_FILE.write_text(creds.to_json())
        log.info("OAuth2 token saved to %s", OAUTH_TOKEN_FILE)

    return creds


def resolve_spreadsheet_id(target: date, creds) -> str:
    """Return the spreadsheet ID for the target date's month.

    Searches Drive for a spreadsheet named 'March' etc.
    Falls back to TEMPLATE_SPREADSHEET_ID for March 2026.
    Creates a new spreadsheet + copies template for new months.
    """
    key = target.strftime("%Y-%m")
    if key in _spreadsheet_cache:
        return _spreadsheet_cache[key]

    month_name = target.strftime("%B") + ": Week Accountability"  # e.g. "April: Week Accountability"
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    # Search for existing spreadsheet by name in the parent folder
    # Use 'contains' to handle trailing whitespace in manually renamed sheets
    month_only = target.strftime("%B")  # e.g. "April"
    if DRIVE_PARENT_FOLDER_ID:
        query = (
            f"name contains '{month_only}' and "
            f"'{DRIVE_PARENT_FOLDER_ID}' in parents and "
            f"mimeType = 'application/vnd.google-apps.spreadsheet' and "
            f"trashed = false"
        )
        results = drive.files().list(q=query, fields="files(id, name)").execute()
        files = [
            f for f in results.get("files", [])
            if f["name"].strip().startswith(month_only)
        ]
        if files:
            sid = files[0]["id"]
            log.info("Found spreadsheet '%s' → %s", files[0]["name"].strip(), sid)
            _spreadsheet_cache[key] = sid
            return sid

    # Not found — if this is March 2026 (the template month), use the template directly
    if TEMPLATE_SPREADSHEET_ID and target.year == 2026 and target.month == 3:
        _spreadsheet_cache[key] = TEMPLATE_SPREADSHEET_ID
        log.info("Using template spreadsheet for %s → %s", month_name, TEMPLATE_SPREADSHEET_ID)
        return TEMPLATE_SPREADSHEET_ID

    # New month — create a new spreadsheet
    log.info("Creating new spreadsheet '%s'...", month_name)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # Create the spreadsheet
    body = {"properties": {"title": month_name}}
    spreadsheet = sheets.spreadsheets().create(body=body).execute()
    new_id = spreadsheet["spreadsheetId"]
    log.info("Created spreadsheet '%s' (id=%s)", month_name, new_id)

    # Move to the parent Drive folder
    if DRIVE_PARENT_FOLDER_ID:
        file_info = drive.files().get(fileId=new_id, fields="parents").execute()
        old_parents = ",".join(file_info.get("parents", []))
        drive.files().update(
            fileId=new_id,
            addParents=DRIVE_PARENT_FOLDER_ID,
            removeParents=old_parents,
            fields="id, parents",
        ).execute()
        log.info("Moved '%s' into Drive folder %s", month_name, DRIVE_PARENT_FOLDER_ID)

    # Copy template tab from the reference spreadsheet
    ref_meta = sheets.spreadsheets().get(
        spreadsheetId=TEMPLATE_SPREADSHEET_ID, fields="sheets.properties"
    ).execute()
    template_sid = None
    for s in ref_meta.get("sheets", []):
        if s["properties"]["title"] == "sheet1":
            template_sid = s["properties"]["sheetId"]
            break
    if template_sid is None:
        template_sid = ref_meta["sheets"][0]["properties"]["sheetId"]

    sheets.spreadsheets().sheets().copyTo(
        spreadsheetId=TEMPLATE_SPREADSHEET_ID,
        sheetId=template_sid,
        body={"destinationSpreadsheetId": new_id},
    ).execute()

    # Rename "Copy of sheet1" → "sheet1" and delete the auto-created "Sheet1"
    new_meta = sheets.spreadsheets().get(
        spreadsheetId=new_id, fields="sheets.properties"
    ).execute()
    batch_requests = []
    for s in new_meta.get("sheets", []):
        title = s["properties"]["title"]
        sid = s["properties"]["sheetId"]
        if title.startswith("Copy of"):
            batch_requests.append({
                "updateSheetProperties": {
                    "properties": {"sheetId": sid, "title": "sheet1"},
                    "fields": "title",
                }
            })
        elif title == "Sheet1":
            batch_requests.append({"deleteSheet": {"sheetId": sid}})

    if batch_requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=new_id, body={"requests": batch_requests}
        ).execute()

    log.info("Template copied into '%s' — ready to use!", month_name)
    _spreadsheet_cache[key] = new_id
    return new_id


def get_week_tab_name(monday: date, saturday: date) -> str:
    """Return tab name like 'Mar 16 – 21'."""
    if monday.month == saturday.month:
        return f"{monday.strftime('%b %d')} – {saturday.day}"
    return f"{monday.strftime('%b %d')} – {saturday.strftime('%b %d')}"


def get_template_sheet_id(service, spreadsheet_id: str) -> int:
    """Return the sheetId of the 'sheet1' template tab."""
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    for sheet in metadata.get("sheets", []):
        if sheet["properties"]["title"] == "sheet1":
            return sheet["properties"]["sheetId"]
    return metadata["sheets"][0]["properties"]["sheetId"]


def find_or_create_tab(service, spreadsheet_id: str, monday: date, saturday: date) -> str:
    """Find the weekly tab or create it by duplicating sheet1. Returns tab name."""
    tab_name = get_week_tab_name(monday, saturday)

    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    existing = {s["properties"]["title"] for s in metadata.get("sheets", [])}

    if tab_name in existing:
        log.info("Using existing tab '%s'", tab_name)
        return tab_name

    # Duplicate the template sheet (preserves all formatting, colors, widths)
    log.info("Creating new weekly tab '%s' (duplicating template)", tab_name)
    template_id = get_template_sheet_id(service, spreadsheet_id)

    result = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "duplicateSheet": {
                "sourceSheetId": template_id,
                "newSheetName": tab_name,
            }
        }]},
    ).execute()
    new_sheet_id = result["replies"][0]["duplicateSheet"]["properties"]["sheetId"]
    log.info("Duplicated template → '%s' (sheetId=%d)", tab_name, new_sheet_id)

    # Clear ALL data cells (keep labels and formatting)
    data_rows = [ROW_STRENGTH, ROW_CARDIO, ROW_STEPS, ROW_SLEEP, ROW_NUTRITION,
                 ROW_CYCLE, ROW_CHALLENGE]
    ranges = [f"'{tab_name}'!C{row}:H{row}" for row in data_rows]
    ranges.append(f"'{tab_name}'!B{ROW_NOTES}")           # Notes text
    ranges.append(f"'{tab_name}'!A{ROW_CHALLENGE}:H{ROW_CHALLENGE}")  # Full score row
    ranges.append(f"'{tab_name}'!C7:H7")                  # Sauna row
    service.spreadsheets().values().batchClear(
        spreadsheetId=spreadsheet_id,
        body={"ranges": ranges},
    ).execute()

    # Update the "Week of:" label
    week_label = f"Week of: {monday.strftime('%b %d')} – {saturday.strftime('%b %d')}"
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [[week_label]]},
    ).execute()

    # Write date numbers (row 3: Mon=9, Tue=10, etc.)
    date_numbers = [[(monday + timedelta(days=i)).day for i in range(6)]]
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!C{ROW_DATE_NUM}:H{ROW_DATE_NUM}",
        valueInputOption="USER_ENTERED",
        body={"values": date_numbers},
    ).execute()
    # Left-align all cells in the new tab
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "repeatCell": {
                "range": {"sheetId": new_sheet_id},
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "LEFT"
                    }
                },
                "fields": "userEnteredFormat.horizontalAlignment"
            }
        }]},
    ).execute()
    log.info("Cleared data, set week label and date numbers on '%s'", tab_name)

    return tab_name


def _get_sheet_id(service, spreadsheet_id: str, tab_name: str) -> int | None:
    """Return the sheetId for a given tab name, or None."""
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    for s in metadata.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    return None


def set_cell_font_size(service, spreadsheet_id: str, tab_name: str,
                       row: int, col_start: int, col_end: int, size: int) -> None:
    """Set font size on a range of cells (1-indexed row, 0-indexed columns)."""
    sheet_id = _get_sheet_id(service, spreadsheet_id, tab_name)
    if sheet_id is None:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": col_start,
                    "endColumnIndex": col_end,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"fontSize": size}
                    }
                },
                "fields": "userEnteredFormat.textFormat.fontSize",
            }
        }]},
    ).execute()


def read_cell(service, spreadsheet_id: str, tab_name: str, cell: str) -> str | None:
    """Read a single cell value from a specific tab."""
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{tab_name}'!{cell}")
        .execute()
    )
    values = result.get("values", [])
    return values[0][0] if values else None


def write_cell(service, spreadsheet_id: str, tab_name: str, cell: str, value) -> None:
    """Write a single cell value to a specific tab, with retry on rate limits."""
    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{tab_name}'!{cell}",
                valueInputOption="USER_ENTERED",
                body={"values": [[value]]},
            ).execute()
            log.info("Wrote %s → %s!%s", value, tab_name, cell)
            return
        except HttpError as e:
            if e.resp.status == 429 and attempt < max_retries:
                wait = 30 * (attempt + 1)
                log.warning("Rate limited writing %s!%s — waiting %ds (attempt %d/%d)",
                            tab_name, cell, wait, attempt + 1, max_retries)
                time.sleep(wait)
            else:
                raise


def ensure_nutrition_row_label(service, spreadsheet_id: str, tab_name: str) -> None:
    """Make sure row 11 column B has the nutrition label."""
    current = read_cell(service, spreadsheet_id, tab_name, f"B{ROW_NUTRITION}")
    if not current or "MyFitnessPal" in current or "P/C/F" in current or current.strip() == "":
        write_cell(service, spreadsheet_id, tab_name, f"B{ROW_NUTRITION}",
                   "🍽️ Calories (MFP)")


def ensure_cycle_row_label(service, spreadsheet_id: str, tab_name: str) -> None:
    """Make sure row 13 column A has the 'Cycle Phase' label."""
    current = read_cell(service, spreadsheet_id, tab_name, "A13")
    if not current or current.strip() == "":
        write_cell(service, spreadsheet_id, tab_name, "A13", "🔄 Cycle Phase")


def ensure_challenge_scoreboard(service, spreadsheet_id: str, tab_name: str) -> None:
    """Ensure the Weekly Challenge scoreboard exists (rows 21-22) and scoring
    guide is in rows 15-20 A-C (next to PMS Quick Guide in D-H).

    Layout:
      Rows 15-20 A-C: ⭐ SCORING GUIDE table (what earns stars + tiers)
      Row 21: ⭐ WEEKLY CHALLENGE  (merged A-H, gold header)
      Row 22: Score | daily ⭐ stars in C-H
    """
    # Always rewrite scoring guide + scoreboard to keep it in sync with code

    # Colors
    GOLD_BG = {"red": 0.95, "green": 0.82, "blue": 0.45}
    GOLD_LIGHT = {"red": 1.0, "green": 0.96, "blue": 0.84}
    DARK_TEXT = {"red": 0.2, "green": 0.15, "blue": 0.05}

    sheet_id = _get_sheet_id(service, spreadsheet_id, tab_name)
    if sheet_id is None:
        return

    requests = []

    # Unmerge rows 21-22 first (0-indexed: 20-21)
    for row_idx in [20, 21]:
        requests.append({
            "unmergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_idx,
                    "endRowIndex": row_idx + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 8,
                }
            }
        })

    # Row 21 (idx 20): merge A-H, gold header, centered bold
    requests.append({
        "mergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 20, "endRowIndex": 21,
                "startColumnIndex": 0, "endColumnIndex": 8,
            },
            "mergeType": "MERGE_ALL",
        }
    })
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 20, "endRowIndex": 21,
                "startColumnIndex": 0, "endColumnIndex": 8,
            },
            "cell": {"userEnteredFormat": {
                "backgroundColor": GOLD_BG,
                "textFormat": {"bold": True, "fontSize": 10,
                               "foregroundColor": DARK_TEXT},
                "horizontalAlignment": "CENTER",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }
    })

    # Row 22 (idx 21): stars C-H centered + larger
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 21, "endRowIndex": 22,
                "startColumnIndex": 2, "endColumnIndex": 8,
            },
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "CENTER",
                "textFormat": {"fontSize": 12},
            }},
            "fields": "userEnteredFormat(horizontalAlignment,textFormat)",
        }
    })

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    except Exception:
        pass  # merges may already exist

    # Write scoreboard content
    write_cell(service, spreadsheet_id, tab_name,
               f"A{ROW_CHALLENGE_HEADER}", "⭐ WEEKLY CHALLENGE  (🥉14  🥈18  🥇22)")
    # A22 will be written by generate_morning_report() with score + tier text

    # Write scoring guide in rows 15-20 A-C (next to PMS Guide in D-H)
    guide = {
        15: ("⭐ SCORING GUIDE", "Earn", "Max"),
        16: ("🚶 Steps", "1⭐/day", "6⭐"),
        17: ("💪 Strength", "1⭐/session", "3⭐"),
        18: ("🚴 Cardio", "1⭐/session", "1⭐"),
        19: ("🍽️ Calories", "1⭐/day", "6⭐"),
        20: ("😴 Sleep", "1⭐/night", "6⭐"),
    }
    for row, (a, b, c) in guide.items():
        write_cell(service, spreadsheet_id, tab_name, f"A{row}", a)
        write_cell(service, spreadsheet_id, tab_name, f"B{row}", b)
        write_cell(service, spreadsheet_id, tab_name, f"C{row}", c)

    # Update cardio row label in data area (A6) to reflect 1x goal
    write_cell(service, spreadsheet_id, tab_name, "A6", "🚴 Cardio (1x)")

    # Row 20 is now Sleep, so tiers move to A21 area — but A21 is ROW_CHALLENGE_HEADER
    # Write tiers inline after the guide in a compact way
    # Overwrite the old row 20 tier text (now occupied by Sleep)
    # Instead, append tier info to the challenge header row
    write_cell(service, spreadsheet_id, tab_name,
               f"A{ROW_CHALLENGE_HEADER}",
               "⭐ WEEKLY CHALLENGE  (🥉14  🥈18  🥇22)")
    # Unmerge row 20 (was previously merged for tier text, now it's a guide row)
    requests_r20 = [
        {"unmergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 19, "endRowIndex": 20,
                "startColumnIndex": 0, "endColumnIndex": 3,
            },
        }},
    ]
    # Row 22: Merge A22:B22 so score + tier text fits in one wide cell
    requests_r20.append({"mergeCells": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 21, "endRowIndex": 22,
            "startColumnIndex": 0, "endColumnIndex": 2,
        },
        "mergeType": "MERGE_ALL",
    }})
    # Row 22 A-B: white background, bold, wrap text
    requests_r20.append({"repeatCell": {
        "range": {
            "sheetId": sheet_id,
            "startRowIndex": 21, "endRowIndex": 22,
            "startColumnIndex": 0, "endColumnIndex": 2,
        },
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 1, "green": 1, "blue": 1},
            "textFormat": {"bold": True, "fontSize": 10},
            "wrapStrategy": "WRAP",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)",
    }})
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests_r20},
        ).execute()
    except Exception:
        pass


def _get_dominant_cycle_day(service, spreadsheet_id: str, tab_name: str) -> int | None:
    """Read all cycle cells (C13:H13) and return a cycle day whose phase
    appears most often in the week — so the highlight reflects the dominant phase."""
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!C{ROW_CYCLE}:H{ROW_CYCLE}",
    ).execute()
    values = result.get("values", [[]])[0] if result.get("values") else []

    # Extract cycle day numbers from cells like "Follicular D9" or "Follicular (Day 9)"
    phase_counts: dict[str, int] = {}
    phase_to_day: dict[str, int] = {}
    for val in values:
        val = str(val).strip()
        if not val:
            continue
        # Parse "Phase D17" (new) or "Phase (Day 9)" (legacy)
        m = re.search(r"D(\d+)", val) or re.search(r"\(Day (\d+)\)", val)
        if m:
            cd = int(m.group(1))
            ph = get_cycle_phase(cd)
            phase_counts[ph] = phase_counts.get(ph, 0) + 1
            phase_to_day[ph] = cd  # keep any representative day for this phase

    if not phase_counts:
        return None

    # Return a cycle day from the most common phase
    dominant_phase = max(phase_counts, key=phase_counts.get)
    return phase_to_day[dominant_phase]


# ── sync_single_day ───────────────────────────────────────────────
def sync_single_day(target: date, service, creds, skip_scoreboard: bool = False) -> bool:
    """Sync one day's Oura data to the sheet. Returns True if data was written."""
    day_str = target.isoformat()
    weekday = target.weekday()

    if weekday == 6:  # Sunday
        log.info("Skipping Sunday %s", day_str)
        return False

    col = DAY_COL[weekday]
    log.info("Syncing %s (%s → column %s)", day_str, target.strftime("%A"), col)

    # Fetch Oura data
    sleep_hrs = fetch_sleep(day_str)
    steps = fetch_steps(day_str)
    cycle_day = fetch_cycle_day(day_str, creds)

    if sleep_hrs is None and steps is None and cycle_day is None:
        log.warning("No Oura data for %s — skipping.", day_str)
        return False

    # Resolve the correct monthly spreadsheet
    spreadsheet_id = resolve_spreadsheet_id(target, creds)

    # Find or create the weekly tab
    monday = target - timedelta(days=weekday)
    saturday = monday + timedelta(days=5)
    tab_name = find_or_create_tab(service, spreadsheet_id, monday, saturday)

    # Write date number (e.g. "9" for March 9) in row 3
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

    # Write strength & cardio activities (Garmin) — clear cell if deleted from Garmin
    activities = fetch_garmin_activities(target)
    if activities["strength"]:
        for s in activities["strength"]:
            text = f"💪 {s['duration_min']}m"
            write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_STRENGTH}", text)
    else:
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_STRENGTH}", "")
    if activities["cardio"]:
        for c in activities["cardio"]:
            type_key = c.get("name", "").lower()
            icon = "🚴" if any(k in type_key for k in ["cycling", "biking", "bike"]) else "🏃‍♀️"
            mi = c["distance_mi"]
            text = f"{icon} {mi}mi" if mi else f"{icon} {c['duration_min']}m"
            write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_CARDIO}", text)
    else:
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_CARDIO}", "")

    # Write nutrition (Garmin/MFP) — calories
    nutrition = fetch_nutrition(target)
    if nutrition:
        ensure_nutrition_row_label(service, spreadsheet_id, tab_name)
        cal = nutrition["calories"]
        nutr_text = f"{cal}"
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_NUTRITION}", nutr_text)
    else:
        log.info("No nutrition data — skipping row %d", ROW_NUTRITION)

    # Write cycle phase + highlight guide (based on dominant phase for the week)
    if cycle_day is not None:
        ensure_cycle_row_label(service, spreadsheet_id, tab_name)
        phase = get_cycle_phase(cycle_day)
        write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_CYCLE}",
                   f"{phase} D{cycle_day}")
        # Read all cycle days written so far in this tab to find dominant phase
        dominant_day = _get_dominant_cycle_day(service, spreadsheet_id, tab_name)
        if dominant_day is not None:
            highlight_active_phase(service, spreadsheet_id, tab_name, dominant_day)
    else:
        log.info("No cycle data — skipping row %d", ROW_CYCLE)

    # ── Challenge: daily star count in Row 14 ──
    daily_stars = 0
    # Steps star
    if steps is not None and steps >= DAILY_STEPS_GOAL:
        daily_stars += 1
    # Sleep star (cycle-aware: 6h for Luteal-PMS & Menstrual, 7h otherwise)
    if sleep_hrs is not None:
        phase_name = get_cycle_phase(cycle_day) if cycle_day else ""
        sleep_threshold = (SLEEP_STAR_THRESHOLD_LOW_ENERGY
                           if phase_name in LOW_ENERGY_PHASES
                           else SLEEP_STAR_THRESHOLD_DEFAULT)
        if sleep_hrs >= sleep_threshold:
            daily_stars += 1
    # Calories star
    if nutrition and nutrition.get("goal") and nutrition["calories"] <= nutrition["goal"]:
        daily_stars += 1
    if not skip_scoreboard:
        ensure_challenge_scoreboard(service, spreadsheet_id, tab_name)
    star_text = "⭐" * daily_stars if daily_stars > 0 else "☆"
    write_cell(service, spreadsheet_id, tab_name, f"{col}{ROW_CHALLENGE}", star_text)

    # Write weekly calendar notes to "Special Notes / Trips:" row
    notes = fetch_week_calendar_notes(monday, saturday, creds)
    if notes:
        write_cell(service, spreadsheet_id, tab_name, f"B{ROW_NOTES}", notes)
        set_cell_font_size(service, spreadsheet_id, tab_name,
                           ROW_NOTES, 1, 2, 9)  # B2 = col index 1–2, font 9
    else:
        log.info("No notable calendar events for the week")

    log.info("✓ Synced %s → tab '%s'", day_str, tab_name)
    return True


# ── steps_left_report ─────────────────────────────────────────────
def steps_left_report():
    """Print a weekly steps progress report."""
    today = date.today()
    weekday = today.weekday()  # 0=Mon … 6=Sun

    # Figure out this week's Mon–Sat
    if weekday == 6:  # Sunday — show the completed week
        monday = today - timedelta(days=6)
    else:
        monday = today - timedelta(days=weekday)
    saturday = monday + timedelta(days=5)
    tab_name = get_week_tab_name(monday, saturday)

    creds = get_google_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    spreadsheet_id = resolve_spreadsheet_id(today, creds)

    # Check if tab exists
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    existing_tabs = {s["properties"]["title"] for s in metadata.get("sheets", [])}

    # Read steps from the sheet (C8:H8 = Mon–Sat)
    sheet_steps = {}
    if tab_name in existing_tabs:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab_name}'!C{ROW_STEPS}:H{ROW_STEPS}"
        ).execute()
        values = result.get("values", [[]])[0] if result.get("values") else []
        for i, val in enumerate(values):
            if val and str(val).strip():
                try:
                    sheet_steps[i] = int(str(val).replace(",", ""))
                except ValueError:
                    pass

    # Get today's live steps from Oura (may be more current than sheet)
    today_live = None
    if weekday <= 5:
        today_live = fetch_steps(today.isoformat())

    # Calculate totals
    total = 0
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    breakdown = []

    for i in range(6):
        day_date = monday + timedelta(days=i)
        if i == weekday and today_live is not None:
            # Use the higher of sheet value or live value for today
            val = max(today_live, sheet_steps.get(i, 0))
            breakdown.append(f"    {day_names[i]} {day_date.strftime('%d')}: {val:>6,}  ← live")
        elif i in sheet_steps:
            val = sheet_steps[i]
            breakdown.append(f"    {day_names[i]} {day_date.strftime('%d')}: {val:>6,}")
        elif day_date <= today:
            val = 0
            breakdown.append(f"    {day_names[i]} {day_date.strftime('%d')}: {val:>6,}  (no data)")
        else:
            val = 0
            breakdown.append(f"    {day_names[i]} {day_date.strftime('%d')}:      –")
        total += val

    remaining = max(0, WEEKLY_STEPS_GOAL - total)

    # Days left after today (through Saturday)
    if weekday == 6:
        days_left = 0
    else:
        days_left = 5 - weekday  # remaining days AFTER today

    per_day = remaining // days_left if days_left > 0 else 0

    # Print report
    print()
    print(f"### 🚶 Steps This Week ({tab_name})")
    print()
    print("| Day | Steps |")
    print("|---|---|")
    for line in breakdown:
        # Parse "    Mon 09:  4,415  ← live" into day and value
        parts = line.strip().split(":", 1)
        day_label = parts[0].strip() if parts else ""
        val_str = parts[1].strip() if len(parts) > 1 else "–"
        print(f"| {day_label} | {val_str} |")
    print()
    print(f"**Total so far:** {total:,}")
    print(f"**Weekly goal:** {WEEKLY_STEPS_GOAL:,}")
    print(f"**Remaining:** {remaining:,}")
    if remaining == 0:
        print("🎉 **GOAL REACHED!**")
    elif days_left > 0:
        print(f"**Days left:** {days_left} · **Per day needed:** {per_day:,}")
    else:
        print(f"⚠️ Week over. Shortfall: {remaining:,}")
    print()


# ── PMS Guide tips ────────────────────────────────────────────────
# Extracted from the PMS Quick Guide in the sheet (rows 16-20, column D)
PMS_GUIDE_TIPS = {
    "Menstrual":   "Low energy → stretch, recover, yoga",
    "Follicular":  "Energy rising → strength training, heavier lifts",
    "Ovulation":   "Peak → PRs, heaviest lifts, strongest performance",
    "Luteal-EM":   "Stable energy → normal workouts",
    "Luteal-PMS":  "Energy drops → stretch, recover",
}


def calculate_challenge_score(
    steps_row: list,
    sleep_row: list,
    nutrition_row: list,
    cycle_row: list,
    strength_count: int,
    cardio_count: int,
    cal_goal: int,
    show_days: list[int],
) -> dict:
    """Calculate Weekly Challenge stars from sheet data."""
    steps_stars = 0
    sleep_stars = 0
    cal_stars = 0
    steps_possible = len(show_days)
    sleep_possible = len(show_days)
    cal_possible = 0  # only count days with calorie data
    daily = {}  # per-day breakdown: {day_index: {"steps": bool, "sleep": bool, "cal": bool}}

    for i in show_days:
        day_stars = {"steps": False, "sleep": False, "cal": False}

        # Steps star
        raw_s = str(steps_row[i]).replace(",", "").strip() if i < len(steps_row) else ""
        if raw_s.isdigit() and int(raw_s) >= DAILY_STEPS_GOAL:
            steps_stars += 1
            day_stars["steps"] = True

        # Sleep star (cycle-aware)
        raw_sl = str(sleep_row[i]).strip() if i < len(sleep_row) else ""
        try:
            sl = float(raw_sl.rstrip("h"))
        except (ValueError, AttributeError):
            sl = 0.0
        if sl > 0:
            # Determine phase for this day from cycle_row
            phase = ""
            if i < len(cycle_row):
                cell = str(cycle_row[i]).strip()
                for p in LOW_ENERGY_PHASES:
                    if p in cell:
                        phase = p
                        break
            threshold = (SLEEP_STAR_THRESHOLD_LOW_ENERGY
                         if phase in LOW_ENERGY_PHASES
                         else SLEEP_STAR_THRESHOLD_DEFAULT)
            if sl >= threshold:
                sleep_stars += 1
                day_stars["sleep"] = True

        # Calories star
        raw_c = str(nutrition_row[i]).strip() if i < len(nutrition_row) else ""
        num_c = raw_c.split(" ")[0].split("/")[0].strip() if raw_c else ""
        if num_c.isdigit() and cal_goal > 0:
            cal_possible += 1
            if int(num_c) <= cal_goal:
                cal_stars += 1
                day_stars["cal"] = True

        daily[i] = day_stars

    # Strength stars (weekly, capped at goal)
    strength_stars = min(strength_count, WEEKLY_STRENGTH_GOAL)
    strength_possible = WEEKLY_STRENGTH_GOAL

    # Cardio stars (weekly, capped at goal)
    cardio_stars = min(cardio_count, WEEKLY_CARDIO_GOAL)
    cardio_possible = WEEKLY_CARDIO_GOAL

    total = steps_stars + sleep_stars + cal_stars + strength_stars + cardio_stars
    max_score = 22  # (3/day × 6 days) + 3 strength + 1 cardio

    # Tier
    if total >= TIER_PERFECT:
        tier = "🥇 Perfect!"
    elif total >= TIER_GREAT:
        tier = "🥈 Great Week!"
    elif total >= TIER_GOOD:
        tier = "🥉 Good Week"
    else:
        tier = ""

    return {
        "steps_stars": steps_stars, "steps_possible": steps_possible,
        "sleep_stars": sleep_stars, "sleep_possible": sleep_possible,
        "cal_stars": cal_stars, "cal_possible": cal_possible,
        "strength_stars": strength_stars, "strength_possible": strength_possible,
        "cardio_stars": cardio_stars, "cardio_possible": cardio_possible,
        "total": total, "max": max_score, "tier": tier,
        "daily": daily,
    }


def generate_morning_report(service, spreadsheet_id: str, creds) -> str:
    """Generate a formatted morning report for the current week."""
    today = date.today()
    weekday = today.weekday()
    monday = today - timedelta(days=weekday) if weekday != 6 else today - timedelta(days=6)
    saturday = monday + timedelta(days=5)
    tab_name = get_week_tab_name(monday, saturday)

    # Read all data from the current week's tab
    try:
        batch = service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[
                f"'{tab_name}'!B{ROW_NOTES}:H{ROW_NOTES}",   # notes [0]
                f"'{tab_name}'!C{ROW_STEPS}:H{ROW_STEPS}",    # steps [1]
                f"'{tab_name}'!C{ROW_SLEEP}:H{ROW_SLEEP}",    # sleep [2]
                f"'{tab_name}'!C{ROW_CYCLE}:H{ROW_CYCLE}",    # cycle [3]
                f"'{tab_name}'!C{ROW_NUTRITION}:H{ROW_NUTRITION}",  # nutrition [4]
                f"'{tab_name}'!C{ROW_STRENGTH}:H{ROW_STRENGTH}",  # strength [5]
                f"'{tab_name}'!C{ROW_CARDIO}:H{ROW_CARDIO}",  # cardio [6]
            ],
        ).execute()
    except Exception as exc:
        log.warning("Could not read tab for report: %s", exc)
        return None

    ranges = batch.get("valueRanges", [])
    notes_row = ranges[0].get("values", [[]])[0] if len(ranges) > 0 and ranges[0].get("values") else []
    steps_row = ranges[1].get("values", [[]])[0] if len(ranges) > 1 and ranges[1].get("values") else []
    sleep_row = ranges[2].get("values", [[]])[0] if len(ranges) > 2 and ranges[2].get("values") else []
    cycle_row = ranges[3].get("values", [[]])[0] if len(ranges) > 3 and ranges[3].get("values") else []
    nutrition_row = ranges[4].get("values", [[]])[0] if len(ranges) > 4 and ranges[4].get("values") else []
    strength_row = ranges[5].get("values", [[]])[0] if len(ranges) > 5 and ranges[5].get("values") else []
    cardio_row = ranges[6].get("values", [[]])[0] if len(ranges) > 6 and ranges[6].get("values") else []

    # Notes value is in B2 (first element of notes_row)
    notes_text = notes_row[0] if notes_row else ""

    # Build daily data (Mon=index 0 through Sat=index 5)
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    lines = []

    # Pre-compute values needed for report
    strength_count = fetch_weekly_strength_count(monday)
    cardio_count = fetch_weekly_cardio_count(monday)
    cal_goal = 0
    try:
        garmin = _get_garmin_client()
        if garmin:
            nutr_data = garmin.get_nutrition_daily_food_log(today.isoformat())
            cal_goal = nutr_data.get("dailyNutritionGoals", {}).get("calories", 0)
    except Exception:
        pass

    # Only show days up to today
    show_days = [i for i in range(6) if (monday + timedelta(days=i)) <= today]

    # ── Compute all values first ──

    # Steps
    total_steps = 0
    for i in range(6):
        if i < len(steps_row) and str(steps_row[i]).strip():
            try:
                total_steps += int(str(steps_row[i]).replace(",", ""))
            except ValueError:
                pass
    today_steps = 0
    if weekday <= 5:
        live_steps = fetch_steps(today.isoformat())
        if live_steps is not None:
            today_steps = live_steps
            sheet_today = 0
            if weekday < len(steps_row) and str(steps_row[weekday]).strip():
                try:
                    sheet_today = int(str(steps_row[weekday]).replace(",", ""))
                except ValueError:
                    pass
            if live_steps > sheet_today:
                total_steps = total_steps - sheet_today + live_steps

    remaining_steps = max(0, WEEKLY_STEPS_GOAL - total_steps)
    pct_steps = min(100, int(total_steps / WEEKLY_STEPS_GOAL * 100)) if WEEKLY_STEPS_GOAL else 0

    # Sleep
    sleep_values = []
    for i in range(min(len(sleep_row), 6)):
        raw = str(sleep_row[i]).strip() if i < len(sleep_row) else ""
        if raw:
            try:
                sleep_values.append(float(raw))
            except ValueError:
                pass
    last_sleep = sleep_values[-1] if sleep_values else None
    avg_sleep = sum(sleep_values) / len(sleep_values) if sleep_values else None

    # Calories — preserve positional indexing (None for missing days)
    cal_values = []
    for i in range(min(len(nutrition_row), 6)):
        raw = str(nutrition_row[i]).strip() if i < len(nutrition_row) else ""
        num = raw.split(" ")[0].split("/")[0].strip() if raw else ""
        if num.isdigit():
            cal_values.append(int(num))
        else:
            cal_values.append(None)

    # Cycle
    latest_cycle_str = ""
    for i in range(min(len(cycle_row), 6) - 1, -1, -1):
        if str(cycle_row[i]).strip():
            latest_cycle_str = str(cycle_row[i]).strip()
            break
    phase_name = ""
    if latest_cycle_str:
        phase_name = latest_cycle_str.split(" D")[0].strip() if " D" in latest_cycle_str else latest_cycle_str

    # Score
    score = calculate_challenge_score(
        steps_row, sleep_row, nutrition_row, cycle_row,
        strength_count, cardio_count, cal_goal, show_days,
    )
    total = score["total"]
    mx = score["max"]

    # ════════════════════════════════════════════════════
    # 1. GREETING + SCORE
    # ════════════════════════════════════════════════════
    tier_label = f"⭐ {total}/{mx} — 🥉{TIER_GOOD}  🥈{TIER_GREAT}  🥇{TIER_PERFECT}"

    lines.append(f"## Good Morning, Sneha!  {tier_label}")
    lines.append("🚶6  😴6  🍽️6  💪3  🚴1 = 22⭐")
    if notes_text:
        lines.append(f"_{notes_text}_")
    lines.append("")

    # ── Stars breakdown (day as hero) ──
    yesterday_wd = weekday - 1
    yesterday_daily = score.get("daily", {}).get(yesterday_wd, {}) if yesterday_wd >= 0 else None
    today_daily = score.get("daily", {}).get(weekday, {})

    def _day_icons(daily, day_idx):
        """Build sorted icon string: ✅ first, ❌ last. Includes strength/cardio."""
        has_str = bool(str(strength_row[day_idx]).strip()) if day_idx < len(strength_row) else False
        has_crd = bool(str(cardio_row[day_idx]).strip()) if day_idx < len(cardio_row) else False
        icons = [
            ("🚶", daily.get("steps", False)),
            ("😴", daily.get("sleep", False)),
            ("🍽️", daily.get("cal", False)),
            ("💪", has_str),
            ("🚴", has_crd),
        ]
        earned = sum(1 for _, v in icons if v)
        # Sort: ✅ first, ❌ last
        icons.sort(key=lambda x: (not x[1],))
        icon_str = "  ".join(f"{ic}{'✅' if v else '❌'}" for ic, v in icons)
        return earned, icon_str

    if yesterday_daily is not None:
        yd_earned, yd_icons = _day_icons(yesterday_daily, yesterday_wd)
        lines.append(f"**Yesterday** {'⭐' * yd_earned if yd_earned else '☆'} {yd_earned}/5  {yd_icons}")
        lines.append("")

    td_earned, td_icons = _day_icons(today_daily, weekday)
    lines.append(f"**Today** {'⭐' * td_earned if td_earned else '☆'} {td_earned}/5 so far  {td_icons}")
    lines.append("")

    # ════════════════════════════════════════════════════
    # 2. LAST NIGHT + BODY
    # ════════════════════════════════════════════════════
    if last_sleep is not None or phase_name:
        lines.append("| Last Night | |")
        lines.append("|---|---|")
        if last_sleep is not None:
            if last_sleep >= 7:
                sleep_note = "good"
            elif last_sleep >= 6:
                sleep_note = "a little short"
            else:
                sleep_note = "rough night"
            lines.append(f"| **Sleep** | **{last_sleep}h** — {sleep_note} |")
        if phase_name:
            energy_map = {"Menstrual": "low energy", "Follicular": "energy rising",
                          "Ovulation": "peak energy", "Luteal-EM": "steady energy",
                          "Luteal-PMS": "energy winding down"}
            energy = energy_map.get(phase_name, "")
            tip = PMS_GUIDE_TIPS.get(phase_name, "")
            cycle_detail = f"**{latest_cycle_str}**"
            if energy:
                cycle_detail += f" — {energy}"
            lines.append(f"| **Cycle** | {cycle_detail} |")
            if tip:
                lines.append("")
                lines.append(f"> {tip}")
        lines.append("")

    # ════════════════════════════════════════════════════
    # 3. TODAY'S ACTIONS (the only thing you need to act on)
    # ════════════════════════════════════════════════════
    today_actions = []
    if weekday <= 5 and remaining_steps > 0:
        days_left = max(1, 5 - weekday + 1)
        today_steps = 0
        if weekday < len(steps_row) and str(steps_row[weekday]).strip():
            try:
                today_steps = int(str(steps_row[weekday]).replace(",", ""))
            except ValueError:
                pass
        live = fetch_steps(today.isoformat())
        if live is not None and live > today_steps:
            today_steps = live
        daily_target = (WEEKLY_STEPS_GOAL - total_steps + today_steps) // days_left
        steps_left_today = max(0, daily_target - today_steps)
        if steps_left_today > 0:
            today_actions.append(("Walk", f"**{steps_left_today:,}** steps (target ~{daily_target:,})"))

    cal_actual = [c for c in cal_values if c is not None]
    if cal_actual and cal_goal:
        today_cal = 0
        if weekday < len(nutrition_row):
            raw_today = str(nutrition_row[weekday]).strip()
            num_today = raw_today.split(" ")[0].split("/")[0].strip() if raw_today else ""
            if num_today.isdigit():
                today_cal = int(num_today)
        if today_cal > 0:
            left = cal_goal - today_cal
            if left > 0:
                today_actions.append(("Eat", f"**{left:,}** cal left ({today_cal}/{cal_goal})"))
            else:
                today_actions.append(("Cals", f"over by **{abs(left)}** ({today_cal}/{cal_goal})"))

    s_remaining = max(0, WEEKLY_STRENGTH_GOAL - strength_count)
    if s_remaining > 0:
        today_actions.append(("Strength", f"**{s_remaining}** sessions left this week"))

    c_remaining = max(0, WEEKLY_CARDIO_GOAL - cardio_count)
    if c_remaining > 0:
        today_actions.append(("Cardio", f"**{c_remaining}** run/bike left this week"))

    if today_actions:
        lines.append("**Today's targets:**")
        lines.append("")
        for label, detail in today_actions:
            lines.append(f"- {label}: {detail}")
    else:
        lines.append("All targets hit — enjoy the day!")
    lines.append("")

    # ════════════════════════════════════════════════════
    # 4. WEEKLY PROGRESS (single table, easy to scan)
    # ════════════════════════════════════════════════════
    lines.append("### Week at a Glance")
    lines.append("")
    lines.append("| | Progress | Status |")
    lines.append("|---|---|---|")

    # Steps
    if remaining_steps == 0:
        lines.append(f"| **Steps** | **{total_steps:,}** / {WEEKLY_STEPS_GOAL:,} | Done! |")
    else:
        days_left = max(1, 5 - weekday + 1) if weekday <= 5 else 1
        per_day = remaining_steps // days_left
        lines.append(f"| **Steps** | **{total_steps:,}** / {WEEKLY_STEPS_GOAL:,} ({pct_steps}%) | ~{per_day:,}/day left |")

    # Strength
    s_dots = "●" * strength_count + "○" * s_remaining
    if strength_count >= WEEKLY_STRENGTH_GOAL:
        lines.append(f"| **Strength** | {s_dots} {strength_count}/{WEEKLY_STRENGTH_GOAL} | Done! |")
    else:
        lines.append(f"| **Strength** | {s_dots} {strength_count}/{WEEKLY_STRENGTH_GOAL} | {s_remaining} left |")

    # Cardio
    c_dots = "●" * cardio_count + "○" * c_remaining
    if cardio_count >= WEEKLY_CARDIO_GOAL:
        lines.append(f"| **Cardio** | {c_dots} {cardio_count}/{WEEKLY_CARDIO_GOAL} | Done! |")
    else:
        lines.append(f"| **Cardio** | {c_dots} {cardio_count}/{WEEKLY_CARDIO_GOAL} | {c_remaining} left |")

    # Calories
    if cal_actual and cal_goal:
        avg_cal = sum(cal_actual) // len(cal_actual)
        on_target = sum(1 for c in cal_actual if c <= cal_goal)
        diff = avg_cal - cal_goal
        if diff < 0:
            cal_status = f"under by {abs(diff)}"
        elif diff > 0:
            cal_status = f"over by {diff}"
        else:
            cal_status = "on target"
        lines.append(f"| **Calories** | avg **{avg_cal}** / {cal_goal} | {cal_status} ({on_target}/{len(cal_actual)} on target) |")
    elif cal_actual:
        avg_cal = sum(cal_actual) // len(cal_actual)
        lines.append(f"| **Calories** | avg **{avg_cal}** | {len(cal_actual)} days logged |")

    # Sleep
    if sleep_values:
        low_nights = sum(1 for s in sleep_values if s < 7)
        if low_nights > 0:
            lines.append(f"| **Sleep** | avg **{avg_sleep:.1f}h** | {low_nights}/{len(sleep_values)} nights under 7h |")
        else:
            lines.append(f"| **Sleep** | avg **{avg_sleep:.1f}h** | All nights 7h+ |")

    # ════════════════════════════════════════════════════
    # 5. DAILY BREAKDOWN (only when 2+ days of data)
    # ════════════════════════════════════════════════════
    if len(show_days) > 1:
        lines.append("")
        lines.append("**Daily breakdown:**")
        lines.append("")

        hdr_cells = [f"**{day_names[i]} {(monday + timedelta(days=i)).day}**" for i in show_days]
        lines.append("| | " + " | ".join(hdr_cells) + " |")
        lines.append("|---|" + "|".join(["---"] * len(show_days)) + "|")

        def _cell(row, i, suffix=""):
            val = row[i] if i < len(row) and str(row[i]).strip() else "–"
            if val != "–" and suffix:
                val = f"{val}{suffix}"
            return str(val)

        sleep_cells = [_cell(sleep_row, i, "h") for i in show_days]
        lines.append("| Sleep | " + " | ".join(sleep_cells) + " |")

        steps_cells = []
        for i in show_days:
            val = steps_row[i] if i < len(steps_row) and str(steps_row[i]).strip() else "–"
            if val != "–":
                try:
                    val = f"{int(str(val).replace(',', '')):,}"
                except ValueError:
                    pass
            steps_cells.append(str(val))
        lines.append("| Steps | " + " | ".join(steps_cells) + " |")

        str_cells = [_cell(strength_row, i) for i in show_days]
        lines.append("| Strength | " + " | ".join(str_cells) + " |")

        cardio_cells = [_cell(cardio_row, i) for i in show_days]
        lines.append("| Cardio | " + " | ".join(cardio_cells) + " |")

        nutr_cells = [_cell(nutrition_row, i) for i in show_days]
        lines.append("| Cals | " + " | ".join(nutr_cells) + " |")

        cycle_cells = []
        for i in show_days:
            val = cycle_row[i] if i < len(cycle_row) and str(cycle_row[i]).strip() else "–"
            val = str(val).replace("Follicular", "Foll").replace("Ovulation", "Ovul")
            val = val.replace("Luteal-EM", "Lt-EM").replace("Luteal-PMS", "Lt-PMS")
            val = val.replace("Menstrual", "Mens")
            cycle_cells.append(val)
        lines.append("| Cycle | " + " | ".join(cycle_cells) + " |")

        _daily = score["daily"]
        star_cells = []
        for i in show_days:
            d = _daily.get(i, {})
            n = sum(1 for k in ("steps", "sleep", "cal") if d.get(k))
            star_cells.append("⭐" * n if n > 0 else "–")
        lines.append("| Stars | " + " | ".join(star_cells) + " |")

    # Write weekly score + tier to sheet scoreboard (A22 = medal, B22 = score)
    try:
        total = score["total"]
        mx = score["max"]
        # Combined score + tier milestones in merged A22:B22
        if total >= TIER_PERFECT:
            cell_text = f"🥇 {total}/{mx} Perfect!"
        elif total >= TIER_GREAT:
            cell_text = f"🥈 {total}/{mx} Great!  Next: 🥇 at {TIER_PERFECT}"
        elif total >= TIER_GOOD:
            cell_text = f"🥉 {total}/{mx} Good!  Next: 🥈 at {TIER_GREAT}"
        else:
            cell_text = f"⭐ {total}/{mx}  Next: 🥉 at {TIER_GOOD}"
        # Use write_cell for retry support (RAW input via direct call)
        write_cell(service, spreadsheet_id, tab_name, f"A{ROW_CHALLENGE}", cell_text)
    except Exception:
        pass


    lines.append("")
    lines.append("---")
    lines.append("*Sheet updated* ✓")
    lines.append("")

    # Build data dict for HTML report
    report_data = {
        "today": today,
        "tab_name": tab_name,
        "notes_text": notes_text,
        "last_sleep": last_sleep,
        "avg_sleep": avg_sleep,
        "sleep_values": sleep_values,
        "phase_name": phase_name,
        "latest_cycle_str": latest_cycle_str,
        "total_steps": total_steps,
        "today_steps": today_steps,
        "remaining_steps": remaining_steps,
        "pct_steps": pct_steps,
        "strength_count": strength_count,
        "cardio_count": cardio_count,
        "cal_values": cal_values,
        "cal_goal": cal_goal,
        "score": score,
        "strength_row": strength_row,
        "cardio_row": cardio_row,
    }

    return "\n".join(lines), report_data


def generate_html_report(data: dict) -> None:
    """Generate a self-contained HTML morning report and open in browser."""
    today = data["today"]
    notes_text = data["notes_text"]
    last_sleep = data["last_sleep"]
    avg_sleep = data["avg_sleep"]
    sleep_values = data["sleep_values"]
    phase_name = data["phase_name"]
    latest_cycle_str = data["latest_cycle_str"]
    total_steps = data["total_steps"]
    today_steps = data.get("today_steps", 0)
    remaining_steps = data["remaining_steps"]
    pct_steps = data["pct_steps"]
    strength_count = data["strength_count"]
    cardio_count = data["cardio_count"]
    cal_values = data["cal_values"]
    cal_goal = data["cal_goal"]
    score = data["score"]

    total_score = score["total"]
    max_score = score["max"]

    # Sleep
    if last_sleep is not None:
        sleep_color = "#22c55e" if last_sleep >= 7 else "#ef4444"
        sleep_label = f"{last_sleep}h"
    else:
        sleep_color = "#9ca3af"
        sleep_label = "–"

    # Cycle
    energy_map = {"Menstrual": "Low energy", "Follicular": "Energy rising",
                  "Ovulation": "Peak energy", "Luteal-EM": "Steady energy",
                  "Luteal-PMS": "Energy winding down"}
    cycle_energy = energy_map.get(phase_name, "")
    cycle_tip = PMS_GUIDE_TIPS.get(phase_name, "")
    cycle_color = {"Menstrual": "#f87171", "Follicular": "#34d399",
                   "Ovulation": "#fbbf24", "Luteal-EM": "#60a5fa",
                   "Luteal-PMS": "#c084fc"}.get(phase_name, "#9ca3af")

    # Today's cards
    # Steps today
    weekday = today.weekday()
    days_left = max(1, 5 - weekday + 1) if weekday <= 5 else 1
    daily_step_target = remaining_steps // days_left if remaining_steps > 0 and days_left > 0 else 0

    # Calories — only use today's value, not yesterday's fallback
    cal_actual = [c for c in cal_values if c is not None]
    today_cal = 0
    if weekday < len(cal_values) and cal_values[weekday] is not None:
        today_cal = cal_values[weekday]
    cal_left = max(0, cal_goal - today_cal) if cal_goal else 0

    s_remaining = max(0, WEEKLY_STRENGTH_GOAL - strength_count)
    c_remaining = max(0, WEEKLY_CARDIO_GOAL - cardio_count)

    # Yesterday/today stars with strength/cardio from sheet rows
    today_daily = score.get("daily", {}).get(today.weekday(), {})
    yesterday_wd = today.weekday() - 1
    yesterday_daily = score.get("daily", {}).get(yesterday_wd, {}) if yesterday_wd >= 0 else {}

    # Read strength/cardio rows from report_data
    _str_row = data.get("strength_row", [])
    _crd_row = data.get("cardio_row", [])

    def _html_day_icons(daily, day_idx):
        """Build sorted icon spans: ✅ first, ❌ last. Includes strength/cardio."""
        has_str = bool(str(_str_row[day_idx]).strip()) if day_idx < len(_str_row) else False
        has_crd = bool(str(_crd_row[day_idx]).strip()) if day_idx < len(_crd_row) else False
        icons = [
            ("🚶", daily.get("steps", False)),
            ("😴", daily.get("sleep", False)),
            ("🍽️", daily.get("cal", False)),
            ("💪", has_str),
            ("🚴", has_crd),
        ]
        earned = sum(1 for _, v in icons if v)
        icons.sort(key=lambda x: (not x[1],))
        icon_html = "".join(f'<span style="margin-right:8px;">{ic}{"✅" if v else "❌"}</span>' for ic, v in icons)
        return earned, icon_html

    today_stars_earned, today_icons_html = _html_day_icons(today_daily, today.weekday())
    if yesterday_wd >= 0:
        yesterday_stars_earned, yesterday_icons_html = _html_day_icons(yesterday_daily, yesterday_wd)
    else:
        yesterday_stars_earned, yesterday_icons_html = 0, ""

    # Tier
    tier_emoji = "⭐"

    # Week metrics for dot display
    def dots_html(filled, total_dots, color="#22c55e"):
        html = ""
        for i in range(total_dots):
            c = color if i < filled else "#e5e7eb"
            html += f'<span style="display:inline-block;width:12px;height:12px;border-radius:50%;background:{c};margin-right:3px;"></span>'
        return html

    def status_color(done, goal):
        if done >= goal:
            return "#22c55e"
        elif done >= goal * 0.5:
            return "#eab308"
        return "#ef4444"

    def progress_bar_html(pct, color="#3b82f6"):
        return f'''<div style="background:#f3f4f6;border-radius:6px;height:8px;width:100%;overflow:hidden;">
            <div style="background:{color};height:100%;width:{min(pct, 100)}%;border-radius:6px;transition:width 0.3s;"></div>
        </div>'''

    day_name = today.strftime("%A, %B %d")

    # Avg calories
    avg_cal = sum(cal_actual) // len(cal_actual) if cal_actual else 0

    # Sleep week stats
    low_nights = sum(1 for s in sleep_values if s < 7) if sleep_values else 0

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Morning Report — {day_name}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f8fafc; color: #1e293b; padding: 24px; max-width: 520px; margin: 0 auto; }}
  .header {{ margin-bottom: 20px; }}
  .header h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .header .date {{ font-size: 14px; color: #64748b; }}
  .header .notes {{ font-size: 13px; color: #94a3b8; margin-top: 6px; font-style: italic; }}
  .pills {{ display: flex; gap: 10px; margin-bottom: 20px; }}
  .pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px;
           border-radius: 20px; font-size: 13px; font-weight: 600; color: white; }}
  .score-badge {{ background: #fef3c7; color: #92400e; padding: 8px 16px; border-radius: 12px;
                  font-size: 14px; font-weight: 600; margin-bottom: 20px; display: inline-block; }}
  .cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 24px; }}
  .card {{ background: white; border-radius: 12px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  .card .label {{ font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px;
                  margin-bottom: 8px; }}
  .card .value {{ font-size: 24px; font-weight: 700; margin-bottom: 8px; }}
  .card .sub {{ font-size: 12px; color: #94a3b8; }}
  h2 {{ font-size: 15px; font-weight: 600; color: #475569; margin-bottom: 12px; }}
  .week-table {{ background: white; border-radius: 12px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06);
                 margin-bottom: 24px; }}
  .week-row {{ display: flex; align-items: center; padding: 10px 0;
               border-bottom: 1px solid #f1f5f9; }}
  .week-row:last-child {{ border-bottom: none; }}
  .week-row .metric {{ width: 80px; font-size: 13px; font-weight: 600; }}
  .week-row .dots {{ flex: 1; }}
  .week-row .status {{ font-size: 12px; font-weight: 600; text-align: right; min-width: 90px; }}
  .insight {{ background: white; border-radius: 12px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06);
              font-size: 13px; color: #475569; line-height: 1.5; }}
  .insight strong {{ color: #1e293b; }}
  .footer {{ text-align: center; margin-top: 20px; font-size: 11px; color: #cbd5e1; }}
  .share-btn {{ display: block; width: 100%; padding: 14px; margin-top: 20px; border: none;
                background: #3b82f6; color: white; font-size: 15px; font-weight: 600;
                border-radius: 12px; cursor: pointer; text-align: center; }}
  .share-btn:active {{ background: #2563eb; }}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
</head>
<body>

<div id="dashboard">
<div class="header">
  <h1>Good Morning, Sneha!</h1>
  <div class="date">{day_name}</div>
  {"<div class='notes'>" + notes_text.replace("<", "&lt;").replace(">", "&gt;") + "</div>" if notes_text else ""}
</div>

<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px;">
  <div class="score-badge" style="margin:0;">{tier_emoji} {total_score}/{max_score} — 🥉{TIER_GOOD}  🥈{TIER_GREAT}  🥇{TIER_PERFECT}</div>
  <span class="pill" style="background:{sleep_color};">😴 {sleep_label} sleep</span>
  <span class="pill" style="background:{cycle_color};">🔄 {latest_cycle_str or '–'}</span>
</div>
<div style="display:flex;gap:10px;margin-bottom:16px;">
  <span style="background:#f1f5f9;padding:3px 8px;border-radius:8px;font-size:12px;">🚶6</span>
  <span style="background:#f1f5f9;padding:3px 8px;border-radius:8px;font-size:12px;">😴6</span>
  <span style="background:#f1f5f9;padding:3px 8px;border-radius:8px;font-size:12px;">🍽️6</span>
  <span style="background:#f1f5f9;padding:3px 8px;border-radius:8px;font-size:12px;">💪3</span>
  <span style="background:#f1f5f9;padding:3px 8px;border-radius:8px;font-size:12px;">🚴1</span>
  <span style="font-size:12px;color:#94a3b8;align-self:center;">= 22⭐</span>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px;">
{"<div style='background:white;border-radius:12px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,0.06);'><div style=display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;><div style=font-size:13px;font-weight:700;color:#475569;>Yesterday</div><div style=font-size:15px;font-weight:700;>" + ('⭐' * yesterday_stars_earned if yesterday_stars_earned else '☆') + " " + str(yesterday_stars_earned) + "/5</div></div><div style=display:flex;gap:6px;font-size:13px;flex-wrap:wrap;>" + yesterday_icons_html + "</div></div>" if yesterday_wd >= 0 else ""}
  <div style="background:white;border-radius:12px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,0.06);">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
      <div style="font-size:13px;font-weight:700;color:#475569;">Today</div>
      <div style="font-size:15px;font-weight:700;">{"⭐" * today_stars_earned if today_stars_earned else "☆"} {today_stars_earned}/5</div>
    </div>
    <div style="display:flex;gap:6px;font-size:13px;flex-wrap:wrap;">
      {today_icons_html}
    </div>
  </div>
</div>
<div class="cards">
  <div class="card">
    <div class="label">Steps</div>
    <div class="value">{daily_step_target:,}</div>
    {progress_bar_html(min(100, int(today_steps / daily_step_target * 100)) if daily_step_target else 0)}
    <div class="sub">target for today</div>
  </div>
  <div class="card">
    <div class="label">Calories left</div>
    <div class="value">{cal_left:,}</div>
    {progress_bar_html(int(today_cal / cal_goal * 100) if cal_goal else 0, "#22c55e")}
    <div class="sub">{today_cal} / {cal_goal} eaten</div>
  </div>
  <div class="card">
    <div class="label">Strength</div>
    <div class="value">{s_remaining}</div>
    {progress_bar_html(int(strength_count / WEEKLY_STRENGTH_GOAL * 100) if WEEKLY_STRENGTH_GOAL else 0, "#8b5cf6")}
    <div class="sub">{strength_count}/{WEEKLY_STRENGTH_GOAL} done this week</div>
  </div>
  <div class="card">
    <div class="label">Cardio</div>
    <div class="value">{c_remaining}</div>
    {progress_bar_html(int(cardio_count / WEEKLY_CARDIO_GOAL * 100) if WEEKLY_CARDIO_GOAL else 0, "#f97316")}
    <div class="sub">{cardio_count}/{WEEKLY_CARDIO_GOAL} run/bike this week</div>
  </div>
</div>

<h2>Week at a Glance</h2>
<div class="week-table">
  <div class="week-row">
    <div class="metric">Steps</div>
    <div class="dots">{progress_bar_html(pct_steps)}</div>
    <div class="status" style="color:{status_color(total_steps, WEEKLY_STEPS_GOAL)};">{total_steps:,} / {WEEKLY_STEPS_GOAL:,}</div>
  </div>
  <div class="week-row">
    <div class="metric">Strength</div>
    <div class="dots">{dots_html(strength_count, WEEKLY_STRENGTH_GOAL, "#8b5cf6")}</div>
    <div class="status" style="color:{status_color(strength_count, WEEKLY_STRENGTH_GOAL)};">{strength_count}/{WEEKLY_STRENGTH_GOAL} {"Done!" if strength_count >= WEEKLY_STRENGTH_GOAL else f"({s_remaining} left)"}</div>
  </div>
  <div class="week-row">
    <div class="metric">Cardio</div>
    <div class="dots">{dots_html(cardio_count, WEEKLY_CARDIO_GOAL, "#f97316")}</div>
    <div class="status" style="color:{status_color(cardio_count, WEEKLY_CARDIO_GOAL)};">{cardio_count}/{WEEKLY_CARDIO_GOAL} {"Done!" if cardio_count >= WEEKLY_CARDIO_GOAL else f"({c_remaining} left)"}</div>
  </div>
  <div class="week-row">
    <div class="metric">Calories</div>
    <div class="dots">{progress_bar_html(int(avg_cal / cal_goal * 100) if cal_goal and avg_cal else 0, "#22c55e")}</div>
    <div class="status" style="color:{status_color(1, 1) if cal_actual and cal_goal and avg_cal <= cal_goal else '#ef4444' if cal_actual and cal_goal else '#9ca3af'};">avg {avg_cal} / {cal_goal}</div>
  </div>
  <div class="week-row">
    <div class="metric">Sleep</div>
    <div class="dots">{dots_html(len(sleep_values) - low_nights, len(sleep_values) if sleep_values else 1, "#22c55e")}</div>
    <div class="status" style="color:{'#22c55e' if low_nights == 0 else '#ef4444'};">{"avg " + f"{avg_sleep:.1f}h" if avg_sleep is not None else "–"}{f' ({low_nights} night{"s" if low_nights != 1 else ""} < 7h)' if low_nights > 0 else ''}</div>
  </div>
</div>

{"<div class='insight'><strong>🔄 " + phase_name + ":</strong> " + cycle_tip + "</div>" if cycle_tip else ""}

<div class="footer">Sheet updated ✓ · {data['tab_name']}</div>
</div>

<button class="share-btn" id="shareBtn" onclick="shareReport()">📤 Share</button>

<script>
async function shareReport() {{
  const btn = document.getElementById('shareBtn');
  btn.textContent = '⏳ Preparing...';
  btn.disabled = true;
  try {{
    const canvas = await html2canvas(document.getElementById('dashboard'), {{
      backgroundColor: '#f8fafc',
      scale: 2,
      useCORS: true,
    }});
    const blob = await new Promise(r => canvas.toBlob(r, 'image/png'));
    const file = new File([blob], 'morning-report.png', {{ type: 'image/png' }});

    if (navigator.canShare && navigator.canShare({{ files: [file] }})) {{
      await navigator.share({{ files: [file] }});
    }} else {{
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'morning-report.png';
      a.click();
      URL.revokeObjectURL(a.href);
    }}
  }} catch (e) {{
    if (e.name !== 'AbortError') console.error('Share failed:', e);
  }}
  btn.textContent = '📤 Share';
  btn.disabled = false;
}}
</script>

</body>
</html>"""

    html_path = Path.home() / "morning_report.html"
    html_path.write_text(html)
    log.info("HTML report written to %s", html_path)

    # Auto-open in browser
    import subprocess
    subprocess.Popen(["open", str(html_path)])


# ── main ───────────────────────────────────────────────────────────
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
    args = parser.parse_args()

    # ── steps-left: read-only report ──────────────────────────────
    if args.steps_left:
        steps_left_report()
        return

    # ── morning: backfill mode ────────────────────────────────────
    if args.morning:
        log.info("=" * 50)
        log.info("☀️  Good morning! Starting backfill sync")

        last = read_last_sync()
        today = date.today()

        if last is None:
            # First run — just sync yesterday
            start_date = today - timedelta(days=1)
            log.info("First run (no sync history) — syncing yesterday only")
        else:
            # Always re-sync yesterday so mid-day snapshots get updated
            # with final end-of-day numbers (steps, calories, etc.)
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
            if current.weekday() != 6:  # skip Sundays
                sync_single_day(current, service, creds,
                                skip_scoreboard=(current < today))
                days_synced += 1
                if current < today:
                    time.sleep(0.5)  # be kind to APIs
            current += timedelta(days=1)

        write_last_sync(today)
        log.info("☀️  Backfill complete! Synced %d day(s)", days_synced)

        # Generate the pretty morning report
        spreadsheet_id = resolve_spreadsheet_id(today, creds)
        result = generate_morning_report(service, spreadsheet_id, creds)
        if result:
            report, report_data = result
            print(report)
            generate_html_report(report_data)
        else:
            print(f"\n  ☀️  Good morning! Synced {days_synced} day(s) "
                  f"({start_date} → {today})\n")
        return

    # ── single date mode (default: yesterday) ─────────────────────
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
