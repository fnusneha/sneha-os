"""Shared constants for the Sneha.OS backend.

One place for goals, thresholds, activity type sets, Google OAuth
scopes, and calendar-notes filtering rules. Every module imports from
here so the numbers never drift.
"""

from pathlib import Path

# ═══════════════════════════════════════════════════════════════════
# Paths
# ═══════════════════════════════════════════════════════════════════
SCRIPT_DIR = Path(__file__).resolve().parent

# ═══════════════════════════════════════════════════════════════════
# External APIs
# ═══════════════════════════════════════════════════════════════════
OURA_BASE = "https://api.ouraring.com/v2/usercollection"
GARMIN_TOKEN_DIR = SCRIPT_DIR / ".garmin_tokens"

# Google OAuth scopes — one token covers all three APIs:
#   Sheets   → Travel Master Planner + cycling Library reads
#   Drive    → Habit Tracker Google Doc export
#   Calendar → cycle-day detection + Week Agenda events
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

# Primary calendar used for period + agenda events.
CALENDAR_ID = "fnu.sneha@gmail.com"

# Period-tracking lookback window (days) when scanning the calendar
# for the most recent "Periods" event.
PERIOD_LOOKBACK_DAYS = 90

# Menstrual cycle length in days.
CYCLE_LENGTH = 28

# ═══════════════════════════════════════════════════════════════════
# Daily + weekly goals
# ═══════════════════════════════════════════════════════════════════
DAILY_STEPS_GOAL = 8_000
WEEKLY_STEPS_GOAL = 48_000
WEEKLY_STRENGTH_GOAL = 3
WEEKLY_CARDIO_GOAL = 1

# ═══════════════════════════════════════════════════════════════════
# Scoring thresholds
# ═══════════════════════════════════════════════════════════════════
# Sleep star threshold is a uniform 6h across all cycle phases — the
# old "7h default, 6h during low-energy phases" split created confusing
# "why did I get the star this week but not last week at the same 6.5h"
# moments. The low-energy phase set is kept so the coach line can still
# tailor advice, but it no longer changes the star bar.
SLEEP_STAR_THRESHOLD_DEFAULT = 6.0
SLEEP_STAR_THRESHOLD_LOW_ENERGY = 6.0
LOW_ENERGY_PHASES = {"Menstrual", "Luteal-PMS"}

# Core Missions star: earned when at least this many of 7 items are done.
CORE_STAR_THRESHOLD = 4

# Weekly medal thresholds (max 3 stars/day × 7 days = 21).
MEDAL_GOOD = 14     # 🥉 roughly 2 stars/day average
MEDAL_PERFECT = 21  # 🥇 every star, every day

# ═══════════════════════════════════════════════════════════════════
# Garmin activity type sets
# ═══════════════════════════════════════════════════════════════════
STRENGTH_TYPES = {"strength_training"}
CARDIO_TYPES = {
    "road_biking", "cycling", "running", "trail_running",
    "treadmill_running", "indoor_cycling",
}
STRETCH_TYPES = {
    "yoga", "pilates", "stretching", "flexibility",
    "breathwork", "meditation",
}

# ═══════════════════════════════════════════════════════════════════
# Cycle phase lookup (day-of-cycle → phase label)
# Each entry: (start_day, end_day, label, legacy_guide_row)
# The fourth field is kept for backwards-compat with older records
# written before the Postgres migration and is ignored by current code.
# ═══════════════════════════════════════════════════════════════════
CYCLE_PHASES = [
    (1, 3, "Menstrual", 16),
    (4, 13, "Follicular", 17),
    (14, 16, "Ovulation", 18),
    (17, 23, "Luteal-EM", 19),
    (24, 28, "Luteal-PMS", 20),
]

# ═══════════════════════════════════════════════════════════════════
# Calendar notes filtering ("Week Agenda" card on Quest Hub)
# ═══════════════════════════════════════════════════════════════════
# Events whose summary starts with any of these (case-insensitive) are
# treated as noise and excluded from the weekly agenda.
NOTES_SKIP_STARTS = [
    "office", "habit:", "reminder", "task", "strength training",
    "cardio", "sprint", "commute", "get ready", "bike", "wash",
    "sauna", "potential", "weatherbug", "attending:", "holiday",
    # Out-of-office markers are implied when a trip event exists;
    # showing "OOO" separately is just noise.
    "ooo", "out of office",
]

# Events that are trip logistics. When a Travel:/Trip event is present
# for the same week, these are hidden (the trip line covers them).
NOTES_TRIP_LOGISTICS = [
    "drive", "checkin", "check in", "arrange", "airbnb", "pack", "commute",
]
