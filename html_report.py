"""
Quest Hub — HTML report generator.

Reads the template from templates/morning_report.html and fills
dynamic placeholders with the `report_data` dict that `data_gather.py`
shapes from Postgres rows.

Architecture
────────────
  sync.py                        ← cron: Oura/Garmin/Strava/GCal → Postgres
      ↓
  data_gather.build_report_data  ← Postgres rows → report_data dict
      ↓
  html_report.generate_html_report (this file) → section builders
      ↓
  templates/morning_report.html  ← pure HTML/CSS/JS with {{PLACEHOLDERS}}
      ↓
  Flask `/dashboard` response
"""

import json
import logging
import time
from pathlib import Path

from tz import local_now, local_today

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

from constants import (
    DAILY_STEPS_GOAL, MAX_DAILY_STARS, MAX_WEEKLY_STARS,
    MEDAL_BRONZE, MEDAL_SILVER, MEDAL_GOLD,
    SLEEP_STAR_THRESHOLD_DEFAULT,
    WEEKLY_CARDIO_GOAL, WEEKLY_STRENGTH_GOAL,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_FILE = TEMPLATE_DIR / "morning_report.html"

# Cycle phase → (energy level, coaching advice).
# Keys match `cycle_phase` values in daily_entries (see cycle.py).
PHASE_TIPS = {
    "Menstrual":  ("low energy", "Go easy — yoga, stretching, gentle walks."),
    "Follicular": ("energy rising", "Good day for heavier lifts."),
    "Ovulation":  ("peak energy", "Push for PRs — strongest performance window."),
    "Luteal-EM":  ("steady energy", "Normal workouts, stay consistent."),
    "Luteal-PMS": ("energy winding down", "Keep it light — stretch, recover."),
}

# Cycle phase → header pill emoji.
CYCLE_ICONS = {
    "Follicular": "\U0001f331",  # 🌱
    "Ovulation":  "\U0001f315",  # 🌕
    "Luteal-EM":  "\U0001f317",  # 🌗
    "Luteal-PMS": "\U0001f317",  # 🌗
    "Menstrual":  "\U0001f534",  # 🔴
}

# Cycle phase → user-facing full name. The DB stores short codes so
# legacy rows stay valid; UI displays the readable version.
PHASE_DISPLAY = {
    "Follicular": "Follicular",
    "Ovulation":  "Ovulation",
    "Luteal-EM":  "Luteal · Early-Mid",
    "Luteal-PMS": "Luteal · PMS",
    "Menstrual":  "Menstrual",
}

DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ═══════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════════

def _row_has(row: list, idx: int) -> bool:
    """True if row[idx] (a 7-element weekday list, 0=Mon) is non-empty.

    Example: ``_row_has(strength_row, 3)`` → True if Thursday had a
    strength session logged.
    """
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
    # Any stage starting with "core" is a read-only status indicator
    # (stages: "core", "core-base", "core-burn", "core-recover"). The
    # data is fully driven by Oura/Garmin/Strava/manual-sauna — the UI
    # doesn't let you flip these because flipping would just get
    # overwritten on next sync. Only the morning/night ritual items
    # and the sauna toggle are interactive.
    if stage.startswith("core"):
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

def _base_earned(data: dict, weekday: int) -> bool:
    """🏔 Base star — steps AND sleep AND calories (all three)."""
    daily = data["score"].get("daily", {}).get(weekday, {})
    return (
        bool(daily.get("steps"))
        and bool(daily.get("sleep"))
        and bool(daily.get("cal"))
    )


def _burn_earned(data: dict, weekday: int) -> bool:
    """🔥 Burn star — strength OR cardio session logged."""
    return (
        _row_has(data.get("strength_row", []), weekday)
        or _row_has(data.get("cardio_row", []), weekday)
    )


def _recover_earned(data: dict, weekday: int) -> bool:
    """🌿 Recover star — stretch OR sauna / steam logged."""
    return (
        _row_has(data.get("stretch_row", []), weekday)
        or _row_has(data.get("sauna_row", []), weekday)
    )


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
    from datetime import timedelta
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
    today = data.get("today") or local_today()
    try:
        monday = today - timedelta(days=today.weekday())
    except AttributeError:
        monday = None

    def _cell(row, wd):
        if wd < len(row):
            v = str(row[wd]).strip()
            return v if v else ""
        return ""

    # Target strings are read from shared constants so the modal can
    # never drift from what the Core Missions section says.
    sleep_target = f"\u2265 {SLEEP_STAR_THRESHOLD_DEFAULT:g}h"

    details = {}
    for wd in range(weekday + 1):  # only past + today (never future)
        # Five-star breakdown for the day
        morning_done = _cell(morning_star_row, wd) == "\u2713"
        night_done = _cell(night_star_row, wd) == "\u2713"
        base_done = _base_earned(data, wd)
        burn_done = _burn_earned(data, wd)
        recover_done = _recover_earned(data, wd)
        stars = sum([
            int(morning_done),
            int(base_done),
            int(burn_done),
            int(recover_done),
            int(night_done),
        ])

        daily = data["score"].get("daily", {}).get(wd, {})
        steps_val = _cell(steps_row, wd)
        sleep_val = _cell(sleep_row, wd)
        cal_val = cal_values[wd] if wd < len(cal_values) and cal_values[wd] else None

        # Sub-items grouped by Base / Burn / Recover for the modal.
        base_items = [
            {
                "name": "🚶 Steps",
                "done": bool(daily.get("steps")),
                "value": f"{int(steps_val.replace(',','')):,}" if steps_val.replace(',','').isdigit() else (steps_val or "—"),
                "target": f"\u2265 {DAILY_STEPS_GOAL:,}",
            },
            {
                "name": "😴 Sleep",
                "done": bool(daily.get("sleep")),
                "value": f"{sleep_val}h" if sleep_val else "—",
                "target": sleep_target,
            },
            {
                "name": "🍽️ Calories",
                "done": bool(daily.get("cal")),
                "value": f"{cal_val:,}" if cal_val else "—",
                "target": "logged",
            },
        ]
        burn_items = [
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
        ]
        recover_items = [
            {
                "name": "🧘 Stretch",
                "done": _row_has(stretch_row, wd),
                "value": _cell(stretch_row, wd) or "—",
                "target": "any session",
            },
            {
                "name": "♨️ Sauna / Steam",
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
            "max_stars": MAX_DAILY_STARS,
            # 5 slot booleans for the modal's star strip
            "morning_done": morning_done,
            "base_done": base_done,
            "burn_done": burn_done,
            "recover_done": recover_done,
            "night_done": night_done,
            # 3 groups with their member items
            "core_groups": [
                {
                    "key": "base",
                    "icon": "🏔️",
                    "name": "Base",
                    "earned": base_done,
                    "rule": "all 3 required",
                    "items": base_items,
                },
                {
                    "key": "burn",
                    "icon": "🔥",
                    "name": "Burn",
                    "earned": burn_done,
                    "rule": "pick one",
                    "items": burn_items,
                },
                {
                    "key": "recover",
                    "icon": "🌿",
                    "name": "Recover",
                    "earned": recover_done,
                    "rule": "pick one",
                    "items": recover_items,
                },
            ],
            "cycle": _cell(cycle_row, wd),
        }
    return details


def _compute_day_stars(data: dict, wd: int) -> int:
    """Return total stars earned on weekday `wd` (0=Mon, 6=Sun).

    Five possible stars: Morning, Base, Burn, Recover, Night.
    """
    morning_star_row = data.get("morning_star_row", [])
    night_star_row = data.get("night_star_row", [])
    s = 0
    if wd < len(morning_star_row) and str(morning_star_row[wd]).strip() == "\u2713":
        s += 1
    if _base_earned(data, wd):
        s += 1
    if _burn_earned(data, wd):
        s += 1
    if _recover_earned(data, wd):
        s += 1
    if wd < len(night_star_row) and str(night_star_row[wd]).strip() == "\u2713":
        s += 1
    return s


def _pick_best_day(data: dict, weekday: int) -> tuple[int, dict] | None:
    """Pick the "best" day of the week so far for the highlight strip.

    Ranking (highest wins):
      1. Most stars earned
      2. Tiebreak: any strength/cardio/stretch session logged that day

    Returns (wd, info) where info has 'stars' and 'why' (one-line reason),
    or None if nothing noteworthy has happened yet this week.
    """

    def activity_note(wd: int) -> str:
        for key in ("cardio_row", "strength_row", "stretch_row"):
            row = data.get(key, [])
            if wd < len(row) and str(row[wd]).strip():
                return str(row[wd]).strip()
        return ""

    best_wd: int | None = None
    best_stars = 0
    best_note = ""

    for wd in range(weekday + 1):  # only past + today
        s = _compute_day_stars(data, wd)
        note = activity_note(wd)
        # Prefer higher stars; on tie, prefer the day with a note.
        if s > best_stars or (s == best_stars and note and not best_note):
            if s > 0 or note:
                best_wd = wd
                best_stars = s
                best_note = note

    if best_wd is None:
        return None
    return best_wd, {"stars": best_stars, "why": best_note}


def _build_best_day_html(data: dict, weekday: int) -> str:
    """Previously returned a textual "🏆 MON · 3⭐ · <activity>" strip
    below the day bubbles. The trophy badge on the winning day bubble
    itself now carries the same meaning without adding a row of height,
    so this returns an empty string.

    Kept as a function so the template placeholder `{{BEST_DAY_HTML}}`
    can stay wired — if we ever want to bring the strip back (e.g. with
    tap-to-explain), this is the single place to populate.
    """
    return ""


def _build_today_hero(
    data: dict,
    weekday: int,
    stars_today: int,
    morning_done: bool,
    base_done: bool,
    burn_done: bool,
    recover_done: bool,
    night_done: bool,
    *,
    cycle_icon: str,
    cycle_label: str,
    cycle_pill_cls: str,
    period_start_str: str,
    coach_line: str,
    season_earned: bool,
    season_month_short: str,
) -> str:
    """Today-is-the-hero card — sits at the very top of Quest Hub.

    Layout (top → bottom):
      1. Eyebrow:  "TODAY   WED APR 22"
         + optional 🎫 <month> chip below the date if this month's
           Season Pass is complete.
      2. 5 big ★ icons, lit/dim according to earned count
      3. Huge "2 of 5" numeral + "STARS EARNED · 3 TO GO" caption
      4. 5-stage strip: Morning · Base · Burn · Recover · Night,
         each with icon + name + ✓/○ indicator (mint when done)
      5. Forward-looking nudge: "3 more for a Perfect Day ✨"
      6. Thin divider
      7. Cycle chip + "since Apr 1" (period start) + coach line
    """
    today = data["today"]
    date_str = today.strftime("%a %b %d").upper()
    to_go = max(0, MAX_DAILY_STARS - stars_today)

    # Season-pass chip below the date — glows gold when this month
    # is complete, matches the "🎫 Apr" indicator from the Week card
    # so the user has visual consistency across cards.
    season_chip_html = (
        f'<span class="hero-season-chip earned">\U0001f3ab {_esc(season_month_short)} '
        f'\u2605 earned</span>'
        if season_earned else ""
    )

    # 5 star glyphs: filled for earned, hollow for pending.
    stars_html = "".join(
        f'<span class="hero-star {"lit" if i < stars_today else "dim"}">\u2605</span>'
        for i in range(MAX_DAILY_STARS)
    )

    # 5-stage strip. "done" stages glow mint; "pending" stays dim.
    stages = [
        ("\u2600\ufe0f",           "Morning", morning_done),
        ("\U0001f3d4\ufe0f",       "Base",    base_done),
        ("\U0001f525",             "Burn",    burn_done),
        ("\U0001f33f",             "Recover", recover_done),
        ("\U0001f319",             "Night",   night_done),
    ]
    stage_cells = []
    for icon, name, done in stages:
        cls = "hero-stage done" if done else "hero-stage pending"
        check = "\u2713" if done else "\u25cb"
        stage_cells.append(
            f'<div class="{cls}">'
            f'  <div class="hero-stage-ico">{icon}</div>'
            f'  <div class="hero-stage-name">{name.upper()}</div>'
            f'  <div class="hero-stage-check">{check}</div>'
            f'</div>'
        )

    # Forward-looking nudge — never shames. Three states:
    if stars_today >= MAX_DAILY_STARS:
        nudge = '<div class="hero-nudge perfect">\u2728 Perfect Day. You hit every star \U0001f31f</div>'
    elif stars_today == 0:
        nudge = (
            '<div class="hero-nudge start">'
            f'  <strong>{MAX_DAILY_STARS}</strong> stars to earn today '
            '\u2728</div>'
        )
    else:
        nudge = (
            '<div class="hero-nudge">'
            f'  <strong>{to_go}</strong> more for a Perfect Day \u2728'
            '</div>'
        )

    # Cycle section at the bottom — moved here from the Week card.
    # This is today's body-state context (phase + energy + period start)
    # so it belongs with the Today Hero, not with the weekly overview.
    # Collapses cleanly if no cycle data is available.
    cycle_section = ""
    has_cycle_data = bool(cycle_label) and cycle_label != "No cycle data yet"
    if has_cycle_data or coach_line:
        period_html = (
            f'<span class="hero-period-start">since {_esc(period_start_str)}</span>'
            if period_start_str else ""
        )
        coach_html = (
            f'<div class="hero-coach">{coach_line}</div>' if coach_line else ""
        )
        pill_html = (
            f'<span class="{cycle_pill_cls}">{cycle_icon} {_esc(cycle_label)}</span>'
            if cycle_label else ""
        )
        cycle_section = (
            '<div class="hero-divider" aria-hidden="true"></div>'
            '<div class="hero-cycle">'
            '  <div class="hero-cycle-row">'
            f'    {pill_html}'
            f'    {period_html}'
            '  </div>'
            f'  {coach_html}'
            '</div>'
        )

    return (
        '<div class="today-hero">'
        '  <div class="hero-top">'
        '    <div class="hero-top-left">'
        '      <span class="hero-today-lbl">Today</span>'
        '    </div>'
        '    <div class="hero-top-right">'
        f'      <span class="hero-date">{date_str}</span>'
        f'      {season_chip_html}'
        '    </div>'
        '  </div>'
        f'  <div class="hero-stars">{stars_html}</div>'
        '  <div class="hero-count-wrap">'
        f'    <span class="hero-count-num">{stars_today}</span>'
        f'    <span class="hero-count-of">of {MAX_DAILY_STARS}</span>'
        '  </div>'
        f'  <div class="hero-caption">Stars earned \u00b7 {to_go} to go</div>'
        f'  <div class="hero-stages">{"".join(stage_cells)}</div>'
        f'  {nudge}'
        f'  {cycle_section}'
        '</div>'
    )


# Medal icons for the comeback line / hero nudges.
_MEDAL_ICONS = {
    "bronze": "\U0001f949",
    "silver": "\U0001f948",
    "gold":   "\U0001f947",
}


def _build_comeback_line(weekly_stars: int, weekday: int) -> str:
    """Forward-looking "you can still reach X" line.

    Apple-Watch-style psychology: always frame the remaining week as
    *possible upside*, never as *past miss*. Picks the highest medal
    tier that's still mathematically reachable given remaining days.
    """
    days_done = weekday + 1
    days_left = 7 - days_done
    if days_left <= 0:
        # End of week — recap instead of nudge.
        if weekly_stars >= MEDAL_GOLD:
            return f'<div class="wp-comeback earned">{_MEDAL_ICONS["gold"]} Gold week — {weekly_stars}/{MAX_WEEKLY_STARS} stars</div>'
        if weekly_stars >= MEDAL_SILVER:
            return f'<div class="wp-comeback earned">{_MEDAL_ICONS["silver"]} Silver week — {weekly_stars}/{MAX_WEEKLY_STARS} stars</div>'
        if weekly_stars >= MEDAL_BRONZE:
            return f'<div class="wp-comeback earned">{_MEDAL_ICONS["bronze"]} Bronze week — {weekly_stars}/{MAX_WEEKLY_STARS} stars</div>'
        return f'<div class="wp-comeback">Week wrapped — {weekly_stars}/{MAX_WEEKLY_STARS} stars</div>'

    max_remaining = days_left * MAX_DAILY_STARS
    max_total = weekly_stars + max_remaining

    # Find the highest tier still reachable.
    if max_total >= MEDAL_GOLD:
        target = ("gold", MEDAL_GOLD)
    elif max_total >= MEDAL_SILVER:
        target = ("silver", MEDAL_SILVER)
    elif max_total >= MEDAL_BRONZE:
        target = ("bronze", MEDAL_BRONZE)
    else:
        # No medal possible. Still frame forward.
        return (
            '<div class="wp-comeback soft">'
            f'<strong>{max_remaining}</strong> stars possible from here \u00b7 '
            f'{days_left} days \u00d7 {MAX_DAILY_STARS}'
            '</div>'
        )

    tier_key, _tier_val = target
    medal = _MEDAL_ICONS[tier_key]
    return (
        '<div class="wp-comeback">'
        f'<strong>{max_remaining}</strong> stars possible from here \u00b7 '
        f'{days_left} days \u00d7 {MAX_DAILY_STARS} \u2014 still on track for {medal}'
        '</div>'
    )


def _build_pulse_days(data: dict, weekday: int, best_wd: int | None = None) -> str:
    """Build the 7-day bubble strip for the Weekly Pulse card.

    Each bubble shows: stars earned · day label · date number.
    Colour states:
      • Today        — gold ring + glow
      • Past, stars  — mint
      • Past, zero   — coral (miss)
      • Future       — dashed outline, dim
    Past / today bubbles open the day-details modal on tap.
    """
    from datetime import timedelta
    today = data.get("today") or local_today()
    monday = today - timedelta(days=today.weekday())

    bubbles = []
    for wd in range(7):
        is_today = (wd == weekday)
        is_future = (wd > weekday)
        day_stars = 0 if is_future else _compute_day_stars(data, wd)
        date_num = (monday + timedelta(days=wd)).day

        classes = ["wp-day"]
        if is_future:
            classes.append("is-future")
            num_html = ""
        elif is_today:
            classes.extend(["is-today", "wp-day-clickable"])
            num_html = f'<span class="wp-day-num" data-day="{wd}">{day_stars}</span>'
        elif day_stars > 0:
            classes.extend(["has-stars", "wp-day-clickable"])
            num_html = f'<span class="wp-day-num" data-day="{wd}">{day_stars}</span>'
        else:
            # Past zero-star days: em-dash so "missed" reads distinctly
            # from the date number beneath. Same treatment as Month grid.
            classes.extend(["zero-stars", "wp-day-clickable"])
            num_html = f'<span class="wp-day-num" data-day="{wd}">\u2014</span>'

        if best_wd is not None and wd == best_wd:
            classes.append("is-best")

        cls = " ".join(classes)
        data_attr = f' data-wd="{wd}" onclick="showDayDetails({wd})" tabindex="0"' if not is_future else ""
        # Label block: date number above day abbreviation.
        date_block = (
            f'<span class="wp-day-date">{date_num}</span>'
            f'<span class="wp-day-lbl">{DAY_LABELS[wd]}</span>'
        )
        bubbles.append(
            f'<div class="{cls}"{data_attr}>'
            f'{num_html}'
            f'{date_block}'
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


# ── Core 3 (Base · Burn · Recover — rendered as three peer stage cards) ──

def _build_core3(data: dict, weekday: int) -> dict:
    """Build content for the three Core 3 peer stage cards.

    Returns:
      {
        "base":    {"items_html": str, "earned": bool, "done_count": int, "total_count": int},
        "burn":    {...},
        "recover": {...},
      }

    Hint text inside each items_html is dynamic so the user sees live
    progress toward the sub-star ("Need 6,591 more · 1,409 / 8,000")
    without opening a modal — pull-to-refresh confirms fresh data.
    """
    daily = data["score"].get("daily", {}).get(weekday, {})

    # ── Steps: live from Oura (data['today_steps']) for today, else from DB
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

    # ── Sleep: hours logged last night
    last_sleep = data.get("last_sleep")
    sleep_done = bool(daily.get("sleep"))
    if last_sleep is not None:
        # Threshold comes from constants (6h uniform) so the hint stays
        # in sync if it ever changes again.
        target = SLEEP_STAR_THRESHOLD_DEFAULT
        target_str = f"{target:.0f}h" if target == int(target) else f"{target}h"
        delta = target - last_sleep
        if sleep_done:
            sleep_hint = f"Done \u2713  \u00b7  {last_sleep}h / {target_str}"
        elif delta > 0:
            sleep_hint = f"{delta:.1f}h short  \u00b7  {last_sleep}h / {target_str}"
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

    # Core 3 is now three peer stage cards (Base / Burn / Recover) at
    # the same level as Morning Ritual and Night Ritual — not nested
    # inside a wrapper. Each group returns its own items-HTML and its
    # earned flag so the template can render three full .stage cards.
    base_items = [
        ("\U0001f45f",    "8,000 Steps",    steps_hint, steps_done),
        ("\U0001f634",    f"Sleep {SLEEP_STAR_THRESHOLD_DEFAULT:g}h+",
         sleep_hint, sleep_done),
        ("\U0001f357",    "Calories Logged", cal_hint,  cal_done),
    ]
    burn_items = [
        ("\U0001f4aa",    "Strength",       strength_hint, bool(strength_v)),
        ("\U0001f6b4",    "Cardio",         cardio_hint,   bool(cardio_v)),
    ]
    recover_items = [
        ("\U0001f9d8",    "Stretch",        stretch_hint,  bool(stretch_v)),
        ("\u2668\ufe0f",  "Sauna / Steam",  sauna_hint,    bool(sauna_v)),
    ]

    base_earned = all(item[3] for item in base_items)
    burn_earned = any(item[3] for item in burn_items)
    recover_earned = any(item[3] for item in recover_items)

    def _items_html(group_key: str, items: list, offset: int) -> str:
        return "\n".join(
            _quest_item(f"core-{group_key}", offset + i, *item)
            for i, item in enumerate(items)
        )

    return {
        "base": {
            "items_html": _items_html("base", base_items, 0),
            "earned": base_earned,
            "done_count": sum(1 for it in base_items if it[3]),
            "total_count": len(base_items),
        },
        "burn": {
            "items_html": _items_html("burn", burn_items, len(base_items)),
            "earned": burn_earned,
            "done_count": sum(1 for it in burn_items if it[3]),
            "total_count": len(burn_items),
        },
        "recover": {
            "items_html": _items_html(
                "recover", recover_items,
                len(base_items) + len(burn_items),
            ),
            "earned": recover_earned,
            "done_count": sum(1 for it in recover_items if it[3]),
            "total_count": len(recover_items),
        },
    }


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

def _parse_agenda_items(data: dict) -> list[str]:
    """Pull the cleaned list of calendar-event labels for this week.

    `notes_row` is written by `sync.py` on Mondays from Google Calendar
    (see `api_clients.fetch_week_calendar_notes`). Raw string uses ``+``
    as a separator; we split, trim, drop blanks + duplicates.
    """
    notes_row = data.get("notes_row") or []
    raw = notes_row[0] if notes_row else ""
    if not raw or not raw.strip():
        return []

    items: list[str] = []
    seen: set[str] = set()
    for piece in raw.split("+"):
        s = piece.strip().strip("\u2014 ").strip()  # trailing em-dash
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(s)
    return items


def _build_context_sections(data: dict) -> str:
    """Return the week's calendar-agenda fragment to be injected
    inline INSIDE the Weekly Pulse card.

    Previously this also rendered today's cycle phase + coach line,
    but those moved up into the Today Hero (they're body-state context
    for *today*, not the week). What's left here is purely the week's
    external events (travel, appointments), shown under an inline
    "─ agenda ─" divider label.

    Returns empty string if no agenda items — whole section hides.
    """
    agenda_items = _parse_agenda_items(data)
    if not agenda_items:
        return ""

    items_html = '<span class="ctx-sep"> \u00b7 </span>'.join(
        f'<span class="ctx-week-item">{_esc(s)}</span>' for s in agenda_items
    )
    return (
        '<div class="wp-section-label">agenda</div>'
        f'<div class="ctx-week-flow">{items_html}</div>'
    )


def _build_coach_line(phase_name: str, last_sleep: float | None) -> str:
    """One-liner coaching advice based on cycle phase and sleep quality.

    The phase chip above already prints the phase name, so the coach
    line opens straight with the energy descriptor + advice. A short
    sleep warning is appended only when the user actually slept under
    7 h; generic nudges that duplicate the Core Missions column have
    been removed to keep this strip focused.
    """
    parts = []
    tip = PHASE_TIPS.get(phase_name)
    if tip:
        energy, advice = tip
        parts.append(f"<em>{_esc(energy)}.</em> {_esc(advice)}")
    if last_sleep is not None and last_sleep < SLEEP_STAR_THRESHOLD_DEFAULT:
        parts.append("Sleep was a touch short &mdash; keep cardio conversational.")
    return " ".join(parts)


# ── Pillar Health (6 life pillars with % bars) ───────────────────

def _build_pillars_html(data: dict) -> str:
    """6 expandable pillar cards — all computed from real data sources.

    Data sources:
        Systems  → sleep average (Oura) + step progress (Oura/Garmin)
        Strength → workout sessions vs weekly goal (Garmin)
        Finance  → calorie tracking consistency (Garmin/MFP) as discipline proxy
        Travel   → booked/completed trips vs total planned (Travel Sheet)
        Mental   → sleep quality nights ≥ SLEEP_STAR_THRESHOLD_DEFAULT + cycle awareness (Oura + Calendar)
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

    # ── Mental: good sleep nights (≥ SLEEP_STAR_THRESHOLD_DEFAULT) + cycle tracking active ──
    good_nights = sum(1 for s in sleep_values if s >= SLEEP_STAR_THRESHOLD_DEFAULT) if sleep_values else 0
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
    today = local_today()
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
    today = local_today()
    current_month = today.strftime("%b")

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
    current_year = today.year
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

    # Annual habits repeat every year — stamp each one with the current
    # year AND the next year so the "2027 · roadmap" section on the
    # Quest Hub pins timeline actually shows the habits, not only the
    # Potential travel trips. Pin IDs get a year suffix so the dedupe
    # pass doesn't collapse 2026-physical into 2027-physical.
    years_to_show = [current_year, current_year + 1]
    habit_pins_with_year = []
    for p in habit_pins:
        pid = p[0]
        for y in years_to_show:
            year_suffixed = (f"{pid}-{y}",) + p[1:] + (y,)
            habit_pins_with_year.append(year_suffixed)

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
# WEEK-tab-specific builders
# ═══════════════════════════════════════════════════════════════════

def _build_cycle_strip(phase_name: str, cycle_label: str) -> str:
    """Compact one-line cycle indicator for the Week tab.

    Format: "🌗 Luteal · Day 22 · steady energy week"
    Collapses when cycle data is missing.
    """
    if not phase_name or not cycle_label or cycle_label == "No cycle data yet":
        return ""
    icon = CYCLE_ICONS.get(phase_name, "\U0001f534")
    tip = PHASE_TIPS.get(phase_name)
    tail = f" \u00b7 {tip[0]} week" if tip else ""
    return (
        '<div class="cycle-strip">'
        f'  <span class="cycle-strip-icon">{icon}</span>'
        f'  <span class="cycle-strip-label">{_esc(cycle_label)}</span>'
        f'  <span class="cycle-strip-tail">{_esc(tail)}</span>'
        '</div>'
    )


# Heuristic keyword → icon mapping for agenda items.
# Falls back to a neutral 📌 when nothing matches.
_AGENDA_ICON_RULES: list[tuple[tuple[str, ...], str]] = [
    (("bike", "cycling", "fondo", "ride", "cardio"),          "\U0001f6b4"),
    (("travel", "flight", "airport", "trip"),                 "\u2708\ufe0f"),
    (("christmas", "new year"),                               "\U0001f384"),
    (("thanksgiving",),                                       "\U0001f983"),
    (("appointment", "appt", "doctor", "dr.", "dentist",
      "physical", "exam", "checkup", "lab"),                  "\U0001fa7a"),
    (("massage", "facial", "spa", "beauty", "lip ", "blush",
      "botox", "lash", "brow", "wax"),                        "\U0001f485"),
    (("ssn", "social security", "passport", "visa", "irs",
      "tax", "dmv", "license", "citizenship", "form"),        "\U0001f3db\ufe0f"),
    (("habit", "month-end", "month end", "review"),           "\U0001f4cb"),
    (("yoga", "stretch", "pilates"),                          "\U0001f9d8"),
    (("sauna", "steam"),                                      "\u2668\ufe0f"),
    (("birthday",),                                           "\U0001f382"),
]


def _agenda_icon(label: str) -> str:
    lo = label.lower()
    for keywords, icon in _AGENDA_ICON_RULES:
        if any(k in lo for k in keywords):
            return icon
    return "\U0001f4cc"  # 📌 default pin


def _build_agenda_card(data: dict) -> str:
    """Rich "This Week's Agenda" card for the Week tab.

    Per-item icons chosen by keyword heuristic (cycling, travel,
    appointment, beauty, habit…). Falls back to a neutral 📌 pin.
    Collapses cleanly if there are no agenda items.
    """
    items = _parse_agenda_items(data)
    if not items:
        return ""
    rows = "".join(
        f'<div class="agenda-row">'
        f'  <span class="agenda-icon">{_agenda_icon(it)}</span>'
        f'  <span class="agenda-label">{_esc(it)}</span>'
        f'</div>'
        for it in items
    )
    return (
        '<div class="card agenda-card">'
        '  <div class="card-title">\U0001f4cc This Week\u2019s Agenda</div>'
        f'  <div class="agenda-list">{rows}</div>'
        '</div>'
    )


def _build_weekly_rollups(data: dict) -> str:
    """Weekly Rollups card: steps / sleep / strength / cardio tallies.

    Shows the aggregate against a soft goal:
      • Steps     → total / (DAILY_STEPS_GOAL × 7)
      • Avg sleep → simple average of logged nights
      • Strength  → count / WEEKLY_STRENGTH_GOAL
      • Cardio    → count / WEEKLY_CARDIO_GOAL (mint ✓ when met)
    """
    total_steps    = data.get("total_steps") or 0
    avg_sleep      = data.get("avg_sleep")
    strength_count = data.get("strength_count") or 0
    cardio_count   = data.get("cardio_count") or 0
    week_steps_goal = DAILY_STEPS_GOAL * 7   # 56k on an 8k/day target

    def _row(icon: str, label: str, value_html: str, done: bool) -> str:
        done_cls = "done" if done else ""
        return (
            f'<div class="rollup-row {done_cls}">'
            f'  <span class="rollup-icon">{icon}</span>'
            f'  <span class="rollup-label">{_esc(label)}</span>'
            f'  <span class="rollup-value">{value_html}</span>'
            '</div>'
        )

    # Steps
    steps_done = total_steps >= week_steps_goal
    steps_goal_k = f"{week_steps_goal // 1000}k"
    steps_val = f"{total_steps:,} / {steps_goal_k}"
    # Sleep
    sleep_val = f"{avg_sleep:.1f}h" if avg_sleep is not None else "—"
    sleep_done = bool(avg_sleep and avg_sleep >= SLEEP_STAR_THRESHOLD_DEFAULT)
    # Strength / Cardio
    strength_done = strength_count >= WEEKLY_STRENGTH_GOAL
    cardio_done   = cardio_count   >= WEEKLY_CARDIO_GOAL
    strength_val = f"{strength_count} of {WEEKLY_STRENGTH_GOAL}"
    cardio_prefix = "\u2713 " if cardio_done else ""
    cardio_val = f"{cardio_prefix}{cardio_count} of {WEEKLY_CARDIO_GOAL}"

    rows = "".join([
        _row("\U0001f45f", "Steps",     steps_val,    steps_done),
        _row("\U0001f634", "Avg sleep", sleep_val,    sleep_done),
        _row("\U0001f4aa", "Strength",  strength_val, strength_done),
        _row("\U0001f6b4", "Cardio",    cardio_val,   cardio_done),
    ])
    return (
        '<div class="card rollups-card">'
        '  <div class="card-title">\U0001f4ca Weekly Rollups</div>'
        f'  <div class="rollups-list">{rows}</div>'
        '</div>'
    )


# ═══════════════════════════════════════════════════════════════════
# MONTH-tab builders
# ═══════════════════════════════════════════════════════════════════

def _build_month_card(
    today, month_stars_by_date: dict, month_stars_total: int,
) -> str:
    """Monthly progress card: mirrors the Week card aesthetic but
    spans 30/31 days.

    `month_stars_by_date` — dict mapping date → daily star count (0-5)
                            for days up to today. Future days omitted.
    """
    import calendar
    import datetime as _dt
    year, month = today.year, today.month
    days_in_month = calendar.monthrange(year, month)[1]
    max_stars = days_in_month * MAX_DAILY_STARS
    # Medal thresholds scale from weekly (21/28/33 of 35) to monthly.
    # Use the same percentages so visual feel stays consistent.
    bronze = round(MEDAL_BRONZE / MAX_WEEKLY_STARS * max_stars)
    silver = round(MEDAL_SILVER / MAX_WEEKLY_STARS * max_stars)
    gold   = round(MEDAL_GOLD   / MAX_WEEKLY_STARS * max_stars)
    pct = min(100, round(month_stars_total / max_stars * 100)) if max_stars else 0

    def _medal_cls(th: int) -> str:
        return "wp-medal-marker lit" if month_stars_total >= th else "wp-medal-marker dim"

    bronze_pos = round(bronze / max_stars * 100, 1)
    silver_pos = round(silver / max_stars * 100, 1)
    gold_pos   = round(gold   / max_stars * 100, 1)

    # Day grid — 7 columns (Mon..Sun) × 4-6 rows. First row may lead
    # Month cells reuse the same .wp-day* classes as Week so the two
    # views look identical — same typography, same colour coding, same
    # "today gold ring" / "past mint or coral" / "future dashed"
    # grammar. Past zero-star days render "—" (not "0") so the user
    # doesn't confuse the missed-state number with the date below it.
    first_weekday = _dt.date(year, month, 1).weekday()  # 0=Mon
    cells: list[str] = []
    for _ in range(first_weekday):
        cells.append('<div class="wp-day is-blank"></div>')
    for d in range(1, days_in_month + 1):
        dt = _dt.date(year, month, d)
        is_today = (dt == today)
        is_future = (dt > today)
        stars = month_stars_by_date.get(dt, 0) if not is_future else 0

        classes = ["wp-day"]
        if is_future:
            classes.append("is-future")
            num_html = ""  # ::before em-dash from existing CSS
        elif is_today:
            classes.append("is-today")
            num_html = f'<span class="wp-day-num">{stars}</span>'
        elif stars > 0:
            classes.append("has-stars")
            num_html = f'<span class="wp-day-num">{stars}</span>'
        else:
            # Past day, zero stars — show em-dash instead of "0" so it's
            # unambiguously "missed" rather than read as the date.
            classes.append("zero-stars")
            num_html = '<span class="wp-day-num">\u2014</span>'

        # Month cells omit the 3-letter day label (the column header
        # row above already says M/T/W/…), but keep the date to stay
        # visually parallel with Week cells (stars on top, date below).
        date_html = f'<span class="wp-day-date">{d}</span>'
        cells.append(
            f'<div class="{" ".join(classes)}">{num_html}{date_html}</div>'
        )

    # Day-of-week header row (Mon → Sun)
    dow_cells = "".join(
        f'<div class="mo-dow">{d}</div>'
        for d in ["M", "T", "W", "T", "F", "S", "S"]
    )

    # Forward-looking monthly comeback line — same pattern as week's.
    days_left = days_in_month - today.day
    max_remaining = days_left * MAX_DAILY_STARS
    max_total = month_stars_total + max_remaining
    if days_left <= 0:
        comeback_html = ""
    else:
        if max_total >= gold:
            target_icon, target_val = _MEDAL_ICONS["gold"], gold
        elif max_total >= silver:
            target_icon, target_val = _MEDAL_ICONS["silver"], silver
        elif max_total >= bronze:
            target_icon, target_val = _MEDAL_ICONS["bronze"], bronze
        else:
            target_icon, target_val = None, None
        if target_icon:
            comeback_html = (
                '<div class="wp-comeback">'
                f'<strong>{max_remaining}</strong> stars possible from here \u00b7 '
                f'{days_left} days \u00d7 {MAX_DAILY_STARS} \u2014 still on track for {target_icon}'
                '</div>'
            )
        else:
            comeback_html = (
                '<div class="wp-comeback soft">'
                f'<strong>{max_remaining}</strong> stars possible from here \u00b7 '
                f'{days_left} days \u00d7 {MAX_DAILY_STARS}'
                '</div>'
            )

    month_name = today.strftime("%B %Y")
    return (
        '<div class="card weekly-pulse compact month-pulse">'
        '  <div class="wp-eyebrow">'
        '    <span class="wp-eyebrow-label">This Month</span>'
        '    <span class="wp-eyebrow-sep">&middot;</span>'
        f'    <span class="wp-eyebrow-range">{_esc(month_name.upper())}</span>'
        '  </div>'
        '  <div class="wp-hero-row compact">'
        '    <div class="wp-num-block">'
        f'      <div class="wp-stars-num">{month_stars_total}</div>'
        f'      <div class="wp-stars-sub">/ {max_stars} stars</div>'
        '    </div>'
        '  </div>'
        '  <div class="wp-bar-row">'
        '    <div class="wp-week-track">'
        f'      <div class="wp-week-fill" style="width:{pct}%;"></div>'
        f'      <div class="{_medal_cls(bronze)}" style="left:{bronze_pos}%;">'
        f'        <span class="wp-medal-icon">\U0001f949</span>'
        f'        <span class="wp-medal-val">{bronze}</span>'
        '      </div>'
        f'      <div class="{_medal_cls(silver)}" style="left:{silver_pos}%;">'
        f'        <span class="wp-medal-icon">\U0001f948</span>'
        f'        <span class="wp-medal-val">{silver}</span>'
        '      </div>'
        f'      <div class="{_medal_cls(gold)}" style="left:{gold_pos}%;">'
        f'        <span class="wp-medal-icon">\U0001f947</span>'
        f'        <span class="wp-medal-val">{gold}</span>'
        '      </div>'
        '    </div>'
        '  </div>'
        f'  <div class="mo-dow-row">{dow_cells}</div>'
        f'  <div class="mo-days">{"".join(cells)}</div>'
        f'  {comeback_html}'
        '</div>'
    )


# ═══════════════════════════════════════════════════════════════════
# PUBLIC API — called by Flask /dashboard handler in app.py
# ═══════════════════════════════════════════════════════════════════

def generate_html_report(
    data: dict,
    *,
    view: str = "today",
    month_stars_by_date: dict | None = None,
    month_stars_total: int = 0,
    ca_coverage_html: str = "",
) -> str:
    """Generate the Quest Hub dashboard HTML.

    `view` selects which tab's content is visible:
      - "today" (default) — Today Hero, Daily Quest, Pillars
      - "week"            — Week card, cycle strip, agenda, rollups
      - "month"           — Month card, Season Pass list
      - "year"            — Roadmap pins timeline, California map

    The template renders all four views; the body class shows only
    the one for `view`. `month_stars_by_date` + `month_stars_total`
    are optional and only meaningful for the Month view.
    `ca_coverage_html` is only computed for the Year view (requires
    rides data).

    Args:
        data: report_data dict shaped by `data_gather.gather_dashboard_data`.
            Expected keys:
              - today (date), tab_name (str)
              - last_sleep (float|None), avg_sleep (float), sleep_values (list)
              - phase_name (str), latest_cycle_str (str)
              - today_steps (int), total_steps (int), pct_steps (int)
              - strength_count (int), cardio_count (int)
              - cal_values (list[int|None]), cal_goal (int)
              - score (dict with 'daily' sub-dict)
              - strength_row / cardio_row / sauna_row / stretch_row /
                morning_star_row / night_star_row (7-element lists,
                index 0 = Monday)
              - notes_row (list with at most one str: the Week Agenda line)
              - travel_pins, annual_habits, season_done_indices

    Returns:
        Rendered HTML string ready for the Flask response.
    """
    today = data["today"]
    weekday = today.weekday()
    last_sleep = data.get("last_sleep")
    phase_name = data.get("phase_name", "")

    # ── Header pills ───────────────────────────────────────────
    sleep_label = f"{last_sleep}h" if last_sleep is not None else "\u2013"
    sleep_emoji = "\U0001f634" if last_sleep and last_sleep >= SLEEP_STAR_THRESHOLD_DEFAULT else "\U0001f62a"
    cycle_icon = CYCLE_ICONS.get(phase_name, "\U0001f534")

    # ── Today's calories ───────────────────────────────────────
    cal_values = data.get("cal_values", [])
    cal_goal = data.get("cal_goal", 0)
    today_cal = cal_values[weekday] if weekday < len(cal_values) and cal_values[weekday] is not None else 0
    today_steps = data.get("today_steps", 0)

    # ── Weekly stars (5/day: morning + base + burn + recover + night) ──
    morning_star_row = data.get("morning_star_row", [])
    night_star_row = data.get("night_star_row", [])

    weekly_stars = sum(_compute_day_stars(data, wd) for wd in range(weekday + 1))

    # Today's slot states for the hero row (5 circles)
    today_morning_earned = (weekday < len(morning_star_row) and
                            str(morning_star_row[weekday]).strip() == "\u2713")
    today_night_earned = (weekday < len(night_star_row) and
                          str(night_star_row[weekday]).strip() == "\u2713")
    today_base_earned    = _base_earned(data, weekday)
    today_burn_earned    = _burn_earned(data, weekday)
    today_recover_earned = _recover_earned(data, weekday)
    today_core_earned    = today_base_earned and today_burn_earned and today_recover_earned

    # Season pass done indices (from season_pass.done_indices in DB)
    season_done_indices = data.get("season_done_indices", set())

    # Week progress bar + 3 medal tiers (bronze/silver/gold)
    xp_pct = _pct(weekly_stars, MAX_WEEKLY_STARS)
    def _medal_cls(th: int) -> str:
        return "wp-medal-marker lit" if weekly_stars >= th else "wp-medal-marker dim"
    medal_bronze_cls = _medal_cls(MEDAL_BRONZE)
    medal_silver_cls = _medal_cls(MEDAL_SILVER)
    medal_gold_cls   = _medal_cls(MEDAL_GOLD)
    # Fractional positions on the track — normalised to 0-1 of the bar.
    bronze_pct = round(MEDAL_BRONZE / MAX_WEEKLY_STARS * 100, 1)
    silver_pct = round(MEDAL_SILVER / MAX_WEEKLY_STARS * 100, 1)
    gold_pct   = 100.0  # MEDAL_GOLD sits near-right even if not exactly 100%

    # ── Build all HTML sections ────────────────────────────────
    best = _pick_best_day(data, weekday)
    best_wd_for_pulse = best[0] if best else None

    # Coach line needs to exist before the hero is built (hero shows it).
    coach_line = _build_coach_line(phase_name, last_sleep)

    # Cycle label: "Luteal · Early-Mid · Day 22" (or "No cycle data yet").
    # Also compute the first day of the current period from cycle_day
    # so the hero can show "since Apr 1" — saves Sneha from flipping
    # to the calendar to look up when her period started.
    raw_cycle = data.get("latest_cycle_str") or ""
    period_start_str = ""
    if phase_name and raw_cycle:
        friendly = PHASE_DISPLAY.get(phase_name, phase_name)
        day_part = raw_cycle.rsplit(" ", 1)[-1] if " D" in raw_cycle else ""
        day_num_str = day_part.lstrip("D") if day_part else ""
        pretty_day = f"Day {day_num_str}" if day_num_str else ""
        cycle_label = f"{friendly} · {pretty_day}" if pretty_day else friendly
        cycle_pill_cls = "today-ctx-pill"
        # Period start = today - (cycle_day - 1). Skips gracefully if
        # day_num can't be parsed.
        try:
            day_num = int(day_num_str)
            if day_num >= 1:
                from datetime import timedelta as _td
                pstart = today - _td(days=day_num - 1)
                period_start_str = pstart.strftime("%b %-d")
        except (ValueError, TypeError):
            pass
    else:
        cycle_icon = ""
        cycle_label = "No cycle data yet"
        cycle_pill_cls = "today-ctx-pill empty"

    # Season pass first — we need season_earned for the hero chip,
    # plus the normal accordion render data for below the Week card.
    season_month, season_done, season_total, season_items_html = _build_season_pass(data)
    season_pct = _pct(season_done, season_total)
    season_earned = bool(season_total) and season_done == season_total
    if season_done == season_total:
        season_badge_cls, season_badge_text = "badge-complete", "Complete"
    elif season_done >= season_total // 2:
        season_badge_cls, season_badge_text = "badge-track", "On Track"
    else:
        season_badge_cls, season_badge_text = "badge-behind", "Behind"

    # Today's hero: stars + forward-looking nudge + (new) cycle/coach
    # + (new) season chip when this month's pass is locked in.
    stars_today = (
        int(today_morning_earned) + int(today_base_earned) + int(today_burn_earned)
        + int(today_recover_earned) + int(today_night_earned)
    )
    today_hero_html = _build_today_hero(
        data, weekday, stars_today,
        today_morning_earned, today_base_earned, today_burn_earned,
        today_recover_earned, today_night_earned,
        cycle_icon=cycle_icon,
        cycle_label=cycle_label,
        cycle_pill_cls=cycle_pill_cls,
        period_start_str=period_start_str,
        coach_line=coach_line,
        season_earned=season_earned,
        season_month_short=today.strftime("%b"),
    )
    comeback_html = _build_comeback_line(weekly_stars, weekday)

    pulse_days_html     = _build_pulse_days(data, weekday, best_wd=best_wd_for_pulse)
    best_day_html       = _build_best_day_html(data, weekday)
    day_details_payload = _build_day_details_payload(data, weekday)
    morning_ritual_html = _build_morning_ritual(data)
    core3 = _build_core3(data, weekday)
    night_ritual_html   = _build_night_ritual(data)
    pillars_html        = _build_pillars_html(data)
    pins_html           = _build_pins_html(data)

    # Context sections now only carries the week's calendar agenda —
    # cycle/coach moved up to the Today Hero where they belong.
    context_sections_html = _build_context_sections(data)

    # Week-tab extras
    cycle_strip_html     = _build_cycle_strip(phase_name, cycle_label)
    agenda_card_html     = _build_agenda_card(data)
    weekly_rollups_html  = _build_weekly_rollups(data)

    # Month-tab extras (month_stars_* defaults are fine for non-month views;
    # the month-only block will just be hidden by CSS).
    month_card_html      = _build_month_card(
        today,
        month_stars_by_date or {},
        month_stars_total or 0,
    )

    # View switching
    view_cls = {
        "today": "view-today",
        "week":  "view-week",
        "month": "view-month",
        "year":  "view-year",
    }.get(view, "view-today")
    tab_today_cls = "active" if view == "today" else ""
    tab_week_cls  = "active" if view == "week"  else ""
    tab_month_cls = "active" if view == "month" else ""
    tab_year_cls  = "active" if view == "year"  else ""

    # ── Fill template ──────────────────────────────────────────
    template = _load_template()
    html = _fill_template(template, {
        # View switching — body class + tab active states
        "VIEW_CLS":            view_cls,
        "TAB_TODAY_CLS":       tab_today_cls,
        "TAB_WEEK_CLS":        tab_week_cls,
        "TAB_MONTH_CLS":       tab_month_cls,
        "TAB_YEAR_CLS":        tab_year_cls,
        # Atlas moved to a hero-bar side link — no main-tab class needed.
        "CA_COVERAGE_HTML":    ca_coverage_html,
        # Hero bar
        "SLEEP_EMOJI":         sleep_emoji,
        "SLEEP_LABEL":         sleep_label,
        # Cycle/coach/agenda inline inside the Week card
        "CONTEXT_SECTIONS_HTML": context_sections_html,
        # Week tab extras
        "CYCLE_STRIP_HTML":    cycle_strip_html,
        "AGENDA_CARD_HTML":    agenda_card_html,
        "WEEKLY_ROLLUPS_HTML": weekly_rollups_html,
        # Month tab
        "MONTH_CARD_HTML":     month_card_html,
        # Today hero — the new top-of-page focus block
        "TODAY_HERO_HTML":     today_hero_html,
        # Weekly Pulse card — 5 stars/day × 7 days = 35 max
        "WEEKLY_STARS":        str(weekly_stars),
        "MAX_WEEKLY_STARS":    str(MAX_WEEKLY_STARS),
        "WEEK_COMEBACK_HTML":  comeback_html,
        "BEST_DAY_HTML":       best_day_html,
        "PULSE_DAYS_HTML":     pulse_days_html,
        "DAY_DETAILS_JSON":    json.dumps(day_details_payload, ensure_ascii=False),
        "SEASON_MONTH_SHORT":  today.strftime("%b"),
        # Today's 5 slot states
        "TODAY_MORNING_CLS":   "slot-earned" if today_morning_earned else "slot-empty",
        "TODAY_BASE_CLS":      "slot-earned" if today_base_earned    else "slot-empty",
        "TODAY_BURN_CLS":      "slot-earned" if today_burn_earned    else "slot-empty",
        "TODAY_RECOVER_CLS":   "slot-earned" if today_recover_earned else "slot-empty",
        "TODAY_NIGHT_CLS":     "slot-earned" if today_night_earned   else "slot-empty",
        # Back-compat placeholder for the unified "core star earned"
        # state used elsewhere (e.g. showing the big ⚡ slot earned icon
        # when all 3 sub-stars are in). Equals Base AND Burn AND Recover.
        "TODAY_CORE_CLS":      "slot-earned" if today_core_earned else "slot-empty",
        # Manual-log toggle: sauna today. Values: "done" class if ticked,
        # empty otherwise; sibling state text reflects either way.
        "SAUNA_CLS":           ("done" if _row_has(data.get("sauna_row", []), weekday) else ""),
        "SAUNA_STATE_TEXT":    ("\u2713 logged" if _row_has(data.get("sauna_row", []), weekday) else "not logged"),
        "MORNING_COLLECTED":   "true" if today_morning_earned else "false",
        "NIGHT_COLLECTED":     "true" if today_night_earned else "false",
        "CORE_COLLECTED":      "true" if today_core_earned else "false",
        "SEASON_DONE_INDICES": ",".join(str(i) for i in sorted(season_done_indices)),
        # Progress track
        "XP_PCT":              str(xp_pct),
        "MEDAL_BRONZE_CLS":    medal_bronze_cls,
        "MEDAL_SILVER_CLS":    medal_silver_cls,
        "MEDAL_GOLD_CLS":      medal_gold_cls,
        "MEDAL_BRONZE_VAL":    str(MEDAL_BRONZE),
        "MEDAL_SILVER_VAL":    str(MEDAL_SILVER),
        "MEDAL_GOLD_VAL":      str(MEDAL_GOLD),
        "MEDAL_BRONZE_POS":    str(bronze_pct),
        "MEDAL_SILVER_POS":    str(silver_pct),
        "MEDAL_GOLD_POS":      str(gold_pct),
        # Daily quest stages
        # Stage collapse defaults: expanded when the star hasn't been
        # earned yet (so the user sees what's left to do), collapsed
        # when earned (already done → get out of the way). Uniform
        # behaviour across all 5 stage cards.
        "MORNING_COLLAPSED":   "collapsed" if today_morning_earned else "",
        "MORNING_RITUAL_HTML": morning_ritual_html,
        # Core 3: Base / Burn / Recover rendered as peer stage cards.
        "BASE_ITEMS_HTML":      core3["base"]["items_html"],
        "BASE_STAR_CLS":        "earned" if core3["base"]["earned"] else "",
        "BASE_STAR_GLYPH":      "\u2B50" if core3["base"]["earned"] else "\u2606",
        "BASE_SUB":             "Steps · Sleep · Calories · all 3 required",
        "BASE_STAGE_STATE":     "earned" if core3["base"]["earned"] else "",
        "BASE_COLLAPSED":       "collapsed" if core3["base"]["earned"] else "",
        "BURN_ITEMS_HTML":      core3["burn"]["items_html"],
        "BURN_STAR_CLS":        "earned" if core3["burn"]["earned"] else "",
        "BURN_STAR_GLYPH":      "\u2B50" if core3["burn"]["earned"] else "\u2606",
        "BURN_SUB":             "Strength or Cardio · pick one",
        "BURN_STAGE_STATE":     "earned" if core3["burn"]["earned"] else "",
        "BURN_COLLAPSED":       "collapsed" if core3["burn"]["earned"] else "",
        "RECOVER_ITEMS_HTML":   core3["recover"]["items_html"],
        "RECOVER_STAR_CLS":     "earned" if core3["recover"]["earned"] else "",
        "RECOVER_STAR_GLYPH":   "\u2B50" if core3["recover"]["earned"] else "\u2606",
        "RECOVER_SUB":          "Stretch or Sauna · pick one",
        "RECOVER_STAGE_STATE":  "earned" if core3["recover"]["earned"] else "",
        "RECOVER_COLLAPSED":    "collapsed" if core3["recover"]["earned"] else "",
        "NIGHT_COLLAPSED":     "collapsed" if today_night_earned else "",
        "NIGHT_RITUAL_HTML":   night_ritual_html,
        # Today's targets
        "TODAY_STEPS":         f"{today_steps:,}",
        "STEPS_BAR_PCT":       str(_pct(today_steps, DAILY_STEPS_GOAL)),
        "TODAY_CAL":           str(today_cal),
        "CAL_BAR_PCT":         str(_pct(today_cal, cal_goal)),
        "CAL_GOAL":            f"{cal_goal:,}",
        # (coach_line is consumed inside context_card_html, not as a
        # top-level template placeholder)
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
        # Freshness stamps. SYNCED_TS (unix seconds) lets the JS render
        # "just now / Xm ago"; SYNCED_LABEL is the same moment in the
        # user's local timezone as a fallback.
        "SYNCED_TS":           str(int(time.time())),
        "SYNCED_LABEL":        local_now().strftime("%-I:%M %p"),
    })

    return html
