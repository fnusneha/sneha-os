"""
Google Doc → structured habit data.

Fetches the "HABIT TRACKER Index | 2026" Google Doc, parses it into
habits grouped by cadence (daily, monthly, quarterly, annual), and
caches the result to disk so the dashboard doesn't hit the API on
every render.

Cache lives at ``cache/habits_doc.json`` and refreshes after 24 hours.
If the fetch fails, the stale cache is returned. If no cache exists,
an empty dict is returned (callers fall back to hardcoded lists).
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

from googleapiclient.discovery import build

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"
CACHE_FILE = CACHE_DIR / "habits_doc.json"
CACHE_MAX_AGE_SECS = 24 * 60 * 60  # 24 hours

HABIT_DOC_ID = os.getenv(
    "HABIT_DOC_ID", "1jQav9ZmBdv2_7kkfMvJZDmn08EaMJ7ejgWOOnsnK9do"
)


# ═══════════════════════════════════════════════════════════════════
# Parsing
# ═══════════════════════════════════════════════════════════════════

# Emoji regex: match leading emoji cluster (1-4 codepoints possibly with VS16)
_EMOJI_RE = re.compile(
    r'^([\U0001F300-\U0001FAD6\u2600-\u27BF\u2702-\u27B0\U0001F900-\U0001F9FF]'
    r'[\uFE0F\u200D]*'
    r'[\U0001F300-\U0001FAD6\u2600-\u27BF\u2702-\u27B0\U0001F900-\U0001F9FF\uFE0F\u200D]*)'
    r'\s*'
)

# Cadence from parentheses: "(every 3 weeks)", "(every 2 weeks)", etc.
_CADENCE_RE = re.compile(r'\(every\s+\d+\s+\w+\)')

# Link extraction: [here](URL) or [*here*](URL)
_LINK_RE = re.compile(r'\[\*?here\*?\]\((https?://[^)]+)\)')


def _parse_habit_line(line: str) -> dict | None:
    """Parse a single habit line from the doc.

    Expected formats:
        ``* **☀️ Habit: Daily Morning Routine [here](URL)**``
        ``* **💆 Habit: Monthly Deep Tissue Massage (every 3 weeks)**``

    Args:
        line: Raw text line from the doc.

    Returns:
        Dict with keys icon, name, cadence, link — or None if unparseable.
    """
    # Must be a list item (starts with *)
    s = line.strip()
    if not s.startswith("* "):
        return None
    s = s[2:]
    s = s.strip("*").strip()

    # Skip section headers that sneak in as list items
    if "HABIT TRACKER" in s or ("DAILY" in s and "WEEKLY" in s) or "MONTHLY" in s.upper().split("HABIT")[0]:
        if "Habit:" not in s:
            return None

    if not s:
        return None

    # Extract month schedule FIRST from trailing "- 17th Feb" or "- March and Sep"
    # Must happen before "here" and "and" stripping which would eat the month text
    months = []
    # Match the LAST "- <month info>" in the line (after all links/text)
    month_suffix = re.search(r'-\s*((?:\d{1,2}(?:st|nd|rd|th)\s+)?[A-Z][a-z]+(?:\s+and\s+[A-Z][a-z]+)*)\s*$', s)
    if month_suffix:
        month_text = month_suffix.group(1).strip()
        s = s[:month_suffix.start()].strip()
        _MONTH_NAMES = {
            "jan": "Jan", "january": "Jan", "feb": "Feb", "february": "Feb",
            "mar": "Mar", "march": "Mar", "apr": "Apr", "april": "Apr",
            "may": "May", "jun": "Jun", "june": "Jun", "jul": "Jul", "july": "Jul",
            "aug": "Aug", "august": "Aug", "sep": "Sep", "september": "Sep",
            "oct": "Oct", "october": "Oct", "nov": "Nov", "november": "Nov",
            "dec": "Dec", "december": "Dec",
        }
        for word in re.split(r'[\s,]+|(?:\band\b)', month_text):
            word_clean = word.strip().lower().rstrip('.')
            if word_clean in _MONTH_NAMES:
                months.append(_MONTH_NAMES[word_clean])

    # Extract link before stripping it from the name
    link = None
    link_match = _LINK_RE.search(s)
    if link_match:
        link = link_match.group(1)
    # Remove all [here](url) and [*here*](url) patterns (from markdown source)
    s = _LINK_RE.sub("", s).strip()
    # Remove literal "here" words (from plain text export where links are stripped)
    s = re.sub(r'\s+here\b', '', s).strip()
    # Remove "and <TextAfterLink>" fragments left after stripping second links
    s = re.sub(r'\s+and\s+\S.*$', '', s).strip()
    # Also remove standalone "and" left at edges
    s = re.sub(r'\band\b\s*$', '', s).strip()
    s = re.sub(r'^\s*\band\b\s*', '', s).strip()

    # Extract emoji
    icon = ""
    em = _EMOJI_RE.match(s)
    if em:
        icon = em.group(1)
        s = s[em.end():]

    # Strip "Habit:" or "Habit: Habit:" prefix
    s = re.sub(r'^Habit:\s*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'^Habit:\s*', '', s, flags=re.IGNORECASE)  # double for "Habit: Habit: Annual..."

    # Extract cadence from parentheses
    cadence = ""
    cad_match = _CADENCE_RE.search(s)
    if cad_match:
        cadence = cad_match.group(0).strip("()")
        s = s[:cad_match.start()] + s[cad_match.end():]

    # Strip cadence prefix words from name: "Daily ", "Monthly ", "Weekly ", etc.
    for prefix in ["Daily ", "Weekly ", "Monthly ", "Bi-Monthly ", "Quarterly ", "Annual ", "Biannually "]:
        if s.startswith(prefix):
            if not cadence:
                cadence = prefix.strip().lower()
            s = s[len(prefix):]
            break

    # Clean up remaining artifacts
    s = s.strip(" —\u2014\u2013*")
    # Remove trailing " — (Sneha & Jeremy)" style suffixes
    s = re.sub(r'\s*—\s*\(.*?\)\s*$', '', s)
    # Remove " deadline", " Doctor schedule", " start" noise
    s = re.sub(r'\s+deadline\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+Doctor schedule\b', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+start\b', '', s, flags=re.IGNORECASE)
    s = s.strip()

    if not s:
        return None

    return {
        "icon": icon,
        "name": s,
        "cadence": cadence,
        "link": link,
        "months": months,
    }


def _parse_doc_text(text: str) -> dict:
    """Parse the full doc text into habits grouped by cadence.

    Args:
        text: Raw text content of the Google Doc.

    Returns:
        Dict with keys: daily, monthly, quarterly, annual. Each is a list
        of habit dicts with icon, name, cadence, link.
    """
    result = {"daily": [], "monthly": [], "quarterly": [], "annual": []}

    # Split by horizontal rules: --- or ___ (plain text export uses ________________)
    sections = re.split(r'^(?:---+|_{3,})\s*$', text, flags=re.MULTILINE)

    for section in sections:
        lines = section.strip().splitlines()
        if not lines:
            continue

        # Determine section category from header
        header = lines[0].lower()
        if "daily" in header or "weekly" in header:
            category = "daily"
        elif "quarterly" in header:
            category = "quarterly"
        elif "annual" in header:
            category = "annual"
        elif "monthly" in header:
            category = "monthly"
        else:
            # Try to detect from content
            category = None

        for line in lines[1:]:
            if not line.strip().startswith("*"):
                continue
            habit = _parse_habit_line(line)
            if habit:
                # Infer category from cadence if section header didn't match
                cat = category
                if cat is None:
                    cad = habit["cadence"].lower()
                    if "annual" in cad or "biannual" in cad:
                        cat = "annual"
                    elif "quarter" in cad or "month" in cad:
                        cat = "quarterly" if "quarter" in cad else "monthly"
                    else:
                        cat = "daily"
                result[cat].append(habit)

    return result


# ═══════════════════════════════════════════════════════════════════
# Cache
# ═══════════════════════════════════════════════════════════════════

def _read_cache() -> dict | None:
    """Read cached habits if fresh enough and same month.

    Auto-busts the cache when the month rolls over so new-month
    dashboards always get fresh data.

    Returns:
        Parsed habits dict if cache exists, is <24h old, and same month. Else None.
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
            log.info("Habits cache from %s, now %s — busting", cached_month, current_month)
            return None
        if time.time() - ts < CACHE_MAX_AGE_SECS:
            log.info("Using cached habits (age: %.0fh)", (time.time() - ts) / 3600)
            return data.get("habits", {})
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _read_stale_cache() -> dict:
    """Read cache regardless of age (fallback when fetch fails).

    Returns:
        Parsed habits dict, or empty dict if no cache.
    """
    if not CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text())
        return data.get("habits", {})
    except (json.JSONDecodeError, KeyError):
        return {}


def _write_cache(habits: dict) -> None:
    """Write habits to cache file with timestamp and current month."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "_timestamp": time.time(),
        "_month": datetime.now().strftime("%Y-%m"),
        "habits": habits,
    }
    CACHE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    log.info("Habits cache written to %s", CACHE_FILE)


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════

def clear_cache() -> None:
    """Delete the habits cache file, forcing a fresh fetch on next call."""
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
        log.info("Habits cache cleared")


def fetch_habits_from_doc(creds, force_refresh: bool = False) -> dict:
    """Fetch and parse the Habit Tracker Index Google Doc.

    Uses a 24-hour disk cache to avoid hitting the API on every sync.
    If the fetch fails, returns the stale cache or an empty dict.

    Args:
        creds: Google OAuth2 credentials (from ``sheets.get_google_creds()``).

    Returns:
        Dict with keys: daily, monthly, quarterly, annual. Each is a list of::

            {"icon": "💰", "name": "Finance Check", "cadence": "monthly", "link": "https://..."}
    """
    # Check cache first (skip if force refresh)
    if not force_refresh:
        cached = _read_cache()
        if cached:
            return cached

    # Fetch via Drive API (export as plain text) — doesn't require Docs API
    try:
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        # Export the doc as plain text
        response = drive.files().export(
            fileId=HABIT_DOC_ID, mimeType="text/plain"
        ).execute()
        raw_text = response.decode("utf-8") if isinstance(response, bytes) else response

        # The doc also has markdown-style links in the text itself, so use those
        habits = _parse_doc_text(raw_text)
        _write_cache(habits)
        log.info("Fetched habits from Google Doc: %s",
                 {k: len(v) for k, v in habits.items()})
        return habits

    except Exception as exc:
        log.warning("Failed to fetch habits doc: %s — using cache", exc)
        return _read_stale_cache()
