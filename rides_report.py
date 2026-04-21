"""
Ride Atlas renderer.

Computes lifetime / yearly / monthly ride stats, the California
coverage map, the year-over-year comparison, and upcoming-trip pins,
then fills `templates/rides.html` and writes the result to
`~/rides_report.html`.

Source of ride data:
    - When USE_DB_RIDES=1 (set in production): reads from the Postgres
      `rides` table. Data is refreshed twice daily by `sync.py --rides`.
    - Otherwise: reads the local `rides_cache.json` produced by
      `python strava_fetch.py`.

Usage:
    python rides_report.py
"""

import json
import html
import logging
from collections import defaultdict, OrderedDict
from datetime import datetime

from tz import local_now
from pathlib import Path
from os.path import expanduser

from dotenv import load_dotenv

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
# Load .env early so USE_DB_RIDES/DATABASE_URL are visible when
# rides_report.py is run as a script (`python rides_report.py`).
load_dotenv(SCRIPT_DIR / ".env")

CACHE_FILE = SCRIPT_DIR / "rides_cache.json"
TEMPLATE = SCRIPT_DIR / "templates" / "rides.html"
OUTPUT = Path(expanduser("~/rides_report.html"))

# Display order + grouping for regions
REGION_GROUPS = OrderedDict([
    ("Bay Area", {
        "label": "Bay Area &middot; Peninsula + South Bay",
        "regions": ["Bay Area", "Peninsula", "East Bay", "South Bay"],
    }),
    ("Marin", {
        "label": "Marin &middot; Headlands + Tam",
        "regions": ["Marin"],
    }),
    ("Wine Country", {
        "label": "Wine Country &middot; Sonoma + Napa",
        "regions": ["Wine Country"],
    }),
    ("Monterey", {
        "label": "Monterey &middot; Central Coast",
        "regions": ["Monterey"],
    }),
    ("Sierra", {
        "label": "Sierra &middot; Tahoe + Foothills",
        "regions": ["Lake Tahoe", "Sierra Foothills"],
    }),
    ("Utah", {
        "label": "Utah",
        "regions": ["Utah"],
    }),
    ("Hawaii", {
        "label": "Hawaii",
        "regions": ["Hawaii"],
    }),
    ("SoCal", {
        "label": "Southern California &middot; SB + Palm Springs",
        "regions": ["Santa Barbara", "Palm Springs"],
    }),
    ("Central Coast", {
        "label": "Central Coast &middot; Paso Robles",
        "regions": ["Paso Robles", "Santa Cruz"],
    }),
    ("NorCal", {
        "label": "NorCal &middot; Sacramento + Humboldt",
        "regions": ["Sacramento", "Humboldt"],
    }),
    ("Travel", {
        "label": "Travel &middot; International",
        "regions": ["Banff", "Puerto Vallarta", "Florence", "Buenos Aires", "Lima"],
    }),
    ("Other", {
        "label": "Other Rides",
        "regions": ["Other"],
    }),
])

# Max cards per region group (show best/most interesting)
MAX_PER_GROUP = 6


def _load_rides() -> list[dict]:
    """Return all rides in the shape the renderer expects.

    When `USE_DB_RIDES=1` (default in production), reads from Postgres.
    Otherwise falls back to a local `rides_cache.json` produced by
    running `python strava_fetch.py` directly.
    """
    import os
    if os.getenv("USE_DB_RIDES") == "1":
        from db import Db
        rows = Db().list_rides()
        # payload JSONB already has every field rides_report expects.
        # Drop 0-mile junk like the cache loader does.
        return [r["payload"] for r in rows
                if (r.get("payload") or {}).get("distance", 0) > 0.5]
    if not CACHE_FILE.exists():
        return []
    rides = json.loads(CACHE_FILE.read_text())
    # Filter out 0-mile junk rides
    return [r for r in rides if r.get("distance", 0) > 0.5]


def _lifetime_stats(rides: list[dict]) -> dict:
    total_miles = sum(r["distance"] for r in rides)
    total_elev = sum(r["elevation"] for r in rides)
    # Short elevation: "148k" style
    if total_elev >= 1000:
        elev_short = f"{total_elev / 1000:,.0f}k"
    else:
        elev_short = f"{total_elev:,.0f}"
    return {
        "miles": f"{total_miles:,.0f}",
        "elevation": f"{total_elev:,.0f}",
        "elevation_short": elev_short,
        "count": str(len(rides)),
    }


def _yearly_breakdown(rides: list[dict]) -> list[dict]:
    by_year = defaultdict(lambda: {"miles": 0, "count": 0, "best": "", "best_dist": 0})
    for r in rides:
        y = r["year"]
        by_year[y]["miles"] += r["distance"]
        by_year[y]["count"] += 1
        if r["distance"] > by_year[y]["best_dist"]:
            by_year[y]["best_dist"] = r["distance"]
            by_year[y]["best"] = r["name"]
    result = []
    for year in sorted(by_year.keys(), reverse=True):
        d = by_year[year]
        result.append({
            "year": year,
            "miles": f"{d['miles']:,.0f}",
            "count": d["count"],
            "best": d["best"],
        })
    return result


def _monthly_pulse(rides: list[dict]) -> dict:
    """Compute this-month stats for the Monthly Pulse card."""
    now = datetime.now()
    month_rides = []
    for r in rides:
        try:
            dt = datetime.strptime(r["date"], "%b %d, %Y")
            if dt.year == now.year and dt.month == now.month:
                month_rides.append(r)
        except ValueError:
            pass

    total = sum(r["distance"] for r in month_rides)
    count = len(month_rides)
    best = max((r["distance"] for r in month_rides), default=0)
    avg_dist = round(total / count, 1) if count else 0

    # Weekly breakdown within this month
    weeks = [0.0] * 5  # up to 5 weeks
    for r in month_rides:
        try:
            dt = datetime.strptime(r["date"], "%b %d, %Y")
            week_idx = min((dt.day - 1) // 7, 4)
            weeks[week_idx] += r["distance"]
        except ValueError:
            pass

    # Current week index
    current_week = min((now.day - 1) // 7, 4)

    # Month goal — 100mi target
    goal = 100
    pct = min(round((total / goal) * 100), 100)

    return {
        "month_name": now.strftime("%b"),
        "total": round(total),
        "count": count,
        "best": round(best, 1),
        "avg": avg_dist,
        "weeks": [round(w) for w in weeks],
        "current_week": current_week,
        "goal": goal,
        "pct": pct,
    }


def _yearly_miles(rides: list[dict], year: int) -> dict:
    """Compute yearly miles data for the Year at a Glance card."""
    months = [0.0] * 12
    for r in rides:
        if r["year"] == year:
            try:
                dt = datetime.strptime(r["date"], "%b %d, %Y")
                months[dt.month - 1] += r["distance"]
            except ValueError:
                pass

    total = sum(months)
    current_month = datetime.now().month

    bronze = total >= 250
    silver = total >= 500
    gold = total >= 1000

    if total < 250:
        pct = (total / 250) * 25
    elif total < 500:
        pct = 25 + ((total - 250) / 250) * 25
    elif total < 1000:
        pct = 50 + ((total - 500) / 500) * 50
    else:
        pct = 100

    return {
        "year": year,
        "total": round(total),
        "months": [round(m) for m in months],
        "current_month": current_month,
        "bronze": bronze,
        "silver": silver,
        "gold": gold,
        "pct": min(round(pct), 100),
    }


def _monthly_pulse_html(mp: dict) -> str:
    """Build the Monthly Pulse card — compact 3-zone design.

    Zone 1 (header):   month label + big miles number + count/best inline
    Zone 2 (progress): bar with tier ticks + single status line
    Zone 3 (grid):     5 weekly cells

    Shares .summary-card / .sum-head / .sum-progress / .sum-grid classes
    with the yearly widget for visual consistency.
    """
    week_labels = ["WK 1", "WK 2", "WK 3", "WK 4", "WK 5"]
    week_cells = []
    for i in range(5):
        val = mp["weeks"][i]
        has = val > 0
        is_now = i == mp["current_week"]
        if is_now:
            cls = "sum-cell is-current"
        elif has:
            cls = "sum-cell has-data"
        else:
            cls = "sum-cell empty"
        display = f'{val}<span class="sum-cell-unit">mi</span>' if has else "&mdash;"
        week_cells.append(
            f'<div class="{cls}"><div class="sum-cell-val">{display}</div>'
            f'<div class="sum-cell-lbl">{week_labels[i]}</div></div>'
        )

    # Medal tiers (calibrated from history, see git log)
    TIERS = [
        ("Bronze", 50,  "\U0001F949"),
        ("Silver", 100, "\U0001F948"),
        ("Gold",   150, "\U0001F947"),
    ]
    total = mp["total"]
    earned = [t for t in TIERS if total >= t[1]]
    nxt = next((t for t in TIERS if total < t[1]), None)
    max_scale = TIERS[-1][1]
    bar_pct = min(round((total / max_scale) * 100), 100)

    month_full_names = {
        "Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April",
        "May": "May", "Jun": "June", "Jul": "July", "Aug": "August",
        "Sep": "September", "Oct": "October", "Nov": "November", "Dec": "December",
    }
    month_full = month_full_names.get(mp["month_name"], mp["month_name"])
    current_year = datetime.now().year
    ride_word = "ride" if mp["count"] == 1 else "rides"

    return f'''<div class="card summary-card">
  <div class="sum-head">
    <div class="sum-title-block">
      <div class="sum-eyebrow">This Month</div>
      <div class="sum-title">{month_full} {current_year}</div>
    </div>
    <div class="sum-hero-block">
      <div class="sum-hero-num">{total}</div>
      <div class="sum-hero-unit">mi</div>
    </div>
  </div>
  <div class="sum-stats">
    {mp["count"]} {ride_word}  &middot;  longest <strong>{mp["best"]}</strong> mi
  </div>
  {_progress_zone_html(total, earned, nxt, TIERS, bar_pct, "mi")}
  <div class="sum-grid sum-grid-weeks">
    {"".join(week_cells)}
  </div>
</div>'''


def _progress_zone_html(total, earned, nxt, tiers, bar_pct, unit):
    """Shared progress zone for both monthly and yearly cards.

    Renders a single horizontal bar with 3 tier ticks (medal icon above,
    threshold label below) plus ONE single-line status telling you
    either what you've earned or how far to the next medal.
    """
    # Tier ticks on the bar
    max_scale = tiers[-1][1]
    ticks = []
    for name, threshold, emoji in tiers:
        pct = round((threshold / max_scale) * 100)
        lit = "lit" if total >= threshold else "dim"
        disp = f"{threshold // 1000}k" if threshold >= 1000 else str(threshold)
        ticks.append(
            f'<div class="sum-tick {lit}" style="left:{pct}%;" title="{name} — {threshold}{unit}">'
            f'<span class="sum-tick-ico">{emoji}</span>'
            f'<span class="sum-tick-lbl">{disp}</span></div>'
        )

    # Status line: one sentence covering both earned + next
    if earned and nxt:
        latest_emoji = earned[-1][2]
        latest_name = earned[-1][0]
        to_go = nxt[1] - total
        status = (
            f'<span class="sum-status-earned">{latest_emoji} {latest_name} earned</span>'
            f'<span class="sum-status-sep"> &middot; </span>'
            f'<span class="sum-status-next"><strong>{to_go}</strong> {unit} to {nxt[2]} {nxt[0]}</span>'
        )
    elif earned and not nxt:
        status = f'<span class="sum-status-done">\U0001F3C6 All 3 medals earned &middot; {total:,} {unit}</span>'
    else:
        to_go = nxt[1] - total if nxt else 0
        status = (
            f'<span class="sum-status-next"><strong>{to_go}</strong> {unit} to first medal '
            f'{nxt[2]} {nxt[0]}</span>' if nxt else
            f'<span class="sum-status-done">{total:,} {unit}</span>'
        )

    return f'''<div class="sum-progress">
    <div class="sum-bar">
      <div class="sum-bar-fill" style="width:{bar_pct}%"></div>
      {"".join(ticks)}
    </div>
    <div class="sum-status">{status}</div>
  </div>'''


def _year_over_year(rides: list[dict], current_year: int) -> dict:
    """Compute cumulative mileage by day-of-year for each year that has rides.

    Returns per-year totals, cumulative curves (365 points), and today's
    same-day-last-year comparisons.
    """
    today = datetime.now()
    today_doy = today.timetuple().tm_yday  # 1..366

    by_year: dict[int, list[float]] = {}  # year -> cumulative miles by day-of-year
    totals: dict[int, float] = {}
    counts: dict[int, int] = {}
    for r in rides:
        try:
            dt = datetime.strptime(r["date"], "%b %d, %Y")
        except ValueError:
            continue
        y = dt.year
        doy = dt.timetuple().tm_yday
        if y not in by_year:
            by_year[y] = [0.0] * 367  # 1..366 inclusive
        by_year[y][doy] += r["distance"]
        totals[y] = totals.get(y, 0.0) + r["distance"]
        counts[y] = counts.get(y, 0) + 1

    # Turn daily buckets into cumulative arrays
    cumulative: dict[int, list[float]] = {}
    for y, arr in by_year.items():
        running = 0.0
        cum = [0.0] * 367
        for d in range(1, 367):
            running += arr[d]
            cum[d] = running
        cumulative[y] = cum

    # Same-day-last-year: where was the user at this day-of-year in each past year?
    same_day = {}
    for y, cum in cumulative.items():
        same_day[y] = cum[min(today_doy, 366)]

    # Pace comparisons (relative to current year's same-day total)
    cur_today = same_day.get(current_year, 0.0)
    past_years = sorted([y for y in totals if y < current_year], reverse=True)
    last_year_same_day = same_day.get(current_year - 1) if (current_year - 1) in same_day else None
    avg_past_same_day = (
        sum(same_day[y] for y in past_years) / len(past_years) if past_years else None
    )

    # Pick top N past years by total miles (not too many) for the sparkline.
    # Always include: current year, last year, user's best year.
    if past_years:
        by_total_desc = sorted(past_years, key=lambda y: totals[y], reverse=True)
        spark_years = set([current_year] + past_years[:1] + by_total_desc[:1] + past_years[:2])
    else:
        spark_years = {current_year}
    # Cap at 6 lines for readability
    spark_years_list = sorted(spark_years, reverse=True)[:6]

    return {
        "current_year": current_year,
        "today_doy": today_doy,
        "today_label": today.strftime("%b %d"),
        "totals": {y: round(totals[y]) for y in totals},
        "counts": counts,
        "same_day": {y: round(same_day[y]) for y in same_day},
        "cur_today": round(cur_today),
        "last_year_same_day": round(last_year_same_day) if last_year_same_day is not None else None,
        "avg_past_same_day": round(avg_past_same_day) if avg_past_same_day is not None else None,
        "spark_years": spark_years_list,
        "cumulative": cumulative,
        "all_years_desc": sorted(totals.keys(), reverse=True),
    }


def _year_over_year_html(yoy: dict) -> str:
    """Build the Year-over-Year Progress card.

    Sections:
      1. Pace headline: where you are today vs same day last year & avg of past
      2. Sparkline: SVG cumulative-miles-by-day-of-year for each highlighted year
      3. Year bars: horizontal bar per recent year, with label + total + bar
    """
    cy = yoy["current_year"]
    cur_today = yoy["cur_today"]
    ly_same = yoy["last_year_same_day"]
    avg_same = yoy["avg_past_same_day"]
    today_label = yoy["today_label"]

    # ── Pace headline (plain English, no cryptic deltas) ──
    def _delta_chip(now_val, then_val, year_label):
        """Render a single comparison chip in plain English.

        e.g. "307 mi behind {year_label}" / "84 mi ahead of {year_label}"
        with a sub-line: "you: {now}mi  ·  {year_label}: {then}mi by today"
        """
        if then_val is None:
            return ""
        diff = now_val - then_val
        ahead = diff >= 0
        abs_diff = abs(diff)
        cls = "yoy-chip yoy-up" if ahead else "yoy-chip yoy-down"
        icon = "\u25B2" if ahead else "\u25BC"  # ▲ ▼
        status_word = "ahead of" if ahead else "behind"
        return (
            f'<div class="{cls}">'
            f'<div class="yoy-chip-hero">'
            f'<span class="yoy-chip-icon">{icon}</span>'
            f'<span class="yoy-chip-big">{abs_diff:,}</span>'
            f'<span class="yoy-chip-unit">mi</span>'
            f'</div>'
            f'<div class="yoy-chip-sentence">{status_word} <strong>{year_label}</strong> at this date</div>'
            f'<div class="yoy-chip-sub">'
            f'You: <strong>{now_val:,} mi</strong>  &middot;  {year_label}: <strong>{then_val:,} mi</strong>'
            f'</div>'
            f'</div>'
        )

    chips = []
    if ly_same is not None:
        chips.append(_delta_chip(cur_today, ly_same, f"{cy - 1}"))
    if avg_same is not None:
        chips.append(_delta_chip(cur_today, avg_same, f"prior avg"))
    chips_html = "".join(chips) or '<div class="yoy-chip-empty">No prior-year data yet.</div>'

    # ── Sparkline (SVG cumulative curves) ──
    vw, vh = 520, 150
    pad_l, pad_r, pad_t, pad_b = 28, 12, 10, 22
    plot_w = vw - pad_l - pad_r
    plot_h = vh - pad_t - pad_b

    # Determine max y-axis value across shown years
    spark = yoy["spark_years"] or [cy]
    max_mi = 0
    for y in spark:
        cum = yoy["cumulative"].get(y, [])
        if cum:
            max_mi = max(max_mi, max(cum))
    if max_mi <= 0:
        max_mi = 100
    # Round up to nice axis value
    def _nice_ceiling(v):
        for step in [250, 500, 1000, 2000, 3000, 5000, 7500, 10000, 15000, 20000]:
            if v <= step:
                return step
        return int(v * 1.1)
    y_max = _nice_ceiling(max_mi)

    def _x(doy):
        return pad_l + (doy / 366) * plot_w
    def _y(miles):
        return pad_t + plot_h - (miles / y_max) * plot_h

    # Month gridlines (subtle)
    month_ticks = []
    for m in range(1, 13):
        doy_first = datetime(2024, m, 1).timetuple().tm_yday  # 2024 is a leap year, nice baseline
        x = _x(doy_first)
        month_ticks.append(
            f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t + plot_h}" '
            f'stroke="rgba(255,255,255,0.05)" stroke-width="1"/>'
        )
        if m in (1, 4, 7, 10):
            label = ["", "Jan", "", "", "Apr", "", "", "Jul", "", "", "Oct", "", ""][m]
            month_ticks.append(
                f'<text x="{x:.1f}" y="{vh - 6}" fill="rgba(125,154,184,0.6)" '
                f'font-size="9" font-family="DM Mono">{label}</text>'
            )

    # Y-axis labels at 0, half, max
    y_labels_html = []
    for frac, label in [(0, "0"), (0.5, f"{y_max // 2:,}"), (1, f"{y_max:,}")]:
        yv = pad_t + plot_h - frac * plot_h
        y_labels_html.append(
            f'<text x="{pad_l - 4}" y="{yv + 3:.1f}" fill="rgba(125,154,184,0.55)" '
            f'font-size="9" font-family="DM Mono" text-anchor="end">{label}</text>'
        )

    # Lines for each year
    line_svg = []
    legend_items = []
    past_year_colors = ["#7a9ab8", "#5f86ae", "#4c6f93", "#3d5a77"]  # cool fading palette
    past_idx = 0
    for y in sorted(spark, reverse=True):
        cum = yoy["cumulative"].get(y, [])
        if not cum:
            continue
        max_doy = 366 if y < datetime.now().year else yoy["today_doy"]
        pts = []
        for d in range(1, max_doy + 1):
            pts.append(f"{_x(d):.1f},{_y(cum[d]):.1f}")
        is_current = y == cy
        stroke = "var(--gold)" if is_current else past_year_colors[past_idx % len(past_year_colors)]
        stroke_w = 2.5 if is_current else 1.2
        if not is_current:
            past_idx += 1
        # Polyline
        line_svg.append(
            f'<polyline fill="none" stroke="{stroke}" stroke-width="{stroke_w}" '
            f'stroke-linecap="round" stroke-linejoin="round" '
            f'opacity="{1.0 if is_current else 0.7}" points="{" ".join(pts)}"/>'
        )
        # End-of-line year label
        if pts:
            last_x, last_y = pts[-1].split(",")
            fs = 10 if is_current else 9
            fw = "600" if is_current else "400"
            line_svg.append(
                f'<text x="{float(last_x) + 4:.1f}" y="{float(last_y) + 3:.1f}" '
                f'fill="{stroke}" font-size="{fs}" font-weight="{fw}" font-family="DM Mono">{y}</text>'
            )
        # Legend
        total = yoy["totals"].get(y, 0)
        legend_items.append(
            f'<div class="yoy-legend-item">'
            f'<span class="yoy-dot" style="background:{stroke};{"box-shadow:0 0 6px " + stroke if is_current else ""}"></span>'
            f'<span class="yoy-legend-year">{y}</span>'
            f'<span class="yoy-legend-mi">{total:,}<span class="yoy-mi">mi</span></span>'
            f'</div>'
        )

    # Today marker (vertical dashed line)
    today_x = _x(yoy["today_doy"])
    today_marker = (
        f'<line x1="{today_x:.1f}" y1="{pad_t}" x2="{today_x:.1f}" y2="{pad_t + plot_h}" '
        f'stroke="var(--mint)" stroke-width="1" stroke-dasharray="3,3" opacity="0.5"/>'
        f'<text x="{today_x:.1f}" y="{pad_t - 2}" fill="var(--mint)" font-size="9" '
        f'font-weight="600" font-family="DM Mono" text-anchor="middle">today &middot; {today_label}</text>'
    )

    sparkline = (
        f'<svg viewBox="0 0 {vw} {vh}" preserveAspectRatio="xMidYMid meet" class="yoy-svg">'
        + "".join(month_ticks)
        + "".join(y_labels_html)
        + "".join(line_svg)
        + today_marker
        + "</svg>"
    )

    # ── Year bars (horizontal ranking) — now merged with the "Yearly
    # Breakdown" section. Each row shows total miles, ride count, and the
    # best ride of that year so users see everything at a glance.
    from collections import defaultdict
    # Collect best-ride string per year from the breakdown helper (reuse
    # what _yearly_breakdown produces for consistency).
    _all_rides_by_year = defaultdict(list)
    # We only have totals + counts here; the "best" comes from full ride data
    # in the caller. So we'll compute it from yoy["cumulative"] missing —
    # instead, derive from totals/counts alone and let the caller pass rides
    # via a closure-like param. Simpler: use yoy["best_by_year"] if present.
    best_by_year = yoy.get("best_by_year", {})

    bars_years = [cy] + [y for y in yoy["all_years_desc"] if y != cy][:5]
    max_total = max((yoy["totals"].get(y, 0) for y in bars_years), default=1) or 1
    bars_html = []
    for y in bars_years:
        total = yoy["totals"].get(y, 0)
        count = yoy["counts"].get(y, 0)
        pct = round((total / max_total) * 100) if max_total else 0
        is_cur = y == cy
        cls = "yoy-bar-row" + (" yoy-bar-current" if is_cur else "")
        best = best_by_year.get(y, "")
        best_html = f'<div class="yoy-bar-best">Best: {html.escape(best)}</div>' if best else ""
        bars_html.append(
            f'<div class="{cls}">'
            f'<div class="yoy-bar-top">'
            f'<div class="yoy-bar-year">{y}</div>'
            f'<div class="yoy-bar-val">{total:,}<span class="yoy-mi"> mi</span></div>'
            f'<div class="yoy-bar-count">{count} rides</div>'
            f'</div>'
            f'<div class="yoy-bar-track"><div class="yoy-bar-fill" style="width:{pct}%;"></div></div>'
            f'{best_html}'
            f'</div>'
        )

    # Section-level subtitle that tells you what you're looking at in one line.
    cur_total = yoy["totals"].get(cy, 0)
    if ly_same is not None:
        diff = cur_today - ly_same
        if diff >= 0:
            one_liner = f"You're <strong>{abs(diff):,} mi ahead</strong> of where you were this time last year."
        else:
            one_liner = f"You're <strong>{abs(diff):,} mi behind</strong> where you were this time last year."
    else:
        one_liner = f"{cur_total:,} miles this year so far."

    return f'''<div class="card yoy-card summary-card">
  <div class="sum-head">
    <div class="sum-title-block">
      <div class="sum-eyebrow">vs. past years</div>
      <div class="sum-title">How {cy} compares</div>
    </div>
  </div>
  <div class="sum-stats">{one_liner}</div>
  <div class="yoy-chips">{chips_html}</div>
  <div class="yoy-spark-wrap">{sparkline}</div>
  <div class="yoy-legend">{"".join(legend_items)}</div>
  <div class="yoy-divider"></div>
  <div class="sum-eyebrow" style="margin-top:6px">Year-by-year totals</div>
  <div class="yoy-bars">{"".join(bars_html)}</div>
</div>'''


def _yearly_widget_html(ym: dict) -> str:
    """Build the Year at a Glance card — compact 3-zone design that
    visually matches the monthly pulse card."""
    month_names = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                   "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

    TIERS = [
        ("Bronze", 250,  "\U0001F949"),
        ("Silver", 500,  "\U0001F948"),
        ("Gold",   1000, "\U0001F947"),
    ]
    total = ym["total"]
    earned = [t for t in TIERS if total >= t[1]]
    nxt = next((t for t in TIERS if total < t[1]), None)
    bar_pct = min(round((total / TIERS[-1][1]) * 100), 100)

    # Compact month strip: past + current months only, +N TO GO chip
    current_m = ym["current_month"]
    cells = []
    for i in range(current_m):
        val = ym["months"][i]
        is_current = (i + 1) == current_m
        has = val > 0
        if is_current:
            cls = "sum-cell is-current"
        elif has:
            cls = "sum-cell has-data"
        else:
            cls = "sum-cell empty"
        display = f'{val}<span class="sum-cell-unit">mi</span>' if has else "&mdash;"
        cells.append(
            f'<div class="{cls}"><div class="sum-cell-val">{display}</div>'
            f'<div class="sum-cell-lbl">{month_names[i]}</div></div>'
        )
    months_remaining = 12 - current_m
    if months_remaining > 0:
        cells.append(
            f'<div class="sum-cell sum-cell-more" title="months remaining in {ym["year"]}">'
            f'<div class="sum-cell-val">+{months_remaining}</div>'
            f'<div class="sum-cell-lbl">TO GO</div></div>'
        )

    # Quick stats: active months so far + avg/active-month
    active_months = sum(1 for v in ym["months"][:current_m] if v > 0)
    avg_per_active = round(total / active_months) if active_months else 0
    stats_text = (
        f'{active_months} active month{"s" if active_months != 1 else ""}'
        f'  &middot;  avg <strong>{avg_per_active}</strong> mi/month'
    )

    return f'''<div class="card summary-card">
  <div class="sum-head">
    <div class="sum-title-block">
      <div class="sum-eyebrow">This Year</div>
      <div class="sum-title">{ym["year"]}</div>
    </div>
    <div class="sum-hero-block">
      <div class="sum-hero-num">{ym["total"]}</div>
      <div class="sum-hero-unit">mi</div>
    </div>
  </div>
  <div class="sum-stats">{stats_text}</div>
  {_progress_zone_html(total, earned, nxt, TIERS, bar_pct, "mi")}
  <div class="sum-grid sum-grid-months">
    {"".join(cells)}
  </div>
</div>'''


# ── California coverage map ─────────────────────────────────────────

CA_REGIONS = [
    "Peninsula", "Bay Area", "East Bay", "Marin", "Wine Country",
    "Monterey", "Sierra Foothills", "Lake Tahoe", "Santa Cruz",
    "Santa Barbara", "Paso Robles", "Sacramento", "Humboldt", "Palm Springs",
]

# Simplified California state outline (lat, lng pairs)
# Traced from a simplified polygon — enough for a visual outline
CA_OUTLINE = [
    (42.0, -124.2), (41.99, -121.35), (42.0, -120.0),
    (39.0, -120.0), (38.99, -119.85), (38.5, -120.0),
    (38.0, -118.5), (37.5, -117.6), (36.0, -116.5),
    (35.6, -115.5), (35.0, -114.6), (34.5, -114.6),
    (32.72, -114.5), (32.53, -117.13), (33.0, -117.3),
    (33.35, -117.6), (33.5, -117.8), (33.75, -118.1),
    (33.95, -118.5), (34.0, -118.6), (34.05, -119.0),
    (34.35, -119.6), (34.45, -120.0), (34.5, -120.5),
    (35.0, -120.9), (35.5, -121.1), (36.0, -121.6),
    (36.6, -121.9), (36.9, -122.0), (37.1, -122.2),
    (37.5, -122.4), (37.8, -122.5), (38.0, -122.7),
    (38.0, -123.0), (38.3, -123.1), (38.7, -123.4),
    (38.9, -123.7), (39.3, -123.8), (39.8, -123.8),
    (40.0, -124.1), (40.4, -124.3), (41.0, -124.1),
    (41.7, -124.2), (42.0, -124.2),
]

# City labels to place on the map. These are shown as the map backdrop
# and take priority over generic pin labels (so a ride clustered in Napa
# will show "Napa" not "Wine Country").
CA_CITIES = [
    # NorCal coast + inland
    ("Crescent City", 41.76, -124.20),
    # (Pin for Crescent City sits further south; keeping this as reference only)
    ("Eureka", 40.80, -124.16),
    ("Redding", 40.59, -122.39),
    ("Mendocino", 39.30, -123.80),
    ("Chico", 39.73, -121.84),
    # Wine Country + Sacramento valley
    ("Healdsburg", 38.61, -122.87),
    ("Santa Rosa", 38.44, -122.71),
    ("Napa", 38.30, -122.30),
    ("Sacramento", 38.58, -121.49),
    ("Davis", 38.55, -121.74),
    ("Nevada City", 39.26, -121.02),
    ("Lake Tahoe", 39.10, -120.03),
    # Bay Area
    ("Point Reyes", 38.07, -122.85),
    ("San Francisco", 37.77, -122.42),
    ("Berkeley", 37.87, -122.27),
    ("Palo Alto", 37.44, -122.14),
    ("San Jose", 37.34, -121.89),
    ("Santa Cruz", 36.97, -122.03),
    ("Monterey", 36.60, -121.89),
    # Central
    ("Yosemite", 37.87, -119.55),
    ("Paso Robles", 35.63, -120.69),
    ("Bakersfield", 35.37, -119.02),
    # SoCal
    ("Santa Barbara", 34.42, -119.70),
    ("Los Angeles", 34.05, -118.24),
    ("Palm Springs", 33.83, -116.55),
    ("San Diego", 32.72, -117.16),
]


# Destination → (lat, lng) geocoding table for booked + wishlist rides
DESTINATION_COORDS = {
    # Booked events
    "sea otter": (36.60, -121.89),
    "levi": (38.44, -122.71),  # Santa Rosa
    "granfondo": (38.44, -122.71),
    "santa rosa": (38.44, -122.71),
    "nevada city": (39.26, -121.02),
    "marin century": (38.06, -122.55),
    "monterey": (36.60, -121.89),
    # Wishlist - events
    "gilroy": (37.01, -121.57),
    "cloverdale": (38.81, -123.02),
    "palo alto": (37.44, -122.14),
    "half moon bay": (37.46, -122.43),
    "carmel valley": (36.48, -121.73),
    "redwood shores": (37.53, -122.25),
    "lodi": (38.13, -121.27),
    "fremont": (37.55, -121.99),
    "davis": (38.55, -121.74),
    "moraga": (37.83, -122.13),
    "watsonville": (36.91, -121.76),
    # Wishlist - regions
    "lake almanor": (40.25, -121.17),
    "mono lake": (37.99, -119.01),
    "june lake": (37.77, -119.07),
    "owens valley": (36.80, -118.20),
    "alabama hills": (36.60, -118.12),
    "santa cruz mountain": (37.15, -121.98),
    "palm springs": (33.83, -116.55),
    "placerville": (38.73, -120.80),
    "golden gate": (37.82, -122.48),
    "sausalito": (37.86, -122.48),
    "tiburon": (37.87, -122.46),
    "lake tahoe": (38.94, -119.97),
    "tahoe": (38.94, -119.97),
    "napa": (38.30, -122.30),
    "napa valley": (38.50, -122.37),
    "paso robles": (35.63, -120.69),
    "point reyes": (38.07, -122.85),
    "santa barbara": (34.42, -119.70),
    "solvang": (34.60, -120.14),
    # Newly added from library
    "lompoc": (34.64, -120.46),
    "jalama": (34.51, -120.50),
    "cambria": (35.56, -121.08),
    "cayucos": (35.44, -120.89),
    "morro bay": (35.37, -120.85),
    "los osos": (35.31, -120.83),
    "murphys": (38.14, -120.46),
    "angels camp": (38.08, -120.55),
    "ukiah": (39.15, -123.21),
    "redwood valley": (39.27, -123.21),
    # More new library additions
    "anza-borrego": (33.26, -116.41),
    "anza borrego": (33.26, -116.41),
    "ojai": (34.45, -119.24),
    "san luis obispo": (35.28, -120.66),
    "tomales": (38.25, -122.90),
    "tomales bay": (38.16, -122.92),
    "sierra valley": (39.70, -120.35),
    "san diego": (32.82, -117.14),
}


# Persistent geocoding cache for auto-geocoded places
GEOCODE_CACHE = SCRIPT_DIR / "cache" / "geocode_cache.json"


def _load_geocode_cache() -> dict:
    if GEOCODE_CACHE.exists():
        try:
            return json.loads(GEOCODE_CACHE.read_text())
        except Exception:
            return {}
    return {}


def _save_geocode_cache(cache: dict) -> None:
    GEOCODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    GEOCODE_CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def _auto_geocode(name: str) -> tuple | None:
    """Look up lat/lng via Nominatim (free, no API key). Cached to disk."""
    cache = _load_geocode_cache()
    key = name.strip().lower()

    if key in cache:
        coords = cache[key]
        return tuple(coords) if coords else None

    # Clean up the query — strip parens content for better geocoding, bias to California
    import re
    cleaned = re.sub(r"\([^)]*\)", "", name).strip()
    cleaned = cleaned.replace("→", ",").replace("·", ",").strip(", ")
    query = f"{cleaned}, California, USA"

    try:
        import ssl
        import certifi
        import os
        # Ensure certifi's cert bundle is used for HTTPS (fixes macOS SSL issues)
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

        from geopy.geocoders import Nominatim

        geolocator = Nominatim(
            user_agent="ride-atlas-sneha-os",
            ssl_context=ssl.create_default_context(cafile=certifi.where()),
        )
        result = geolocator.geocode(query, timeout=10)
        if result:
            coords = (result.latitude, result.longitude)
            cache[key] = list(coords)
            _save_geocode_cache(cache)
            log.info("Geocoded '%s' → %.3f, %.3f", name, *coords)
            return coords
        else:
            # Cache the miss so we don't keep hitting the API
            cache[key] = None
            _save_geocode_cache(cache)
            return None
    except Exception as e:
        log.warning("Geocode failed for '%s': %s", name, e)
        return None


def _geocode_destination(name: str) -> tuple | None:
    """Find lat/lng for a destination — try manual table first, then auto-geocode."""
    n = name.lower()
    # 1. Manual table (fastest, always reliable)
    for keyword, coords in DESTINATION_COORDS.items():
        if keyword in n:
            return coords
    # 2. Fallback to auto-geocoding (Nominatim, cached)
    return _auto_geocode(name)


def _ca_coverage_html(rides: list[dict]) -> str:
    """Build the California ride coverage map card."""
    ca_rides = [r for r in rides if r["region"] in CA_REGIONS]
    if not ca_rides:
        return ""

    # Map bounds — cover all of CA with padding
    min_lat, max_lat = 32.3, 42.2
    min_lng, max_lng = -124.5, -114.0

    vw, vh = 400, 520
    pad = 0.02

    def to_svg(lat, lng):
        x = ((lng - min_lng) / (max_lng - min_lng)) * vw
        y = (1 - (lat - min_lat) / (max_lat - min_lat)) * vh
        return round(x, 1), round(y, 1)

    # 1. California state outline
    outline_pts = [to_svg(lat, lng) for lat, lng in CA_OUTLINE]
    outline_d = " ".join(
        f"{'M' if i == 0 else 'L'}{p[0]},{p[1]}"
        for i, p in enumerate(outline_pts)
    ) + " Z"

    # Track placed label positions + names so we don't double-label a spot
    # or render the same city name twice.
    placed_labels = []  # list of (x, y) tuples
    placed_names = set()  # lowercase city names already placed

    def reserve_label(x, y, name=None):
        """Return True if no existing label overlaps this spot AND this name
        hasn't been placed already. Reserves the slot on success."""
        if name and name.lower() in placed_names:
            return False
        # Estimated label width — labels are ~5px per char, drawn to the right
        # of the dot. Use the longer name (the one being placed) to gauge.
        my_width = max(30, len(name) * 5) if name else 40
        for px, py, pw in placed_labels:
            # Vertical: only collide if same line (within 8px)
            if abs(y - py) >= 8:
                continue
            # Horizontal: they overlap if new label's left edge is inside
            # the existing label's span, or vice-versa.
            my_left, my_right = x, x + my_width
            p_left, p_right = px, px + pw
            if my_left < p_right + 4 and my_right > p_left - 4:
                return False
        placed_labels.append((x, y, my_width))
        if name:
            placed_names.add(name.lower())
        return True

    # Track pin positions so we can decide which CA_CITIES are near any pin
    # (and therefore worth showing).
    pin_positions = []  # list of (x, y)

    def extract_city(raw_name: str) -> str:
        """Extract a clean city name from a pin's raw name.
        Event names have city in parens: 'ACTC Tierra Bella (Gilroy)' → 'Gilroy'.
        Trip names have city after ·: 'Road Cycling Trip · Nevada City' → 'Nevada City'.
        Compound names take the first segment: 'Lompoc + Jalama Road' → 'Lompoc',
        'Owens Valley + Alabama Hills' → 'Owens Valley'.
        """
        s = raw_name.strip()
        if "(" in s and ")" in s:
            return s[s.index("(") + 1 : s.index(")")].strip()
        if "·" in s:
            parts = [p.strip() for p in s.split("·")]
            return parts[-1]
        if "+" in s:
            return s.split("+")[0].strip()
        if "→" in s:
            return s.split("→")[0].strip()
        return s

    # 2. Cluster completed rides by rounded coordinates so N overlapping
    #    rides at the same trailhead become ONE dot with aggregated stats.
    #
    #    Clustering key = region + (lat rounded to 0.1deg, lng rounded to 0.1deg)
    #    ≈ 11km cell, scoped within a region. This deduplicates things like
    #    "Marin Headlands" (6 rides, all Marin, all within a ~5km radius)
    #    into a SINGLE pin instead of the 3 confusingly-named pins the user
    #    was seeing. Region scoping means we never accidentally merge two
    #    unrelated regions that happen to sit in the same 11km grid cell.
    from collections import defaultdict as _dd
    clusters = _dd(list)
    for r in ca_rides:
        latlng = r.get("start_latlng")
        if not latlng:
            continue
        region = r.get("region", "")
        key = (region, round(latlng[0], 1), round(latlng[1], 1))
        clusters[key].append(r)

    ride_paths = []  # kept empty — we no longer draw polylines
    ridden_data = []
    # Collect (x, y, idx) tuples; we'll super-cluster then render markers.
    ridden_points = []
    # Sort: biggest clusters first (most rides → most important to label)
    sorted_clusters = sorted(
        clusters.items(),
        key=lambda kv: (-len(kv[1]), -sum(r["distance"] for r in kv[1])),
    )
    for i, (key, group) in enumerate(sorted_clusters):
        # Use average position of the cluster
        avg_lat = sum(r["start_latlng"][0] for r in group) / len(group)
        avg_lng = sum(r["start_latlng"][1] for r in group) / len(group)
        x, y = to_svg(avg_lat, avg_lng)

        # Aggregate stats
        total_mi = sum(r["distance"] for r in group)
        longest = max(group, key=lambda r: r["distance"])
        regions = sorted({r["region"] for r in group})
        region_label = regions[0] if len(regions) == 1 else f"{regions[0]} area"

        # Use the region name or longest ride name for the cluster title
        if len(group) == 1:
            title = group[0]["name"]
        else:
            title = f"{region_label} &middot; {len(group)} rides"

        # Short place name for next-to-pin label (muted gray, map-style)
        # Strip ride-type suffixes from the name so "Eureka Ride" becomes "Eureka".
        RIDE_SUFFIXES = {"ride", "rides", "loop", "loops", "climb", "climbs",
                         "century", "epic", "trip"}
        if len(group) > 1:
            short_name = region_label
        else:
            raw = group[0]["name"]
            # Remove distance tag like "40mi" and trailing ride-type suffix
            words = raw.split()
            # Drop trailing suffix words (e.g., "Climb", "Loop")
            while words and words[-1].lower() in RIDE_SUFFIXES:
                words.pop()
            # Drop trailing distance tag like "40mi"
            if words and words[-1].endswith("mi") and words[-1][:-2].isdigit():
                words.pop()
            short_name = " ".join(words[:3]) if words else raw
        # Add a city-name label near the pin ONLY if no existing city is nearby.
        # For clustered ridden pins, prefer the region name as the "city".
        if len(group) == 1:
            # Strip trailing distance + ride-type ("Eureka Ride", "Stanford Loop 30mi")
            words = group[0]["name"].split()
            suffixes = {"ride", "rides", "loop", "loops", "climb", "climbs",
                        "century", "epic", "trip"}
            while words and (words[-1].lower() in suffixes or
                             (words[-1].endswith("mi") and words[-1][:-2].isdigit())):
                words.pop()
            city = " ".join(words) if words else region_label
        else:
            city = region_label.replace(" area", "")
        label_svg = ""
        if reserve_label(x, y, city):
            label_svg = f'<text x="{x + 6}" y="{y + 3}" class="map-city">{html.escape(city)}</text>'
        pin_positions.append((x, y))
        ridden_points.append({
            "x": x, "y": y,
            "idx": f"ridden-{i}",
            "label_svg": label_svg,
        })

        # Store cluster center for wishlist dedup
        ridden_data.append({
            "idx": f"ridden-{i}",
            "lat": avg_lat,
            "lng": avg_lng,
            "name": title,
            "date": f'{len(group)} ride{"" if len(group) == 1 else "s"} · {total_mi:.0f} mi total',
            "type": "ridden",
            "notes": (
                f'Longest: {longest["name"]} ({longest["distance"]} mi, '
                f'{longest["elevation"]:,} ft, {longest["date"]})'
                if len(group) > 1
                else f'{longest["distance"]} mi · {longest["elevation"]:,} ft · {longest["date"]}'
            ),
        })

    # 3. City labels — show only cities that are near an actual pin, so we
    #    don't clutter the map with irrelevant place names (e.g., San Diego
    #    when no dots exist near it).
    #    We'll populate this AFTER all pins are placed so we know their coords.
    city_labels = []  # filled in after pins are processed

    # 4. BOOKED pins (from Master Planner — upcoming cycling trips)
    # Uses the same Google OAuth flow as the Quest Hub (token.json on
    # Mac, GOOGLE_TOKEN_JSON env var on Render). The old code here tried
    # to read service_account.json which was never actually in the
    # OAuth flow and broke on Render.
    booked_data = []
    booked_points = []
    try:
        from travel_source import fetch_travel_pins
        from google_auth import get_google_creds
        creds = get_google_creds()
        pins = fetch_travel_pins(creds)
        booked_cycling = [p for p in pins if not p["pinned"] and p["icon"] == "\U0001f6b4"]

        for i, p in enumerate(booked_cycling):
            coords = _geocode_destination(p["name"])
            if not coords:
                continue
            x, y = to_svg(coords[0], coords[1])
            # Short place label — prefer the destination (after "·") over
            # the generic trip name (e.g., "Nevada City" over "Road Cycling Trip")
            parts = [s.strip() for s in p["name"].split("·")]
            if len(parts) > 1:
                label = parts[-1]  # destination
            else:
                label = parts[0]
            short = " ".join(label.split()[:2])
            city = extract_city(p["name"])
            label_svg = ""
            if reserve_label(x, y, city):
                label_svg = f'<text x="{x + 6}" y="{y + 3}" class="map-city">{html.escape(city)}</text>'
            pin_positions.append((x, y))
            booked_points.append({
                "x": x, "y": y,
                "idx": f"booked-{i}",
                "label_svg": label_svg,
            })
            booked_data.append({
                "idx": f"booked-{i}",
                "name": p["name"],
                "date": f'{p["start_date"]} → {p.get("end_date", "")}',
                "type": "booked",
                "notes": f'{p.get("days", 1)} day trip · Booked',
                "lat": coords[0],
                "lng": coords[1],
            })
    except Exception as e:
        log.warning("Could not fetch booked pins: %s", e)

    # 5. WISHLIST pins (from Library — cycling dreams)
    wishlist_data = []
    wishlist_points = []
    try:
        from travel_source import fetch_library_cycling
        from google_auth import get_google_creds
        creds = get_google_creds()
        wish = fetch_library_cycling(creds)

        # Deduplicate by location
        seen_coords = set()
        # Also collect ridden cluster centers for proximity dedup
        ridden_centers = [(d["lat"], d["lng"]) for d in ridden_data]

        for i, item in enumerate(wish):
            coords = _geocode_destination(item["name"])
            if not coords:
                continue
            # Dedupe multiple wishlist entries in the same spot
            key = (round(coords[0], 2), round(coords[1], 2))
            if key in seen_coords:
                continue
            seen_coords.add(key)

            # Dedup rule for GENERIC wishlist entries ("Napa Valley",
            # "Lake Tahoe") — hide if you've already ridden OR booked
            # within ~15 miles. Event-tagged entries always show because
            # they're specific dated rides, not a generic location.
            tags_lower = item.get("tags", "").lower()
            is_event = "event" in tags_lower

            if not is_event:
                # Collect nearby anchors: ridden clusters + booked destinations
                anchors = list(ridden_centers)
                for bp in booked_data:
                    if "lat" in bp and "lng" in bp:
                        anchors.append((bp["lat"], bp["lng"]))

                too_close = any(
                    (coords[0] - lat) ** 2 + (coords[1] - lng) ** 2 < 0.04
                    for lat, lng in anchors
                )
                if too_close:
                    continue

            x, y = to_svg(coords[0], coords[1])
            # Short label: city in parens ("ACTC Tierra Bella (Gilroy)" → "Gilroy")
            raw = item["name"]
            if "(" in raw and ")" in raw:
                wlabel = raw[raw.index("(") + 1 : raw.index(")")]
            else:
                wlabel = " ".join(raw.split()[:2])
            city = extract_city(item["name"])
            label_svg = ""
            if reserve_label(x, y, city):
                label_svg = f'<text x="{x + 6}" y="{y + 3}" class="map-city">{html.escape(city)}</text>'
            pin_positions.append((x, y))
            wishlist_points.append({
                "x": x, "y": y,
                "idx": f"wish-{i}",
                "label_svg": label_svg,
            })
            wishlist_data.append({
                "idx": f"wish-{i}",
                "name": item["name"],
                "date": item.get("best_months", ""),
                "type": "wishlist",
                "notes": item.get("notes", ""),
            })
    except Exception as e:
        log.warning("Could not fetch wishlist pins: %s", e)

    # Add city labels only for cities that have a pin within ~30px (visual proximity)
    # AND aren't already labeled by a pin.
    NEAR_PIN_DIST = 30
    for cname, clat, clng in CA_CITIES:
        cx, cy = to_svg(clat, clng)
        # Skip if a pin already labels this name
        if cname.lower() in placed_names:
            continue
        # Must be near a pin
        near = any(
            ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5 < NEAR_PIN_DIST
            for px, py in pin_positions
        )
        if not near:
            continue
        if reserve_label(cx, cy, cname):
            city_labels.append(
                f'<text x="{cx + 6}" y="{cy + 3}" class="map-city">{html.escape(cname)}</text>'
            )

    # ── Screen-space super-clustering ─────────────────────────────────
    # Merge pins of the same color that land within ~9 SVG px into a single
    # "cluster dot" with a count badge — keeps dense areas readable.
    def build_markers(points, color, pin_class, super_dist=9):
        groups = []
        for p in points:
            x, y, idx, label_svg = p["x"], p["y"], p["idx"], p["label_svg"]
            placed = False
            for g in groups:
                if (x - g["cx"]) ** 2 + (y - g["cy"]) ** 2 < super_dist ** 2:
                    g["pts"].append((x, y))
                    g["ids"].append(idx)
                    g["label_svg"] = g["label_svg"] or label_svg
                    g["cx"] = sum(pt[0] for pt in g["pts"]) / len(g["pts"])
                    g["cy"] = sum(pt[1] for pt in g["pts"]) / len(g["pts"])
                    placed = True
                    break
            if not placed:
                groups.append({
                    "cx": x, "cy": y,
                    "pts": [(x, y)], "ids": [idx],
                    "label_svg": label_svg,
                })
        out = []
        for g in groups:
            n = len(g["ids"])
            cx, cy = g["cx"], g["cy"]
            primary = g["ids"][0]
            cluster_ids = ",".join(g["ids"])
            if n == 1:
                out.append(
                    f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.5" fill="{color}" '
                    f'class="{pin_class}" data-pin-idx="{primary}" '
                    f'data-cluster-ids="{cluster_ids}" style="cursor:pointer;"/>'
                    f'{g["label_svg"]}'
                )
            else:
                # Cluster pill — slightly larger with count
                out.append(
                    f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="6" fill="{color}" '
                    f'class="{pin_class} pin-cluster" data-pin-idx="{primary}" '
                    f'data-cluster-ids="{cluster_ids}" style="cursor:pointer;"/>'
                    f'<text x="{cx:.1f}" y="{cy + 2.5:.1f}" class="pin-count" '
                    f'text-anchor="middle">{n}</text>'
                    f'{g["label_svg"]}'
                )
        return "\n".join(out)

    ridden_markers = [build_markers(ridden_points, "#e05050", "pin-ridden")]
    booked_markers = [build_markers(booked_points, "#7dd3fc", "pin-booked")]
    wishlist_markers = [build_markers(wishlist_points, "#f5c842", "pin-wishlist")]

    pin_data_json = json.dumps(ridden_data + booked_data + wishlist_data, ensure_ascii=False)

    svg = f'''<svg viewBox="0 0 {vw} {vh}" preserveAspectRatio="xMidYMid meet" class="ca-map-svg">
  <!-- State outline -->
  <path d="{outline_d}" fill="rgba(125,211,252,0.04)" stroke="rgba(125,211,252,0.15)"
        stroke-width="1.5" stroke-linejoin="round"/>
  <!-- City labels (subtle background "map feel") — drawn first so pins sit on top -->
  {"".join(city_labels)}
  <!-- Wishlist pins (hollow gold) -->
  {"".join(wishlist_markers)}
  <!-- Booked pins (hollow blue) -->
  {"".join(booked_markers)}
  <!-- Ridden pins (hollow red) — clickable, clustered by location -->
  {"".join(ridden_markers)}
</svg>'''

    # Region summary stats
    region_stats = defaultdict(lambda: {"count": 0, "miles": 0})
    for r in ca_rides:
        region_stats[r["region"]]["count"] += 1
        region_stats[r["region"]]["miles"] += r["distance"]

    # Group some regions for display
    display_groups = [
        ("Bay Area", ["Peninsula", "Bay Area", "East Bay"]),
        ("Wine Country", ["Wine Country"]),
        ("Marin", ["Marin"]),
        ("Sierra / Tahoe", ["Lake Tahoe", "Sierra Foothills"]),
        ("Monterey", ["Monterey", "Santa Cruz"]),
        ("Central Coast", ["Paso Robles"]),
        ("SoCal", ["Santa Barbara", "Palm Springs"]),
        ("NorCal", ["Sacramento", "Humboldt"]),
    ]

    region_cells = []
    for label, regions in display_groups:
        count = sum(region_stats[r]["count"] for r in regions)
        miles = sum(region_stats[r]["miles"] for r in regions)
        if count == 0:
            continue
        region_cells.append(
            f'<div class="reg-cell">'
            f'<div class="reg-dot"></div>'
            f'<div class="reg-info">'
            f'<div class="reg-name">{label}</div>'
            f'<div class="reg-sub">{count} rides</div>'
            f'</div>'
            f'<div class="reg-miles">{miles:,.0f}<span>mi</span></div>'
            f'</div>'
        )

    region_grid = "\n".join(region_cells)

    return f'''<div class="section-label">California &middot; Ride Coverage</div>
<div class="card ca-coverage">
  <div class="ca-header">
    <div class="ca-header-text">
      <div class="ca-sub">ridden &middot; booked &middot; wishlist</div>
    </div>
    <div class="ca-count">
      <div class="ca-count-num">{len(ca_rides)}</div>
      <div class="ca-count-label">{len(ridden_data)} locations</div>
    </div>
  </div>
  <div class="ca-map-zone">
    {svg}
    <div class="ca-legend-float">
      <div class="leg-row">
        <span class="leg-dot leg-ridden"></span>
        <span class="leg-label">Ridden</span>
        <span class="leg-count">{len(ridden_data)}</span>
      </div>
      <div class="leg-row">
        <span class="leg-dot leg-booked"></span>
        <span class="leg-label">Booked</span>
        <span class="leg-count">{len(booked_data)}</span>
      </div>
      <div class="leg-row">
        <span class="leg-dot leg-wishlist"></span>
        <span class="leg-label">Wishlist</span>
        <span class="leg-count">{len(wishlist_data)}</span>
      </div>
    </div>
  </div>
  <div class="ca-hint">&#x1f449; Tap any dot for details</div>
  <div class="ca-chips">
    <div class="chip-row">
      <span class="chip-label chip-ridden">Ridden</span>
      {" ".join(f'<button class="chip chip-ridden-btn" data-pin-idx="{d["idx"]}">{html.escape(d["name"].split(" &middot; ")[0])}</button>' for d in ridden_data)}
    </div>
    <div class="chip-row">
      <span class="chip-label chip-booked">Booked</span>
      {" ".join(f'<button class="chip chip-booked-btn" data-pin-idx="{d["idx"]}">{html.escape(d["name"].split(" · ")[-1] if " · " in d["name"] else d["name"])}</button>' for d in booked_data)}
    </div>
    <div class="chip-row">
      <span class="chip-label chip-wishlist">Wishlist</span>
      {" ".join(f'<button class="chip chip-wishlist-btn" data-pin-idx="{d["idx"]}">{html.escape(d["name"].split(" (")[0])}</button>' for d in wishlist_data)}
    </div>
  </div>
  <div class="ca-popup" id="caPopup">
    <div class="ca-popup-close" onclick="document.getElementById('caPopup').classList.remove('show')">&times;</div>
    <div class="ca-popup-type" id="caPopupType"></div>
    <div class="ca-popup-title" id="caPopupTitle"></div>
    <div class="ca-popup-meta" id="caPopupMeta"></div>
    <div class="ca-popup-notes" id="caPopupNotes"></div>
  </div>
</div>
<script>
window.CA_PIN_DATA = {pin_data_json};
</script>

<div class="section-label">By Region</div>
<div class="reg-grid">
  {region_grid}
</div>'''


def _yearly_rows_html(breakdown: list[dict]) -> str:
    rows = []
    for y in breakdown:
        best = html.escape(y["best"])
        rows.append(
            f'<tr><td>{y["year"]}</td><td>{y["miles"]}</td>'
            f'<td>{y["count"]}</td><td class="best-ride">{best}</td></tr>'
        )
    return "\n".join(rows)


def _crown_html(rides: list[dict]) -> str:
    """Build the Crowning Achievement card — the longest ride."""
    if not rides:
        return ""
    crown = max(rides, key=lambda r: r["distance"])
    name = html.escape(crown["name"])
    region = html.escape(crown["region"])
    elev = f"{crown['elevation']:,}"
    # Format date as "Mon YYYY"
    try:
        dt = datetime.strptime(crown["date"], "%b %d, %Y")
        date_short = dt.strftime("%b %Y")
    except ValueError:
        date_short = crown["date"]
    # Moving time approx
    secs = crown.get("moving_time_secs", 0)
    hours = secs // 3600
    time_approx = f"~{hours}h saddle time" if hours else crown.get("moving_time", "")

    epic = '<div class="epic-badge">&#x2605; EPIC</div>' if crown["distance"] > 80 else ""

    return f'''<div class="crown-card" data-ride-id="{crown["id"]}">
  <div class="crown-map">
    <div style="color:var(--dim);font-size:10px;">&middot;</div>
    <div class="region-badge">{region}</div>
    {epic}
  </div>
  <div class="crown-info">
    <div class="crown-left">
      <div class="crown-name">{name}</div>
      <div class="crown-meta">
        <span>{elev} ft gain</span>
        <span>{date_short}</span>
        <span>{time_approx}</span>
      </div>
    </div>
    <div class="crown-right">
      <div class="crown-dist">{crown["distance"]} mi</div>
      <div class="crown-dist-label">longest ride</div>
    </div>
  </div>
</div>'''


def _route_card_html(r: dict) -> str:
    """Build a single route card."""
    name = html.escape(r["name"])
    region = html.escape(r["region"])
    epic = '<div class="epic-badge">EPIC</div>' if r["distance"] > 80 else ""
    # Date with year: "Oct 2023"
    try:
        dt = datetime.strptime(r["date"], "%b %d, %Y")
        date_short = dt.strftime("%b %Y")
    except ValueError:
        date_short = r["date"]

    return f'''<div class="route-card" data-ride-id="{r["id"]}">
  <div class="map-zone">
    <div style="color:var(--dim);font-size:10px;">&middot;</div>
    <div class="region-badge">{region}</div>
    {epic}
  </div>
  <div class="route-info">
    <div class="route-name">{name}</div>
    <div class="route-meta">{r["distance"]} mi &middot; {r["elevation"]:,} ft</div>
    <div class="route-date">{date_short}</div>
  </div>
</div>'''


def _regions_html(rides: list[dict]) -> str:
    """Build region-grouped card sections."""
    # Index rides by region
    by_region = defaultdict(list)
    for r in rides:
        by_region[r["region"]].append(r)

    sections = []
    for group_key, group_info in REGION_GROUPS.items():
        # Collect rides for all regions in this group
        group_rides = []
        for reg in group_info["regions"]:
            group_rides.extend(by_region.get(reg, []))

        if not group_rides:
            continue

        # Sort by distance desc (show most impressive first), limit
        group_rides.sort(key=lambda r: r["distance"], reverse=True)
        show = group_rides[:MAX_PER_GROUP]

        label = group_info["label"]
        cards = "\n".join(_route_card_html(r) for r in show)

        sections.append(
            f'<div class="section-label">{label}</div>\n'
            f'<div class="region-grid">\n{cards}\n</div>'
        )

    return "\n\n".join(sections)


def _upcoming_rides_html() -> str:
    """Fetch cycling trips from the Travel Master Planner sheet."""
    try:
        from travel_source import fetch_travel_pins
        from google_auth import get_google_creds
        creds = get_google_creds()
        pins = fetch_travel_pins(creds)
    except Exception as e:
        log.warning("Could not fetch travel pins: %s", e)
        return '<div class="upcoming-empty">No upcoming rides yet.</div>'

    # Filter to cycling trips only, not yet completed
    cycling_trips = [p for p in pins if not p["pinned"] and p["icon"] == "\U0001f6b4"]

    if not cycling_trips:
        return '<div class="upcoming-empty">No cycling trips on calendar.</div>'

    # Sort by start date
    def _parse(d):
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(d, fmt)
            except ValueError:
                continue
        return datetime.max

    cycling_trips.sort(key=lambda p: _parse(p["start_date"]))

    rows = []
    for p in cycling_trips:
        name = html.escape(p["name"])
        # Format dates: "Apr 17-18" or "Apr 24" or "May 22-25"
        try:
            start = _parse(p["start_date"])
            end = _parse(p["end_date"]) if p["end_date"] else start
            if start.month == end.month and start.day == end.day:
                date_str = start.strftime("%b %-d, %Y")
            elif start.month == end.month:
                date_str = f'{start.strftime("%b %-d")}-{end.day}, {start.year}'
            else:
                date_str = f'{start.strftime("%b %-d")} - {end.strftime("%b %-d")}, {start.year}'
        except Exception:
            date_str = p["start_date"]

        rows.append(
            f'<div class="upcoming-row">'
            f'<span class="upcoming-icon">&#x1f6b4;</span>'
            f'<div class="upcoming-info">'
            f'<div class="upcoming-name">{name}</div>'
            f'<div class="upcoming-sub">{date_str}</div>'
            f'</div>'
            f'</div>'
        )

    return "\n".join(rows)


def _insight_text(rides: list[dict], breakdown: list[dict]) -> str:
    if not rides:
        return "No rides yet. Get out there!"
    stats = _lifetime_stats(rides)
    biggest = max(breakdown, key=lambda y: float(y["miles"].replace(",", "")))

    # Try to get first upcoming cycling trip
    next_up = None
    try:
        from travel_source import fetch_travel_pins
        from google_auth import get_google_creds
        creds = get_google_creds()
        pins = fetch_travel_pins(creds)
        cycling = [p for p in pins if not p["pinned"] and p["icon"] == "\U0001f6b4"]

        def _parse(d):
            for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
                try: return datetime.strptime(d, fmt)
                except ValueError: continue
            return datetime.max

        cycling.sort(key=lambda p: _parse(p["start_date"]))
        if cycling:
            # Strip the region suffix if present: "Name · Region" → "Name"
            next_up = cycling[0]["name"].split(" · ")[0]
    except Exception:
        pass

    insight = (
        f'<strong>{stats["miles"]}</strong> total miles across '
        f'<strong>{stats["count"]}</strong> rides. '
        f'<em>{biggest["year"]}</em> was your biggest year '
        f'({biggest["miles"]} mi).'
    )
    if next_up:
        insight += f' Next up: <strong>{next_up}</strong>.'
    return insight


def generate() -> str:
    """Render the Ride Atlas page and return the HTML as a string.

    Also writes ``~/rides_report.html`` as a convenience so the page can
    be opened directly from a terminal when running locally. The Flask
    handler uses the return value; the file is only a side-effect.
    """
    rides = _load_rides()
    stats = _lifetime_stats(rides)
    breakdown = _yearly_breakdown(rides)

    template = TEMPLATE.read_text()

    now = local_now()
    date_label = now.strftime("%a, %b %d")

    mp = _monthly_pulse(rides)
    ym = _yearly_miles(rides, now.year)
    yoy = _year_over_year(rides, now.year)
    # Feed the YoY card a best-ride-per-year map so its bar rows can
    # show "Best: X" inline.
    yoy["best_by_year"] = {b["year"]: b["best"] for b in breakdown if b.get("best")}

    replacements = {
        "{{DATE_LABEL}}": date_label,
        "{{MONTHLY_PULSE_HTML}}": _monthly_pulse_html(mp),
        "{{YEARLY_WIDGET_HTML}}": _yearly_widget_html(ym),
        "{{YEAR_OVER_YEAR_HTML}}": _year_over_year_html(yoy),
        "{{TOTAL_MILES}}": stats["miles"],
        "{{TOTAL_ELEVATION_SHORT}}": stats["elevation_short"],
        "{{TOTAL_RIDES}}": stats["count"],
        "{{CROWN_HTML}}": _crown_html(rides),
        "{{CA_COVERAGE_HTML}}": _ca_coverage_html(rides),
        "{{REGIONS_HTML}}": _regions_html(rides),
        "{{YEARLY_ROWS_HTML}}": _yearly_rows_html(breakdown),
        "{{UPCOMING_RIDES_HTML}}": _upcoming_rides_html(),
        "{{INSIGHT_TEXT}}": _insight_text(rides, breakdown),
        "{{RIDES_JSON}}": json.dumps(rides, ensure_ascii=False),
    }

    page = template
    for key, val in replacements.items():
        page = page.replace(key, val)

    try:
        OUTPUT.write_text(page)
    except OSError:
        # Read-only FS on Render is fine — we only needed the return value.
        pass
    log.info("Ride Atlas rendered (%d rides)", len(rides))
    return page


if __name__ == "__main__":
    generate()
