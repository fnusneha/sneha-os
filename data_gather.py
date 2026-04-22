"""
Assemble the `report_data` dict that `html_report.generate_html_report`
consumes, sourced from the Postgres row store.

Keeps the renderer ignorant of SQL: this module does the Postgres reads,
the Oura live-steps call, the Google reads (travel pins + habits), and
returns a plain dict keyed by the names the template renderer expects.

Usage:
    from data_gather import gather_dashboard_data
    data = gather_dashboard_data()               # today
    data = gather_dashboard_data(date(2026,4,1)) # any date (useful in tests)
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

from db import Db
from constants import WEEKLY_STEPS_GOAL
from scoring import calculate_challenge_score
from api_clients import fetch_nutrition, fetch_steps
from tz import local_today

log = logging.getLogger(__name__)

# Per-process cache for live steps so mobile refreshes don't each trigger
# an Oura API call. TTL is short (60s) — enough to dedupe rapid reloads
# but still current enough for a "did my steps update?" check.
_live_steps_cache: dict[str, tuple[float, int | None]] = {}
_LIVE_STEPS_TTL = 60.0


def _cached_fetch_steps(iso_day: str, *, force: bool = False) -> int | None:
    """Live Oura step-count fetch with a 60s in-memory cache.

    `force=True` bypasses the cache (used by pull-to-refresh) so the
    user isn't staring at a cached value until the TTL expires.

    Logs the result at INFO so Render logs show exactly what Oura
    returned for each request — critical for debugging "I pulled to
    refresh but steps didn't update" reports.
    """
    if not force:
        hit = _live_steps_cache.get(iso_day)
        if hit and time.time() - hit[0] < _LIVE_STEPS_TTL:
            log.info("live steps: cache hit day=%s steps=%s", iso_day, hit[1])
            return hit[1]
    try:
        val = fetch_steps(iso_day)
        log.info("live steps: Oura fetch day=%s steps=%s force=%s", iso_day, val, force)
    except Exception as exc:
        log.warning("live steps fetch failed: %s", exc)
        val = None
    _live_steps_cache[iso_day] = (time.time(), val)
    return val


# Live Garmin nutrition fetch (calories + goal). Same shape as steps:
# cached in-memory so rapid reloads don't each hit Garmin.
_live_nutrition_cache: dict[str, tuple[float, dict | None]] = {}
_LIVE_NUTRITION_TTL = 60.0


def _cached_fetch_nutrition(day: date, *, force: bool = False) -> dict | None:
    iso_day = day.isoformat()
    if not force:
        hit = _live_nutrition_cache.get(iso_day)
        if hit and time.time() - hit[0] < _LIVE_NUTRITION_TTL:
            log.info("live nutrition: cache hit day=%s val=%s", iso_day, hit[1])
            return hit[1]
    try:
        val = fetch_nutrition(day)
        log.info("live nutrition: Garmin fetch day=%s val=%s force=%s", iso_day, val, force)
    except Exception as exc:
        log.warning("live nutrition fetch failed: %s", exc)
        val = None
    _live_nutrition_cache[iso_day] = (time.time(), val)
    return val


# The old pipeline returned sheet-column-style lists where index 0 = Monday,
# index 1 = Tuesday, … index 6 = Sunday. html_report.py iterates by weekday
# index, so we preserve that ordering.


def _build_weekday_list(week: list[dict | None], field: str,
                        default: str = "") -> list:
    """Extract `field` across Mon..Sun, yielding '' for missing days.

    Converts None → '' and numeric types → str (html_report does
    `str(row[i]).strip()` everywhere, so strings are safest).
    """
    out = []
    for row in week:
        if row is None:
            out.append(default)
            continue
        v = row.get(field)
        out.append("" if v is None else str(v))
    return out


def _cycle_cell(row: dict | None) -> str:
    """Render 'Follicular D12' style label from a daily_entries row."""
    if not row or not row.get("cycle_phase"):
        return ""
    phase = row.get("cycle_phase") or ""
    day = row.get("cycle_day")
    return f"{phase} D{day}" if day else phase


def gather_dashboard_data(
    today: date | None = None,
    *,
    live_steps: bool = True,
    force: bool = False,
) -> dict:
    """Build the `report_data` dict that html_report.generate_html_report wants.

    Args:
        today: The day to build the dashboard FOR. Defaults to real today.
        live_steps: If True, call the Oura live-steps endpoint to top
            up today's count so the "X steps left" hint stays current
            between syncs. Set False in tests to avoid the network hit.
    """
    db = Db()
    if today is None:
        today = local_today()
    weekday = today.weekday()
    monday = today - timedelta(days=weekday)
    sunday = monday + timedelta(days=6)

    # Pull the whole week in one query (7 rows or fewer).
    week = db.get_week_entries(today)

    # Days with any data, by index — the list of weekday indices that actually have data.
    show_days = [i for i, r in enumerate(week) if r is not None]

    # Per-weekday lists in the format html_report.py expects (strings).
    steps_row    = _build_weekday_list(week, "steps")
    sleep_row    = _build_weekday_list(week, "sleep_hours")
    nutrition_row = _build_weekday_list(week, "calories")
    strength_row = _build_weekday_list(week, "strength_note")
    cardio_row   = _build_weekday_list(week, "cardio_note")
    stretch_row  = _build_weekday_list(week, "stretch_note")
    sauna_row    = [("✓" if (r and r.get("sauna")) else "") for r in week]
    morning_star_row = [("✓" if (r and r.get("morning_star")) else "") for r in week]
    night_star_row   = [("✓" if (r and r.get("night_star")) else "") for r in week]
    cycle_row    = [_cycle_cell(r) for r in week]

    # Today-specific live data.
    today_row = week[weekday] if weekday < len(week) else None
    today_steps_db = (today_row or {}).get("steps") or 0
    today_cal_goal = (today_row or {}).get("calorie_goal") or 0

    # Fetch today's steps fresh from Oura so the "X steps left" hint
    # always reflects current activity, even between scheduled syncs.
    # `force` is set on pull-to-refresh and bypasses the 60s cache.
    today_steps = today_steps_db
    if live_steps:
        fresh = _cached_fetch_steps(today.isoformat(), force=force)
        if fresh:
            today_steps = fresh

    # Live Garmin nutrition is ONLY fetched on an explicit pull-to-
    # refresh (force=True). Every regular tab switch back to Quest Hub
    # would otherwise spend 500ms–2s on a synchronous Garmin call,
    # which made the dashboard feel noticeably sluggish. For regular
    # loads we trust the DB (populated by sync.py 4x/day); for
    # pull-to-refresh the user has explicitly asked for fresh data and
    # the extra latency is expected.
    live_nutrition = _cached_fetch_nutrition(today, force=force) if (live_steps and force) else None
    if live_nutrition:
        db_cal = (today_row or {}).get("calories") or 0
        live_cal = live_nutrition.get("calories") or 0
        if live_cal > db_cal:
            # Patch today's row in memory so downstream builders see the fresh
            # value (cal_values list + today_row reads both flow from `week`).
            if today_row is None:
                today_row = {}
                week[weekday] = today_row
            today_row["calories"] = live_cal
        # Goal sometimes changes in Garmin too; pick the fresher one.
        live_goal = live_nutrition.get("goal") or 0
        if live_goal and live_goal != today_cal_goal:
            today_cal_goal = live_goal
            if today_row is not None:
                today_row["calorie_goal"] = live_goal

    # Weekly totals.
    total_steps = 0
    for r in week:
        if r and r.get("steps"):
            total_steps += int(r["steps"])
    # Include "today_steps" if today's row is missing or stale vs. live fetch.
    # `today_row["steps"]` may be None when the column exists but hasn't been
    # populated yet, so coerce to 0 BEFORE the subtraction (previously the
    # expression relied on precedence and could do `int - None`).
    if live_steps and today_steps:
        logged_today = (today_row or {}).get("steps") or 0
        if today_steps > logged_today:
            total_steps += today_steps - logged_today

    remaining_steps = max(0, WEEKLY_STEPS_GOAL - total_steps)
    pct_steps = min(100, round((total_steps / WEEKLY_STEPS_GOAL) * 100)) if WEEKLY_STEPS_GOAL else 0

    # Sleep stats.
    sleep_vals = [float(r["sleep_hours"]) for r in week if r and r.get("sleep_hours") is not None]
    avg_sleep = sum(sleep_vals) / len(sleep_vals) if sleep_vals else None
    last_sleep = float(today_row["sleep_hours"]) if today_row and today_row.get("sleep_hours") is not None else None

    # Calorie values (7-length, None where missing).
    cal_values = [(int(r["calories"]) if (r and r.get("calories") is not None) else None) for r in week]
    # Cal goal: use today's goal, falling back to any day in the week that has one.
    cal_goal = today_cal_goal
    if not cal_goal:
        for r in week:
            if r and r.get("calorie_goal"):
                cal_goal = int(r["calorie_goal"])
                break

    # Cycle phase for the header chip + coach line ("Luteal-EM", "D19").
    # sync.py only writes cycle_phase when it runs — and cron fires 4×/day,
    # so between midnight and the first morning slot, today's row has a
    # NULL cycle_phase. Fall back to the most recent past day that did
    # get populated, advancing the day counter forward so the chip still
    # reads "Luteal-EM · D20" at 6am instead of showing yesterday's
    # D19 or (worse) a blank.
    phase_name = (today_row or {}).get("cycle_phase") or ""
    today_cycle_day = (today_row or {}).get("cycle_day")
    latest_cycle_str = _cycle_cell(today_row)

    if not phase_name:
        from cycle import get_cycle_phase  # local import to keep sync lean
        for offset in range(1, 8):  # look back up to a week
            idx = weekday - offset
            if idx < 0:
                break
            prev = week[idx]
            if prev and prev.get("cycle_phase") and prev.get("cycle_day"):
                advanced_day = int(prev["cycle_day"]) + offset
                phase_name = get_cycle_phase(advanced_day) or prev["cycle_phase"]
                today_cycle_day = advanced_day
                latest_cycle_str = (
                    f"{phase_name} D{advanced_day}" if advanced_day else phase_name
                )
                log.debug(
                    "Cycle phase back-filled from %d days ago: %s D%d",
                    offset, phase_name, advanced_day,
                )
                break

    # Strength / cardio counts (used by text report, not HTML directly).
    strength_count = sum(1 for r in week if r and r.get("strength_note"))
    cardio_count   = sum(1 for r in week if r and r.get("cardio_note"))

    # Notes — stored on Monday's row; fall back to any row that has them.
    notes_text = ""
    for r in week:
        if r and r.get("notes"):
            notes_text = r["notes"]
            break

    # Weekly score (steps/sleep/cal booleans per day).
    score = calculate_challenge_score(
        steps_row=steps_row,
        sleep_row=sleep_row,
        nutrition_row=nutrition_row,
        cycle_row=cycle_row,
        strength_count=strength_count,
        cardio_count=cardio_count,
        cal_goal=cal_goal,
        show_days=show_days,
    )

    # Season pass indices for the current month.
    month_key = f"{today.year:04d}-{today.month:02d}"
    season_done_indices = set(db.get_season_pass(month_key))

    # Tab name — still useful as a footer label (shows the week range).
    def _fmt(d): return d.strftime("%b %d")
    tab_name = f"{_fmt(monday)} - {_fmt(sunday) if monday.month == sunday.month else sunday.strftime('%b %d')}"

    # ── Optional data sources: travel pins + doc habits.
    # These read from Google external sources. If we can't get creds
    # (no token.json on Render), degrade gracefully to empty.
    travel_pins = []
    monthly_habits = quarterly_habits = annual_habits = []
    try:
        from google_auth import get_google_creds
        from travel_source import fetch_travel_pins
        from habit_source import fetch_habits_from_doc
        creds = get_google_creds()
        try:
            travel_pins = fetch_travel_pins(creds)
        except Exception as exc:
            log.warning("travel pin fetch failed: %s", exc)
        try:
            habits = fetch_habits_from_doc(creds) or {}
            monthly_habits = habits.get("monthly", []) or []
            quarterly_habits = habits.get("quarterly", []) or []
            annual_habits = habits.get("annual", []) or []
        except Exception as exc:
            log.warning("habit doc fetch failed: %s", exc)
    except Exception as exc:
        log.warning("Google external reads skipped: %s", exc)

    return {
        # Core
        "today": today,
        "tab_name": tab_name,
        # Per-weekday lists (strings; 7 entries indexed 0=Mon..6=Sun)
        "steps_row": steps_row,
        "sleep_row": sleep_row,
        "nutrition_row": nutrition_row,
        "strength_row": strength_row,
        "cardio_row": cardio_row,
        "sauna_row": sauna_row,
        "stretch_row": stretch_row,
        "cycle_row": cycle_row,
        "morning_star_row": morning_star_row,
        "night_star_row": night_star_row,
        # Notes row (html_report reads .get("notes_row", [text]))
        "notes_row": [notes_text] if notes_text else [],
        # Sleep
        "last_sleep": last_sleep,
        "avg_sleep": avg_sleep,
        "sleep_values": sleep_vals,
        # Steps
        "today_steps": today_steps,
        "total_steps": total_steps,
        "remaining_steps": remaining_steps,
        "pct_steps": pct_steps,
        # Calories
        "cal_values": cal_values,
        "cal_goal": cal_goal,
        # Cycle
        "phase_name": phase_name,
        "latest_cycle_str": latest_cycle_str,
        # Counts
        "strength_count": strength_count,
        "cardio_count": cardio_count,
        # Score + season
        "score": score,
        "season_done_indices": season_done_indices,
        # External (possibly empty)
        "travel_pins": travel_pins,
        "monthly_habits": monthly_habits,
        "quarterly_habits": quarterly_habits,
        "annual_habits": annual_habits,
    }


# ═══════════════════════════════════════════════════════════════════
# CLI — for local sanity checking against the live DB
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else local_today()
    data = gather_dashboard_data(d, live_steps=False)

    # Print a digestible summary (full dict is huge)
    print(json.dumps({
        "today": str(data["today"]),
        "tab_name": data["tab_name"],
        "weekday": data["today"].weekday(),
        "steps_row": data["steps_row"],
        "sleep_row": data["sleep_row"],
        "sauna_row": data["sauna_row"],
        "morning_star_row": data["morning_star_row"],
        "night_star_row": data["night_star_row"],
        "cycle_row": data["cycle_row"],
        "today_steps": data["today_steps"],
        "total_steps": data["total_steps"],
        "remaining_steps": data["remaining_steps"],
        "pct_steps": data["pct_steps"],
        "last_sleep": data["last_sleep"],
        "avg_sleep": data["avg_sleep"],
        "cal_values": data["cal_values"],
        "cal_goal": data["cal_goal"],
        "phase_name": data["phase_name"],
        "latest_cycle_str": data["latest_cycle_str"],
        "score_summary": {"total": data["score"]["total"],
                          "days_scored": list(data["score"]["daily"].keys())},
        "season_done_indices": sorted(data["season_done_indices"]),
        "travel_pins_count": len(data["travel_pins"]),
        "annual_habits_count": len(data["annual_habits"]),
    }, indent=2, default=str))
