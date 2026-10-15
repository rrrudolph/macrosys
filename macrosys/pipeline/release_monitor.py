"""Daily release monitor: one gap-gate check per configured response and horizon (spec §11.2, §11.3).

The daily cadence ingests releases and writes a monitoring note. It does not trade. For each
release-to-market pair in the events config, and each horizon that config names, this module asks
``assess_release`` what was knowable at ``as_of``. The slope, the surprise, and the rule that drops
anything published later all stay in that function.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict

from macrosys.events.assess import assess_release
from macrosys.events.config import EventsConfig


class ReleaseMonitorRow(BaseModel):
    """One release, market, and horizon as of the monitor's timestamp (spec §11.2, §11.3).

    ``horizon`` is the configured horizon in trading days. ``blocked`` and ``reason`` are the
    ``assess_release`` result: a blocked row fails the gap gate for that pair.
    """

    model_config = ConfigDict(frozen=True)

    release: str
    market: str
    horizon: int
    blocked: bool
    reason: str


class ReleaseMonitorResult(BaseModel):
    """Frozen assessment rows and the plain-text note for one daily pass (spec §11.2)."""

    model_config = ConfigDict(frozen=True)

    rows: tuple[ReleaseMonitorRow, ...]
    note: str


def monitor_releases(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    config: EventsConfig,
    parameters: str | Path,
) -> ReleaseMonitorResult:
    """Assess every configured response at every configured horizon and return frozen rows plus a note (spec §11.2, §11.3).

    ``as_of`` must be timezone-aware and is passed through to ``assess_release`` once per response and
    horizon, so nothing published after that timestamp is used and the slope is not recomputed here.
    ``parameters`` is the event_response file. ``rows`` is frozen, and ``note`` is the plain-text
    monitoring note for that same timestamp.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    rows = tuple(
        _row(conn, spec.release, spec.market, horizon, as_of, config, parameters)
        for spec in config.responses
        for horizon in config.horizons_days
    )
    return ReleaseMonitorResult(rows=rows, note=_note(as_of, rows))


def _row(
    conn: duckdb.DuckDBPyConnection,
    release: str,
    market: str,
    horizon: int,
    as_of: datetime,
    config: EventsConfig,
    parameters: str | Path,
) -> ReleaseMonitorRow:
    assessment = assess_release(
        conn,
        release,
        market,
        horizon,
        as_of,
        config=config,
        parameters=parameters,
    )
    return ReleaseMonitorRow(
        release=release,
        market=market,
        horizon=horizon,
        blocked=assessment.blocked,
        reason=assessment.reason,
    )


def _note(as_of: datetime, rows: tuple[ReleaseMonitorRow, ...]) -> str:
    blocked = sum(row.blocked for row in rows)
    horizons = ", ".join(str(horizon) for horizon in dict.fromkeys(row.horizon for row in rows))
    lines = [
        f"Daily release monitor as of {as_of.isoformat()}.",
        f"Checked {len(rows)} release responses at horizons {horizons} trading days. {blocked} blocked.",
        "A blocked row fails the gap gate for that release, market, and horizon. This note does not place a trade.",
        "",
    ]
    for row in rows:
        state = "blocked" if row.blocked else "not blocked"
        day = "trading day" if row.horizon == 1 else "trading days"
        lines.append(f"{row.release} -> {row.market}, {row.horizon} {day}: {state}. {row.reason}")
    return "\n".join(lines)
