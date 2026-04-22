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


@app.get("/dashboard")
def dashboard():
    try:
        data = gather_dashboard_data(live_steps=True)
        html = generate_html_report(data)
    except Exception as exc:
        log.exception("dashboard render failed")
        resp = app.response_class(
            f"<h1>Dashboard unavailable</h1><pre>{exc}</pre>",
            status=500,
            content_type="text/html",
        )
        return _no_cache(resp)
    resp = app.response_class(html, content_type="text/html; charset=utf-8")
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
          "core_done": 2,
          "core_threshold": 4,
          "stars_today": 1,
          "stars_week": 3,
          "last_sync": "2026-04-21"
        }
    """
    from constants import CORE_STAR_THRESHOLD, DAILY_STEPS_GOAL
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

        # Live steps (same cached helper as the dashboard)
        from data_gather import _cached_fetch_steps
        steps_db = (today_row or {}).get("steps") or 0
        steps_live = _cached_fetch_steps(today.isoformat())
        steps = max(steps_db, steps_live or 0) or 0

        sleep = (today_row or {}).get("sleep_hours")
        calories = (today_row or {}).get("calories")
        cal_goal = (today_row or {}).get("calorie_goal") or 0
        morning_done = bool((today_row or {}).get("morning_star"))
        night_done   = bool((today_row or {}).get("night_star"))
        sauna_done   = bool((today_row or {}).get("sauna"))
        cycle_phase  = (today_row or {}).get("cycle_phase") or ""
        cycle_day    = (today_row or {}).get("cycle_day")

        # Core items count for today (steps/sleep/cal + strength/cardio/stretch/sauna)
        STEPS_GOAL = DAILY_STEPS_GOAL
        core_done = 0
        if steps >= STEPS_GOAL: core_done += 1
        if sleep and float(sleep) >= 7.0: core_done += 1
        if calories and calories > 0: core_done += 1
        if (today_row or {}).get("strength_note"): core_done += 1
        if (today_row or {}).get("cardio_note"):   core_done += 1
        if (today_row or {}).get("stretch_note"):  core_done += 1
        if sauna_done: core_done += 1

        core_earned = core_done >= CORE_STAR_THRESHOLD
        stars_today = int(morning_done) + int(core_earned) + int(night_done)

        # Weekly stars: same logic across each weekday row in the week
        stars_week = 0
        for i, row in enumerate(week[: weekday + 1]):
            if not row: continue
            stars_week += int(bool(row.get("morning_star")))
            stars_week += int(bool(row.get("night_star")))
            # core star for that day
            s = row.get("steps") or 0
            sl = row.get("sleep_hours")
            cal_i = row.get("calories") or 0
            c = 0
            if s >= STEPS_GOAL: c += 1
            if sl and float(sl) >= 7.0: c += 1
            if cal_i > 0: c += 1
            for k in ("strength_note","cardio_note","stretch_note"):
                if row.get(k): c += 1
            if row.get("sauna"): c += 1
            if c >= CORE_STAR_THRESHOLD:
                stars_week += 1

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
            "core_done": core_done,
            "core_threshold": CORE_STAR_THRESHOLD,
            "stars_today": stars_today,
            "stars_week": stars_week,
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
