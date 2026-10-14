"""IMF SDMX 3.0 connector (spec §9.1, §9.2, §9.6).

`GET {base}data/dataflow/{agency}/{dataflow}/+/{key}` with `c[TIME_PERIOD]`. A live call on
2026-10-14 to `IMF.STA/CPI/+/USA.CPI.CP01.IX.M` returned CSV when `Accept` was
`application/vnd.sdmx.data+csv;version=1.0.0` (columns include `STRUCTURE_ID`, `TIME_PERIOD`,
`OBS_VALUE`, `COUNTRY`, `INDEX_TYPE`, `COICOP_1999`, `TYPE_OF_TRANSFORMATION`, `FREQUENCY`,
`UNIT`, `SCALE`) and SDMX-JSON otherwise. This parser reads the CSV. The registry limit of
10 requests / 5 seconds is the HTTP token bucket.

Series ids are `IMF:<dataflow>:<key>`. `2024-M01` periods are calendar months. An empty `UNIT`
with transformation `IX` is an index. There is no publication clock, so `published_at` is
`fetched_at` and flagged `reconstructed_pubtime`. No API key.
"""

from __future__ import annotations

import csv
from datetime import date
from io import StringIO
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
_DIMS = ("COUNTRY", "INDEX_TYPE", "COICOP_1999", "TYPE_OF_TRANSFORMATION", "FREQUENCY")


def _time_token(token: str) -> str:
    text = token.strip()
    if len(text) == 8 and text[4:6] == "-M":
        return f"{text[:4]}-{text[6:]}"
    return text


def _unit(row: dict[str, str]) -> str:
    unit = (row.get("UNIT") or "").strip().upper()
    scale = (row.get("SCALE") or "").strip()
    if unit in {"", "IX"} or (row.get("TYPE_OF_TRANSFORMATION") or "").upper() == "IX":
        label = "index"
    elif unit in {"PC", "PCT", "PT"}:
        label = "pct"
    else:
        suffix = {"": "", "0": "", "3": "_k", "6": "_mn", "9": "_bn"}.get(scale)
        if suffix is None:
            raise ValueError(f"IMF SCALE {scale!r} has no mapping")
        label = f"{unit}{suffix}" if suffix else unit
    try:
        assert_unit(label)
    except UnitError as exc:
        raise ValueError(f"IMF unit {unit!r} has no mapping") from exc
    return label


def parse_imf_csv(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse an IMF SDMX 3.0 CSV body into observations (spec §9.1, §9.2)."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParserChangedError("IMF CSV is not UTF-8") from exc
    reader = csv.DictReader(StringIO(text))
    fields = list(reader.fieldnames or [])
    missing = [name for name in ("TIME_PERIOD", "OBS_VALUE", "STRUCTURE_ID", *_DIMS) if name not in fields]
    if missing:
        raise ParserChangedError(f"IMF CSV is missing {missing}")
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for record in reader:
        structure = record.get("STRUCTURE_ID") or ""
        dataflow = structure.split(":")[-1].split("(")[0]
        if not dataflow:
            raise ParserChangedError("IMF STRUCTURE_ID has no dataflow")
        if any(not (record.get(name) or "").strip() for name in _DIMS):
            raise ParserChangedError("IMF row is missing a key dimension")
        key = ".".join(record[name].strip() for name in _DIMS)
        start, end = sdmx_period(_time_token(record["TIME_PERIOD"]))
        raw = (record.get("OBS_VALUE") or "").strip()
        rows.append(
            Observation(
                series_id=f"IMF:{dataflow}:{key}",
                ref_period_start=start,
                ref_period_end=end,
                value=None if raw == "" else float(raw),
                unit=_unit(record),
                published_at=ingested,
                ingested_at=ingested,
                vintage_id=payload.sha256,
                source_url=url,
                source_hash=payload.sha256,
                quality_flag="reconstructed_pubtime",
            )
        )
    return rows


class ImfSource:
    """IMF SDMX 3.0 `DataSource` (spec §9.6). No API key."""

    source_id = "IMF"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """IMF dataflows are named by the caller (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """GET one dataflow key as CSV (spec §9.6). `series_id` is `IMF:<dataflow>:<key>`."""
        native = series_id.removeprefix("IMF:")
        dataflow, sep, key = native.partition(":")
        if not sep or not key:
            raise ValueError(f"IMF series id {series_id!r} is not IMF:<dataflow>:<key>")
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        params: dict[str, Any] = {}
        if since is not None:
            params["c[TIME_PERIOD]"] = f"ge:{since.isoformat()}"
        agency = "IMF.STA"
        path = f"data/dataflow/{agency}/{dataflow}/+/{key}"
        return client.get(
            urljoin(self.cfg.access.base_url or "", path),
            params=params,
            headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"},
        )

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse IMF CSV. See `parse_imf_csv` (spec §9.1)."""
        return parse_imf_csv(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """IMF CSV has no release calendar (spec §9.1). Returns []."""
        return []
