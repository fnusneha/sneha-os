"""
Simple star scoring — 3 stars per day, 21 per week.

Each quest section earns 1 star:
  ☀️ Morning Ritual — all 4 checked → 1 star (browser-side)
  ⚡ Core Missions  — 4 of 7 items done → 1 star
  🌙 Night Ritual  — all 4 checked → 1 star (browser-side)

This module computes per-day booleans for the 3 API-sourced items
(steps, sleep, calories) used to pre-check Core Missions in the UI.
"""

from constants import (
    DAILY_STEPS_GOAL,
    SLEEP_STAR_THRESHOLD_DEFAULT,
    SLEEP_STAR_THRESHOLD_LOW_ENERGY,
    LOW_ENERGY_PHASES,
)


def parse_steps(val) -> int:
    """Parse a steps cell value to an integer, stripping commas.

    Args:
        val: Raw cell value (string or number).

    Returns:
        Integer step count, or 0 if unparseable.
    """
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, AttributeError):
        return 0


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
    """Compute per-day booleans for steps/sleep/calories.

    These booleans drive the pre-checked state of Core Mission items
    in the dashboard. The actual star count is computed in html_report.py
    using _count_day_stars() which checks all 7 items.

    Args:
        steps_row: 7-element list of per-day step counts (index 0 = Monday).
        sleep_row: 7-element list of per-day sleep hours.
        nutrition_row: 7-element list of per-day calorie totals.
        cycle_row: 7-element list of per-day cycle phase labels.
        strength_count: Total strength sessions this week (unused for scoring).
        cardio_count: Total cardio sessions this week (unused for scoring).
        cal_goal: Daily calorie goal from Garmin.
        show_days: List of day indices (0=Mon) with data.

    Returns:
        Dict with ``daily`` breakdown and ``total`` count of booleans hit.
    """
    daily = {}
    total = 0

    for i in show_days:
        day_stars = {"steps": False, "sleep": False, "cal": False}

        # Steps
        raw_s = str(steps_row[i]).replace(",", "").strip() if i < len(steps_row) else ""
        if raw_s.isdigit() and int(raw_s) >= DAILY_STEPS_GOAL:
            day_stars["steps"] = True
            total += 1

        # Sleep (cycle-aware threshold)
        raw_sl = str(sleep_row[i]).strip() if i < len(sleep_row) else ""
        try:
            sl = float(raw_sl.rstrip("h"))
        except (ValueError, AttributeError):
            sl = 0.0
        if sl > 0:
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
                day_stars["sleep"] = True
                total += 1

        # Calories
        raw_c = str(nutrition_row[i]).strip() if i < len(nutrition_row) else ""
        num_c = raw_c.split(" ")[0].split("/")[0].strip() if raw_c else ""
        if num_c.isdigit() and cal_goal > 0:
            if int(num_c) <= cal_goal:
                day_stars["cal"] = True
                total += 1

        daily[i] = day_stars

    return {"total": total, "daily": daily}
