"""
Menstrual cycle phase logic.

Maps cycle day numbers to phase labels, determines which PMS Quick Guide
row to highlight, and applies background colors in the Google Sheet.
"""

import logging
import re

from constants import (
    CYCLE_PHASES, PHASE_HIGHLIGHT, PHASE_DEFAULT_BG, ROW_CYCLE,
)

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Phase lookups
# ═══════════════════════════════════════════════════════════════════

def get_cycle_phase(cycle_day: int) -> str:
    """Return the phase label for a given day of the menstrual cycle.

    Args:
        cycle_day: 1-based day number in the cycle.

    Returns:
        Phase name string (e.g. "Follicular", "Luteal-PMS").
    """
    for start, end, label, _row in CYCLE_PHASES:
        if start <= cycle_day <= end:
            return label
    if cycle_day > 28:
        return "Luteal (PMS)"
    return "Unknown"


def get_cycle_phase_guide_row(cycle_day: int) -> int | None:
    """Return the PMS Quick Guide row number for the current cycle phase.

    Args:
        cycle_day: 1-based day number in the cycle.

    Returns:
        1-indexed row number, or None if cycle_day is invalid.
    """
    for start, end, _label, row in CYCLE_PHASES:
        if start <= cycle_day <= end:
            return row
    if cycle_day > 28:
        return 20  # Luteal (PMS) row
    return None


# ═══════════════════════════════════════════════════════════════════
# Sheet highlighting
# ═══════════════════════════════════════════════════════════════════

def highlight_active_phase(service, spreadsheet_id: str, tab_name: str, cycle_day: int) -> None:
    """Highlight the active cycle phase row in the PMS Quick Guide.

    Sets a yellow background on the row matching the current phase,
    and resets all other phase rows to white. Also sets font size 9
    on cycle value cells (C13:H13).

    Args:
        service: Google Sheets API service.
        spreadsheet_id: Target spreadsheet.
        tab_name: Target tab.
        cycle_day: 1-based day number in the cycle.
    """
    active_row = get_cycle_phase_guide_row(cycle_day)
    if active_row is None:
        return

    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    sheet_id = None
    for s in metadata.get("sheets", []):
        if s["properties"]["title"] == tab_name:
            sheet_id = s["properties"]["sheetId"]
            break
    if sheet_id is None:
        return

    requests_list = []
    for _start, _end, _label, row in CYCLE_PHASES:
        row_idx = row - 1
        is_active = (row == active_row)
        bg = PHASE_HIGHLIGHT if is_active else PHASE_DEFAULT_BG
        requests_list.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_idx,
                    "endRowIndex": row_idx + 1,
                    "startColumnIndex": 3,   # column D
                    "endColumnIndex": 8,     # through column H
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": bg,
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    # Set font size 9 on cycle value cells (C13:H13)
    requests_list.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": ROW_CYCLE - 1,
                "endRowIndex": ROW_CYCLE,
                "startColumnIndex": 2,
                "endColumnIndex": 8,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"fontSize": 9}
                }
            },
            "fields": "userEnteredFormat.textFormat.fontSize",
        }
    })

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests_list},
    ).execute()
    log.info("Highlighted phase row %d in PMS Quick Guide", active_row)


def get_dominant_cycle_day(service, spreadsheet_id: str, tab_name: str) -> int | None:
    """Read all cycle cells (C13:I13) and return a cycle day whose phase
    appears most often in the week.

    This ensures the highlight reflects the dominant phase rather than
    just the latest day synced.

    Args:
        service: Google Sheets API service.
        spreadsheet_id: Target spreadsheet.
        tab_name: Target tab.

    Returns:
        A representative cycle day number, or None if no data.
    """
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!C{ROW_CYCLE}:I{ROW_CYCLE}",
    ).execute()
    values = result.get("values", [[]])[0] if result.get("values") else []

    phase_counts: dict[str, int] = {}
    phase_to_day: dict[str, int] = {}
    for val in values:
        val = str(val).strip()
        if not val:
            continue
        m = re.search(r"D(\d+)", val) or re.search(r"\(Day (\d+)\)", val)
        if m:
            cd = int(m.group(1))
            ph = get_cycle_phase(cd)
            phase_counts[ph] = phase_counts.get(ph, 0) + 1
            phase_to_day[ph] = cd

    if not phase_counts:
        return None

    dominant_phase = max(phase_counts, key=phase_counts.get)
    return phase_to_day[dominant_phase]
