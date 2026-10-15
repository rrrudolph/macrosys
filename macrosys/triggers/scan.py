"""Evaluate trigger rules at a decision instant and over history (spec §4.1, §11.2, §9.2).

``scan`` reads each rule's series as it was visible at ``as_of`` — prices by trade date at or
before the day, observations through ``snapshot(as_of)`` — computes the signal, and returns a
``Fired`` record with a ``Shock`` for every rule whose crossing is fresh and armed. ``replay`` runs
the same scan on a weekly clock across a date range, which is how a history of data-filed shocks is
produced without look-ahead.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

import duckdb
import pandas as pd
from pydantic import BaseModel, ConfigDict

from macrosys.core.quantity import Quantity
from macrosys.core.units import assert_unit, compatible
from macrosys.decide.shock import Shock
from macrosys.store.observations import ObservationStore
from macrosys.store.prices import PriceStore
from macrosys.triggers.rules import SeriesRef, TriggerConfig, TriggerRule
from macrosys.triggers.signals import compute_signal, crossing

# [J] Extra calendar days read beyond the signal's own needs, so holidays and weekly prints do not
# leave the rearm window short.
_HISTORY_PAD_DAYS = 60


class Fired(BaseModel):
    """One rule that fired at ``as_of``. ``value`` is the signal in ``unit`` at the last observation."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    series: str
    as_of: date
    crossed: date
    last_date: date
    value: float
    unit: str
    shock: Shock

    def sentence(self) -> str:
        """Plain sentence for the week note."""
        return (
            f"Trigger {self.rule_id} fired: {self.series} reads {self.value:.4g} {self.unit} "
            f"(crossed {self.crossed.isoformat()}, last print {self.last_date.isoformat()}); "
            f"shock {self.shock.id} is filed."
        )


class ReadSeries(BaseModel):
    """One series as visible at an instant: values by reference date, when each became known, unit.

    ``published`` is aligned with ``values`` and is ``None`` for an exchange close, which is known on
    its own date. ``unit`` is ``None`` when the source carries none (prices).
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    values: pd.Series
    published: pd.Series | None = None
    unit: str | None = None

    @property
    def empty(self) -> bool:
        return self.values.empty


class SeriesReader(Protocol):
    """Point-in-time access to one series (spec §9.2)."""

    def read(self, ref: SeriesRef, start: date, as_of: datetime) -> ReadSeries: ...


class StoreReader:
    """Read prices and observations from the DuckDB store as of an instant (spec §9.2, §9.6).

    Observations come through ``snapshot(as_of)`` with their ``published_at``, so a print that
    reaches the store a week after its reference date is fresh on the day it was published.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, price_id: Callable[[str], str]) -> None:
        self._conn = conn
        self._price_id = price_id

    def read(self, ref: SeriesRef, start: date, as_of: datetime) -> ReadSeries:
        end = as_of.date()
        if ref.kind == "price":
            assert ref.instrument is not None
            store_id = self._price_id(ref.instrument)
            closes = PriceStore(self._conn).read_closes([store_id], start, end)
            if closes.empty:
                return ReadSeries(values=pd.Series(dtype=float))
            return ReadSeries(values=closes[store_id].dropna())
        assert ref.series_id is not None
        frame = ObservationStore(self._conn).snapshot(as_of).get(ref.series_id, start=start, end=end)
        frame = frame.dropna(subset=["value"])
        if frame.empty:
            return ReadSeries(values=pd.Series(dtype=float))
        index = pd.to_datetime(frame["ref_period_end"])
        values = pd.Series(frame["value"].to_numpy(dtype=float), index=index)
        published = pd.Series(
            pd.to_datetime(frame["published_at"], utc=True).dt.tz_localize(None).to_numpy(), index=index
        )
        return ReadSeries(values=values, published=published, unit=str(frame["unit"].iloc[-1]))


def scan(reader: SeriesReader, config: TriggerConfig, as_of: datetime) -> list[Fired]:
    """Rules that fire at ``as_of`` (spec §4.1, owner decision 2026-10-15).

    ``as_of`` is timezone-aware and becomes each shock's ``information_cutoff``. The shock id is
    ``<rule id>-<as_of date>``, its size is the signal value in the threshold unit, its start is the
    crossing date, and its evidence reference names the rule, the series, and the crossing. A series
    with no visible data does not fire and does not raise; a threshold unit that does not fit the
    stored series unit raises ``ValueError`` because the rule is misconfigured, not the data.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    day = as_of.date()
    fired: list[Fired] = []
    for rule in config.rules:
        read = reader.read(rule.series, day - timedelta(days=_history_days(rule, config)), as_of)
        if read.empty:
            continue
        _check_unit(rule, read.unit)
        signal = compute_signal(read.values, rule.signal)
        hit = crossing(
            signal,
            rule.threshold,
            day,
            fresh_days=config.fresh_days,
            rearm_days=config.rearm_days,
            stale_days=config.stale_days,
            published=read.published,
        )
        if hit is None:
            continue
        fired.append(
            Fired(
                rule_id=rule.id,
                series=rule.series.label,
                as_of=day,
                crossed=hit.crossed,
                last_date=hit.last_date,
                value=hit.value,
                unit=rule.threshold.unit,
                shock=_shock(rule, hit.value, hit.crossed, as_of),
            )
        )
    return fired


def replay(
    reader: SeriesReader,
    config: TriggerConfig,
    start: date,
    end: date,
    *,
    step_days: int = 7,
    cutoff: time = time(23, 59, 59),
) -> Iterator[tuple[date, list[Fired]]]:
    """Scan every ``step_days`` from ``start`` to ``end`` inclusive, each at that day's cutoff (UTC).

    Yields ``(as_of_date, fired)`` in order. Each scan reads only what was visible at its own
    instant, so the sequence is a point-in-time history of data-filed shocks (spec §9.2, §10.2).
    """
    if step_days < 1:
        raise ValueError("step_days must be at least 1")
    if end < start:
        raise ValueError("end is before start")
    day = start
    while day <= end:
        as_of = datetime.combine(day, cutoff, tzinfo=UTC)
        yield day, scan(reader, config, as_of)
        day += timedelta(days=step_days)


def write_replay(path: str | Path, runs: Iterator[tuple[date, list[Fired]]]) -> dict[str, int]:
    """Write one JSON line per fired rule to ``path`` and return fire counts by rule id."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        for _day, fired in runs:
            for item in fired:
                counts[item.rule_id] = counts.get(item.rule_id, 0) + 1
                handle.write(
                    json.dumps(item.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) + "\n"
                )
    return counts


def _history_days(rule: TriggerRule, config: TriggerConfig) -> int:
    lookback = rule.signal.lookback_days or 0
    return lookback + config.rearm_days + config.fresh_days + _HISTORY_PAD_DAYS


def _check_unit(rule: TriggerRule, series_unit: str | None) -> None:
    if series_unit is None:
        return
    kind = rule.signal.kind
    want = rule.threshold.unit
    if kind == "level" and want != series_unit:
        raise ValueError(f"{rule.id}: level threshold is {want!r} but the series is {series_unit!r}")
    if kind == "change":
        stored = assert_unit(series_unit)
        if stored.dimension == "rate_level":
            if not compatible(want, "pp"):
                raise ValueError(f"{rule.id}: a change in a {series_unit} series is pp or bp, not {want!r}")
        elif want != series_unit:
            raise ValueError(f"{rule.id}: change threshold is {want!r} but the series is {series_unit!r}")


def _shock(rule: TriggerRule, value: float, crossed: date, as_of: datetime) -> Shock:
    template = rule.shock
    return Shock(
        id=f"{rule.id}-{as_of.date().isoformat()}",
        type=template.type,
        subtype=template.subtype,
        origin=template.origin,
        size=Quantity(value=value, unit=rule.threshold.unit, provenance=f"trigger:{rule.id}"),
        start=crossed,
        stage=template.stage,
        certainty=template.certainty,
        evidence_refs=[f"trigger:{rule.id}:{rule.series.label}:{crossed.isoformat()}"],
        information_cutoff=as_of,
    )
