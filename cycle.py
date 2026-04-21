"""Menstrual cycle phase logic.

Maps 1-based cycle day numbers to phase labels (Follicular, Ovulation,
Luteal-EM, Luteal-PMS, Menstrual). Used by both `sync.py` (to persist
the phase in `daily_entries.cycle_phase`) and `html_report.py` (to pick
a coaching line on the Quest Hub header).
"""

from constants import CYCLE_PHASES


def get_cycle_phase(cycle_day: int) -> str:
    """Return the phase label for a given day of the menstrual cycle.

    Args:
        cycle_day: 1-based day number in the cycle.

    Returns:
        Phase label (e.g. "Follicular", "Luteal-PMS"). Falls through to
        "Luteal (PMS)" for day > 28 (irregular cycles) and "Unknown"
        for invalid inputs.
    """
    for start, end, label, _row in CYCLE_PHASES:
        if start <= cycle_day <= end:
            return label
    if cycle_day > 28:
        return "Luteal (PMS)"
    return "Unknown"
