"""BLS Public Data API v2 (spec §9.1, §9.2, §9.6).

`fetch_many` POSTs to `timeseries/data/` with `seriesid`, `startyear`, `endyear`, and
`registrationkey` from env `BLS_API_KEY` (registry auth `body:registrationkey`). The registry caps
are `series_per_query` (50) and `years_per_query` (20); requests are chunked to those sizes. The
daily and burst limits are the HTTP client's token bucket.

The timeseries JSON has no publication clock. `published_at` is a datetime from
`request["release_times"]` keyed by `seriesID|year|period` when that map carries a clock time;
otherwise it is `fetched_at` with `quality_flag='reconstructed_pubtime'`. The payload also has no
unit, so `request["series_units"]` must map each series id to a controlled-vocabulary label.
Series ids are `BLS:<id>`. Env: `BLS_API_KEY`.
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin

from macrosys.core.contracts import (
    Observation,
    ParserChangedError,
    RawPayload,
    RawStore,
    ReleaseEvent,
    SeriesMeta,
    SourceTier,
)
from macrosys.core.units import assert_unit
from macrosys.sources.common import fetched_at, parse_instant, source_url
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"


def _chunks(items: list[str], size: int) -> list[list[str]]:
    if size < 1:
        raise ValueError("BLS chunk size must be positive")
    return [items[index: index + size] for index in range(0, len(items), size)] or [[]]


def _year_windows(start: int, end: int, width: int) -> list[tuple[int, int]]:
    if width < 1:
        raise ValueError("BLS year window must be positive")
    windows: list[tuple[int, int]] = []
    year = start
    while year <= end:
        stop = min(end, year + width - 1)
        windows.append((year, stop))
        year = stop + 1
    return windows


def _period(year: int, period: str) -> tuple[date, date, str]:
    if period.startswith("M") and period[1:].isdigit():
        month = int(period[1:])
        if month == 13:
            return date(year, 1, 1), date(year, 12, 31), "A"
        if 1 <= month <= 12:
            start = date(year, month, 1)
            return start, date(year, month, calendar.monthrange(year, month)[1]), "M"
    if period.startswith("Q") and period[1:].isdigit():
        quarter = int(period[1:])
        if 1 <= quarter <= 4:
            start_month = 3 * (quarter - 1) + 1
            end_month = start_month + 2
            start = date(year, start_month, 1)
            return start, date(year, end_month, calendar.monthrange(year, end_month)[1]), "Q"
    if period in {"A01", "A00"}:
        return date(year, 1, 1), date(year, 12, 31), "A"
    raise ParserChangedError(f"BLS period {period!r} is not a monthly, quarterly, or annual code")


def parse_timeseries(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse a BLS v2 timeseries JSON body into observations (spec §9.1, §9.2)."""
    from macrosys.sources.common import load_json, require_mapping

    doc = require_mapping(load_json(content, what="BLS"), what="BLS")
    results = doc.get("Results")
    if not isinstance(results, dict) or not isinstance(results.get("series"), list):
        raise ParserChangedError("BLS payload is missing Results.series")
    units = payload.request.get("series_units")
    if not isinstance(units, dict):
        raise ValueError("BLS parse needs request.series_units; the timeseries JSON has no unit")
    release_times = payload.request.get("release_times") if isinstance(payload.request.get("release_times"), dict) else {}
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for series in results["series"]:
        if not isinstance(series, dict) or "seriesID" not in series or not isinstance(series.get("data"), list):
            raise ParserChangedError("BLS series entry is missing seriesID or data")
        native = str(series["seriesID"])
        unit_label = units.get(native)
        if not isinstance(unit_label, str):
            raise ValueError(f"BLS series {native} has no unit in request.series_units")
        unit = assert_unit(unit_label).label
        for item in series["data"]:
            if not isinstance(item, dict):
                raise ParserChangedError("BLS data row is not an object")
            for key in ("year", "period", "value"):
                if key not in item:
                    raise ParserChangedError(f"BLS data row is missing {key}")
            year = int(item["year"])
            start, end, _freq = _period(year, str(item["period"]))
            raw_value = str(item["value"]).strip()
            value = None if raw_value in {"", "-"} else float(raw_value)
            stamp = release_times.get(f"{native}|{item['year']}|{item['period']}")
            published = parse_instant(stamp) if isinstance(stamp, str) else None
            flag = "ok" if published is not None else "reconstructed_pubtime"
            rows.append(
                Observation(
                    series_id=f"BLS:{native}",
                    ref_period_start=start,
                    ref_period_end=end,
                    value=value,
                    unit=unit,
                    published_at=published or ingested,
                    ingested_at=ingested,
                    vintage_id=payload.sha256,
                    source_url=url,
                    source_hash=payload.sha256,
                    quality_flag=flag,  # type: ignore[arg-type]
                )
            )
    return rows


class BlsSource:
    """BLS API v2 `DataSource` (spec §9.6). Env: `BLS_API_KEY`."""

    source_id = "BLS"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """BLS series are named by the caller (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """Download one series from `since.year` through the current year (spec §9.6)."""
        start = since.year if since is not None else datetime.now(UTC).year - 1
        pages = self.fetch_many([series_id.removeprefix("BLS:")], start, datetime.now(UTC).year, raw)
        return pages[-1]

    def fetch_many(self, series_ids: list[str], start_year: int, end_year: int, raw: RawStore) -> list[RawPayload]:
        """POST chunked timeseries requests honouring the registry series and year caps (spec §9.6)."""
        limit = self.cfg.rate_limit
        series_cap = (limit.series_per_query if limit and limit.series_per_query else 50)
        year_cap = limit.years_per_query if limit and limit.years_per_query else 20
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        base = self.cfg.access.base_url or ""
        url = urljoin(base, "timeseries/data/")
        pages: list[RawPayload] = []
        natives = [series.removeprefix("BLS:") for series in series_ids]
        for group in _chunks(natives, series_cap):
            for start, stop in _year_windows(start_year, end_year, year_cap):
                pages.append(
                    client.post(
                        url,
                        json_body={"seriesid": group, "startyear": str(start), "endyear": str(stop)},
                        extra_request={"series_units": {}},
                    )
                )
        return pages

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse a timeseries response. See `parse_timeseries` (spec §9.1)."""
        return parse_timeseries(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """The timeseries response is not the BLS release calendar (spec §9.1). Returns []."""
        return []
