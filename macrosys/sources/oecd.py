"""OECD SDMX REST connector (spec §9.1, §9.2, §9.6).

`GET {base}data/{agency},{dataflow},{version}/{key}?format=csvfilewithlabels`. A live call on
2026-10-14 for `OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0` returned that CSV, with code/label
column pairs and `TIME_PERIOD` / `OBS_VALUE`. The registry limit of 60 downloads per hour is the
HTTP token bucket. Series ids are `OECD:<dataflow>:<key>`. `UNIT_MEASURE` `PA` is `pct` and `IX`
is `index`. There is no publication clock, so `published_at` is `fetched_at` and flagged
`reconstructed_pubtime`. No API key.
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
from macrosys.core.units import assert_unit
from macrosys.sources.common import fetched_at, sdmx_period, source_url
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"
_KEY_COLUMNS = (
    "REF_AREA",
    "FREQ",
    "METHODOLOGY",
    "MEASURE",
    "UNIT_MEASURE",
    "EXPENDITURE",
    "ADJUSTMENT",
    "TRANSFORMATION",
)


def _oecd_unit(code: str) -> str:
    token = code.strip().upper()
    if token in {"PA", "PC", "PT", "PCT"}:
        label = "pct"
    elif token in {"IX", "INDEX"}:
        label = "index"
    else:
        raise ValueError(f"OECD UNIT_MEASURE {code!r} has no mapping in parser {PARSER_VERSION}")
    assert_unit(label)
    return label


def parse_oecd_csv(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse an OECD `csvfilewithlabels` body (spec §9.1, §9.2)."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParserChangedError("OECD CSV is not UTF-8") from exc
    reader = csv.DictReader(StringIO(text))
    fields = list(reader.fieldnames or [])
    missing = [name for name in ("TIME_PERIOD", "OBS_VALUE", "STRUCTURE_ID", "UNIT_MEASURE") if name not in fields]
    if missing:
        raise ParserChangedError(f"OECD CSV is missing {missing}")
    key_cols = [name for name in _KEY_COLUMNS if name in fields]
    if not key_cols:
        raise ParserChangedError("OECD CSV has no dimension columns")
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for record in reader:
        structure = record.get("STRUCTURE_ID") or ""
        dataflow = structure.split(":")[-1].split("(")[0]
        if not dataflow:
            raise ParserChangedError("OECD STRUCTURE_ID has no dataflow")
        if any(not (record.get(name) or "").strip() for name in key_cols):
            raise ParserChangedError("OECD row is missing a key dimension")
        key = ".".join(record[name].strip() for name in key_cols)
        start, end = sdmx_period(record["TIME_PERIOD"].strip())
        raw = (record.get("OBS_VALUE") or "").strip()
        rows.append(
            Observation(
                series_id=f"OECD:{dataflow}:{key}",
                ref_period_start=start,
                ref_period_end=end,
                value=None if raw == "" else float(raw),
                unit=_oecd_unit(record["UNIT_MEASURE"]),
                published_at=ingested,
                ingested_at=ingested,
                vintage_id=payload.sha256,
                source_url=url,
                source_hash=payload.sha256,
                quality_flag="reconstructed_pubtime",
            )
        )
    return rows


class OecdSource:
    """OECD SDMX `DataSource` (spec §9.6). No API key. 60 downloads/hour via the token bucket."""

    source_id = "OECD"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """OECD dataflows are named by the caller (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """GET one `agency,dataflow,version/key` resource (spec §9.6).

        `series_id` is `OECD:<agency>,<dataflow>,<version>/<key>` because the REST path needs the
        agency and version, which the short `OECD:<dataflow>:<key>` id does not carry. The parsed
        series id is still `OECD:<dataflow>:<key>`.
        """
        native = series_id.removeprefix("OECD:")
        resource, sep, _key = native.partition("/")
        if not sep:
            raise ValueError("OECD fetch id must be OECD:<agency>,<dataflow>,<version>/<key>")
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        params: dict[str, Any] = {"format": "csvfilewithlabels"}
        if since is not None:
            params["startPeriod"] = since.isoformat()[:7]
        return client.get(urljoin(self.cfg.access.base_url or "", f"data/{resource}/{native.split('/', 1)[1]}"), params=params)

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse OECD CSV. See `parse_oecd_csv` (spec §9.1)."""
        return parse_oecd_csv(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """OECD CSV has no release calendar (spec §9.1). Returns []."""
        return []
