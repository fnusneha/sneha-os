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
from datetime import date, datetime
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

    Render takes ~200 ms — it's just an in-memory template fill against
    rows already in Postgres. No Strava API call in the request path;
    a separate cron job refreshes the `rides` table twice a day.
    """
    try:
        # Import lazily so a broken rides_report.py never breaks /dashboard.
        from rides_report import generate
        generate()  # writes ~/rides_report.html
        html_path = Path(os.path.expanduser("~/rides_report.html"))
        if not html_path.exists():
            return _no_cache(app.response_class("rides not rendered", status=500))
        resp = app.response_class(html_path.read_bytes(), content_type="text/html; charset=utf-8")
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
