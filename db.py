"""
Postgres access layer.

Typed read/write methods for the `daily_entries`, `rides`, `season_pass`
and `sync_state` tables. Every other module talks to the database through
this wrapper — no raw SQL leaks elsewhere.

    from db import Db

    db = Db()  # reads DATABASE_URL from env
    entry = db.get_entry(date(2026, 4, 20))
    db.upsert_entry(date(2026, 4, 20), steps=8200, sleep_hours=7.3)
    db.set_star(date.today(), "morning", True)

The app opens short-lived connections per call via PgBouncer (Neon's
pooled endpoint). One user + low QPS means an in-process pool isn't
worth the complexity; swap in psycopg_pool.ConnectionPool if that ever
changes.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Connection
# ═══════════════════════════════════════════════════════════════════

class Db:
    """Thin wrapper around a Neon Postgres connection.

    Not a pool — each public method opens a short-lived connection and
    closes it. That's fine for this workload (single user, low QPS) and
    makes local testing trivial. If the dashboard ever gets hot, swap
    `_connect` to pull from a psycopg_pool.
    """

    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("DATABASE_URL")
        if not self.url:
            raise RuntimeError(
                "DATABASE_URL is not set. Put it in .env (local) or as an "
                "env var (Render/GitHub Actions)."
            )

    @contextmanager
    def _connect(self, *, autocommit: bool = True):
        """Yield a connection with dict-row factory. Short-lived."""
        with psycopg.connect(self.url, autocommit=autocommit, row_factory=dict_row) as conn:
            yield conn

    # ───────────────────────────────────────────────────────────────
    # daily_entries
    # ───────────────────────────────────────────────────────────────

    def get_entry(self, d: date) -> dict | None:
        """Return the row for date `d`, or None if no row exists."""
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM daily_entries WHERE date = %s", (d,))
            return cur.fetchone()

    def get_entries_in_range(self, start: date, end: date) -> list[dict]:
        """All rows between `start` and `end` inclusive, oldest first."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM daily_entries WHERE date BETWEEN %s AND %s ORDER BY date",
                (start, end),
            )
            return cur.fetchall()

    def get_week_entries(self, any_date_in_week: date) -> list[dict]:
        """Get all 7 entries for the week containing `any_date_in_week`.

        Always returns 7 items; missing days become None in the list so
        the caller can index by weekday (0=Mon..6=Sun).
        """
        monday = any_date_in_week - timedelta(days=any_date_in_week.weekday())
        sunday = monday + timedelta(days=6)
        rows = self.get_entries_in_range(monday, sunday)
        by_date = {r["date"]: r for r in rows}
        week: list[dict | None] = []
        for i in range(7):
            week.append(by_date.get(monday + timedelta(days=i)))
        return week

    def upsert_entry(self, d: date, **fields: Any) -> None:
        """Insert or update fields for date `d`.

        Only columns you explicitly name are touched — existing values
        for other columns are preserved. This enforces a "never overwrite
        existing data with nothing" rule so a failed API fetch can't wipe
        a previously-good value.

        Example:
            db.upsert_entry(date(2026, 4, 20), steps=8200, sleep_hours=7.3)
        """
        if not fields:
            return
        allowed = _ENTRY_COLUMNS - {"date"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown daily_entries columns: {unknown}")

        # Serialize JSONB fields automatically
        for k in ("morning_checks", "night_checks"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])

        cols = ["date"] + list(fields.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in fields)
        sql = f"""
            INSERT INTO daily_entries ({', '.join(cols)})
            VALUES ({placeholders})
            ON CONFLICT (date) DO UPDATE SET {updates}
        """
        with self._connect() as conn:
            conn.execute(sql, [d] + list(fields.values()))
        log.info("upsert_entry %s: %s", d, list(fields.keys()))

    def set_star(self, d: date, which: str, value: bool) -> None:
        """Set morning_star or night_star for date `d`."""
        col = {"morning": "morning_star", "night": "night_star"}.get(which)
        if not col:
            raise ValueError(f"which must be 'morning' or 'night', got {which!r}")
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO daily_entries (date, {col}) VALUES (%s, %s) "
                f"ON CONFLICT (date) DO UPDATE SET {col} = EXCLUDED.{col}",
                (d, value),
            )

    def set_sauna(self, d: date, value: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO daily_entries (date, sauna) VALUES (%s, %s) "
                "ON CONFLICT (date) DO UPDATE SET sauna = EXCLUDED.sauna",
                (d, value),
            )

    def set_stretch(self, d: date, value: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO daily_entries (date, stretch_logged) VALUES (%s, %s) "
                "ON CONFLICT (date) DO UPDATE SET stretch_logged = EXCLUDED.stretch_logged",
                (d, value),
            )

    # ───────────────────────────────────────────────────────────────
    # season_pass
    # ───────────────────────────────────────────────────────────────

    def get_season_pass(self, month: str) -> list[int]:
        """Return the list of done season-pass indices for a month like '2026-04'."""
        with self._connect() as conn:
            cur = conn.execute("SELECT done_indices FROM season_pass WHERE month = %s", (month,))
            row = cur.fetchone()
            return list(row["done_indices"]) if row else []

    def set_season_pass(self, month: str, indices: Iterable[int]) -> None:
        sorted_unique = sorted(set(int(i) for i in indices))
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO season_pass (month, done_indices) VALUES (%s, %s) "
                "ON CONFLICT (month) DO UPDATE SET done_indices = EXCLUDED.done_indices",
                (month, sorted_unique),
            )

    def toggle_season_item(self, month: str, index: int, done: bool) -> list[int]:
        """Toggle a single season-pass item and return the updated list."""
        current = set(self.get_season_pass(month))
        if done:
            current.add(index)
        else:
            current.discard(index)
        self.set_season_pass(month, current)
        return sorted(current)

    # ───────────────────────────────────────────────────────────────
    # rides
    # ───────────────────────────────────────────────────────────────

    def list_rides(self) -> list[dict]:
        """All rides, newest first."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT strava_id, date, year, distance_mi, elevation_ft, payload "
                "FROM rides ORDER BY date DESC"
            )
            return cur.fetchall()

    def list_rides_in_year(self, year: int) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT strava_id, date, year, distance_mi, elevation_ft, payload "
                "FROM rides WHERE year = %s ORDER BY date DESC",
                (year,),
            )
            return cur.fetchall()

    def upsert_ride(self, strava_id: int, d: date, distance_mi: float,
                    elevation_ft: int, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rides (strava_id, date, year, distance_mi, elevation_ft, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (strava_id) DO UPDATE SET "
                "  date = EXCLUDED.date, year = EXCLUDED.year, "
                "  distance_mi = EXCLUDED.distance_mi, "
                "  elevation_ft = EXCLUDED.elevation_ft, "
                "  payload = EXCLUDED.payload",
                (strava_id, d, d.year, distance_mi, elevation_ft, json.dumps(payload)),
            )

    def upsert_rides_bulk(self, rides: list[dict]) -> int:
        """Bulk upsert a list of ride payloads. Each item must have:
        strava_id, date (ISO string or date), distance_mi, elevation_ft, payload.
        Returns count inserted/updated.
        """
        if not rides:
            return 0
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO rides (strava_id, date, year, distance_mi, elevation_ft, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (strava_id) DO UPDATE SET "
                    "  date = EXCLUDED.date, year = EXCLUDED.year, "
                    "  distance_mi = EXCLUDED.distance_mi, "
                    "  elevation_ft = EXCLUDED.elevation_ft, "
                    "  payload = EXCLUDED.payload",
                    [
                        (
                            r["strava_id"],
                            r["date"] if isinstance(r["date"], date) else date.fromisoformat(r["date"]),
                            (r["date"].year if isinstance(r["date"], date)
                             else int(r["date"][:4])),
                            r["distance_mi"],
                            r["elevation_ft"],
                            json.dumps(r["payload"]),
                        )
                        for r in rides
                    ],
                )
        return len(rides)

    # ───────────────────────────────────────────────────────────────
    # sync_state (generic KV)
    # ───────────────────────────────────────────────────────────────

    def get_state(self, key: str) -> str | None:
        with self._connect() as conn:
            cur = conn.execute("SELECT value FROM sync_state WHERE key = %s", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )

    # ───────────────────────────────────────────────────────────────
    # Diagnostics
    # ───────────────────────────────────────────────────────────────

    def health(self) -> dict:
        """Quick health probe: row counts + server time."""
        with self._connect() as conn:
            out = {}
            for t in ("daily_entries", "rides", "season_pass", "sync_state"):
                cur = conn.execute(f"SELECT COUNT(*) AS n FROM {t}")
                out[t] = cur.fetchone()["n"]
            cur = conn.execute("SELECT NOW() AS now")
            out["server_time"] = cur.fetchone()["now"].isoformat()
            return out


# ═══════════════════════════════════════════════════════════════════
# Column allow-list (defense-in-depth against typos in upsert_entry)
# ═══════════════════════════════════════════════════════════════════

_ENTRY_COLUMNS = {
    "date",
    "sleep_hours", "steps",
    "calories", "calorie_goal",
    "strength_note", "cardio_note", "stretch_note",
    "cycle_phase", "cycle_day",
    "notes",
    "sauna", "stretch_logged", "morning_star", "night_star",
    "morning_checks", "night_checks",
}


# ═══════════════════════════════════════════════════════════════════
# CLI for local testing
#   python db.py health
#   python db.py get 2026-04-20
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")

    db = Db()
    if len(sys.argv) < 2 or sys.argv[1] == "health":
        print(json.dumps(db.health(), indent=2, default=str))
    elif sys.argv[1] == "get" and len(sys.argv) >= 3:
        d = date.fromisoformat(sys.argv[2])
        row = db.get_entry(d)
        print(json.dumps(row, indent=2, default=str))
    else:
        print("Usage: python db.py [health | get YYYY-MM-DD]")
        sys.exit(1)
