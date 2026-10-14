"""FRED / ALFRED connector (spec §9.1, §9.2, §9.6).

Observations come from `series/observations` with `realtime_start` / `realtime_end`. Vintage dates come
from `series/vintagedates`. Release rows come from `releases/dates`. The API key is env `FRED_API_KEY`
(registry auth `query:api_key`). The observations JSON does not carry the series unit text, so `fetch`
copies `units` and `frequency_short` from the `series` endpoint onto the stored request; `parse` reads
them from there and is still a function of that stored payload.

`vintage_id` is `realtime_start`. `published_at` is a release datetime when the stored request's
`release_times` map supplies a clock time for that vintage; otherwise it is `realtime_start` at 00:00 UTC
with `quality_flag='reconstructed_pubtime'`. FRED's release calendar itself is date-only (verified
2026-10-14). Env: `FRED_API_KEY`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
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
from macrosys.sources.common import (
    fetched_at,
    fred_period,
    load_json,
    require_keys,
    require_mapping,
    source_url,
)
from macrosys.sources.registry import HttpClient, SourceConfig, require_source

PARSER_VERSION = "1"

# Explicit map. An unknown FRED `units` string raises; it is never guessed (spec §9.1).
FRED_UNIT_MAP: dict[str, str] = {
    "Billions of Chained 2017 Dollars": "USD_bn_real2017",
    "Billions of Chained 2012 Dollars": "USD_bn_real2012",
    "Billions of Dollars": "USD_bn",
    "Millions of Dollars": "USD_mn",
    "Thousands of Dollars": "USD_k",
    "Dollars": "USD",
    "Percent": "pct",
    "Percent Change": "pct",
    "Percent Change from Year Ago": "pct",
    "Percent Change at Annual Rate": "pct",
    "Index": "index",
    "Index 1982-1984=100": "index",
    "Index 2017=100": "index",
    "Basis Points": "bp",
}

_FREQ = {
    "D": "D",
    "W": "W",
    "M": "M",
    "Q": "Q",
    "A": "A",
    "Daily": "D",
    "Weekly": "W",
    "Monthly": "M",
    "Quarterly": "Q",
    "Annual": "A",
}


def map_fred_unit(text: str) -> str:
    """Map a FRED `units` label onto the controlled vocabulary, or raise (spec §9.1)."""
    try:
        label = FRED_UNIT_MAP[text]
    except KeyError as exc:
        raise ValueError(
            f"FRED unit {text!r} has no mapping in FRED_UNIT_MAP (parser {PARSER_VERSION}); "
            "add an explicit entry"
        ) from exc
    assert_unit(label)
    return label


def _native_id(series_id: str) -> str:
    return series_id.removeprefix("FRED:")


def _frequency(text: str) -> str:
    try:
        return _FREQ[text]
    except KeyError as exc:
        raise ValueError(f"FRED frequency {text!r} has no mapping in parser {PARSER_VERSION}") from exc


def _published(vintage: date, release_times: dict[str, Any]) -> tuple[datetime, str]:
    raw = release_times.get(vintage.isoformat())
    if isinstance(raw, str) and raw:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
        if (parsed.hour, parsed.minute, parsed.second, parsed.microsecond) != (0, 0, 0, 0):
            return parsed, "ok"
    return datetime.combine(vintage, time(0), tzinfo=UTC), "reconstructed_pubtime"


def parse_observations(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse a FRED `series/observations` body into one row per vintage (spec §9.1, §9.2).

    A missing value (FRED's `.`) is stored as None, never as zero. `series_units` and `frequency`
    must already be on `payload.request` from the companion `series` call.
    """
    doc = require_mapping(load_json(content, what="FRED observations"), what="FRED observations")
    require_keys(doc, ("observations",), what="FRED observations")
    if not isinstance(doc["observations"], list):
        raise ParserChangedError("FRED observations is not a list")
    request = payload.request
    unit_text = request.get("series_units")
    if not isinstance(unit_text, str) or not unit_text:
        raise ValueError("FRED parse needs request.series_units from the series endpoint")
    unit = map_fred_unit(unit_text)
    frequency_text = request.get("frequency")
    if not isinstance(frequency_text, str) or not frequency_text:
        raise ValueError("FRED parse needs request.frequency from the series endpoint")
    frequency = _frequency(frequency_text)
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    native = request.get("native_id") or params.get("series_id")
    if not isinstance(native, str) or not native:
        raise ParserChangedError("FRED observations request has no series_id")
    series_id = native if native.startswith("FRED:") else f"FRED:{native}"
    release_times = request.get("release_times") if isinstance(request.get("release_times"), dict) else {}
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for item in doc["observations"]:
        if not isinstance(item, dict):
            raise ParserChangedError("FRED observation row is not an object")
        require_keys(item, ("realtime_start", "realtime_end", "date", "value"), what="FRED observation")
        vintage = date.fromisoformat(str(item["realtime_start"])[:10])
        obs_date = date.fromisoformat(str(item["date"])[:10])
        start, end = fred_period(obs_date, frequency)
        raw_value = str(item["value"])
        value = None if raw_value in {".", ""} else float(raw_value)
        published, flag = _published(vintage, release_times)
        rows.append(
            Observation(
                series_id=series_id,
                ref_period_start=start,
                ref_period_end=end,
                value=value,
                unit=unit,
                published_at=published,
                ingested_at=ingested,
                vintage_id=vintage.isoformat(),
                source_url=url,
                source_hash=payload.sha256,
                quality_flag=flag,  # type: ignore[arg-type]
            )
        )
    return rows


def parse_vintage_dates(content: bytes) -> list[date]:
    """Parse `series/vintagedates` into vintage dates (spec §9.2 ALFRED)."""
    doc = require_mapping(load_json(content, what="FRED vintage dates"), what="FRED vintage dates")
    require_keys(doc, ("vintage_dates",), what="FRED vintage dates")
    if not isinstance(doc["vintage_dates"], list):
        raise ParserChangedError("FRED vintage_dates is not a list")
    return [date.fromisoformat(str(item)[:10]) for item in doc["vintage_dates"]]


def parse_release_dates(payload: RawPayload, content: bytes) -> list[ReleaseEvent]:
    """Parse `releases/dates` into release events (spec §9.1).

    The verified payload has a date and no clock time, so `scheduled_at` is that date at 00:00 UTC.
    A `datetime` field, when a caller has merged an agency clock time, is used instead.
    """
    doc = require_mapping(load_json(content, what="FRED release dates"), what="FRED release dates")
    require_keys(doc, ("release_dates",), what="FRED release dates")
    if not isinstance(doc["release_dates"], list):
        raise ParserChangedError("FRED release_dates is not a list")
    events: list[ReleaseEvent] = []
    for item in doc["release_dates"]:
        if not isinstance(item, dict):
            raise ParserChangedError("FRED release date row is not an object")
        require_keys(item, ("release_id", "date"), what="FRED release date")
        clock = item.get("datetime")
        if isinstance(clock, str) and clock:
            scheduled = datetime.fromisoformat(clock.replace("Z", "+00:00"))
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=UTC)
            scheduled = scheduled.astimezone(UTC)
        else:
            scheduled = datetime.combine(date.fromisoformat(str(item["date"])[:10]), time(0), tzinfo=UTC)
        events.append(
            ReleaseEvent(
                release_id=str(item["release_id"]),
                scheduled_at=scheduled,
                actual_at=None,
                source_id="FRED",
            )
        )
    return events


def parse_series(content: bytes) -> list[SeriesMeta]:
    """Parse a FRED `series` response into series metadata (spec §9.1, §9.6)."""
    doc = require_mapping(load_json(content, what="FRED series"), what="FRED series")
    require_keys(doc, ("seriess",), what="FRED series")
    if not isinstance(doc["seriess"], list):
        raise ParserChangedError("FRED seriess is not a list")
    rows: list[SeriesMeta] = []
    for item in doc["seriess"]:
        if not isinstance(item, dict):
            raise ParserChangedError("FRED series row is not an object")
        require_keys(item, ("id", "title", "units", "frequency_short"), what="FRED series")
        native = str(item["id"])
        rows.append(
            SeriesMeta(
                series_id=f"FRED:{native}",
                source_id="FRED",
                native_id=native,
                title=str(item["title"]),
                unit=map_fred_unit(str(item["units"])),
                frequency=_frequency(str(item["frequency_short"])),  # type: ignore[arg-type]
                seasonal_adj=item.get("seasonal_adjustment_short"),
                tier="record",
            )
        )
    return rows


class FredSource:
    """FRED / ALFRED `DataSource` (spec §9.6). Env: `FRED_API_KEY`."""

    source_id = "FRED"
    PARSER_VERSION = PARSER_VERSION

    def __init__(
        self,
        raw: RawStore,
        *,
        transport: Any = None,
        sleeper: Any = None,
        series_ids: list[str] | None = None,
        client: HttpClient | None = None,
    ) -> None:
        self.cfg: SourceConfig = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.series_ids = list(series_ids or [])
        self.client = client or HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def _url(self, path: str) -> str:
        base = self.cfg.access.base_url or ""
        return urljoin(base, path)

    def discover(self) -> list[SeriesMeta]:
        """Fetch `series` metadata for the ids configured on this instance (spec §9.6)."""
        found: list[SeriesMeta] = []
        for series_id in self.series_ids:
            payload = self.client.get(
                self._url("series"),
                params={"series_id": _native_id(series_id), "file_type": "json"},
            )
            found.extend(parse_series(self.raw.get(payload)))
        return found

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """Download vintaged observations for one series and remember its unit (spec §9.1, §9.2)."""
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        native = _native_id(series_id)
        meta_payload = client.get(self._url("series"), params={"series_id": native, "file_type": "json"})
        meta = parse_series(raw.get(meta_payload))
        if not meta:
            raise ParserChangedError(f"FRED series {native} returned no seriess row")
        info = meta[0]
        params: dict[str, Any] = {
            "series_id": native,
            "file_type": "json",
            "realtime_start": "1776-07-04",
            "realtime_end": "9999-12-31",
        }
        if since is not None:
            params["observation_start"] = since.isoformat()
        return client.get(
            self._url("series/observations"),
            params=params,
            extra_request={
                "native_id": native,
                "series_units": _unit_text_from_meta(raw.get(meta_payload)),
                "frequency": info.frequency,
                "title": info.title,
                "seasonal_adj": info.seasonal_adj,
            },
        )

    def fetch_vintage_dates(self, series_id: str) -> RawPayload:
        """Download `series/vintagedates` for one series (spec §9.2)."""
        return self.client.get(
            self._url("series/vintagedates"),
            params={"series_id": _native_id(series_id), "file_type": "json"},
        )

    def fetch_release_dates(self) -> RawPayload:
        """Download `releases/dates` (spec §9.1)."""
        return self.client.get(self._url("releases/dates"), params={"file_type": "json"})

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse observations. See `parse_observations` (spec §9.1)."""
        return parse_observations(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """Fetch and parse the FRED release calendar (spec §9.1)."""
        payload = self.fetch_release_dates()
        return parse_release_dates(payload, self.raw.get(payload))


def _unit_text_from_meta(content: bytes) -> str:
    doc = require_mapping(load_json(content, what="FRED series"), what="FRED series")
    require_keys(doc, ("seriess",), what="FRED series")
    row = doc["seriess"][0]
    if not isinstance(row, dict) or "units" not in row:
        raise ParserChangedError("FRED series row has no units")
    return str(row["units"])
