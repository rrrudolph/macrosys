"""Prospective weekly decision: refresh, gate, and freeze a forecast (spec §11.2, §10.3)."""

from macrosys.forward.week import DuplicateWeekError, WeekResult, forward_week, week_status_text

__all__ = [
    "DuplicateWeekError",
    "WeekResult",
    "forward_week",
    "week_status_text",
]
