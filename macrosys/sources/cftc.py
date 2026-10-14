"""CFTC Commitments of Traders via Socrata (spec §9.1, §9.2, §9.6).

Dataset ids and the Friday 15:30 America/New_York schedule are read from the registry, not hard-coded.
Pages use `$limit`, `$offset`, and `$where report_date_as_yyyy_mm_dd > …`. The optional app token is
env `SOCRATA_APP_TOKEN` (header `X-App-Token`).

Each numeric field on a row becomes an observation. Series ids are
`CFTC:<family>:<cftc_contract_market_code>:<field>`. `published_at` is the Friday 15:30
America/New_York on or after the Tuesday report date. Holidays move the real release, so the flag is
always `reconstructed_pubtime`. Env: `SOCRATA_APP_TOKEN` (optional).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

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
from macrosys.sources.common import fetched_at, load_json, source_url
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"

# [J] Unauthenticated SODA pages are 1_000 rows. The registry does not set a page size.
PAGE_SIZE = 1000

_FAMILY = {
    "legacy_futures": "LEGACY",
    "legacy_combined": "LEGACY",
    "disagg_futures": "DISAGG",
    "disagg_combined": "DISAGG",
    "tff_futures": "TFF",
    "tff_combined": "TFF",
    "cit": "CIT",
}

_SKIP = {
    "id",
    "market_and_exchange_names",
    "report_date_as_yyyy_mm_dd",
    "yyyy_report_week_ww",
    "contract_market_name",
    "cftc_contract_market_code",
    "cftc_market_code",
    "cftc_region_code",
    "cftc_commodity_code",
    "commodity_name",
    "commodity",
    "commodity_subgroup_name",
    "contract_units",
    "cftc_subgroup_code",
    "futonly_or_combined",
}

_WEEKDAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def cot_published_at(report_date: date) -> datetime:
    """Friday 15:30 America/New_York on or after `report_date` (spec §9.2, §9.6).

    The registry schedule is Friday 15:30 for Tuesday data. A US holiday can move the release, so
    callers flag the result `reconstructed_pubtime`.
    """
    friday = report_date + timedelta(days=(4 - report_date.weekday()) % 7)
    local = datetime.combine(friday, time(15, 30), tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(UTC)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _family(payload: RawPayload) -> str:
    cfg = require_source("CFTC")
    datasets = cfg.datasets or {}
    key = payload.request.get("dataset_key")
    if isinstance(key, str) and key:
        if key not in datasets:
            raise ParserChangedError(f"CFTC dataset_key {key!r} is not in the registry")
        try:
            return _FAMILY[key]
        except KeyError as exc:
            raise ParserChangedError(f"CFTC dataset_key {key!r} has no series-family mapping") from exc
    url = str(payload.request.get("url") or "")
    for name, dataset_id in datasets.items():
        if dataset_id and dataset_id in url:
            try:
                return _FAMILY[name]
            except KeyError as exc:
                raise ParserChangedError(f"CFTC dataset {name!r} has no series-family mapping") from exc
    raise ParserChangedError("CFTC payload does not identify a registry dataset")


def _report_date(value: Any) -> date:
    text = str(value).strip()
    if len(text) < 10:
        raise ParserChangedError(f"CFTC report date {value!r} is not a date")
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ParserChangedError(f"CFTC report date {value!r} is not a date") from exc


def parse_socrata(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse one Socrata page into position observations (spec §9.1, §9.2)."""
    doc = load_json(content, what="CFTC")
    if not isinstance(doc, list):
        raise ParserChangedError("CFTC Socrata payload is not a JSON list")
    family = _family(payload)
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for record in doc:
        if not isinstance(record, dict):
            raise ParserChangedError("CFTC row is not an object")
        if "report_date_as_yyyy_mm_dd" not in record or "cftc_contract_market_code" not in record:
            raise ParserChangedError("CFTC row is missing report_date_as_yyyy_mm_dd or cftc_contract_market_code")
        report = _report_date(record["report_date_as_yyyy_mm_dd"])
        code = str(record["cftc_contract_market_code"]).strip()
        if not code:
            raise ParserChangedError("CFTC cftc_contract_market_code is blank")
        published = cot_published_at(report)
        for field, raw in record.items():
            if field in _SKIP:
                continue
            value = _number(raw)
            if value is None:
                continue
            unit = "pct" if "pct" in field else "count"
            assert_unit(unit)
            rows.append(
                Observation(
                    series_id=f"CFTC:{family}:{code}:{field}",
                    ref_period_start=report,
                    ref_period_end=report,
                    value=value,
                    unit=unit,
                    published_at=published,
                    ingested_at=ingested,
                    vintage_id=payload.sha256,
                    source_url=url,
                    source_hash=payload.sha256,
                    quality_flag="reconstructed_pubtime",
                )
            )
    return rows


def _next_scheduled(weekday: str, time_local: str, zone_name: str, today: date) -> datetime:
    try:
        target = _WEEKDAYS[weekday[:3].title()]
    except KeyError as exc:
        raise ValueError(f"CFTC schedule weekday {weekday!r} is not a day name") from exc
    hour, minute = (int(part) for part in time_local.split(":", 1))
    delta = (target - today.weekday()) % 7
    day = today + timedelta(days=delta)
    local = datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo(zone_name))
    return local.astimezone(UTC)


class CftcSource:
    """CFTC COT `DataSource` (spec §9.6). Env: `SOCRATA_APP_TOKEN` (optional)."""

    source_id = "CFTC"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """The COT catalogue is the weekly file itself (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """Download the dataset named by `series_id` (a registry dataset key) (spec §9.6).

        A series id `CFTC:TFF:...` selects `tff_futures`. Combined and other cuts are selected by
        passing the dataset key (`tff_combined`, `disagg_futures`, …). Returns the last page; every
        page is stored.
        """
        dataset_key = _dataset_key(series_id, self.cfg.datasets or {})
        pages = self.fetch_dataset(dataset_key, since, raw)
        if not pages:
            raise RuntimeError(f"CFTC dataset {dataset_key} returned no pages")
        return pages[-1]

    def fetch_dataset(
        self,
        dataset_key: str,
        since: date | None,
        raw: RawStore,
        *,
        page_size: int = PAGE_SIZE,
    ) -> list[RawPayload]:
        """Page a Socrata dataset with `$limit` / `$offset` / `$where` (spec §9.6)."""
        datasets = self.cfg.datasets or {}
        if dataset_key not in datasets:
            raise KeyError(f"CFTC dataset {dataset_key!r} is not in the registry")
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        base = self.cfg.access.base_url or ""
        url = urljoin(base, f"{datasets[dataset_key]}.json")
        offset = 0
        pages: list[RawPayload] = []
        while True:
            params: dict[str, Any] = {
                "$limit": page_size,
                "$offset": offset,
                "$order": "report_date_as_yyyy_mm_dd",
            }
            if since is not None:
                params["$where"] = f"report_date_as_yyyy_mm_dd > '{since.isoformat()}'"
            payload = client.get(url, params=params, extra_request={"dataset_key": dataset_key})
            pages.append(payload)
            body = load_json(raw.get(payload), what="CFTC")
            if not isinstance(body, list) or len(body) < page_size:
                return pages
            offset += page_size

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse one Socrata page. See `parse_socrata` (spec §9.1)."""
        return parse_socrata(payload, content)

    def schedule(self, *, today: date | None = None) -> list[ReleaseEvent]:
        """Next registry publication slot for the COT report (spec §9.6)."""
        sched = self.cfg.schedule
        if sched is None:
            return []
        when = _next_scheduled(sched.weekday, sched.time_local, sched.tz, today or date.today())
        return [ReleaseEvent(release_id="CFTC:COT", scheduled_at=when, source_id=self.source_id)]


def _dataset_key(series_id: str, datasets: dict[str, str]) -> str:
    if series_id in datasets:
        return series_id
    if series_id.startswith("CFTC:TFF:"):
        return "tff_futures"
    if series_id.startswith("CFTC:DISAGG:"):
        return "disagg_futures"
    if series_id.startswith("CFTC:LEGACY:"):
        return "legacy_futures"
    if series_id.startswith("CFTC:CIT:"):
        return "cit"
    raise ValueError(f"CFTC series id {series_id!r} does not name a registry dataset")
