"""
API clients for Oura Ring, Garmin Connect, and Google Calendar.

Handles all external data fetching: sleep, steps, cycle day, nutrition,
activities (strength/cardio), and calendar events for weekly notes.
"""

import logging
import os
import re
from datetime import date, timedelta, datetime

import requests
from googleapiclient.discovery import build

from constants import (
    OURA_BASE, GARMIN_TOKEN_DIR, CALENDAR_ID, CYCLE_LENGTH,
    STRENGTH_TYPES, CARDIO_TYPES, STRETCH_TYPES,
    NOTES_SKIP_STARTS, NOTES_TRIP_LOGISTICS,
    PERIOD_LOOKBACK_DAYS,
)

log = logging.getLogger(__name__)

def _oura_token():
    """Read OURA_TOKEN lazily (after .env is loaded by the orchestrator)."""
    return os.getenv("OURA_TOKEN")

def _garmin_email():
    return os.getenv("GARMIN_EMAIL")

def _garmin_password():
    return os.getenv("GARMIN_PASSWORD")


# ═══════════════════════════════════════════════════════════════════
# Oura Ring API
# ═══════════════════════════════════════════════════════════════════

def oura_get(endpoint: str, params: dict) -> dict | None:
    """GET from Oura API v2.

    Args:
        endpoint: API path after /v2/usercollection/ (e.g. "sleep").
        params: Query parameters (start_date, end_date, etc.).

    Returns:
        Parsed JSON dict, or None on any request failure.
    """
    headers = {"Authorization": f"Bearer {_oura_token()}"}
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
    """Return the day after ``day`` as YYYY-MM-DD (Oura end_date is exclusive)."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    return (d + timedelta(days=1)).isoformat()


def fetch_sleep(day: str) -> float | None:
    """Return total sleep duration in hours for the given date, or None.

    Args:
        day: Date string in YYYY-MM-DD format.

    Returns:
        Sleep hours rounded to 1 decimal, or None if no data.
    """
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
    """Return step count for the given date, or None.

    Args:
        day: Date string in YYYY-MM-DD format.

    Returns:
        Integer step count, or None if no data.
    """
    end = _next_day(day)
    data = oura_get("daily_activity", {"start_date": day, "end_date": end})
    if not data or not data.get("data"):
        return None
    steps = data["data"][0].get("steps")
    if steps is not None:
        log.info("Steps on %s: %d", day, steps)
    return steps


# ═══════════════════════════════════════════════════════════════════
# Garmin Connect (nutrition + activities)
# ═══════════════════════════════════════════════════════════════════

_garmin_client_cache = None


def _get_garmin_client():
    """Return an authenticated Garmin Connect client (cached per run).

    Returns:
        A ``garminconnect.Garmin`` instance, or None if credentials are missing.
    """
    global _garmin_client_cache
    if _garmin_client_cache is not None:
        return _garmin_client_cache

    from garminconnect import Garmin

    if not _garmin_email() or not _garmin_password():
        log.warning("Garmin credentials not set in .env")
        return None

    garmin = Garmin(_garmin_email(), _garmin_password())
    token_dir = str(GARMIN_TOKEN_DIR)

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

    Args:
        day: The date to query.

    Returns:
        Dict with keys ``calories`` and ``goal``, or None if unavailable.
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

    Args:
        day: The date to query.

    Returns:
        Dict with keys ``strength`` (list) and ``cardio`` (list).
        Each entry has: duration_min, calories, avg_hr, name, distance_mi.
    """
    result = {"strength": [], "cardio": [], "stretch": []}
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
            elif type_key in STRETCH_TYPES:
                result["stretch"].append(entry)

        if result["strength"] or result["cardio"] or result["stretch"]:
            log.info("Garmin activities for %s: %d strength, %d cardio, %d stretch",
                     day, len(result["strength"]), len(result["cardio"]), len(result["stretch"]))
    except Exception as exc:
        log.warning("Garmin activities fetch failed: %s", exc)

    return result


def fetch_weekly_activity_count(monday: date, type_set: set[str]) -> int:
    """Count activity sessions of the given types for Mon–Sun of the week.

    Args:
        monday: The Monday of the target week.
        type_set: Set of Garmin activity type keys to count.

    Returns:
        Number of matching sessions, or 0 on error.
    """
    try:
        garmin = _get_garmin_client()
        if garmin is None:
            return 0
        sunday = monday + timedelta(days=6)
        activities = garmin.get_activities_by_date(monday.isoformat(), sunday.isoformat())
        return sum(1 for a in activities
                   if a.get("activityType", {}).get("typeKey", "") in type_set)
    except Exception as exc:
        log.warning("Weekly activity count failed: %s", exc)
        return 0


# ═══════════════════════════════════════════════════════════════════
# Google Calendar — cycle day detection
# ═══════════════════════════════════════════════════════════════════

def fetch_cycle_day(day: str, creds=None) -> int | None:
    """Return the current cycle day by finding 'Periods' events in Google Calendar.

    Searches the past 90 days for events named 'Periods' (any color),
    takes the most recent one's start date, and calculates cycle day
    assuming a CYCLE_LENGTH-day cycle.

    Args:
        day: Date string in YYYY-MM-DD format.
        creds: Google OAuth2 credentials.

    Returns:
        Integer cycle day (1-based), or None if no period events found.
    """
    if creds is None:
        log.info("No Google creds for calendar lookup — skipping cycle")
        return None

    try:
        cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        target = datetime.strptime(day, "%Y-%m-%d").date()

        time_min = (target - timedelta(days=PERIOD_LOOKBACK_DAYS)).isoformat() + "T00:00:00Z"
        time_max = (target + timedelta(days=1)).isoformat() + "T00:00:00Z"

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

        period_starts = []
        for ev in events:
            summary = (ev.get("summary") or "").lower()
            if "period" in summary:
                start = ev.get("start", {})
                ev_date = start.get("date") or start.get("dateTime", "")[:10]
                period_starts.append(datetime.strptime(ev_date, "%Y-%m-%d").date())

        if not period_starts:
            log.info("No 'Periods' calendar events found in past %d days", PERIOD_LOOKBACK_DAYS)
            return None

        period_starts.sort()
        latest_period_start = period_starts[-1]

        cycle_day = (target - latest_period_start).days + 1
        if cycle_day > CYCLE_LENGTH:
            periods_passed = (target - latest_period_start).days // CYCLE_LENGTH
            predicted_start = latest_period_start + timedelta(days=periods_passed * CYCLE_LENGTH)
            cycle_day = (target - predicted_start).days + 1

        log.info("Cycle day %d on %s (period started %s, from Google Calendar)",
                 cycle_day, day, latest_period_start)
        return cycle_day

    except Exception as exc:
        log.warning("Calendar cycle lookup failed: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════
# Google Calendar — weekly notes
# ═══════════════════════════════════════════════════════════════════

def _should_skip_event(summary: str) -> bool:
    """Return True if the event should be excluded from weekly notes."""
    lower = summary.lower().strip()
    if "<appointment>" in lower:
        return False
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
    for prefix in ["Appt:", "Appointment:", "Habit<appointment>:", "Travel:"]:
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    if s.startswith("Sneha "):
        s = s[len("Sneha "):]
    s = re.sub(r"<[^>]+>", "", s).strip()
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()
    for filler in ["BiAnnualy ", "BiAnnually "]:
        if s.startswith(filler):
            s = s[len(filler):]
    if ": " in s and not s.lower().startswith(("appt", "task")):
        s = s.split(":")[0].strip()
    for suffix in ["Before Temple", "Photo & Reels Meetup", "Adjustment of Status Interview",
                    "Adjustment of Status"]:
        s = s.replace(suffix, "").strip()
    s = re.sub(r"\s{2,}", " ", s).strip()
    if len(s) > 30:
        s = " ".join(s.split()[:4])
    return s.strip()


def fetch_week_calendar_notes(monday: date, sunday: date, creds) -> str | None:
    """Fetch notable calendar events for the week and return a '+ '-joined summary.

    Filters out office work, routines, reminders, tasks, workouts, and
    tentative ('Potential') events. Collapses clusters of monthly/quarterly
    habits into 'Month end habits'.

    Args:
        monday: First day (Monday) of the target week.
        sunday: Last day (Sunday) of the target week.
        creds: Google OAuth2 credentials.

    Returns:
        A string like "Birthday + Dentist", or None if no notable events.
    """
    try:
        cal = build("calendar", "v3", credentials=creds, cache_discovery=False)

        time_min = monday.isoformat() + "T00:00:00Z"
        time_max = (sunday + timedelta(days=1)).isoformat() + "T00:00:00Z"

        events_result = cal.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        monthly_habit_count = 0
        timed_events: list[str] = []
        allday_events: list[str] = []
        has_trip = False

        for ev in events_result.get("items", []):
            summary = (ev.get("summary") or "").strip()
            if not summary:
                continue

            start = ev.get("start", {})
            is_allday = "date" in start and "dateTime" not in start

            if is_allday:
                ev_start = datetime.strptime(start["date"], "%Y-%m-%d").date()
                if ev_start < monday:
                    continue

            if _is_monthly_quarterly_habit(summary):
                monthly_habit_count += 1
                continue

            if _should_skip_event(summary):
                continue

            lower = summary.lower()
            if "trip" in lower or lower.startswith("travel:"):
                has_trip = True

            if is_allday:
                allday_events.append(summary)
            else:
                timed_events.append(summary)

        # Prefer timed events; drop all-day markers that duplicate them
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
                continue
            candidates.append(s)

        # Filter logistics if a trip exists, deduplicate similar names
        kept: list[str] = []
        seen: set[str] = set()

        for summary in candidates:
            if has_trip and _is_trip_logistics(summary):
                continue

            short = _shorten_event_name(summary)
            key = short.lower()
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
