"""
Travel Master Planner Google Sheet → structured travel pin data.

Fetches travel data from the Master Planner sheet for the current year
and the year ahead (so 2027 roadmap is visible while we're in 2026),
determines pinned/upcoming status, and caches to disk.

Cache lives at ``cache/travel_pins.json`` and refreshes after 6 hours.
"""

import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path

from googleapiclient.discovery import build

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"
CACHE_FILE = CACHE_DIR / "travel_pins.json"
CACHE_MAX_AGE_SECS = 6 * 60 * 60  # 6 hours

TRAVEL_SHEET_ID = os.getenv(
    "TRAVEL_SHEET_ID", "1vnONhVzzDh_hBN0_Rqs4vaV0AAWdijWxsRVDr2p8E4s"
)
TRAVEL_RANGE = "Master Planner!A1:J50"
LIBRARY_RANGE = "Library!A1:P250"
LIBRARY_CACHE_FILE = None  # set below after CACHE_DIR is defined

# Month number → abbreviated name
_MONTH_ABBR = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


# ═══════════════════════════════════════════════════════════════════
# Smart icons based on trip name keywords
# ═══════════════════════════════════════════════════════════════════

def _travel_icon(name: str, destination: str) -> str:
    """Pick an emoji icon based on trip name or destination keywords.

    Args:
        name: Trip name from the sheet.
        destination: Destination string from the sheet.

    Returns:
        Single emoji string.
    """
    combined = (name + " " + destination).lower()
    if any(w in combined for w in ["bike", "century", "fondo", "cycling", "otter"]):
        return "\U0001f6b4"    # 🚴
    if any(w in combined for w in ["christmas", "new year"]):
        return "\U0001f384"    # 🎄
    if any(w in combined for w in ["thanksgiving"]):
        return "\U0001f983"    # 🦃
    if any(w in combined for w in ["halloween"]):
        return "\U0001f383"    # 🎃
    if any(w in combined for w in ["independence", "july"]):
        return "\U0001f386"    # 🎆
    if any(w in combined for w in ["sierra", "mountain", "canyon", "rocky",
                                    "badlands", "yosemite", "sedona", "glacier"]):
        return "\U0001f3d4\ufe0f"  # 🏔️
    if any(w in combined for w in ["death valley", "desert", "anza"]):
        return "\U0001f3dc\ufe0f"  # 🏜️
    if any(w in combined for w in ["lake", "june lake"]):
        return "\U0001f3de\ufe0f"  # 🏞️
    if any(w in combined for w in ["point reyes", "marin", "coast"]):
        return "\U0001f30a"    # 🌊
    return "\u2708\ufe0f"      # ✈️


# ═══════════════════════════════════════════════════════════════════
# Sheet fetching
# ═══════════════════════════════════════════════════════════════════

_DATE_FORMATS = ["%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"]


def _parse_date(s: str) -> date:
    """Parse a date string, trying multiple formats."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r}")


def _parse_sheet_rows(rows: list[list], allowed_years: set[int] | None = None) -> list[dict]:
    """Parse raw sheet rows into travel pin dicts.

    Args:
        rows: Raw rows from Google Sheets API (first row is header).
        allowed_years: Years to include (default: current year + next year).
            Status filter is applied per-year:
              - Current year: "Completed" or "Booked" only
              - Future years: "Completed", "Booked", or "Potential"
                (so 2027 planning shows up as a roadmap section).

    Returns:
        List of travel pin dicts with name, destination, dates, status,
        month, year, icon, and pinned flag.
    """
    if not rows or len(rows) < 2:
        return []

    today = date.today()
    if allowed_years is None:
        allowed_years = {today.year, today.year + 1}

    pins = []

    for row in rows[1:]:  # skip header
        # Pad row to 10 columns
        while len(row) < 10:
            row.append("")

        year_str = row[0].strip()
        start_str = row[1].strip()
        end_str = row[2].strip()
        name = row[3].strip()
        destination = row[4].strip()
        days_str = row[5].strip()
        status = row[7].strip()

        try:
            year_int = int(year_str)
        except ValueError:
            continue

        if year_int not in allowed_years:
            continue

        # Status filter: for the current year we only pin Completed/Booked
        # (Potential = not yet decided, not worth cluttering). For future
        # years, include Potential too so the roadmap is visible.
        if year_int == today.year:
            if status not in ("Completed", "Booked"):
                continue
        else:
            if status not in ("Completed", "Booked", "Potential"):
                continue

        if not name:
            continue

        # Parse dates (support both "2026-01-19" and "January 19, 2026")
        try:
            start_date = _parse_date(start_str)
        except (ValueError, TypeError):
            continue
        try:
            end_date = _parse_date(end_str) if end_str else start_date
        except (ValueError, TypeError):
            end_date = start_date

        # Parse days
        try:
            days = int(days_str) if days_str else (end_date - start_date).days + 1
        except ValueError:
            days = 1

        # Determine month from start date
        month = _MONTH_ABBR.get(start_date.month, "Jan")

        # Determine pinned status
        # Pinned if: completed, OR booked and end_date has passed
        pinned = (status == "Completed") or (status == "Booked" and end_date < today)

        # Build label: trip name + destination if useful
        label = name
        if destination and destination != "?" and destination != "Home":
            # Only append destination if it adds info not in the name
            if destination.lower().split(",")[0].split("(")[0].strip() not in name.lower():
                label = f"{name} · {destination.split(',')[0].split('(')[0].strip()}"

        icon = _travel_icon(name, destination)

        pins.append({
            "name": label,
            "destination": destination,
            "start_date": start_str,
            "end_date": end_str,
            "days": days,
            "status": status,
            "month": month,
            "year": year_int,
            "icon": icon,
            "pinned": pinned,
        })

    return pins


# ═══════════════════════════════════════════════════════════════════
# Cache
# ═══════════════════════════════════════════════════════════════════

def _read_cache() -> list | None:
    """Read cached travel pins if fresh enough (<6h) and same month.

    Auto-busts the cache when the month rolls over.

    Returns:
        List of travel pin dicts, or None if cache is stale/missing.
    """
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        ts = data.get("_timestamp", 0)
        # Auto-bust on month change
        cached_month = data.get("_month", "")
        current_month = datetime.now().strftime("%Y-%m")
        if cached_month and cached_month != current_month:
            log.info("Travel cache from %s, now %s — busting", cached_month, current_month)
            return None
        if time.time() - ts < CACHE_MAX_AGE_SECS:
            log.info("Using cached travel pins (age: %.1fh)", (time.time() - ts) / 3600)
            return data.get("pins", [])
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _read_stale_cache() -> list:
    """Read cache regardless of age (fallback when fetch fails).

    Returns:
        List of travel pin dicts, or empty list.
    """
    if not CACHE_FILE.exists():
        return []
    try:
        data = json.loads(CACHE_FILE.read_text())
        return data.get("pins", [])
    except (json.JSONDecodeError, KeyError):
        return []


def _write_cache(pins: list) -> None:
    """Write travel pins to cache with timestamp and current month."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "_timestamp": time.time(),
        "_month": datetime.now().strftime("%Y-%m"),
        "pins": pins,
    }
    CACHE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    log.info("Travel pins cache written (%d trips)", len(pins))


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════

def clear_cache() -> None:
    """Delete the travel pins cache file, forcing a fresh fetch on next call."""
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
        log.info("Travel pins cache cleared")


def fetch_travel_pins(creds, force_refresh: bool = False) -> list[dict]:
    """Fetch travel data from the Master Planner sheet.

    Pulls current year (Completed + Booked) and next year (Completed +
    Booked + Potential, so roadmap is visible). Uses a 6-hour disk cache.
    Falls back to stale cache or empty list on fetch failure.

    Args:
        creds: Google OAuth2 credentials.

    Returns:
        List of travel pin dicts, each with::

            {
                "name": "Sea Otter Classic · Monterey",
                "destination": "Monterey",
                "start_date": "2026-04-17",
                "end_date": "2026-04-18",
                "days": 2,
                "status": "Booked",
                "month": "Apr",
                "year": 2026,
                "icon": "🚴",
                "pinned": False,
            }
    """
    if not force_refresh:
        cached = _read_cache()
        if cached is not None:
            return cached

    try:
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = service.spreadsheets().values().get(
            spreadsheetId=TRAVEL_SHEET_ID,
            range=TRAVEL_RANGE,
        ).execute()
        rows = result.get("values", [])

        pins = _parse_sheet_rows(rows)
        _write_cache(pins)
        log.info("Fetched %d travel pins from sheet", len(pins))
        return pins

    except Exception as exc:
        log.warning("Failed to fetch travel sheet: %s — using cache", exc)
        return _read_stale_cache()


# ═══════════════════════════════════════════════════════════════════
# Library: cycling wishlist
# ═══════════════════════════════════════════════════════════════════

LIBRARY_CACHE = CACHE_DIR / "library_cycling.json"


def fetch_library_cycling(creds, force_refresh: bool = False) -> list[dict]:
    """Fetch cycling entries from the Library tab (wishlist rides).

    Filters to California entries tagged with 'Biking' that are not yet booked.
    Uses a 6-hour cache.

    Returns:
        List of dicts with: name, location, notes, tags, best_months.
    """
    # Cache check
    if not force_refresh and LIBRARY_CACHE.exists():
        try:
            data = json.loads(LIBRARY_CACHE.read_text())
            if time.time() - data.get("_timestamp", 0) < CACHE_MAX_AGE_SECS:
                return data.get("items", [])
        except (json.JSONDecodeError, KeyError):
            pass

    try:
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = service.spreadsheets().values().get(
            spreadsheetId=TRAVEL_SHEET_ID,
            range=LIBRARY_RANGE,
        ).execute()
        rows = result.get("values", [])

        if len(rows) < 2:
            return []

        items = []
        for row in rows[1:]:
            while len(row) < 16:
                row.append("")
            name = row[0].strip()
            country = row[1].strip()
            state = row[2].strip()
            status = row[4].strip()
            tags = row[5].strip()
            best_months = row[6].strip()
            notes = row[9].strip()

            if not name:
                continue
            # Only California biking entries that are Want to Go
            is_ca = state.lower() in ("california", "ca") or "california" in name.lower()
            is_biking = "biking" in tags.lower() or "cycling" in tags.lower()
            if not (is_ca and is_biking):
                continue
            if status and status.lower() == "completed":
                continue

            items.append({
                "name": name,
                "state": state,
                "tags": tags,
                "best_months": best_months,
                "notes": notes,
                "status": status,
            })

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        LIBRARY_CACHE.write_text(json.dumps({
            "_timestamp": time.time(),
            "items": items,
        }, indent=2, ensure_ascii=False))
        log.info("Fetched %d library cycling items", len(items))
        return items

    except Exception as exc:
        log.warning("Failed to fetch library: %s", exc)
        if LIBRARY_CACHE.exists():
            try:
                return json.loads(LIBRARY_CACHE.read_text()).get("items", [])
            except Exception:
                pass
        return []
