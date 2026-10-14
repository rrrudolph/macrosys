"""Cboe historical index CSVs (spec §9.1, §9.2, §9.6).

Confirmed 2026-10-14. Files are `{base}{INDEX}_History.csv` with base
`https://cdn.cboe.com/api/global/us_indices/daily_prices/`:

- `VIX_History.csv` columns `DATE,OPEN,HIGH,LOW,CLOSE` (dates `MM/DD/YYYY`)
- `OVX_History.csv` and `GVZ_History.csv` columns `DATE,<INDEX>`

Series ids are `CBOE:<index>:<field>` and the unit is `index`. The file has no clock, so
`published_at` is the end of the trade date in America/Chicago and
`quality_flag='reconstructed_pubtime'`. No API key.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from io import StringIO
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
from macrosys.sources.common import end_of_day, fetched_at, source_url
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"
_CHICAGO = ZoneInfo("America/Chicago")


def parse_cboe_csv(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse a Cboe `_History.csv` file into one observation per field (spec §9.1, §9.2)."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParserChangedError("Cboe CSV is not UTF-8") from exc
    reader = csv.DictReader(StringIO(text))
    fields = list(reader.fieldnames or [])
    if "DATE" not in fields or len(fields) < 2:
        raise ParserChangedError("Cboe CSV is missing DATE or a value column")
    index = str(payload.request.get("index") or "")
    if not index:
        raise ParserChangedError("Cboe request has no index")
    assert_unit("index")
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for record in reader:
        raw_date = (record.get("DATE") or "").strip()
        try:
            day = datetime.strptime(raw_date, "%m/%d/%Y").date()
        except ValueError as exc:
            raise ParserChangedError(f"Cboe DATE {raw_date!r} is not MM/DD/YYYY") from exc
        published = end_of_day(day, _CHICAGO)
        for field in fields:
            if field == "DATE":
                continue
            token = (record.get(field) or "").strip()
            if token == "":
                continue
            rows.append(
                Observation(
                    series_id=f"CBOE:{index}:{field}",
                    ref_period_start=day,
                    ref_period_end=day,
                    value=float(token),
                    unit="index",
                    published_at=published,
                    ingested_at=ingested,
                    vintage_id=payload.sha256,
                    source_url=url,
                    source_hash=payload.sha256,
                    quality_flag="reconstructed_pubtime",
                )
            )
    return rows


class CboeSource:
    """Cboe history CSV `DataSource` (spec §9.6). No API key."""

    source_id = "CBOE"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """The v0 indexes are VIX, OVX, and GVZ (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """Download `{INDEX}_History.csv` (spec §9.6). `since` is unused; the file is complete."""
        index = series_id.removeprefix("CBOE:").split(":")[0].upper()
        if index not in {"VIX", "OVX", "GVZ"}:
            raise ValueError(f"Cboe series {series_id!r} is not VIX, OVX, or GVZ")
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        return client.get(
            urljoin(self.cfg.access.base_url or "", f"{index}_History.csv"),
            extra_request={"index": index},
        )

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse a history CSV. See `parse_cboe_csv` (spec §9.1)."""
        return parse_cboe_csv(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """The history file has no release calendar (spec §9.1). Returns []."""
        return []
