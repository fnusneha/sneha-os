"""
Shared constants for the fitness automation pipeline.

All magic numbers, row mappings, color definitions, goals, and activity
type sets live here so every module imports from one place.
"""

from pathlib import Path

# ═══════════════════════════════════════════════════════════════════
# Paths
# ═══════════════════════════════════════════════════════════════════
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
LOG_FILE = LOG_DIR / "sync.log"
LAST_SYNC_FILE = SCRIPT_DIR / ".last_sync.json"

# ═══════════════════════════════════════════════════════════════════
# API / Auth
# ═══════════════════════════════════════════════════════════════════
OURA_BASE = "https://api.ouraring.com/v2/usercollection"
GARMIN_TOKEN_DIR = SCRIPT_DIR / ".garmin_tokens"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

# Calendar with period events
CALENDAR_ID = "fnu.sneha@gmail.com"

# Cycle config
CYCLE_LENGTH = 28

# ═══════════════════════════════════════════════════════════════════
# Google Sheets layout (1-indexed row numbers)
# ═══════════════════════════════════════════════════════════════════
ROW_NOTES = 2          # "Special Notes / Trips:" row
ROW_DATE_NUM = 3
ROW_STRENGTH = 5
ROW_CARDIO = 6
ROW_SAUNA = 7
ROW_STEPS = 8
ROW_STRETCH = 9
ROW_NUTRITION = 11
ROW_SLEEP = 12
ROW_CYCLE = 13
ROW_SEASON_PASS = 14        # Season pass done indices (comma-separated in B14)
ROW_CHALLENGE_HEADER = 21   # "⭐ DAILY STARS — max 3/day" header
ROW_MORNING_STAR = 19       # "✓" per day when morning ritual collected
ROW_NIGHT_STAR = 20         # "✓" per day when night ritual collected
ROW_DAILY_TOTAL = 22        # 0-3 daily star total

# Template tab name used when copying
TEMPLATE_TAB_NAME = "sheet1"

# Column mapping: weekday (0=Mon) → column letter
DAY_COL = {0: "C", 1: "D", 2: "E", 3: "F", 4: "G", 5: "H", 6: "I"}

# ═══════════════════════════════════════════════════════════════════
# Weekly & daily goals
# ═══════════════════════════════════════════════════════════════════
WEEKLY_STEPS_GOAL = 48000
WEEKLY_STRENGTH_GOAL = 3
WEEKLY_CARDIO_GOAL = 1
DAILY_STEPS_GOAL = 8000

# ═══════════════════════════════════════════════════════════════════
# Scoring thresholds
# ═══════════════════════════════════════════════════════════════════
SLEEP_STAR_THRESHOLD_DEFAULT = 7.0
SLEEP_STAR_THRESHOLD_LOW_ENERGY = 6.0   # Luteal-PMS & Menstrual phases
LOW_ENERGY_PHASES = {"Menstrual", "Luteal-PMS"}
# Core Missions star: earned when at least this many of 7 items are done
CORE_STAR_THRESHOLD = 4

# Weekly medal thresholds (max 3/day × 7 days = 21)
MEDAL_GOOD = 14     # 🥉 ~2 stars/day average
MEDAL_PERFECT = 21  # 🥇 all 3 every day

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
# Each entry: (start_day, end_day, label, guide_row)
# ═══════════════════════════════════════════════════════════════════
CYCLE_PHASES = [
    (1, 3, "Menstrual", 16),
    (4, 13, "Follicular", 17),
    (14, 16, "Ovulation", 18),
    (17, 23, "Luteal-EM", 19),
    (24, 28, "Luteal-PMS", 20),
]

# ═══════════════════════════════════════════════════════════════════
# Sheet formatting colors (RGB 0.0–1.0)
# ═══════════════════════════════════════════════════════════════════
PHASE_HIGHLIGHT = {"red": 1.0, "green": 0.95, "blue": 0.6}   # light yellow
PHASE_DEFAULT_BG = {"red": 1.0, "green": 1.0, "blue": 1.0}   # white
GOLD_BG = {"red": 0.95, "green": 0.82, "blue": 0.45}
GOLD_LIGHT = {"red": 1.0, "green": 0.96, "blue": 0.84}
DARK_TEXT = {"red": 0.2, "green": 0.15, "blue": 0.05}
WHITE_BG = {"red": 1, "green": 1, "blue": 1}

# ═══════════════════════════════════════════════════════════════════
# Calendar notes filtering
# ═══════════════════════════════════════════════════════════════════
NOTES_SKIP_STARTS = [
    "office", "habit:", "reminder", "task", "strength training",
    "cardio", "sprint", "commute", "get ready", "bike", "wash",
    "sauna", "potential", "weatherbug", "attending:", "holiday",
]

NOTES_TRIP_LOGISTICS = [
    "drive", "checkin", "check in", "arrange", "airbnb", "pack", "commute",
]

# ═══════════════════════════════════════════════════════════════════
# PMS Guide tips (shown in morning report)
# ═══════════════════════════════════════════════════════════════════
PMS_GUIDE_TIPS = {
    "Menstrual":   "Low energy → stretch, recover, yoga",
    "Follicular":  "Energy rising → strength training, heavier lifts",
    "Ovulation":   "Peak → PRs, heaviest lifts, strongest performance",
    "Luteal-EM":   "Stable energy → normal workouts",
    "Luteal-PMS":  "Energy drops → stretch, recover",
}

# ═══════════════════════════════════════════════════════════════════
# HTTP retry config
# ═══════════════════════════════════════════════════════════════════
MAX_RETRIES = 3
RATE_LIMIT_STATUS = 429
RATE_LIMIT_BACKOFF_SECS = 30

# Period lookback window
PERIOD_LOOKBACK_DAYS = 90
