"""World Bank API v2 connector (spec §9.1, §9.2, §9.6).

`GET {base}country/{iso3}/indicator/{id}?format=json&per_page=…&page=…`. A live call on 2026-10-14
for `US` / `NY.GDP.MKTP.CD` returned a two-element JSON array: paging metadata (including
`lastupdated`) and the observation list. `fetch` follows `page` until `pages`. Series ids are
`WORLDBANK:<iso3>:<indicator>`. An empty `unit` on a current-US-dollar indicator is `USD`; other
blank units raise. `lastupdated` is a date with no clock, so `published_at` is that date at 00:00 UTC
and flagged `reconstructed_pubtime`. No API key.
"""

from __future__ import annotations

import json
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
from macrosys.core.units import UnitError, assert_unit
from macrosys.sources.common import fetched_at, sdmx_period, source_url
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"


def _unit(indicator_id: str, unit: str) -> str:
    token = unit.strip()
    if token:
        try:
            assert_unit(token)
        except UnitError as exc:
            raise ValueError(f"World Bank unit {unit!r} has no mapping") from exc
        return token
    if indicator_id.endswith(".CD"):
        return "USD"
    raise ValueError(f"World Bank indicator {indicator_id} has a blank unit and no mapping")


def parse_worldbank(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse one World Bank indicator page (spec §9.1, §9.2)."""
    from macrosys.sources.common import load_json

    doc = load_json(content, what="World Bank")
    if not isinstance(doc, list) or len(doc) != 2 or not isinstance(doc[0], dict) or not isinstance(doc[1], list):
        raise ParserChangedError("World Bank payload is not [metadata, rows]")
    meta, records = doc
    updated = str(meta.get("lastupdated") or "")
    try:
        published = datetime.combine(date.fromisoformat(updated[:10]), time(0), tzinfo=UTC)
    except ValueError as exc:
        raise ParserChangedError(f"World Bank lastupdated {updated!r} is not a date") from exc
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for item in records:
        if not isinstance(item, dict):
            raise ParserChangedError("World Bank row is not an object")
        indicator = item.get("indicator")
        country = item.get("countryiso3code") or (item.get("country") or {}).get("id")
        if not isinstance(indicator, dict) or "id" not in indicator or "date" not in item or not country:
            raise ParserChangedError("World Bank row is missing indicator, country, or date")
        start, end = sdmx_period(str(item["date"]))
        raw = item.get("value")
        rows.append(
            Observation(
                series_id=f"WORLDBANK:{country}:{indicator['id']}",
                ref_period_start=start,
                ref_period_end=end,
                value=None if raw is None else float(raw),
                unit=_unit(str(indicator["id"]), str(item.get("unit") or "")),
                published_at=published,
                ingested_at=ingested,
                vintage_id=updated[:10],
                source_url=url,
                source_hash=payload.sha256,
                quality_flag="reconstructed_pubtime",
            )
        )
    return rows


class WorldBankSource:
    """World Bank API v2 `DataSource` (spec §9.6). No API key."""

    source_id = "WORLDBANK"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None, per_page: int = 1000) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)
        self.per_page = per_page

    def discover(self) -> list[SeriesMeta]:
        """Indicators are named by the caller (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """GET every page of one country indicator (spec §9.6). Returns the last page; all are stored.

        `series_id` is `WORLDBANK:<iso3>:<indicator>`.
        """
        native = series_id.removeprefix("WORLDBANK:")
        iso3, sep, indicator = native.partition(":")
        if not sep:
            raise ValueError(f"World Bank series id {series_id!r} is not WORLDBANK:<iso3>:<indicator>")
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        base = urljoin(self.cfg.access.base_url or "", f"country/{iso3}/indicator/{indicator}")
        page = 1
        last: RawPayload | None = None
        while True:
            params: dict[str, Any] = {"format": "json", "per_page": str(self.per_page), "page": str(page)}
            if since is not None:
                params["date"] = f"{since.year}:{date.today().year}"
            last = client.get(base, params=params)
            meta = json.loads(raw.get(last))[0]
            pages = int(meta.get("pages") or 1)
            if page >= pages:
                return last
            page += 1
        raise RuntimeError("World Bank pagination did not finish")

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse one indicator page. See `parse_worldbank` (spec §9.1)."""
        return parse_worldbank(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """The indicator API has no release calendar (spec §9.1). Returns []."""
        return []
