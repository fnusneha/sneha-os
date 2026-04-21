"""
tz.py — User-local timezone helpers.

The user lives in Pacific time. Every "today" decision (what day is it
for sync, what month for season pass, which day's row to flip from a
star tap) must match their local wall clock — NOT the server's UTC.

Rule of thumb:
    - NEVER use date.today() / datetime.now() directly anywhere in app
      code. Use local_today() or local_now() from this module.
    - Exception: db.py's updated_at trigger uses server NOW() in
      Postgres. That's intentional — it's a debug timestamp, not
      user-visible.

APP_TIMEZONE is pinned to America/Los_Angeles. Override via env var
for testing:
    APP_TIMEZONE=UTC python sync.py --date 2026-04-21
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

_TZ_NAME = os.environ.get("APP_TIMEZONE", "America/Los_Angeles")
APP_TZ = ZoneInfo(_TZ_NAME)


def local_now() -> datetime:
    """Current wall-clock datetime in the user's timezone."""
    return datetime.now(APP_TZ)


def local_today() -> date:
    """Today's date in the user's timezone.

    Resolves the UTC-vs-local trap: in Actions running at 02:00 UTC
    on Tuesday, this correctly returns Monday because it's still
    evening in Pacific.
    """
    return local_now().date()


def local_yesterday() -> date:
    """Yesterday in the user's timezone."""
    from datetime import timedelta
    return local_today() - timedelta(days=1)


def tz_name() -> str:
    """The configured timezone name (useful for logging / headers)."""
    return _TZ_NAME
