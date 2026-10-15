"""Survey consensus as the forecast behind a release surprise (spec §7.3, §9.8, §10.1 item 11; P2.4).

The event study's statistical forecaster (an AR(1) on earlier headline changes) stands in for what
the literature actually uses: the pre-release survey median. This module supplies that median from a
calendar extract that lives in a local SQLite file (``ff_cal_raw``: ``datetime``, ``ccy``,
``ccy_event``, ``forecast``, ``actual``, ``previous``), produced outside this repository. Nothing
here makes a request; the file path is an argument.

Matching is by release date. For each first print in ALFRED the lookup finds the mapped calendar
event scheduled on the same UTC date, converts its printed numbers to the release's headline-change
unit (``ConsensusMapping.kind``), and accepts the match only if the calendar's printed *actual*
agrees with the store's as-reported change within the print's rounding tolerance. That check is the
unit guard: the extract dropped the printed suffix, so a scale error, a wrong event, or a release
that printed two periods on one day (a shutdown catch-up) shows up as a mismatch and the event is
left out rather than scored against the wrong forecast. Events with no calendar row, no forecast, or
an ambiguous row (two prints of one event on one date) are left out too, and counted.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict

from macrosys.core.contracts import ParserChangedError
from macrosys.core.quantity import Quantity
from macrosys.events.config import ConsensusConfig, ConsensusMapping, EventsConfig, ReleaseSpec, Transform
from macrosys.events.schema import ConsensusPrint
from macrosys.events.store import ConsensusStore
from macrosys.events.surprise import headline_change

LEGACY_TABLE = "ff_cal_raw"
_LEGACY_COLUMNS = ("datetime", "ccy", "ccy_event", "forecast", "actual", "previous")
_QUARTERS_PER_YEAR = 4


class LegacyLoad(BaseModel):
    """What ``read_legacy_sqlite`` kept and what it dropped."""

    model_config = ConfigDict(frozen=True)

    prints: list[ConsensusPrint]
    source_hash: str
    rows_read: int
    dropped_future_actual: int
    dropped_empty: int


def read_legacy_sqlite(path: str | Path, source_id: str, *, loaded_at: datetime | None = None) -> LegacyLoad:
    """Read every ``ff_cal_raw`` row of a local calendar extract into typed prints (spec §9.8).

    The table must have exactly the legacy columns, else ``ParserChangedError``. Timestamps in the
    file are naive UTC wall clocks and come back timezone-aware. ``source_hash`` is the SHA-256 of
    the file bytes, so a row can be traced to the file state it was read from. A row that carries an
    actual for a release scheduled after ``loaded_at`` cannot be a real print (the legacy tool's test
    rows look like this) and is dropped; so is a row with no numbers at all.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(file_path)
    stamp = loaded_at if loaded_at is not None else datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("loaded_at must be timezone-aware")
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    conn = sqlite3.connect(f"file:{file_path.as_posix()}?mode=ro", uri=True)
    try:
        columns = tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{LEGACY_TABLE}")').fetchall())
        if columns != _LEGACY_COLUMNS:
            raise ParserChangedError(f"{LEGACY_TABLE} columns are {columns}, expected {_LEGACY_COLUMNS}")
        rows = conn.execute(
            f'SELECT datetime, ccy, ccy_event, forecast, actual, previous FROM "{LEGACY_TABLE}" ORDER BY datetime, ccy_event'
        ).fetchall()
    finally:
        conn.close()
    prints: list[ConsensusPrint] = []
    future_actual = 0
    empty = 0
    for text, currency, event, forecast, actual, previous in rows:
        if forecast is None and actual is None and previous is None:
            empty += 1
            continue
        scheduled = _legacy_clock(str(text))
        if actual is not None and scheduled > stamp:
            future_actual += 1
            continue
        prints.append(
            ConsensusPrint(
                source=source_id,
                currency=str(currency),
                event=str(event),
                scheduled_at=scheduled,
                forecast=_float(forecast),
                actual=_float(actual),
                previous=_float(previous),
                ingested_at=stamp,
                source_hash=digest,
            )
        )
    return LegacyLoad(
        prints=prints,
        source_hash=digest,
        rows_read=len(rows),
        dropped_future_actual=future_actual,
        dropped_empty=empty,
    )


def _legacy_clock(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ParserChangedError(f"{LEGACY_TABLE}.datetime {text!r} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raise ParserChangedError(f"{LEGACY_TABLE} numeric column holds {value!r}")


def printed_to_change(value: float, mapping: ConsensusMapping, previous: Quantity, transform: Transform, provenance: str) -> Quantity:
    """Convert a printed calendar number to the release's headline-change unit (``ConsensusMapping.kind``)."""
    if mapping.kind == "change":
        return Quantity(value=value, unit=mapping.unit, provenance=provenance)
    if mapping.kind == "level":
        return headline_change(Quantity(value=value, unit=mapping.unit, provenance=provenance), previous, transform)
    if mapping.kind == "annualized_q_pct":
        if mapping.unit != "pct":
            raise ValueError("annualized_q_pct needs a percent print")
        quarterly = ((1.0 + value / 100.0) ** (1.0 / _QUARTERS_PER_YEAR) - 1.0) * 100.0
        return Quantity(value=quarterly, unit="pct", provenance=provenance)
    raise ValueError(f"unknown consensus kind {mapping.kind!r}")


@dataclass
class MatchStats:
    """Why events did or did not get a consensus forecast, per release."""

    matched: int = 0
    no_print: int = 0
    ambiguous: int = 0
    no_forecast: int = 0
    no_actual: int = 0
    mismatch: int = 0
    mismatches: list[tuple[date, float, float]] = field(default_factory=list)  # (release date, calendar actual, store change)

    def as_line(self) -> str:
        return (
            f"matched {self.matched}  no_print {self.no_print}  ambiguous {self.ambiguous}  "
            f"no_forecast {self.no_forecast}  no_actual {self.no_actual}  mismatch {self.mismatch}"
        )


class ConsensusMatch(BaseModel):
    """A consensus forecast accepted for one first print."""

    model_config = ConfigDict(frozen=True)

    forecast_change: Quantity
    calendar_actual_change: Quantity
    print: ConsensusPrint


class ConsensusLookup:
    """Point-in-time consensus forecasts for mapped releases (spec §9.2, §9.8).

    Loads the stored calendar prints for the mapped events, scheduled at or before ``as_of``, and
    indexes them by (release, UTC release date). ``match`` applies the actual cross-check described
    in the module docstring. Stats accumulate per release for the build report.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, config: EventsConfig, as_of: datetime | None = None) -> None:
        if config.consensus is None:
            raise ValueError("events config has no consensus block")
        self._config: ConsensusConfig = config.consensus
        self._releases = {item.series_id: item for item in config.releases}
        prints = ConsensusStore(conn).read(self._config.source_id, self._config.events, as_of)
        by_event: dict[str, str] = {m.event: m.release for m in self._config.mappings}
        self._index: dict[tuple[str, date], list[ConsensusPrint]] = defaultdict(list)
        for item in prints:
            self._index[(by_event[item.event], item.scheduled_at.astimezone(UTC).date())].append(item)
        self.stats: dict[str, MatchStats] = defaultdict(MatchStats)

    @property
    def name(self) -> str:
        return self._config.forecaster_name

    @property
    def source_id(self) -> str:
        return self._config.source_id

    def mapped(self, release_id: str) -> bool:
        return self._config.mapping_for(release_id) is not None

    def prints_loaded(self) -> int:
        return sum(len(items) for items in self._index.values())

    def match(
        self,
        release: ReleaseSpec,
        released: datetime,
        previous: Quantity,
        actual_change: Quantity,
    ) -> ConsensusMatch | None:
        """The consensus forecast for the first print released at ``released``, or ``None`` with the reason counted."""
        mapping = self._config.mapping_for(release.series_id)
        if mapping is None:
            return None
        stats = self.stats[release.series_id]
        release_date = released.astimezone(UTC).date()
        rows = self._index.get((release.series_id, release_date), [])
        if not rows:
            stats.no_print += 1
            return None
        if len(rows) > 1:
            stats.ambiguous += 1
            return None
        row = rows[0]
        if row.forecast is None:
            stats.no_forecast += 1
            return None
        if row.actual is None:
            stats.no_actual += 1
            return None
        provenance = f"{self._config.source_id}:{row.event}"
        calendar_actual = printed_to_change(row.actual, mapping, previous, release.transform, provenance)
        if calendar_actual.unit != actual_change.unit:
            raise ValueError(
                f"{release.series_id}: consensus mapping unit {calendar_actual.unit!r} does not match the "
                f"store's change unit {actual_change.unit!r}; fix config.events.consensus"
            )
        assert calendar_actual.value is not None and actual_change.value is not None
        if abs(calendar_actual.value - actual_change.value) > mapping.tolerance:
            stats.mismatch += 1
            stats.mismatches.append((release_date, calendar_actual.value, actual_change.value))
            return None
        forecast = printed_to_change(row.forecast, mapping, previous, release.transform, provenance)
        stats.matched += 1
        return ConsensusMatch(forecast_change=forecast, calendar_actual_change=calendar_actual, print=row)
