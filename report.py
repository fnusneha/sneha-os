"""
Report generation — morning report and weekly steps report.

``generate_morning_report`` produces a markdown summary of the week's
fitness data (sleep, steps, calories, workouts, cycle phase) along with
a data dict for the HTML report renderer.

``steps_left_report`` prints a quick steps-remaining breakdown.
"""

import logging
from datetime import date, timedelta

from googleapiclient.discovery import build

from constants import (
    ROW_NOTES, ROW_STEPS, ROW_SLEEP, ROW_CYCLE, ROW_NUTRITION,
    ROW_STRENGTH, ROW_CARDIO, ROW_SAUNA, ROW_STRETCH,
    ROW_MORNING_STAR, ROW_NIGHT_STAR, ROW_SEASON_PASS,
    WEEKLY_STEPS_GOAL, WEEKLY_STRENGTH_GOAL, WEEKLY_CARDIO_GOAL,
    STRENGTH_TYPES, CARDIO_TYPES,
    PMS_GUIDE_TIPS,
)
from api_clients import fetch_steps, fetch_weekly_activity_count, _get_garmin_client
from scoring import calculate_challenge_score, parse_steps
from sheets import (
    get_google_creds, get_week_tab_name, resolve_spreadsheet_id,
)

log = logging.getLogger(__name__)


def _parse_season_indices(raw_row: list) -> set[int]:
    """Parse season pass done indices from sheet cell (comma-separated string)."""
    if not raw_row:
        return set()
    raw = str(raw_row[0]).strip() if raw_row else ""
    indices = set()
    for s in raw.split(","):
        s = s.strip()
        if s.isdigit():
            indices.add(int(s))
    return indices


# ═══════════════════════════════════════════════════════════════════
# Morning report
# ═══════════════════════════════════════════════════════════════════

def _fetch_report_data(service, spreadsheet_id: str, tab_name: str) -> dict | None:
    """Batch-read all rows from the weekly tab needed for the report.

    Args:
        service: Google Sheets API service.
        spreadsheet_id: Target spreadsheet.
        tab_name: Weekly tab name.

    Returns:
        Dict of row data keyed by name, or None on failure.
    """
    try:
        batch = service.spreadsheets().values().batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=[
                f"'{tab_name}'!B{ROW_NOTES}:I{ROW_NOTES}",
                f"'{tab_name}'!C{ROW_STEPS}:I{ROW_STEPS}",
                f"'{tab_name}'!C{ROW_SLEEP}:I{ROW_SLEEP}",
                f"'{tab_name}'!C{ROW_CYCLE}:I{ROW_CYCLE}",
                f"'{tab_name}'!C{ROW_NUTRITION}:I{ROW_NUTRITION}",
                f"'{tab_name}'!C{ROW_STRENGTH}:I{ROW_STRENGTH}",
                f"'{tab_name}'!C{ROW_CARDIO}:I{ROW_CARDIO}",
                f"'{tab_name}'!C{ROW_SAUNA}:I{ROW_SAUNA}",
                f"'{tab_name}'!C{ROW_STRETCH}:I{ROW_STRETCH}",
                f"'{tab_name}'!C{ROW_MORNING_STAR}:I{ROW_MORNING_STAR}",
                f"'{tab_name}'!C{ROW_NIGHT_STAR}:I{ROW_NIGHT_STAR}",
                f"'{tab_name}'!B{ROW_SEASON_PASS}",
            ],
        ).execute()
    except Exception as exc:
        log.warning("Could not read tab for report: %s", exc)
        return None

    ranges = batch.get("valueRanges", [])

    def _row(idx):
        return ranges[idx].get("values", [[]])[0] if len(ranges) > idx and ranges[idx].get("values") else []

    return {
        "notes_row": _row(0),
        "steps_row": _row(1),
        "sleep_row": _row(2),
        "cycle_row": _row(3),
        "nutrition_row": _row(4),
        "strength_row": _row(5),
        "cardio_row": _row(6),
        "sauna_row": _row(7),
        "stretch_row": _row(8),
        "morning_star_row": _row(9),
        "night_star_row": _row(10),
        "season_pass_raw": _row(11),  # B14: comma-separated indices
    }


def _build_report_sections(
    today: date, monday: date, tab_name: str, rows: dict,
    strength_count: int, cardio_count: int, cal_goal: int, score: dict,
    total_steps: int, today_steps: int, remaining_steps: int, pct_steps: int,
    last_sleep: float | None, avg_sleep: float | None, sleep_values: list,
    phase_name: str, latest_cycle_str: str, cal_values: list,
) -> list[str]:
    """Build all markdown sections of the morning report.

    Args:
        today: Today's date.
        monday: Monday of the current week.
        tab_name: Weekly tab name.
        rows: Dict of sheet row data from ``_fetch_report_data``.
        strength_count: Weekly strength session count.
        cardio_count: Weekly cardio session count.
        cal_goal: Daily calorie goal.
        score: Result from ``calculate_challenge_score``.
        total_steps: Total steps for the week so far.
        today_steps: Today's live step count.
        remaining_steps: Steps remaining to hit weekly goal.
        pct_steps: Percentage of weekly step goal completed.
        last_sleep: Most recent night's sleep hours.
        avg_sleep: Average sleep hours for the week.
        sleep_values: List of sleep hour floats.
        phase_name: Current cycle phase name.
        latest_cycle_str: Full cycle cell text (e.g. "Follicular D9").
        cal_values: List of calorie ints (None for missing days).

    Returns:
        List of markdown lines.
    """
    weekday = today.weekday()
    show_days = [i for i in range(7) if (monday + timedelta(days=i)) <= today]
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    lines = []

    steps_row = rows["steps_row"]
    sleep_row = rows["sleep_row"]
    nutrition_row = rows["nutrition_row"]
    cycle_row = rows["cycle_row"]
    strength_row = rows["strength_row"]
    cardio_row = rows["cardio_row"]
    notes_text = rows["notes_row"][0] if rows["notes_row"] else ""

    # Compute full weekly stars (morning + core + night from sheet data)
    morning_star_row = rows.get("morning_star_row", [])
    night_star_row = rows.get("night_star_row", [])
    weekly_stars = 0
    for wd in show_days:
        if wd < len(morning_star_row) and str(morning_star_row[wd]).strip() == "\u2713":
            weekly_stars += 1
        # Core star from score daily (steps+sleep+cal+strength+cardio+stretch+sauna ≥ 4)
        daily = score.get("daily", {}).get(wd, {})
        core_count = sum(1 for k in ("steps", "sleep", "cal") if daily.get(k))
        core_count += sum(1 for r in [rows.get("strength_row", []), rows.get("cardio_row", []),
                                       rows.get("sauna_row", []), rows.get("stretch_row", [])]
                          if wd < len(r) and str(r[wd]).strip())
        if core_count >= 4:
            weekly_stars += 1
        if wd < len(night_star_row) and str(night_star_row[wd]).strip() == "\u2713":
            weekly_stars += 1

    # ── 1. GREETING + SCORE ──
    lines.append(f"## Good Morning, Sneha!  ⭐ {weekly_stars} stars this week")
    lines.append("☀️ morning + ⚡ 4-of-7 core + 🌙 night = 3⭐/day")
    if notes_text:
        lines.append(f"_{notes_text}_")
    lines.append("")

    # ── Stars breakdown (day as hero) ──
    yesterday_wd = weekday - 1
    yesterday_daily = score.get("daily", {}).get(yesterday_wd, {}) if yesterday_wd >= 0 else None
    today_daily = score.get("daily", {}).get(weekday, {})

    def _day_icons(daily, day_idx):
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
        icons.sort(key=lambda x: (not x[1],))
        icon_str = "  ".join(f"{ic}{'✅' if v else ''}" for ic, v in icons)
        return earned, icon_str

    if yesterday_daily is not None:
        yd_earned, yd_icons = _day_icons(yesterday_daily, yesterday_wd)
        lines.append(f"**Yesterday** {'⭐' * yd_earned if yd_earned else '☆'} {yd_earned}/5  {yd_icons}")
        lines.append("")

    td_earned, td_icons = _day_icons(today_daily, weekday)
    lines.append(f"**Today** {'⭐' * td_earned if td_earned else '☆'} {td_earned}/5 so far  {td_icons}")
    lines.append("")

    # ── 2. LAST NIGHT + BODY ──
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

    # ── 3. TODAY'S ACTIONS ──
    today_actions = []
    if remaining_steps > 0:
        days_left = max(1, 5 - weekday + 1)
        sheet_today_steps = 0
        if weekday < len(steps_row) and str(steps_row[weekday]).strip():
            sheet_today_steps = parse_steps(steps_row[weekday])
        live = fetch_steps(today.isoformat())
        cur_today = max(today_steps, live or 0, sheet_today_steps)
        daily_target = (WEEKLY_STEPS_GOAL - total_steps + cur_today) // days_left
        steps_left_today = max(0, daily_target - cur_today)
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

    # ── 4. WEEKLY PROGRESS ──
    lines.append("### Week at a Glance")
    lines.append("")
    lines.append("| | Progress | Status |")
    lines.append("|---|---|---|")

    if remaining_steps == 0:
        lines.append(f"| **Steps** | **{total_steps:,}** / {WEEKLY_STEPS_GOAL:,} | Done! |")
    else:
        days_left = max(1, 6 - weekday + 1)
        per_day = remaining_steps // days_left
        lines.append(f"| **Steps** | **{total_steps:,}** / {WEEKLY_STEPS_GOAL:,} ({pct_steps}%) | ~{per_day:,}/day left |")

    s_dots = "●" * strength_count + "○" * s_remaining
    if strength_count >= WEEKLY_STRENGTH_GOAL:
        lines.append(f"| **Strength** | {s_dots} {strength_count}/{WEEKLY_STRENGTH_GOAL} | Done! |")
    else:
        lines.append(f"| **Strength** | {s_dots} {strength_count}/{WEEKLY_STRENGTH_GOAL} | {s_remaining} left |")

    c_dots = "●" * cardio_count + "○" * c_remaining
    if cardio_count >= WEEKLY_CARDIO_GOAL:
        lines.append(f"| **Cardio** | {c_dots} {cardio_count}/{WEEKLY_CARDIO_GOAL} | Done! |")
    else:
        lines.append(f"| **Cardio** | {c_dots} {cardio_count}/{WEEKLY_CARDIO_GOAL} | {c_remaining} left |")

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

    if sleep_values:
        low_nights = sum(1 for s in sleep_values if s < 7)
        if low_nights > 0:
            lines.append(f"| **Sleep** | avg **{avg_sleep:.1f}h** | {low_nights}/{len(sleep_values)} nights under 7h |")
        else:
            lines.append(f"| **Sleep** | avg **{avg_sleep:.1f}h** | All nights 7h+ |")

    # ── 5. DAILY BREAKDOWN ──
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

    lines.append("")
    lines.append("---")
    lines.append("*Sheet updated* ✓")
    lines.append("")

    return lines


def generate_morning_report(service, spreadsheet_id: str, creds) -> tuple[str, dict] | None:
    """Generate a formatted morning report for the current week.

    Fetches all sheet data, computes scores, builds a markdown report,
    and writes the weekly score to the sheet scoreboard.

    Args:
        service: Google Sheets API service.
        spreadsheet_id: Target spreadsheet.
        creds: Google OAuth2 credentials.

    Returns:
        Tuple of (markdown_text, report_data_dict) for the HTML renderer,
        or None if the tab can't be read.
    """
    today = date.today()
    weekday = today.weekday()
    monday = today - timedelta(days=weekday)
    sunday = monday + timedelta(days=6)
    tab_name = get_week_tab_name(monday, sunday)

    rows = _fetch_report_data(service, spreadsheet_id, tab_name)
    if rows is None:
        return None

    steps_row = rows["steps_row"]
    sleep_row = rows["sleep_row"]
    nutrition_row = rows["nutrition_row"]
    cycle_row = rows["cycle_row"]
    notes_text = rows["notes_row"][0] if rows["notes_row"] else ""

    show_days = [i for i in range(7) if (monday + timedelta(days=i)) <= today]

    # Pre-compute activity counts
    strength_count = fetch_weekly_activity_count(monday, STRENGTH_TYPES)
    cardio_count = fetch_weekly_activity_count(monday, CARDIO_TYPES)
    cal_goal = 0
    try:
        garmin = _get_garmin_client()
        if garmin:
            nutr_data = garmin.get_nutrition_daily_food_log(today.isoformat())
            cal_goal = nutr_data.get("dailyNutritionGoals", {}).get("calories", 0)
    except Exception:
        pass

    # Steps totals
    total_steps = 0
    for i in range(7):
        if i < len(steps_row) and str(steps_row[i]).strip():
            total_steps += parse_steps(steps_row[i])

    today_steps = 0
    live_steps = fetch_steps(today.isoformat())
    if live_steps is not None:
        today_steps = live_steps
        sheet_today = parse_steps(steps_row[weekday]) if weekday < len(steps_row) else 0
        if live_steps > sheet_today:
            total_steps = total_steps - sheet_today + live_steps

    remaining_steps = max(0, WEEKLY_STEPS_GOAL - total_steps)
    pct_steps = min(100, int(total_steps / WEEKLY_STEPS_GOAL * 100)) if WEEKLY_STEPS_GOAL else 0

    # Sleep
    sleep_values = []
    for i in range(min(len(sleep_row), 7)):
        raw = str(sleep_row[i]).strip()
        if raw:
            try:
                sleep_values.append(float(raw))
            except ValueError:
                pass
    last_sleep = sleep_values[-1] if sleep_values else None
    avg_sleep = sum(sleep_values) / len(sleep_values) if sleep_values else None

    # Calories (positional, None for missing days)
    cal_values = []
    for i in range(min(len(nutrition_row), 7)):
        raw = str(nutrition_row[i]).strip()
        num = raw.split(" ")[0].split("/")[0].strip() if raw else ""
        if num.isdigit():
            cal_values.append(int(num))
        else:
            cal_values.append(None)

    # Cycle phase
    latest_cycle_str = ""
    for i in range(min(len(cycle_row), 7) - 1, -1, -1):
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

    # Build report text
    lines = _build_report_sections(
        today=today, monday=monday, tab_name=tab_name, rows=rows,
        strength_count=strength_count, cardio_count=cardio_count,
        cal_goal=cal_goal, score=score,
        total_steps=total_steps, today_steps=today_steps,
        remaining_steps=remaining_steps, pct_steps=pct_steps,
        last_sleep=last_sleep, avg_sleep=avg_sleep,
        sleep_values=sleep_values,
        phase_name=phase_name, latest_cycle_str=latest_cycle_str,
        cal_values=cal_values,
    )

    # Fetch habits from Google Doc (cached, refreshes every 24h)
    habits = {}
    try:
        from habit_source import fetch_habits_from_doc
        habits = fetch_habits_from_doc(creds)
    except Exception as exc:
        log.warning("Habit doc fetch failed: %s — using hardcoded fallback", exc)

    # Fetch travel pins from Master Planner sheet — ALWAYS force-refresh
    # during the morning sync so new sheet edits flow to the dashboard
    # immediately (no 6-hour cache lag).
    travel_pins = []
    try:
        from travel_source import fetch_travel_pins, fetch_library_cycling
        travel_pins = fetch_travel_pins(creds, force_refresh=True)
        # Also refresh the library cycling cache so Ride Atlas gets fresh
        # wishlist data on the next render.
        try:
            fetch_library_cycling(creds, force_refresh=True)
        except Exception as lib_exc:
            log.warning("Library cycling refresh failed: %s", lib_exc)
    except Exception as exc:
        log.warning("Travel sheet fetch failed: %s — travel pins will be empty", exc)

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
        "strength_row": rows["strength_row"],
        "cardio_row": rows["cardio_row"],
        "sauna_row": rows["sauna_row"],
        "stretch_row": rows["stretch_row"],
        # Pass raw metric rows through so the day-details modal can show
        # actual daily values (steps, sleep, cycle, nutrition) per day.
        "steps_row": rows["steps_row"],
        "sleep_row": rows["sleep_row"],
        "cycle_row": rows.get("cycle_row", []),
        "nutrition_row": rows.get("nutrition_row", []),
        "morning_star_row": rows.get("morning_star_row", []),
        "night_star_row": rows.get("night_star_row", []),
        "season_done_indices": _parse_season_indices(rows.get("season_pass_raw", [])),
        "monthly_habits": habits.get("monthly", []),
        "quarterly_habits": habits.get("quarterly", []),
        "annual_habits": habits.get("annual", []),
        "travel_pins": travel_pins,
    }

    return "\n".join(lines), report_data


# ═══════════════════════════════════════════════════════════════════
# Steps-left report
# ═══════════════════════════════════════════════════════════════════

def steps_left_report() -> None:
    """Print a weekly steps progress report to stdout.

    Reads step data from the current week's sheet tab, fetches live
    steps from Oura, and prints a markdown table with totals and
    remaining targets.
    """
    today = date.today()
    weekday = today.weekday()

    if weekday == 6:  # Sunday — show the completed week
        monday = today - timedelta(days=6)
    else:
        monday = today - timedelta(days=weekday)
    sunday = monday + timedelta(days=6)
    tab_name = get_week_tab_name(monday, sunday)

    creds = get_google_creds()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    # Anchor on Monday so cross-month weeks stay in one spreadsheet.
    spreadsheet_id = resolve_spreadsheet_id(monday, creds)

    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    existing_tabs = {s["properties"]["title"] for s in metadata.get("sheets", [])}

    # Read steps from the sheet (C8:I8 = Mon–Sun)
    sheet_steps = {}
    if tab_name in existing_tabs:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab_name}'!C{ROW_STEPS}:I{ROW_STEPS}"
        ).execute()
        values = result.get("values", [[]])[0] if result.get("values") else []
        for i, val in enumerate(values):
            if val and str(val).strip():
                parsed = parse_steps(val)
                if parsed > 0:
                    sheet_steps[i] = parsed

    # Get today's live steps from Oura
    today_live = fetch_steps(today.isoformat())

    # Calculate totals
    total = 0
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    breakdown = []

    for i in range(7):
        day_date = monday + timedelta(days=i)
        if i == weekday and today_live is not None:
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

    if weekday == 6:
        days_left = 0
    else:
        days_left = 5 - weekday  # remaining days AFTER today

    per_day = remaining // days_left if days_left > 0 else 0

    print()
    print(f"### 🚶 Steps This Week ({tab_name})")
    print()
    print("| Day | Steps |")
    print("|---|---|")
    for line in breakdown:
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
