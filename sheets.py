"""
Google Sheets and Drive operations.

Handles OAuth2 authentication, spreadsheet resolution (find or create
monthly spreadsheets), weekly tab management, and all cell read/write
operations including formatting and the challenge scoreboard.
"""

import logging
import os
import sys
import time
from datetime import date, timedelta

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from constants import (
    SCRIPT_DIR, SCOPES, TEMPLATE_TAB_NAME,
    ROW_DATE_NUM, ROW_STRENGTH, ROW_CARDIO, ROW_STEPS, ROW_SLEEP,
    ROW_NUTRITION, ROW_CYCLE, ROW_NOTES, ROW_CHALLENGE_HEADER, ROW_DAILY_TOTAL,
    ROW_MORNING_STAR, ROW_NIGHT_STAR,
    MAX_RETRIES, RATE_LIMIT_STATUS, RATE_LIMIT_BACKOFF_SECS,
    GOLD_BG, DARK_TEXT, WHITE_BG,
)

log = logging.getLogger(__name__)

TEMPLATE_SPREADSHEET_ID = os.getenv(
    "SPREADSHEET_ID", "1xTqB26-HdeNSqPdNT-Bs8qSmGeAyPf0wlQTz68Mj3ds"
)
def _drive_folder_id():
    """Read DRIVE_PARENT_FOLDER_ID lazily (after .env is loaded)."""
    return os.getenv("DRIVE_PARENT_FOLDER_ID")
OAUTH_CREDENTIALS_FILE = SCRIPT_DIR / os.getenv(
    "OAUTH_CREDENTIALS_FILE", "credentials.json"
)
OAUTH_TOKEN_FILE = SCRIPT_DIR / "token.json"

# Cache: "YYYY-MM" → spreadsheet_id (avoids repeated Drive lookups during backfill)
_spreadsheet_cache: dict[str, str] = {}


# ═══════════════════════════════════════════════════════════════════
# OAuth2 authentication
# ═══════════════════════════════════════════════════════════════════

def _load_creds_from_env() -> Credentials | None:
    """Try to construct credentials from env vars (for Render / Actions).

    Accepts either:
      GOOGLE_TOKEN_JSON   — full user OAuth token (preferred, refresh-capable)
      GOOGLE_CREDS_JSON   — OAuth CLIENT credentials (can't auth headlessly
                            on its own, but callers may combine it later)

    Returns valid Credentials, or None if env vars aren't set / invalid.
    """
    import json as _json
    token_blob = os.getenv("GOOGLE_TOKEN_JSON")
    if token_blob:
        try:
            data = _json.loads(token_blob)
            creds = Credentials.from_authorized_user_info(data, SCOPES)
            if creds and creds.valid:
                return creds
            if creds and creds.expired and creds.refresh_token:
                log.info("Refreshing OAuth2 token from env...")
                creds.refresh(Request())
                return creds
        except Exception as exc:
            log.warning("GOOGLE_TOKEN_JSON parse failed: %s", exc)
    return None


def get_google_creds() -> Credentials:
    """Get OAuth2 credentials.

    Search order:
      1. GOOGLE_TOKEN_JSON env var  (Render, GitHub Actions)
      2. token.json on disk         (local Mac dev)
      3. Interactive browser login  (first-time local setup only)

    The interactive path is a *hard* local-only path — in a headless
    container (no browser, no port to bind run_local_server) it raises
    so callers (Flask, cron) can degrade gracefully rather than hanging.

    Returns:
        Valid Google OAuth2 Credentials object.
    """
    # 1. env var (cloud)
    env_creds = _load_creds_from_env()
    if env_creds:
        return env_creds

    # 2. token.json (local)
    creds = None
    if OAUTH_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(OAUTH_TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                log.info("Refreshing OAuth2 token...")
                creds.refresh(Request())
            except Exception as exc:
                log.warning("Token refresh failed (%s) — re-authenticating...", exc)
                creds = None

        # 3. Interactive login (LOCAL ONLY — browser + port required)
        if not creds or not creds.valid:
            if os.getenv("GOOGLE_NO_INTERACTIVE") == "1":
                raise RuntimeError(
                    "Google OAuth token missing/expired and interactive login "
                    "is disabled. Set GOOGLE_TOKEN_JSON env var."
                )
            if not OAUTH_CREDENTIALS_FILE.exists():
                log.error("OAuth credentials file not found: %s", OAUTH_CREDENTIALS_FILE)
                log.error("Download it from GCP Console → APIs → Credentials → OAuth 2.0 Client IDs")
                sys.exit(1)
            log.info("Opening browser for Google login...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(OAUTH_CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Persist locally. In containers OAUTH_TOKEN_FILE is writable only
        # under /tmp, so wrap in try/except.
        try:
            OAUTH_TOKEN_FILE.write_text(creds.to_json())
            log.info("OAuth2 token saved to %s", OAUTH_TOKEN_FILE)
        except OSError as exc:
            log.warning("Could not persist token.json (%s) — carrying on", exc)

    return creds


# ═══════════════════════════════════════════════════════════════════
# Spreadsheet resolution (find or create monthly spreadsheets)
# ═══════════════════════════════════════════════════════════════════

def _find_spreadsheet(drive, month_only: str) -> str | None:
    """Search Drive for an existing monthly spreadsheet.

    Args:
        drive: Google Drive API service.
        month_only: Month name (e.g. "April").

    Returns:
        Spreadsheet ID if found, else None.
    """
    if not _drive_folder_id():
        return None
    query = (
        f"name contains '{month_only}' and "
        f"'{_drive_folder_id()}' in parents and "
        f"mimeType = 'application/vnd.google-apps.spreadsheet' and "
        f"trashed = false"
    )
    results = drive.files().list(q=query, fields="files(id, name)").execute()
    files = [
        f for f in results.get("files", [])
        if f["name"].strip().startswith(month_only)
    ]
    if files:
        sid = files[0]["id"]
        log.info("Found spreadsheet '%s' → %s", files[0]["name"].strip(), sid)
        return sid
    return None


def _create_spreadsheet(creds, month_name: str) -> str:
    """Create a new monthly spreadsheet from the template.

    Args:
        creds: Google OAuth2 credentials.
        month_name: Full name like "April: Week Accountability".

    Returns:
        The new spreadsheet ID.
    """
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    body = {"properties": {"title": month_name}}
    spreadsheet = sheets.spreadsheets().create(body=body).execute()
    new_id = spreadsheet["spreadsheetId"]
    log.info("Created spreadsheet '%s' (id=%s)", month_name, new_id)

    # Move to the parent Drive folder
    if _drive_folder_id():
        file_info = drive.files().get(fileId=new_id, fields="parents").execute()
        old_parents = ",".join(file_info.get("parents", []))
        drive.files().update(
            fileId=new_id,
            addParents=_drive_folder_id(),
            removeParents=old_parents,
            fields="id, parents",
        ).execute()
        log.info("Moved '%s' into Drive folder %s", month_name, _drive_folder_id())

    # Copy template tab from the reference spreadsheet
    ref_meta = sheets.spreadsheets().get(
        spreadsheetId=TEMPLATE_SPREADSHEET_ID, fields="sheets.properties"
    ).execute()
    template_sid = None
    for s in ref_meta.get("sheets", []):
        if s["properties"]["title"] == TEMPLATE_TAB_NAME:
            template_sid = s["properties"]["sheetId"]
            break
    if template_sid is None:
        template_sid = ref_meta["sheets"][0]["properties"]["sheetId"]

    sheets.spreadsheets().sheets().copyTo(
        spreadsheetId=TEMPLATE_SPREADSHEET_ID,
        sheetId=template_sid,
        body={"destinationSpreadsheetId": new_id},
    ).execute()

    # Rename "Copy of sheet1" → "sheet1" and delete the auto-created "Sheet1"
    new_meta = sheets.spreadsheets().get(
        spreadsheetId=new_id, fields="sheets.properties"
    ).execute()
    batch_requests = []
    for s in new_meta.get("sheets", []):
        title = s["properties"]["title"]
        sid = s["properties"]["sheetId"]
        if title.startswith("Copy of"):
            batch_requests.append({
                "updateSheetProperties": {
                    "properties": {"sheetId": sid, "title": TEMPLATE_TAB_NAME},
                    "fields": "title",
                }
            })
        elif title == "Sheet1":
            batch_requests.append({"deleteSheet": {"sheetId": sid}})

    if batch_requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=new_id, body={"requests": batch_requests}
        ).execute()

    log.info("Template copied into '%s' — ready to use!", month_name)
    return new_id


def resolve_spreadsheet_id(target: date, creds) -> str:
    """Return the spreadsheet ID for the target date's month.

    Searches Drive for a spreadsheet named 'April: Week Accountability' etc.
    Falls back to TEMPLATE_SPREADSHEET_ID for March 2026.
    Creates a new spreadsheet from the template for new months.

    Safety: refuses to create a new spreadsheet when DRIVE_PARENT_FOLDER_ID
    is not set. Without this guard, a script that imports sheets.py before
    loading the .env file would silently drop duplicate spreadsheets into
    Drive root.

    Args:
        target: The date whose month determines which spreadsheet to use.
        creds: Google OAuth2 credentials.

    Returns:
        Google Sheets spreadsheet ID string.

    Raises:
        RuntimeError: if the target month's spreadsheet doesn't exist AND
            DRIVE_PARENT_FOLDER_ID is missing (refuses to scatter orphans).
    """
    key = target.strftime("%Y-%m")
    if key in _spreadsheet_cache:
        return _spreadsheet_cache[key]

    month_name = target.strftime("%B") + ": Week Accountability"
    month_only = target.strftime("%B")

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sid = _find_spreadsheet(drive, month_only)
    if sid:
        _spreadsheet_cache[key] = sid
        return sid

    # Not found — if this is March 2026 (the template month), use the template directly
    if TEMPLATE_SPREADSHEET_ID and target.year == 2026 and target.month == 3:
        _spreadsheet_cache[key] = TEMPLATE_SPREADSHEET_ID
        log.info("Using template spreadsheet for %s → %s", month_name, TEMPLATE_SPREADSHEET_ID)
        return TEMPLATE_SPREADSHEET_ID

    # Refuse to create if folder ID isn't configured — otherwise we scatter
    # orphan spreadsheets across Drive root on every invocation where .env
    # wasn't loaded.
    if not _drive_folder_id():
        raise RuntimeError(
            f"Cannot create '{month_name}' — DRIVE_PARENT_FOLDER_ID is not set. "
            "Ensure .env is loaded before importing sheets.py."
        )

    new_id = _create_spreadsheet(creds, month_name)
    _spreadsheet_cache[key] = new_id
    return new_id


# ═══════════════════════════════════════════════════════════════════
# Weekly tab management
# ═══════════════════════════════════════════════════════════════════

def get_week_tab_name(monday: date, sunday: date) -> str:
    """Return tab name like 'Mar 16 – 22' or 'Mar 30 – Apr 05'.

    Args:
        monday: First day of the week.
        sunday: Last day of the week.

    Returns:
        Human-readable tab name string.
    """
    if monday.month == sunday.month:
        return f"{monday.strftime('%b %d')} - {sunday.day}"
    return f"{monday.strftime('%b %d')} - {sunday.strftime('%b %d')}"


def get_template_sheet_id(service, spreadsheet_id: str) -> int:
    """Return the sheetId of the template tab.

    Args:
        service: Google Sheets API service.
        spreadsheet_id: The spreadsheet to look in.

    Returns:
        Integer sheet ID.
    """
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    for sheet in metadata.get("sheets", []):
        if sheet["properties"]["title"] == TEMPLATE_TAB_NAME:
            return sheet["properties"]["sheetId"]
    return metadata["sheets"][0]["properties"]["sheetId"]


def find_or_create_tab(service, spreadsheet_id: str, monday: date, sunday: date) -> str:
    """Find the weekly tab or create it by duplicating the template.

    Args:
        service: Google Sheets API service.
        spreadsheet_id: The spreadsheet to use.
        monday: First day of the week.
        sunday: Last day of the week.

    Returns:
        The tab name string.
    """
    tab_name = get_week_tab_name(monday, sunday)

    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    existing = {s["properties"]["title"] for s in metadata.get("sheets", [])}

    if tab_name in existing:
        log.info("Using existing tab '%s'", tab_name)
        return tab_name

    log.info("Creating new weekly tab '%s' (duplicating template)", tab_name)
    template_id = get_template_sheet_id(service, spreadsheet_id)

    result = service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "duplicateSheet": {
                "sourceSheetId": template_id,
                "newSheetName": tab_name,
            }
        }]},
    ).execute()
    new_sheet_id = result["replies"][0]["duplicateSheet"]["properties"]["sheetId"]
    log.info("Duplicated template → '%s' (sheetId=%d)", tab_name, new_sheet_id)

    # Defensive: unmerge Morning Star (row 19) and Night Star (row 20) cells
    # D–H so per-day writes actually persist. Historical templates had these
    # columns merged, which made writes to D/E/F/G/H silently vanish.
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [
                {"unmergeCells": {"range": {
                    "sheetId": new_sheet_id,
                    "startRowIndex": row_idx,
                    "endRowIndex": row_idx + 1,
                    "startColumnIndex": 3,
                    "endColumnIndex": 8,
                }}}
                for row_idx in (18, 19)
            ]},
        ).execute()
    except Exception as exc:
        log.warning("Could not unmerge star rows (may not have been merged): %s", exc)

    # Clear ALL data cells (keep labels and formatting)
    # Must include ROW_MORNING_STAR and ROW_NIGHT_STAR — otherwise a new week
    # inherits last week's ritual ✓'s from the duplicated tab.
    data_rows = [ROW_STRENGTH, ROW_CARDIO, ROW_STEPS, ROW_SLEEP, ROW_NUTRITION,
                 ROW_CYCLE, ROW_MORNING_STAR, ROW_NIGHT_STAR, ROW_DAILY_TOTAL]
    ranges = [f"'{tab_name}'!C{row}:I{row}" for row in data_rows]
    ranges.append(f"'{tab_name}'!B{ROW_NOTES}")
    ranges.append(f"'{tab_name}'!A{ROW_DAILY_TOTAL}:I{ROW_DAILY_TOTAL}")
    ranges.append(f"'{tab_name}'!C7:I7")  # Sauna row
    service.spreadsheets().values().batchClear(
        spreadsheetId=spreadsheet_id,
        body={"ranges": ranges},
    ).execute()

    # Update the "Week of:" label
    week_label = f"Week of: {monday.strftime('%b %d')} – {sunday.strftime('%b %d')}"
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [[week_label]]},
    ).execute()

    # Write date numbers (row 3: Mon=13, Tue=14, etc.)
    date_numbers = [[(monday + timedelta(days=i)).day for i in range(7)]]
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!C{ROW_DATE_NUM}:I{ROW_DATE_NUM}",
        valueInputOption="USER_ENTERED",
        body={"values": date_numbers},
    ).execute()

    # Ensure day headers exist (template may be missing "Sun" in column I)
    day_headers = [["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]]
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!C4:I4",
        valueInputOption="USER_ENTERED",
        body={"values": day_headers},
    ).execute()

    # Left-align all cells in the new tab
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "repeatCell": {
                "range": {"sheetId": new_sheet_id},
                "cell": {
                    "userEnteredFormat": {
                        "horizontalAlignment": "LEFT"
                    }
                },
                "fields": "userEnteredFormat.horizontalAlignment"
            }
        }]},
    ).execute()
    log.info("Cleared data, set week label and date numbers on '%s'", tab_name)

    return tab_name


# ═══════════════════════════════════════════════════════════════════
# Cell read/write helpers
# ═══════════════════════════════════════════════════════════════════

def _get_sheet_id(service, spreadsheet_id: str, tab_name: str) -> int | None:
    """Return the sheetId for a given tab name, or None."""
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    for s in metadata.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    return None


def set_cell_font_size(service, spreadsheet_id: str, tab_name: str,
                       row: int, col_start: int, col_end: int, size: int) -> None:
    """Set font size on a range of cells.

    Args:
        service: Google Sheets API service.
        spreadsheet_id: Target spreadsheet.
        tab_name: Target tab.
        row: 1-indexed row number.
        col_start: 0-indexed start column.
        col_end: 0-indexed end column (exclusive).
        size: Font size in points.
    """
    sheet_id = _get_sheet_id(service, spreadsheet_id, tab_name)
    if sheet_id is None:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row - 1,
                    "endRowIndex": row,
                    "startColumnIndex": col_start,
                    "endColumnIndex": col_end,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"fontSize": size}
                    }
                },
                "fields": "userEnteredFormat.textFormat.fontSize",
            }
        }]},
    ).execute()


def read_cell(service, spreadsheet_id: str, tab_name: str, cell: str) -> str | None:
    """Read a single cell value from a specific tab.

    Args:
        service: Google Sheets API service.
        spreadsheet_id: Target spreadsheet.
        tab_name: Target tab.
        cell: Cell reference like "A1" or "B12".

    Returns:
        Cell value as string, or None if empty.
    """
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{tab_name}'!{cell}")
        .execute()
    )
    values = result.get("values", [])
    return values[0][0] if values else None


def write_cell(service, spreadsheet_id: str, tab_name: str, cell: str, value) -> None:
    """Write a single cell value with retry on rate limits.

    Args:
        service: Google Sheets API service.
        spreadsheet_id: Target spreadsheet.
        tab_name: Target tab.
        cell: Cell reference like "A1".
        value: Value to write.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{tab_name}'!{cell}",
                valueInputOption="USER_ENTERED",
                body={"values": [[value]]},
            ).execute()
            log.info("Wrote %s → %s!%s", value, tab_name, cell)
            return
        except HttpError as e:
            if e.resp.status == RATE_LIMIT_STATUS and attempt < MAX_RETRIES:
                wait = RATE_LIMIT_BACKOFF_SECS * (attempt + 1)
                log.warning("Rate limited writing %s!%s — waiting %ds (attempt %d/%d)",
                            tab_name, cell, wait, attempt + 1, MAX_RETRIES)
                time.sleep(wait)
            else:
                raise


# ═══════════════════════════════════════════════════════════════════
# Row label helpers
# ═══════════════════════════════════════════════════════════════════

def ensure_nutrition_row_label(service, spreadsheet_id: str, tab_name: str) -> None:
    """Make sure row 11 column B has the nutrition label."""
    current = read_cell(service, spreadsheet_id, tab_name, f"B{ROW_NUTRITION}")
    if not current or "MyFitnessPal" in current or "P/C/F" in current or current.strip() == "":
        write_cell(service, spreadsheet_id, tab_name, f"B{ROW_NUTRITION}",
                   "🍽️ Calories (MFP)")


def ensure_cycle_row_label(service, spreadsheet_id: str, tab_name: str) -> None:
    """Make sure row 13 column A has the 'Cycle Phase' label."""
    current = read_cell(service, spreadsheet_id, tab_name, "A13")
    if not current or current.strip() == "":
        write_cell(service, spreadsheet_id, tab_name, "A13", "🔄 Cycle Phase")


# ═══════════════════════════════════════════════════════════════════
# Challenge scoreboard (rows 15-22)
# ═══════════════════════════════════════════════════════════════════

def ensure_challenge_scoreboard(service, spreadsheet_id: str, tab_name: str) -> None:
    """Ensure the Weekly Challenge scoreboard exists (rows 21-22) and scoring
    guide is in rows 15-20 A-C (next to PMS Quick Guide in D-H).

    Layout:
        Rows 15-20 A-C: Scoring guide table (what earns stars + tiers)
        Row 21: WEEKLY CHALLENGE header (merged A-H, gold)
        Row 22: Score | daily star cells in C-H

    Args:
        service: Google Sheets API service.
        spreadsheet_id: Target spreadsheet.
        tab_name: Target tab.
    """
    sheet_id = _get_sheet_id(service, spreadsheet_id, tab_name)
    if sheet_id is None:
        return

    requests_list = []

    # Unmerge rows 21-22 first (0-indexed: 20-21)
    for row_idx in [20, 21]:
        requests_list.append({
            "unmergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_idx,
                    "endRowIndex": row_idx + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": 8,
                }
            }
        })

    # Row 21 (idx 20): merge A-H, gold header, centered bold
    requests_list.append({
        "mergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 20, "endRowIndex": 21,
                "startColumnIndex": 0, "endColumnIndex": 8,
            },
            "mergeType": "MERGE_ALL",
        }
    })
    requests_list.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 20, "endRowIndex": 21,
                "startColumnIndex": 0, "endColumnIndex": 8,
            },
            "cell": {"userEnteredFormat": {
                "backgroundColor": GOLD_BG,
                "textFormat": {"bold": True, "fontSize": 10,
                               "foregroundColor": DARK_TEXT},
                "horizontalAlignment": "CENTER",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }
    })

    # Row 22 (idx 21): stars C-H centered + larger
    requests_list.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 21, "endRowIndex": 22,
                "startColumnIndex": 2, "endColumnIndex": 8,
            },
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "CENTER",
                "textFormat": {"fontSize": 12},
            }},
            "fields": "userEnteredFormat(horizontalAlignment,textFormat)",
        }
    })

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests_list},
        ).execute()
    except Exception:
        pass  # merges may already exist

    # Write scoreboard content
    write_cell(service, spreadsheet_id, tab_name,
               f"A{ROW_CHALLENGE_HEADER}", "⭐ DAILY STARS — max 3/day")

    # Write scoring guide in rows 15-20 A-C
    guide = {
        15: ("⭐ SCORING GUIDE", "Earn", ""),
        16: ("☀️ Morning Ritual", "1⭐", "all 4 done"),
        17: ("⚡ Core Missions", "1⭐", "4 of 7 done"),
        18: ("🌙 Night Ritual", "1⭐", "all 4 done"),
        19: ("", "", ""),
        20: ("", "", ""),
    }
    for row, (a, b, c) in guide.items():
        write_cell(service, spreadsheet_id, tab_name, f"A{row}", a)
        write_cell(service, spreadsheet_id, tab_name, f"B{row}", b)
        write_cell(service, spreadsheet_id, tab_name, f"C{row}", c)

    # Update cardio row label
    write_cell(service, spreadsheet_id, tab_name, "A6", "🚴 Cardio (1x)")

    # Row 22: Merge A22:B22, style for score + tier text
    requests_r22 = [
        {"unmergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 19, "endRowIndex": 20,
                "startColumnIndex": 0, "endColumnIndex": 3,
            },
        }},
        {"mergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 21, "endRowIndex": 22,
                "startColumnIndex": 0, "endColumnIndex": 2,
            },
            "mergeType": "MERGE_ALL",
        }},
        {"repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 21, "endRowIndex": 22,
                "startColumnIndex": 0, "endColumnIndex": 2,
            },
            "cell": {"userEnteredFormat": {
                "backgroundColor": WHITE_BG,
                "textFormat": {"bold": True, "fontSize": 10},
                "wrapStrategy": "WRAP",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)",
        }},
    ]
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests_r22},
        ).execute()
    except Exception:
        pass
