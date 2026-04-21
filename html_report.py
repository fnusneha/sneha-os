"""
Quest Hub v3 — HTML report generator.

Reads the template from templates/morning_report.html, fills dynamic
placeholders with fitness data from the Oura/Garmin/Sheets pipeline,
writes ~/morning_report.html, and optionally opens it in the browser.

Architecture
────────────
  oura_sheets_sync.py          ← syncs APIs → Google Sheet
      ↓ builds report_data dict
  html_report.py  (this file)  ← builds HTML sections from data
      ↓ fills template
  templates/morning_report.html ← pure HTML/CSS/JS with {{PLACEHOLDERS}}
      ↓ output
  ~/morning_report.html         ← served at /dashboard by MCP server
"""

import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_FILE = TEMPLATE_DIR / "morning_report.html"

# Weekly goals (shared with oura_sheets_sync.py scoring)
WEEKLY_STEPS_GOAL = 48_000
DAILY_STEPS_GOAL = 8_000
WEEKLY_STRENGTH_GOAL = 3
WEEKLY_CARDIO_GOAL = 1
CORE_STAR_THRESHOLD = 4  # items needed for core star
MEDAL_GOOD = 14    # 🥉
MEDAL_PERFECT = 21 # 🥇
MAX_WEEKLY_STARS = 21

# Cycle phase → (energy level, coaching advice)
PHASE_TIPS = {
    "Menstrual":  ("low energy", "Go easy — yoga, stretching, gentle walks."),
    "Follicular": ("energy rising", "Good day for heavier lifts."),
    "Ovulation":  ("peak energy", "Push for PRs — strongest performance window."),
    "Luteal-EM":  ("steady energy", "Normal workouts, stay consistent."),
    "Luteal-PMS": ("energy winding down", "Keep it light — stretch, recover."),
}

# Cycle phase → header pill emoji
CYCLE_ICONS = {
    "Follicular": "\U0001f331",  # 🌱
    "Ovulation":  "\U0001f315",  # 🌕
    "Luteal-EM":  "\U0001f317",  # 🌗
    "Luteal-PMS": "\U0001f317",  # 🌗
    "Menstrual":  "\U0001f534",  # 🔴
}

DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ═══════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════════

def _row_has(row: list, idx: int) -> bool:
    """True if sheet row[idx] is non-empty (e.g. strength_row[3] = 'Arms 30m')."""
    return bool(str(row[idx]).strip()) if idx < len(row) else False


def _pct(value: float, goal: float) -> int:
    """Percentage clamped to 0–100. Returns 0 if goal is 0."""
    return min(100, int(value / goal * 100)) if goal else 0


def _esc(text: str) -> str:
    """HTML-escape a string to prevent injection."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _quest_item(stage: str, index: int, icon: str, name: str,
                hint: str, done: bool = False) -> str:
    """Build one quest checklist row (shared by morning, core, night stages).

    Args:
        stage: 'morning', 'core', or 'night' — used in JS onclick handler.
        index: item index within the stage (0-based).
        icon:  emoji icon.
        name:  display name.
        hint:  short description.
        done:  whether pre-checked from sheet data.
    """
    if stage == "core":
        # Core items are read-only status indicators (no checkbox, no click)
        status = '\u2705' if done else '\u25CB'  # ✅ or ○
        done_cls = " core-done" if done else " core-pending"
        return (
            f'<div class="q-item{done_cls}" data-readonly="true">'
            f'<span class="core-status">{status}</span>'
            f'<span class="q-icon">{icon}</span>'
            f'<div class="q-text">'
            f'<div class="q-name">{_esc(name)}</div>'
            f'<div class="q-hint">{_esc(hint)}</div>'
            f'</div></div>'
        )
    cls = "q-item done" if done else "q-item"
    return (
        f'<div class="{cls}" onclick="toggleQuestItem(this,\'{stage}\',{index})">'
        f'<div class="q-check"></div>'
        f'<span class="q-icon">{icon}</span>'
        f'<div class="q-text">'
        f'<div class="q-name">{_esc(name)}</div>'
        f'<div class="q-hint">{_esc(hint)}</div>'
        f'</div></div>'
    )


# ═══════════════════════════════════════════════════════════════════
# STAR COUNTING
# ═══════════════════════════════════════════════════════════════════

def _count_core_items(data: dict, weekday: int) -> int:
    """Count how many of the 7 Core Mission items were done on a given day.

    Checks: steps, sleep, calories (from score dict) + strength,
    cardio, sauna, stretch (from sheet rows).
    """
    daily = data["score"].get("daily", {}).get(weekday, {})
    return sum([
        bool(daily.get("steps")),
        bool(daily.get("sleep")),
        bool(daily.get("cal")),
        _row_has(data.get("strength_row", []), weekday),
        _row_has(data.get("cardio_row", []), weekday),
        _row_has(data.get("sauna_row", []), weekday),
        _row_has(data.get("stretch_row", []), weekday),
    ])


def _day_earned_core_star(data: dict, weekday: int) -> bool:
    """Return True if ≥4 of 7 Core Mission items were done (earns 1 star)."""
    return _count_core_items(data, weekday) >= CORE_STAR_THRESHOLD


# ═══════════════════════════════════════════════════════════════════
# SECTION BUILDERS — each returns an HTML fragment string
# ═══════════════════════════════════════════════════════════════════

# ── Week Strip (7-day overview) ──────────────────────────────────

def _build_day_details_payload(data: dict, weekday: int) -> dict:
    """Build a JSON payload with per-day breakdown for the past/current days.

    Returned structure:
      {
        0: {                     # weekday index
          "day_label": "Mon",
          "date_num": 13,
          "is_today": false,
          "stars": 2,             # total stars earned that day
          "morning_done": true,
          "night_done": false,
          "core_count": 5,
          "core_items": [          # 7 items, each with name, done bool, value text
            {"name": "Steps", "done": true, "value": "6,094"},
            ...
          ]
        },
        ...
      }
    """
    from datetime import date, timedelta
    morning_star_row = data.get("morning_star_row", [])
    night_star_row = data.get("night_star_row", [])
    steps_row = data.get("steps_row", [])
    sleep_row = data.get("sleep_row", [])
    cal_values = data.get("cal_values", []) or []
    strength_row = data.get("strength_row", [])
    cardio_row = data.get("cardio_row", [])
    sauna_row = data.get("sauna_row", [])
    stretch_row = data.get("stretch_row", [])
    cycle_row = data.get("cycle_row", [])

    # Compute the Monday date so we can show actual dates (e.g. "Mon, Apr 13")
    today = data.get("today") or date.today()
    try:
        monday = today - timedelta(days=today.weekday())
    except AttributeError:
        monday = None

    def _cell(row, wd):
        if wd < len(row):
            v = str(row[wd]).strip()
            return v if v else ""
        return ""

    details = {}
    for wd in range(weekday + 1):  # only past + today (never future)
        # Star counts
        morning_done = _cell(morning_star_row, wd) == "\u2713"
        night_done = _cell(night_star_row, wd) == "\u2713"
        core_count = _count_core_items(data, wd)
        core_earned = core_count >= CORE_STAR_THRESHOLD
        stars = int(morning_done) + int(core_earned) + int(night_done)

        # Core item breakdown with the actual values
        daily = data["score"].get("daily", {}).get(wd, {})
        steps_val = _cell(steps_row, wd)
        sleep_val = _cell(sleep_row, wd)
        cal_val = cal_values[wd] if wd < len(cal_values) and cal_values[wd] else None
        core_items = [
            {
                "name": "🚶 Steps",
                "done": bool(daily.get("steps")),
                "value": f"{int(steps_val.replace(',','')):,}" if steps_val.replace(',','').isdigit() else (steps_val or "—"),
                "target": "≥ 8,000",
            },
            {
                "name": "😴 Sleep",
                "done": bool(daily.get("sleep")),
                "value": f"{sleep_val}h" if sleep_val else "—",
                "target": "≥ 7h",
            },
            {
                "name": "🍽️ Calories",
                "done": bool(daily.get("cal")),
                "value": f"{cal_val:,}" if cal_val else "—",
                "target": "logged",
            },
            {
                "name": "💪 Strength",
                "done": _row_has(strength_row, wd),
                "value": _cell(strength_row, wd) or "—",
                "target": "any session",
            },
            {
                "name": "🚴 Cardio",
                "done": _row_has(cardio_row, wd),
                "value": _cell(cardio_row, wd) or "—",
                "target": "any session",
            },
            {
                "name": "🧘 Stretch",
                "done": _row_has(stretch_row, wd),
                "value": _cell(stretch_row, wd) or "—",
                "target": "any session",
            },
            {
                "name": "♨️ Sauna",
                "done": _row_has(sauna_row, wd),
                "value": _cell(sauna_row, wd) or "—",
                "target": "any session",
            },
        ]

        # Date label
        date_str = ""
        if monday:
            day_date = monday + timedelta(days=wd)
            date_str = day_date.strftime("%a, %b %-d") if hasattr(day_date, "strftime") else ""

        details[wd] = {
            "day_label": DAY_LABELS[wd],
            "date_str": date_str,
            "is_today": wd == weekday,
            "stars": stars,
            "morning_done": morning_done,
            "night_done": night_done,
            "core_count": core_count,
            "core_earned": core_earned,
            "core_threshold": CORE_STAR_THRESHOLD,
            "core_items": core_items,
            "cycle": _cell(cycle_row, wd),
        }
    return details


def _build_pulse_days(data: dict, weekday: int) -> str:
    """Build the 7-day bubble strip for the Weekly Pulse card.

    Past/today bubbles are clickable → open a modal with that day's breakdown.
    Future bubbles are faded and inert.
    """
    morning_star_row = data.get("morning_star_row", [])
    night_star_row = data.get("night_star_row", [])

    bubbles = []
    for wd in range(7):
        is_today = (wd == weekday)
        is_future = (wd > weekday)

        if is_future:
            day_stars = 0
        else:
            day_stars = 0
            # Morning star
            if wd < len(morning_star_row) and str(morning_star_row[wd]).strip() == "\u2713":
                day_stars += 1
            # Core star
            if _count_core_items(data, wd) >= CORE_STAR_THRESHOLD:
                day_stars += 1
            # Night star
            if wd < len(night_star_row) and str(night_star_row[wd]).strip() == "\u2713":
                day_stars += 1

        if is_future:
            cls = "wp-day is-future"
            num_html = ""
        elif is_today:
            cls = "wp-day is-today wp-day-clickable"
            num_html = f'<span class="wp-day-num" data-day="{wd}">{day_stars}</span>'
        elif day_stars > 0:
            cls = "wp-day has-stars wp-day-clickable"
            num_html = f'<span class="wp-day-num" data-day="{wd}">{day_stars}</span>'
        else:
            cls = "wp-day zero-stars wp-day-clickable"
            num_html = f'<span class="wp-day-num" data-day="{wd}">0</span>'

        # data-wd attribute lets JS know which day to open on click
        data_attr = f' data-wd="{wd}" onclick="showDayDetails({wd})" tabindex="0"' if not is_future else ""
        bubbles.append(
            f'<div class="{cls}"{data_attr}>'
            f'{num_html}'
            f'<span class="wp-day-lbl">{DAY_LABELS[wd]}</span>'
            f'</div>'
        )
    return "".join(bubbles)


# ── Morning Ritual (4 items, no API data — localStorage only) ────

def _build_morning_ritual(data: dict) -> str:
    """4 morning habits. Not pre-checked — state comes from localStorage."""
    items = [
        ("\U0001f6bf", "Body Reset",        "Brush, floss, shower, skin care"),
        ("\u2615",     "Activate Systems",   "Fuel, hydrate, supplements + protein"),
        ("\U0001f4c5", "Lock Agenda",        "Review calendar, set alarms"),
        ("\U0001f3af", "Anchor Intention",   "Sync morning metrics, set daily focus"),
    ]
    return "\n".join(
        _quest_item("morning", i, icon, name, hint)
        for i, (icon, name, hint) in enumerate(items)
    )


# ── Core Missions (5 items, pre-checked from Google Sheet data) ──

def _build_core_missions(data: dict, weekday: int) -> str:
    """7 core habits — pre-checked from sheet/API data (read-only in the UI).

    Hint text is dynamic so the user can see live progress toward the goal
    (e.g. 'Need 6,591 more · 1,409 / 8,000') without opening the day-details
    modal. The user asked for this so a pull-to-refresh confirms new data
    is flowing.
    """
    from constants import DAILY_STEPS_GOAL
    daily = data["score"].get("daily", {}).get(weekday, {})

    # ── Steps: live from Oura (data['today_steps']) for today, else from sheet
    steps_row = data.get("steps_row", [])
    if weekday == data["today"].weekday():
        steps_today = data.get("today_steps", 0) or 0
    else:
        try:
            steps_today = int(str(steps_row[weekday]).replace(",", "")) if weekday < len(steps_row) and str(steps_row[weekday]).strip() else 0
        except (ValueError, IndexError):
            steps_today = 0
    steps_done = steps_today >= DAILY_STEPS_GOAL
    if steps_done:
        steps_hint = f"Done \u2713  \u00b7  {steps_today:,} / {DAILY_STEPS_GOAL:,}"
    else:
        steps_left = DAILY_STEPS_GOAL - steps_today
        steps_hint = f"Need {steps_left:,} more  \u00b7  {steps_today:,} / {DAILY_STEPS_GOAL:,}"

    # ── Sleep: hours logged last night (from sheet)
    sleep_row = data.get("sleep_row", [])
    last_sleep = data.get("last_sleep")
    sleep_done = bool(daily.get("sleep"))
    if last_sleep is not None:
        # Threshold is 7 in non-low-energy phases, 8 otherwise. Use the
        # daily score bool for truth, just show the delta either way.
        target = 7
        delta = target - last_sleep
        if sleep_done:
            sleep_hint = f"Done \u2713  \u00b7  {last_sleep}h / {target}h"
        elif delta > 0:
            # e.g. "0.1h short · 6.9h / 7h"
            sleep_hint = f"{delta:.1f}h short  \u00b7  {last_sleep}h / {target}h"
        else:
            sleep_hint = f"{last_sleep}h logged"
    else:
        sleep_hint = "No sleep data yet"

    # ── Calories: logged vs. goal (from Garmin Connect / MFP)
    cal_goal = data.get("cal_goal", 0)
    cal_values = data.get("cal_values", []) or []
    today_cal = cal_values[weekday] if weekday < len(cal_values) and cal_values[weekday] else 0
    cal_done = bool(daily.get("cal"))
    if cal_done and today_cal:
        cal_hint = f"{today_cal:,} logged  \u00b7  target {cal_goal:,}"
    else:
        cal_hint = f"Not logged yet  \u00b7  target {cal_goal:,}"

    # ── Strength / Cardio / Stretch / Sauna — show what was logged (or "—")
    def _row_val(row_key):
        row = data.get(row_key, [])
        if weekday < len(row):
            v = str(row[weekday]).strip()
            return v if v else None
        return None

    strength_v = _row_val("strength_row")
    cardio_v   = _row_val("cardio_row")
    stretch_v  = _row_val("stretch_row")
    sauna_v    = _row_val("sauna_row")

    strength_hint = f"Logged: {strength_v}" if strength_v else "Lift session needed"
    cardio_hint   = f"Logged: {cardio_v}"   if cardio_v   else "Ride or run needed"
    stretch_hint  = f"Logged: {stretch_v}"  if stretch_v  else "Yoga / mobility needed"
    sauna_hint    = f"Logged: {sauna_v}"    if sauna_v    else "Heat recovery needed"

    missions = [
        ("\U0001f4aa",    "Strength",       strength_hint, bool(strength_v)),
        ("\U0001f6b4",    "Cardio",         cardio_hint,   bool(cardio_v)),
        ("\U0001f45f",    "8,000 Steps",    steps_hint,    steps_done),
        ("\U0001f357",    "Calories Logged", cal_hint,     cal_done),
        ("\U0001f634",    "Sleep 7h+",      sleep_hint,    sleep_done),
        ("\U0001f9d8",    "Stretch",        stretch_hint,  bool(stretch_v)),
        ("\u2668\ufe0f",  "Sauna / Steam",  sauna_hint,    bool(sauna_v)),
    ]
    return "\n".join(
        _quest_item("core", i, icon, name, hint, done)
        for i, (icon, name, hint, done) in enumerate(missions)
    )


# ── Night Ritual (4 items, no API data — localStorage only) ──────

def _build_night_ritual(data: dict) -> str:
    """4 evening wind-down habits. Not pre-checked."""
    items = [
        ("\U0001f9f4",       "Reset Routine",     "Hygiene (retinol), water, clean space"),
        ("\U0001f56f\ufe0f", "Quiet Environment", "Dinner set, space tidy, no loose ends"),
        ("\U0001f4dd",       "Unload Mind",       "Brain dump, finalize tomorrow's key actions"),
        ("\U0001f634",       "Sleep Protocol",    "In bed by 10 PM, devices down"),
    ]
    return "\n".join(
        _quest_item("night", i, icon, name, hint)
        for i, (icon, name, hint) in enumerate(items)
    )


# ── Coach Line ───────────────────────────────────────────────────

def _build_coach_line(phase_name: str, last_sleep: float | None) -> str:
    """One-liner coaching advice based on cycle phase and sleep quality."""
    parts = []
    tip = PHASE_TIPS.get(phase_name)
    if tip:
        energy, advice = tip
        parts.append(
            f"<strong>{_esc(phase_name)}</strong> &mdash; "
            f"<em>{_esc(energy)}.</em> {_esc(advice)}"
        )
    if last_sleep is not None and last_sleep < 7:
        parts.append("Sleep was a touch short &mdash; keep cardio conversational.")
    parts.append("Log food early so you\u2019re not playing catch-up tonight.")
    return " ".join(parts)


# ── Pillar Health (6 life pillars with % bars) ───────────────────

def _build_pillars_html(data: dict) -> str:
    """6 expandable pillar cards — all computed from real data sources.

    Data sources:
        Systems  → sleep average (Oura) + step progress (Oura/Garmin)
        Strength → workout sessions vs weekly goal (Garmin)
        Finance  → calorie tracking consistency (Garmin/MFP) as discipline proxy
        Travel   → booked/completed trips vs total planned (Travel Sheet)
        Mental   → sleep quality nights ≥7h + cycle awareness (Oura + Calendar)
    """
    avg_sleep = data.get("avg_sleep") or 0
    pct_steps = data.get("pct_steps", 0)
    strength = data.get("strength_count", 0)
    cardio = data.get("cardio_count", 0)
    sleep_values = data.get("sleep_values", [])
    cal_values = data.get("cal_values", [])
    cal_goal = data.get("cal_goal", 0)
    travel_pins = data.get("travel_pins", [])
    phase_name = data.get("phase_name", "")
    score = data.get("score", {})

    # ── Systems: sleep quality (60%) + step progress (40%) ──
    sleep_score = min(100, int((avg_sleep / 8) * 100)) if avg_sleep else 0
    systems_pct = int(sleep_score * 0.6 + pct_steps * 0.4)
    if avg_sleep:
        systems_reason = f"Sleep avg {avg_sleep:.1f}h, steps {pct_steps}%"
    else:
        systems_reason = "No sleep data yet"

    # ── Strength: workouts done vs goal (strength 3x + cardio 1x = 4) ──
    workout_goal = WEEKLY_STRENGTH_GOAL + WEEKLY_CARDIO_GOAL
    workout_total = strength + cardio
    strength_pct = min(100, int(workout_total / workout_goal * 100)) if workout_goal else 0
    strength_reason = f"{strength} strength + {cardio} cardio this week"

    # ── Finance: calorie logging discipline (days logged / days elapsed) ──
    # Using nutrition tracking as a discipline proxy — consistently logging
    # food reflects the same habits that drive financial tracking
    cal_logged = sum(1 for c in cal_values if c is not None)
    today_wd = data.get("today").weekday() if data.get("today") else 0
    days_elapsed = max(1, today_wd + 1)
    finance_pct = min(100, int((cal_logged / days_elapsed) * 100))
    if cal_logged > 0 and cal_goal:
        cal_on_target = sum(1 for c in cal_values if c is not None and c <= cal_goal)
        finance_reason = f"{cal_logged}/{days_elapsed} days logged, {cal_on_target} on target"
    elif cal_logged > 0:
        finance_reason = f"{cal_logged}/{days_elapsed} days logged"
    else:
        finance_reason = "No nutrition data yet"

    # ── Travel: completed + booked trips vs total planned ──
    if travel_pins:
        completed = sum(1 for t in travel_pins if t.get("status") == "Completed")
        booked = sum(1 for t in travel_pins if t.get("status") == "Booked")
        total_trips = len(travel_pins)
        # Completed = 100%, Booked = 50% credit (planned but not done yet)
        travel_score = completed * 100 + booked * 50
        travel_pct = min(100, int(travel_score / total_trips)) if total_trips else 0
        travel_reason = f"{completed} done, {booked} booked of {total_trips} trips"
    else:
        travel_pct = 0
        travel_reason = "No travel data"

    # ── Mental: good sleep nights (≥7h) + cycle tracking active ──
    good_nights = sum(1 for s in sleep_values if s >= 7) if sleep_values else 0
    total_nights = len(sleep_values) if sleep_values else 0
    sleep_quality_pct = int((good_nights / total_nights) * 100) if total_nights else 0
    cycle_bonus = 10 if phase_name else 0  # +10% for actively tracking cycle
    mental_pct = min(100, sleep_quality_pct + cycle_bonus)
    if total_nights > 0 and phase_name:
        mental_reason = f"{good_nights}/{total_nights} nights ≥7h, cycle tracked"
    elif total_nights > 0:
        mental_reason = f"{good_nights}/{total_nights} nights ≥7h"
    else:
        mental_reason = "No sleep data yet"

    pillars = [
        ("Systems",  "var(--amber)",  systems_pct,  systems_reason),
        ("Strength", "var(--coral)",  strength_pct, strength_reason),
        ("Finance",  "var(--gold)",   finance_pct,  finance_reason),
        ("Travel",   "var(--mint)",   travel_pct,   travel_reason),
        ("Mental",   "var(--violet)", mental_pct,   mental_reason),
    ]

    cards = []
    for name, color, pct, reason in pillars:
        cards.append(
            f'<div class="pillar" onclick="togglePillar(this)">'
            f'<div class="pillar-top">'
            f'<span class="pillar-name">{name}</span>'
            f'<span class="pillar-pct" style="color:{color}">{pct}%</span>'
            f'</div>'
            f'<div class="pillar-bar">'
            f'<div class="pillar-fill" style="width:{pct}%;background:{color};"></div>'
            f'</div>'
            f'<div class="pillar-reason">{_esc(reason)}</div>'
            f'</div>'
        )
    return "\n".join(cards)


# ── Season Pass (monthly habit tracker) ──────────────────────────

def _build_season_pass(data: dict) -> tuple[str, int, int, str]:
    """Build the monthly habits section from Google Doc data.

    Uses monthly + quarterly habits from the doc. Falls back to a
    hardcoded list if the doc data isn't available.

    Returns:
        (month_name, done_count, total_count, items_html)
    """
    month_name = data["today"].strftime("%B %Y")

    # ── Build habits list from doc data or hardcoded fallback ──
    doc_monthly = data.get("monthly_habits", [])
    doc_quarterly = data.get("quarterly_habits", [])

    if doc_monthly:
        # Merge monthly + quarterly into one Season Pass list
        habits = []
        for h in doc_monthly + doc_quarterly:
            habits.append((
                h.get("icon", "📋"),
                h["name"],
                h.get("cadence", "monthly"),
                "due",        # default status — completion tracked in localStorage
                "due",
            ))
    else:
        # Hardcoded fallback (original list)
        habits = [
            ("\U0001f486", "Deep Tissue Massage",     "every 3 weeks",  "Due",  "due"),
            ("\u2728",     "Facial",                   "monthly",        "Due",  "due"),
            ("\U0001f9ea", "Renpho Body Check",        "every 2 weeks",  "Due",  "due"),
            ("\U0001f4b0", "Finance Check",            "monthly",        "Due",  "due"),
            ("\U0001f9e0", "Emotional Check-In",       "monthly",        "Due",  "due"),
            ("\u2708\ufe0f", "Travel Maintenance",     "monthly",        "Due",  "due"),
            ("\U0001f5d3", "Digital Cleanup",           "bi-monthly",    "Due",  "due"),
            ("\U0001f5d3", "Deep Cleaning & Refills",   "monthly",       "Due",  "due"),
        ]

    season_done_indices = data.get("season_done_indices", set())
    done_count = sum(1 for i in range(len(habits)) if i in season_done_indices)
    total = len(habits)

    rows = []
    for i, (icon, name, cadence, _last_text, status) in enumerate(habits):
        is_done = i in season_done_indices
        si_cls = "season-item si-done" if is_done else "season-item"
        status_label = cadence
        rows.append(
            f'<div class="{si_cls}" onclick="toggleSeasonItem(this,{i})">'
            f'<div class="si-check"></div>'
            f'<div class="si-info">'
            f'<div class="si-name">{icon} {_esc(name)}</div>'
            f'<div class="si-meta">{_esc(cadence)}</div>'
            f'</div>'
            f'<span class="si-status si-status-{status}">{_esc(status_label)}</span>'
            f'</div>'
        )
    return month_name, done_count, total, "\n".join(rows)


# ── Pins · 2026 (annual milestones timeline) ────────────────────

# Fallback if no doc data — hardcoded annual habit pins
FALLBACK_ANNUAL_PINS = [
    ("p-annual-goals",    True,  "Annual Goal Setting", "Jan", "\U0001f3af", "habit", "2026-01-01"),
    ("p-dentist-1",       True,  "Dentist Visit #1",    "Mar", "\U0001f601", "habit", "2026-03-01"),
    ("p-tax-docs",        True,  "Tax Docs + File",     "Feb", "\U0001f9fe", "habit", "2026-02-01"),
    ("p-annual-physical", False, "Annual Physical Exam", "Jan", "\U0001fa7a", "habit", "2026-01-01"),
    ("p-dentist-2",       False, "Dentist Visit #2",    "Sep", "\U0001f601", "habit", "2026-09-01"),
]


def _build_pins_from_doc(annual_habits: list) -> list[tuple]:
    """Convert doc annual habits into pin tuples using months from the doc.

    Each habit's ``months`` field (parsed from "- March and Sep" suffixes)
    determines which month(s) the pin appears in. Habits with multiple
    months (e.g. Dentist in Mar + Sep) create one pin per month.

    Args:
        annual_habits: List of habit dicts from the doc's annual section.

    Returns:
        List of pin tuples: (id, pinned, label, month, icon, source).
    """
    import datetime
    today = datetime.date.today()
    current_month_idx = today.month  # 1-12

    MONTH_TO_IDX = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
                    "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}

    pins = []
    for h in annual_habits:
        name = h["name"]
        icon = h.get("icon", "\U0001f4cb")
        doc_months = h.get("months", [])

        if not doc_months:
            # No month specified — put in Jan as default
            doc_months = ["Jan"]

        for i, month in enumerate(doc_months):
            # Create a unique pin ID from name + month
            slug = name.lower().replace(" ", "-").replace("+", "")[:20]
            suffix = f"-{i+1}" if len(doc_months) > 1 else ""
            pid = f"p-{slug}{suffix}"

            # Label: add "#N" if multiple occurrences (e.g. "Dentist #1", "Dentist #2")
            if len(doc_months) > 1:
                label = f"{name} #{i+1}"
            else:
                label = name

            # Pinned if the month has already passed this year
            month_idx = MONTH_TO_IDX.get(month, 1)
            pinned = month_idx < current_month_idx

            # Habit pins get a synthetic date (1st of month) for sorting
            month_num = MONTH_TO_IDX.get(month, 1)
            year = today.year
            pins.append((pid, pinned, label, month, icon, "habit", f"{year}-{month_num:02d}-01"))

    return pins


def _build_pins_html(data: dict = None) -> str:
    """Build the annual milestone timeline, grouped by month.

    Merges annual habits from the Google Doc with hardcoded one-time
    events. Falls back to hardcoded pins if doc data is unavailable.

    Done pins show pinned icon, upcoming show their content icon.
    Upcoming items in current month get class "soon" (brighter),
    future months get "upcoming" (dimmer). Source tags (CAL/HABIT) added.

    Args:
        data: Optional report_data dict containing annual_habits from doc.

    Returns:
        HTML string for the pins timeline.
    """
    import datetime
    current_month = datetime.date.today().strftime("%b")

    MONTH_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    # Build annual habit pins from doc or fallback
    annual_habits = (data or {}).get("annual_habits", [])
    if annual_habits:
        habit_pins = _build_pins_from_doc(annual_habits)
    else:
        habit_pins = list(FALLBACK_ANNUAL_PINS)

    # Build travel pins from live sheet data
    # Pin tuple: (pid, pinned, label, month, icon, source, start_date, year)
    travel_data = (data or {}).get("travel_pins", [])
    current_year = datetime.date.today().year
    cal_pins = []
    for trip in travel_data:
        name_slug = trip["name"].lower().replace(" ", "-").replace("·", "").replace("'", "")[:30]
        trip_year = int(trip.get("year") or current_year)
        # Include both year AND start_date in the id so multiple entries
        # with the same name (e.g. 4 "Tahoe Ski Weekend" trips in 2027)
        # don't collapse into one via the dedup pass.
        date_slug = (trip.get("start_date") or "").replace(",", "").replace(" ", "")[:12]
        pid = f"p-travel-{name_slug}-{trip_year}-{date_slug}"
        cal_pins.append((
            pid, trip["pinned"], trip["name"], trip["month"],
            trip.get("icon", "\u2708\ufe0f"), "cal", trip.get("start_date", ""),
            trip_year,
        ))

    # Annual habit pins belong to the current year
    habit_pins_with_year = [(p + (current_year,)) for p in habit_pins]

    # Combine: doc habit pins + live travel pins
    all_pins = habit_pins_with_year + cal_pins

    # Sort by year, then month, then pinned-first, then start date
    def sort_key(pin):
        month_idx = MONTH_ORDER.index(pin[3]) if pin[3] in MONTH_ORDER else 99
        start_date = pin[6] if len(pin) > 6 else ""
        year = pin[7] if len(pin) > 7 else current_year
        return (year, month_idx, not pin[1], start_date)

    all_pins.sort(key=sort_key)

    # Deduplicate by pin ID
    seen_ids = set()
    deduped = []
    for pin in all_pins:
        if pin[0] not in seen_ids:
            seen_ids.add(pin[0])
            deduped.append(pin)

    # Group by (year, month)
    by_year: dict[int, dict[str, list]] = {}
    for pin in deduped:
        pid, pinned, label, month, content_icon, source = pin[:6]
        year = pin[7] if len(pin) > 7 else current_year
        by_year.setdefault(year, {}).setdefault(month, []).append(
            (pid, pinned, label, content_icon, source, month)
        )

    parts = []
    for year in sorted(by_year.keys()):
        months = by_year[year]
        # Year header for multi-year timelines — keeps 2026 and 2027 clearly
        # separated. For single-year (shouldn't happen mid-2026 but safe), we
        # still render the header to keep the UX consistent.
        is_future_year = year > current_year
        year_cls = "pin-year-header" + (" pin-year-future" if is_future_year else "")
        sub = " &middot; roadmap" if is_future_year else ""
        parts.append(f'<div class="{year_cls}"><span class="pin-year-num">{year}</span><span class="pin-year-sub">{sub}</span></div>')

        for month in MONTH_ORDER:
            items = months.get(month, [])
            if not items:
                continue
            parts.append(f'<div class="pin-month">{month.upper()}</div>')
            for pid, default_pinned, label, content_icon, source, item_month in items:
                # Future-year pins are always "upcoming" regardless of month
                if default_pinned and not is_future_year:
                    cls = "pin-row pinned"
                elif is_future_year:
                    cls = "pin-row upcoming"
                elif item_month == current_month:
                    cls = "pin-row soon"
                else:
                    cls = "pin-row upcoming"

                icon = "\U0001f4cd" if (default_pinned and not is_future_year) else content_icon

                if source == "cal":
                    tag = '<span class="pin-src pin-src-cal">CAL</span>'
                else:
                    tag = '<span class="pin-src pin-src-habit">HABIT</span>'

                server_pinned = ' data-server-pinned="true"' if (default_pinned and not is_future_year) else ''
                parts.append(
                    f'<div class="{cls}"{server_pinned} data-pid="{pid}" data-icon="{content_icon}" onclick="togglePin(this,\'{pid}\')">'
                    f'<span class="pin-icon">{icon}</span>'
                    f'<span class="pin-label">{_esc(label)}</span>'
                    f'{tag}'
                    f'</div>'
                )
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════
# TEMPLATE ENGINE
# ═══════════════════════════════════════════════════════════════════

def _load_template() -> str:
    """Read the HTML template from disk."""
    return TEMPLATE_FILE.read_text(encoding="utf-8")


def _fill_template(template: str, placeholders: dict[str, str]) -> str:
    """Replace all {{KEY}} markers in the template with computed values."""
    html = template
    for key, value in placeholders.items():
        html = html.replace(f"{{{{{key}}}}}", str(value))
    return html


# ═══════════════════════════════════════════════════════════════════
# PUBLIC API — called by oura_sheets_sync.py
# ═══════════════════════════════════════════════════════════════════

def generate_html_report(data: dict) -> str:
    """Generate the Quest Hub dashboard HTML.

    Args:
        data: report_data dict from generate_morning_report() containing:
            - today (date), tab_name (str)
            - last_sleep (float|None), avg_sleep (float), sleep_values (list)
            - phase_name (str), latest_cycle_str (str)
            - today_steps (int), total_steps (int), pct_steps (int)
            - strength_count (int), cardio_count (int)
            - cal_values (list[int|None]), cal_goal (int)
            - score (dict with 'daily' sub-dict)
            - strength_row, cardio_row, sauna_row, stretch_row (list)

    Returns:
        Full HTML string. Also writes to ~/morning_report.html.
    """
    today = data["today"]
    weekday = today.weekday()
    last_sleep = data.get("last_sleep")
    phase_name = data.get("phase_name", "")

    # ── Header pills ───────────────────────────────────────────
    sleep_label = f"{last_sleep}h" if last_sleep is not None else "\u2013"
    sleep_emoji = "\U0001f634" if last_sleep and last_sleep >= 7 else "\U0001f62a"
    cycle_icon = CYCLE_ICONS.get(phase_name, "\U0001f534")

    # ── Today's calories ───────────────────────────────────────
    cal_values = data.get("cal_values", [])
    cal_goal = data.get("cal_goal", 0)
    today_cal = cal_values[weekday] if weekday < len(cal_values) and cal_values[weekday] is not None else 0
    today_steps = data.get("today_steps", 0)

    # ── Weekly stars (fully server-side from sheet data) ──
    morning_star_row = data.get("morning_star_row", [])
    night_star_row = data.get("night_star_row", [])

    weekly_stars = 0
    for wd in range(weekday + 1):
        # Morning star from row 19
        if wd < len(morning_star_row) and str(morning_star_row[wd]).strip() == "\u2713":
            weekly_stars += 1
        # Core star (4 of 7 items)
        if _day_earned_core_star(data, wd):
            weekly_stars += 1
        # Night star from row 20
        if wd < len(night_star_row) and str(night_star_row[wd]).strip() == "\u2713":
            weekly_stars += 1

    # Today's slot states (for the 3-circle hero)
    today_core_items = _count_core_items(data, weekday)
    today_core_earned = today_core_items >= CORE_STAR_THRESHOLD
    today_morning_earned = (weekday < len(morning_star_row) and
                            str(morning_star_row[weekday]).strip() == "\u2713")
    today_night_earned = (weekday < len(night_star_row) and
                          str(night_star_row[weekday]).strip() == "\u2713")

    # Season pass done indices (from sheet row 14)
    season_done_indices = data.get("season_done_indices", set())

    # Week progress bar
    xp_pct = _pct(weekly_stars, MAX_WEEKLY_STARS)
    medal_good_cls = "wp-medal-marker lit" if weekly_stars >= MEDAL_GOOD else "wp-medal-marker dim"
    medal_perfect_cls = "wp-medal-marker lit" if weekly_stars >= MEDAL_PERFECT else "wp-medal-marker dim"

    # ── Build all HTML sections ────────────────────────────────
    pulse_days_html    = _build_pulse_days(data, weekday)
    day_details_payload = _build_day_details_payload(data, weekday)
    morning_ritual_html = _build_morning_ritual(data)
    core_missions_html  = _build_core_missions(data, weekday)
    night_ritual_html   = _build_night_ritual(data)
    coach_line          = _build_coach_line(phase_name, last_sleep)
    pillars_html        = _build_pillars_html(data)
    pins_html           = _build_pins_html(data)

    # Season pass (returns tuple)
    season_month, season_done, season_total, season_items_html = _build_season_pass(data)
    season_pct = _pct(season_done, season_total)
    if season_done == season_total:
        season_badge_cls, season_badge_text = "badge-complete", "Complete"
    elif season_done >= season_total // 2:
        season_badge_cls, season_badge_text = "badge-track", "On Track"
    else:
        season_badge_cls, season_badge_text = "badge-behind", "Behind"

    # ── Fill template ──────────────────────────────────────────
    template = _load_template()
    html = _fill_template(template, {
        # Hero bar
        "SLEEP_EMOJI":         sleep_emoji,
        "SLEEP_LABEL":         sleep_label,
        "CYCLE_ICON":          cycle_icon,
        "CYCLE_LABEL":         data.get("latest_cycle_str") or "\u2013",
        # Weekly Pulse card
        "WEEKLY_STARS":        str(weekly_stars),
        "PULSE_DAYS_HTML":     pulse_days_html,
        "DAY_DETAILS_JSON":    json.dumps(day_details_payload, ensure_ascii=False),
        "SEASON_MONTH_SHORT":  today.strftime("%b"),
        "TODAY_MORNING_CLS":   "slot-earned" if today_morning_earned else "slot-empty",
        "TODAY_CORE_CLS":      "slot-earned" if today_core_earned else "slot-empty",
        "TODAY_CORE_COUNT":    str(today_core_items),
        "TODAY_NIGHT_CLS":     "slot-earned" if today_night_earned else "slot-empty",
        # Core progress banner: how many done vs. threshold (so the UI
        # always answers "how many do I need to hit for the star?")
        "CORE_DONE_COUNT":     str(today_core_items),
        "CORE_NEEDED_COUNT":   str(max(0, CORE_STAR_THRESHOLD - today_core_items)),
        "CORE_PROGRESS_PCT":   str(min(100, round((today_core_items / 7) * 100))),
        # Manual-log toggle: sauna today. Values: "done" class if ticked,
        # empty otherwise; sibling state text reflects either way.
        "SAUNA_CLS":           ("done" if _row_has(data.get("sauna_row", []), weekday) else ""),
        "SAUNA_STATE_TEXT":    ("\u2713 logged" if _row_has(data.get("sauna_row", []), weekday) else "not logged"),
        "MORNING_COLLECTED":   "true" if today_morning_earned else "false",
        "NIGHT_COLLECTED":     "true" if today_night_earned else "false",
        "CORE_COLLECTED":      "true" if today_core_earned else "false",
        "SEASON_DONE_INDICES": ",".join(str(i) for i in sorted(season_done_indices)),
        "XP_PCT":              str(xp_pct),
        "MEDAL_GOOD_CLS":     medal_good_cls,
        "MEDAL_PERFECT_CLS":  medal_perfect_cls,
        # Daily quest stages
        "MORNING_RITUAL_HTML": morning_ritual_html,
        "CORE_MISSIONS_HTML":  core_missions_html,
        "NIGHT_RITUAL_HTML":   night_ritual_html,
        # Today's targets
        "TODAY_STEPS":         f"{today_steps:,}",
        "STEPS_BAR_PCT":       str(_pct(today_steps, DAILY_STEPS_GOAL)),
        "TODAY_CAL":           str(today_cal),
        "CAL_BAR_PCT":         str(_pct(today_cal, cal_goal)),
        "CAL_GOAL":            f"{cal_goal:,}",
        # Coach
        "COACH_LINE":          coach_line,
        # Pillar health
        "PILLARS_HTML":        pillars_html,
        # Season pass
        "SEASON_MONTH":        season_month,
        "SEASON_DONE":         str(season_done),
        "SEASON_TOTAL":        str(season_total),
        "SEASON_PCT":          str(season_pct),
        "SEASON_BADGE_CLS":    season_badge_cls,
        "SEASON_BADGE_TEXT":    season_badge_text,
        "SEASON_ITEMS_HTML":   season_items_html,
        # Pins
        "PINS_HTML":           pins_html,
        # Footer
        "TAB_NAME":            data.get("tab_name", ""),
        "BUILD_DATE":          today.strftime("%Y.%m.%d"),
        "TODAY_DAY_LABEL":     today.strftime("%a, %b %d"),
        # Timestamps so the user knows exactly how fresh the data is.
        # SYNCED_TS: unix seconds — used by JS to render "just now / Xm ago".
        "SYNCED_TS":           str(int(__import__("time").time())),
        "SYNCED_LABEL":        __import__("datetime").datetime.now().strftime("%-I:%M %p"),
    })

    # ── Write to disk + open ───────────────────────────────────
    html_path = Path.home() / "morning_report.html"
    html_path.write_text(html, encoding="utf-8")
    log.info("HTML report written to %s", html_path)

    if not os.environ.get("OURA_EMIT_HTML"):
        subprocess.Popen(["open", str(html_path)])

    return html
