"""
Flask web service — Quest Hub + Ride Atlas.

Routes:
    GET  /                 → redirect to /dashboard
    GET  /dashboard        → Quest Hub HTML (rendered live from DB)
    GET  /rides            → Ride Atlas HTML
    POST /api/collect      → {action: morning|night|core, date} — set daily star
    POST /api/manual       → {field: sauna, value: bool, date}  — toggle manual field
    POST /api/season       → {index, done}  — toggle season-pass item for current month
    GET  /api/season       → current month's done_indices
    GET  /api/health       → JSON: DB row counts + last-sync date
    GET  /healthz          → liveness probe (200 "ok")

Design:
    - Every render pulls fresh rows from Postgres. Rows are small
      (~10 per week) and Neon is fast, so there's no HTML cache.
    - Mutations return JSON so the browser can surface errors.
    - Responses set Cache-Control: no-store so mobile always sees
      fresh data on refresh.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing modules that need DATABASE_URL etc.
load_dotenv(Path(__file__).resolve().parent / ".env")

from flask import Flask, jsonify, redirect, request  # noqa: F401 (Flask used below)

# Rides render pulls from Neon because rides_report.py respects USE_DB_RIDES.
os.environ.setdefault("USE_DB_RIDES", "1")

from db import Db
from data_gather import gather_dashboard_data
from html_report import generate_html_report
from tz import local_today

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _db() -> Db:
    """Fresh short-lived Db instance per request."""
    return Db()


def _parse_date(raw: str | None, default: date | None = None) -> date:
    if not raw:
        if default is None:
            raise ValueError("Missing `date`")
        return default
    return date.fromisoformat(raw)


def _no_cache(resp):
    """Mark responses as uncacheable so mobile always gets fresh data."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ═══════════════════════════════════════════════════════════════════
# Pages
# ═══════════════════════════════════════════════════════════════════

@app.get("/")
def index():
    return redirect("/dashboard", code=302)


def _render_quest_hub(view: str) -> "tuple[str, int]":
    """Shared render path for the Today / Week / Month / Year tabs.

    All four views render from the same `morning_report.html` template;
    the `view` arg flips a body class so CSS hides the sections that
    don't belong. Month view needs a per-day star tally; Year view
    needs the California coverage map built from rides.
    """
    force = request.args.get("force") == "1"
    try:
        data = gather_dashboard_data(live_steps=True, force=force)
        month_by_date: dict = {}
        month_total = 0
        ca_html = ""
        if view == "month":
            month_by_date, month_total = _gather_monthly_stars(data)
            # Also load ride data for the Monthly Pulse card (April's
            # ride miles + medal progression) which now lives here.
            try:
                from rides_report import _load_rides, _monthly_pulse, _monthly_pulse_html
                rides = _load_rides()
                mp = _monthly_pulse(rides)
                data["_monthly_pulse_html"] = _monthly_pulse_html(mp)
            except Exception as exc:
                log.warning("month: monthly pulse render failed: %s", exc)
                data["_monthly_pulse_html"] = ""
        elif view == "year":
            # Year view surfaces rides data in several places:
            # California coverage map, "This Year" mileage widget,
            # Upcoming Rides, All-Time stats, and the Insight pull-quote.
            # Lazy-import rides_report so a failure there doesn't block
            # the other tabs.
            yearly_widget_html = ""
            upcoming_rides_html = ""
            total_miles = total_elevation = total_rides = "—"
            insight_text = ""
            try:
                from rides_report import (
                    _load_rides, _ca_coverage_html,
                    _yearly_miles, _yearly_widget_html,
                    _upcoming_rides_html,
                    _lifetime_stats, _yearly_breakdown, _insight_text,
                )
                rides = _load_rides()
                ca_html = _ca_coverage_html(rides)
                ym = _yearly_miles(rides, data["today"].year)
                yearly_widget_html = _yearly_widget_html(ym)
                upcoming_rides_html = _upcoming_rides_html()
                stats = _lifetime_stats(rides)
                total_miles     = stats["miles"]
                total_elevation = stats["elevation_short"]
                total_rides     = stats["count"]
                breakdown = _yearly_breakdown(rides)
                insight_text = _insight_text(rides, breakdown)
            except Exception as exc:
                log.warning("year: rides render failed: %s", exc)
            data["_yearly_widget_html"]  = yearly_widget_html
            data["_upcoming_rides_html"] = upcoming_rides_html
            data["_total_miles"]         = total_miles
            data["_total_elevation"]     = total_elevation
            data["_total_rides"]         = total_rides
            data["_insight_text"]        = insight_text
        html = generate_html_report(
            data,
            view=view,
            month_stars_by_date=month_by_date,
            month_stars_total=month_total,
            ca_coverage_html=ca_html,
        )
        return html, 200
    except Exception as exc:
        log.exception("%s render failed", view)
        return f"<h1>{view.title()} unavailable</h1><pre>{exc}</pre>", 500


def _gather_monthly_stars(data: dict) -> "tuple[dict, int]":
    """Compute per-day star counts for every day of the current month
    (up to today). Returns (dict[date→int], total_sum)."""
    import calendar as _cal
    from datetime import date as _date, timedelta as _td

    today = data["today"]
    y, m = today.year, today.month
    days_in_month = _cal.monthrange(y, m)[1]
    month_start = _date(y, m, 1)
    month_end = _date(y, m, days_in_month)

    rows = _db().get_entries_in_range(month_start, month_end)
    by_date = {r["date"]: r for r in rows}

    # Build a synthetic per-day "data" mini-dict so we can reuse the
    # existing _base/_burn/_recover/_compute_day_stars helpers. Those
    # helpers read by weekday index from *_row lists, so we construct
    # a one-off data where index 0 = this specific day.
    from html_report import (
        _base_earned, _burn_earned, _recover_earned,
    )
    stars_by_date: dict = {}
    total = 0
    for d in range(1, days_in_month + 1):
        dt = _date(y, m, d)
        if dt > today:
            break
        row = by_date.get(dt)
        if not row:
            continue
        # Build a 1-slot data dict keyed at index 0.
        from constants import SLEEP_STAR_THRESHOLD_DEFAULT as SLEEP_T, DAILY_STEPS_GOAL
        steps_ok   = (row.get("steps") or 0) >= DAILY_STEPS_GOAL
        sleep_hrs  = row.get("sleep_hours")
        sleep_ok   = bool(sleep_hrs and float(sleep_hrs) >= SLEEP_T)
        cal_ok     = bool((row.get("calories") or 0) > 0)
        base_ok    = steps_ok and sleep_ok and cal_ok
        burn_ok    = bool(row.get("strength_note") or row.get("cardio_note"))
        recover_ok = bool(row.get("stretch_note") or row.get("sauna"))
        morning_ok = bool(row.get("morning_star"))
        night_ok   = bool(row.get("night_star"))
        stars = sum(map(int, [morning_ok, base_ok, burn_ok, recover_ok, night_ok]))
        stars_by_date[dt] = stars
        total += stars
    return stars_by_date, total


@app.get("/dashboard")
def dashboard():
    html, status = _render_quest_hub("today")
    resp = app.response_class(html, content_type="text/html; charset=utf-8", status=status)
    return _no_cache(resp)


@app.get("/week")
def week():
    html, status = _render_quest_hub("week")
    resp = app.response_class(html, content_type="text/html; charset=utf-8", status=status)
    return _no_cache(resp)


@app.get("/month")
def month():
    html, status = _render_quest_hub("month")
    resp = app.response_class(html, content_type="text/html; charset=utf-8", status=status)
    return _no_cache(resp)


@app.get("/year")
def year():
    html, status = _render_quest_hub("year")
    resp = app.response_class(html, content_type="text/html; charset=utf-8", status=status)
    return _no_cache(resp)


@app.get("/rides")
def rides():
    """Re-render the Ride Atlas HTML from the DB on every hit.

    `rides_report.generate()` reads from Postgres, fills a string
    template, and returns the HTML — no disk round-trip and no Strava
    API call in the request path (a separate cron job refreshes the
    `rides` table).
    """
    try:
        # Import lazily so a broken rides_report.py never breaks /dashboard.
        from rides_report import generate
        html = generate()
        resp = app.response_class(html, content_type="text/html; charset=utf-8")
        return _no_cache(resp)
    except Exception as exc:
        log.exception("rides render failed")
        return _no_cache(app.response_class(
            f"<h1>Rides unavailable</h1><pre>{exc}</pre>",
            status=500, content_type="text/html",
        ))


# ═══════════════════════════════════════════════════════════════════
# Mutations
# ═══════════════════════════════════════════════════════════════════

@app.post("/api/collect")
def api_collect():
    """Set a morning/night star (or compute core star) for a date.

    Body: {"action": "morning" | "night" | "core", "date": "YYYY-MM-DD"}
    Core currently has no dedicated DB field — it's computed from the
    per-metric daily score. We accept the call for frontend compatibility
    and simply no-op it.
    """
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    try:
        d = _parse_date(body.get("date"), default=local_today())
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    db = _db()
    if action in ("morning", "night"):
        db.set_star(d, action, True)
    elif action == "core":
        # Core is computed from metrics; no persistence needed.
        pass
    else:
        return jsonify(ok=False, error=f"unknown action {action!r}"), 400

    return jsonify(ok=True, action=action, date=d.isoformat())


@app.post("/api/manual")
def api_manual():
    """Generic manual-field toggle. Today: sauna. Future: anything else
    we decide to track via mobile.

    Body: {"field": "sauna", "value": true, "date": "YYYY-MM-DD"}
    """
    body = request.get_json(silent=True) or {}
    field = body.get("field")
    value = body.get("value")
    try:
        d = _parse_date(body.get("date"), default=local_today())
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    db = _db()
    if field == "sauna":
        if not isinstance(value, bool):
            return jsonify(ok=False, error="value must be bool"), 400
        db.set_sauna(d, value)
    else:
        return jsonify(ok=False, error=f"unknown field {field!r}"), 400

    return jsonify(ok=True, field=field, value=value, date=d.isoformat())


@app.post("/api/season")
def api_season_toggle():
    """Toggle a season-pass item for the current month.

    Body: {"index": 0, "done": true}
    """
    body = request.get_json(silent=True) or {}
    idx = body.get("index")
    done = body.get("done")
    if not isinstance(idx, int) or not isinstance(done, bool):
        return jsonify(ok=False, error="index (int) and done (bool) required"), 400

    month = local_today().strftime("%Y-%m")
    db = _db()
    updated = db.toggle_season_item(month, idx, done)
    return jsonify(ok=True, month=month, indices=updated)


@app.get("/api/season")
def api_season_get():
    """Return the current month's done_indices (used on page load)."""
    month = local_today().strftime("%Y-%m")
    return jsonify(ok=True, month=month, indices=_db().get_season_pass(month))


@app.post("/api/refresh-travel")
def api_refresh_travel():
    """Force-refresh the travel + library caches from Google Sheets.

    `travel_source` normally caches sheet reads for 6 hours to keep
    dashboard loads snappy — but when Sneha edits the Master Planner
    or Library sheet directly, the new rows aren't visible until the
    cache expires (up to 6 h). This endpoint bypasses the cache and
    writes a fresh pull to disk, so the next `/dashboard` render
    picks up the new rows immediately.

    Intentionally POST-only so browser pre-fetch / crawlers can't
    trigger a Google Sheets round-trip.
    """
    try:
        from google_auth import get_google_creds
        from travel_source import fetch_travel_pins, fetch_library_cycling
        creds = get_google_creds()
        pins = fetch_travel_pins(creds, force_refresh=True)
        wish = fetch_library_cycling(creds, force_refresh=True)
        return jsonify(
            ok=True,
            travel_pins=len(pins),
            library_wishlist=len(wish),
        )
    except Exception as exc:
        log.exception("travel refresh failed")
        return jsonify(ok=False, error=str(exc)), 500


@app.get("/api/today")
def api_today():
    """Compact JSON snapshot of today's numbers. Used by the Android
    home-screen widget.

    Reads only from the DB (+ live Oura step count) — skips the Google
    Sheets/Docs reads that `/dashboard` uses for travel pins + habits,
    so this endpoint stays fast (~200 ms) and never blocks on an
    expired Google token.

    Shape:
        {
          "date": "2026-04-20",
          "weekday": "Monday",
          "steps": 4505,
          "steps_goal": 8000,
          "steps_left": 3495,
          "sleep_hours": 6.9,
          "calories": 1203,
          "calorie_goal": 1520,
          "cycle_phase": "Luteal-EM",
          "cycle_day": 19,
          "morning_star": true,
          "night_star": false,
          "sauna": true,
          "base_earned": true,      # steps AND sleep AND calories
          "base_done":   3,          # 0–3 count toward Base sub-star
          "burn_earned": false,     # strength OR cardio
          "burn_done":   0,
          "recover_earned": true,   # stretch OR sauna
          "recover_done":   1,
          "core_done": 2,            # number of earned sub-stars (0–3)
          "core_threshold": 3,
          "stars_today": 3,          # 0–5 max: morning + base + burn + recover + night
          "stars_week": 12,          # 0–35 max
          "max_daily_stars": 5,
          "max_weekly_stars": 35,
          "last_sync": "2026-04-21"
        }
    """
    from constants import (
        DAILY_STEPS_GOAL, MAX_DAILY_STARS, MAX_WEEKLY_STARS,
        SLEEP_STAR_THRESHOLD_DEFAULT as SLEEP_STAR_THRESHOLD,
    )
    from datetime import timedelta
    try:
        db = _db()
        today = local_today()
        weekday = today.weekday()
        monday = today - timedelta(days=weekday)
        sunday = monday + timedelta(days=6)

        week_rows = db.get_entries_in_range(monday, sunday)
        by_date = {r["date"]: r for r in week_rows}
        week = [by_date.get(monday + timedelta(days=i)) for i in range(7)]
        today_row = week[weekday]

        # Live steps + calories (same cached helpers as the dashboard)
        # so consumers see fresh Oura + Garmin numbers instead of the
        # last sync-snapshot. `force=1` bypasses the 60s in-memory
        # cache for instant refresh.
        from data_gather import _cached_fetch_steps, _cached_fetch_nutrition
        force = request.args.get("force") == "1"
        steps_db = (today_row or {}).get("steps") or 0
        steps_live = _cached_fetch_steps(today.isoformat(), force=force)
        steps = max(steps_db, steps_live or 0) or 0

        live_nutrition = _cached_fetch_nutrition(today, force=force)
        cal_db  = (today_row or {}).get("calories") or 0
        cal_live = (live_nutrition or {}).get("calories") or 0
        calories = max(cal_db, cal_live)
        cal_goal = (live_nutrition or {}).get("goal") or (today_row or {}).get("calorie_goal") or 0

        sleep = (today_row or {}).get("sleep_hours")
        morning_done = bool((today_row or {}).get("morning_star"))
        night_done   = bool((today_row or {}).get("night_star"))
        sauna_done   = bool((today_row or {}).get("sauna"))
        cycle_phase  = (today_row or {}).get("cycle_phase") or ""
        cycle_day    = (today_row or {}).get("cycle_day")

        # ── Today's Core 3 sub-stars ───────────────────────────
        steps_ok   = steps >= DAILY_STEPS_GOAL
        sleep_ok   = bool(sleep and float(sleep) >= SLEEP_STAR_THRESHOLD)
        cal_ok     = bool(calories and calories > 0)
        base_ok    = steps_ok and sleep_ok and cal_ok
        burn_ok    = bool((today_row or {}).get("strength_note") or (today_row or {}).get("cardio_note"))
        recover_ok = bool((today_row or {}).get("stretch_note") or sauna_done)
        core_done  = int(base_ok) + int(burn_ok) + int(recover_ok)
        stars_today = (
            int(morning_done) + int(base_ok) + int(burn_ok)
            + int(recover_ok) + int(night_done)
        )

        # ── Weekly stars across Mon..today ─────────────────────
        stars_week = 0
        for row in week[: weekday + 1]:
            if not row:
                continue
            s  = row.get("steps") or 0
            sl = row.get("sleep_hours")
            cal_i = row.get("calories") or 0
            d_base    = (s >= DAILY_STEPS_GOAL and sl is not None
                         and float(sl) >= SLEEP_STAR_THRESHOLD and cal_i > 0)
            d_burn    = bool(row.get("strength_note") or row.get("cardio_note"))
            d_recover = bool(row.get("stretch_note") or row.get("sauna"))
            stars_week += (
                int(bool(row.get("morning_star")))
                + int(d_base) + int(d_burn) + int(d_recover)
                + int(bool(row.get("night_star")))
            )

        return jsonify({
            "ok": True,
            "date": today.isoformat(),
            "weekday": today.strftime("%A"),
            "steps": steps,
            "steps_goal": DAILY_STEPS_GOAL,
            "steps_left": max(0, DAILY_STEPS_GOAL - steps),
            "sleep_hours": float(sleep) if sleep is not None else None,
            "calories": calories,
            "calorie_goal": cal_goal,
            "cycle_phase": cycle_phase,
            "cycle_day": cycle_day,
            "morning_star": morning_done,
            "night_star": night_done,
            "sauna": sauna_done,
            # New star structure
            "base_earned": base_ok,
            "burn_earned": burn_ok,
            "recover_earned": recover_ok,
            "core_done": core_done,              # 0–3 sub-stars earned
            "core_threshold": 3,                 # all 3 for full Core
            "stars_today": stars_today,          # 0–5
            "stars_week":  stars_week,           # 0–35
            "max_daily_stars":  MAX_DAILY_STARS,
            "max_weekly_stars": MAX_WEEKLY_STARS,
            "last_sync": db.get_state("last_sync_date"),
        })
    except Exception as exc:
        log.exception("api_today failed")
        return jsonify(ok=False, error=str(exc)), 500


# ═══════════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════════

@app.get("/healthz")
def healthz():
    """Cheap liveness — used by Render's health check."""
    return "ok", 200


@app.get("/api/health")
def api_health():
    """Detailed health: row counts + last-sync timestamp."""
    db = _db()
    h = db.health()
    h["last_sync_date"] = db.get_state("last_sync_date")
    return jsonify(h)


# ═══════════════════════════════════════════════════════════════════
# Entrypoint
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
