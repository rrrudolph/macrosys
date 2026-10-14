"""US Treasury par yield curve and the Fed GSW nominal curve (spec §9.1, §9.2, §9.6).

Treasury XML URL, confirmed 2026-10-14 from
https://home.treasury.gov/treasury-daily-interest-rate-xml-feed :

`{base}/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value={yyyy}`

`base` is the registry `access.base_url`. One series per tenor, `TREASURY:PAR:<tenor>`, unit `pct`.
The feed has a date and no clock time, so `published_at` is the end of that New York date and the flag
is `reconstructed_pubtime`. `BC_*DISPLAY` columns are duplicates and are skipped.

Fed GSW is the CSV at the registry `access.url` (`feds200628.csv`): comment lines, then
`Date,BETA0,...,SVENY01..SVENY30,TAU1,TAU2`. Yield and beta columns are `pct`. `TAU*` is a maturity
parameter in years (`y`), not a yield ([J]). `NA` is absent, not zero. The file is posted weekly and
has no per-row publication time, so `published_at` is `fetched_at` with `reconstructed_pubtime`
(spec §9.2). Env: none for either source.
"""

from __future__ import annotations

import csv
import re
from datetime import date
from io import StringIO
from typing import Any
from xml.etree import ElementTree

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
from macrosys.sources.common import ET, end_of_day, fetched_at, source_url
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"

_ATOM = "http://www.w3.org/2005/Atom"
_META = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
_DATA = "http://schemas.microsoft.com/ado/2007/08/dataservices"
_NS = {"m": _META, "d": _DATA, "a": _ATOM}
_TENOR = re.compile(r"^BC_(\d+(?:_\d+)?)(MONTH|YEAR)$")
_PAR_DATA = "daily_treasury_yield_curve"


def treasury_xml_url(year: int, *, data: str = _PAR_DATA) -> str:
    """Build the daily Treasury XML URL from the registry base (spec §9.6)."""
    base = (require_source("TREASURY").access.base_url or "").rstrip("/")
    return (
        f"{base}/resource-center/data-chart-center/interest-rates/pages/xml"
        f"?data={data}&field_tdr_date_value={year}"
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _tenor(tag: str) -> str | None:
    match = _TENOR.fullmatch(tag)
    if not match:
        return None
    number = match.group(1).replace("_", ".")
    suffix = "M" if match.group(2) == "MONTH" else "Y"
    return f"{number}{suffix}"


def parse_par_yields(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse the daily par yield Atom/XML feed into one row per tenor (spec §9.1, §9.2)."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ParserChangedError("Treasury yield feed is not XML") from exc
    properties = root.findall(".//m:properties", _NS)
    if not properties:
        raise ParserChangedError("Treasury yield feed has no m:properties entries")
    ingested = fetched_at(payload)
    url = source_url(payload) or treasury_xml_url(ingested.year)
    assert_unit("pct")
    rows: list[Observation] = []
    for block in properties:
        values = {_local_name(child.tag): (child.text or "").strip() for child in list(block)}
        if "NEW_DATE" not in values:
            raise ParserChangedError("Treasury yield entry is missing NEW_DATE")
        try:
            curve_date = date.fromisoformat(values["NEW_DATE"][:10])
        except ValueError as exc:
            raise ParserChangedError(f"Treasury NEW_DATE {values['NEW_DATE']!r} is not a date") from exc
        published = end_of_day(curve_date, ET)
        for tag, text in values.items():
            tenor = _tenor(tag)
            if tenor is None or text == "":
                continue
            try:
                value = float(text)
            except ValueError as exc:
                raise ParserChangedError(f"Treasury {tag} value {text!r} is not a number") from exc
            rows.append(
                Observation(
                    series_id=f"TREASURY:PAR:{tenor}",
                    ref_period_start=curve_date,
                    ref_period_end=curve_date,
                    value=value,
                    unit="pct",
                    published_at=published,
                    ingested_at=ingested,
                    vintage_id=payload.sha256,
                    source_url=url,
                    source_hash=payload.sha256,
                    quality_flag="reconstructed_pubtime",
                )
            )
    return rows


def gsw_unit(column: str) -> str:
    """Unit for one GSW column. Yields and Nelson-Siegel betas are percent; tau is in years."""
    if column.startswith(("SVEN", "BETA")):
        label = "pct"
    elif column.startswith("TAU"):
        label = "y"
    else:
        raise ParserChangedError(f"FEDGSW column {column!r} has no unit mapping in parser {PARSER_VERSION}")
    assert_unit(label)
    return label


def parse_gsw(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse `feds200628.csv` into one observation per non-missing curve point (spec §9.1, §9.2)."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParserChangedError("FEDGSW CSV is not UTF-8") from exc
    header: list[str] | None = None
    data_rows: list[list[str]] = []
    for record in csv.reader(StringIO(text)):
        if not record:
            continue
        if header is None:
            if record[0] == "Date":
                header = record
            continue
        data_rows.append(record)
    if not header:
        raise ParserChangedError("FEDGSW CSV has no Date header")
    ingested = fetched_at(payload)
    url = source_url(payload) or str(payload.request.get("url") or "")
    rows: list[Observation] = []
    for record in data_rows:
        if len(record) != len(header):
            raise ParserChangedError("FEDGSW data row does not match the Date header")
        try:
            curve_date = date.fromisoformat(record[0])
        except ValueError as exc:
            raise ParserChangedError(f"FEDGSW date {record[0]!r} is not ISO") from exc
        for column, raw in zip(header[1:], record[1:], strict=True):
            token = raw.strip()
            if token in {"", "NA", "N/A"}:
                continue
            try:
                value = float(token)
            except ValueError as exc:
                raise ParserChangedError(f"FEDGSW {column} value {raw!r} is not a number") from exc
            rows.append(
                Observation(
                    series_id=f"FEDGSW:{column}",
                    ref_period_start=curve_date,
                    ref_period_end=curve_date,
                    value=value,
                    unit=gsw_unit(column),
                    published_at=ingested,
                    ingested_at=ingested,
                    vintage_id=payload.sha256,
                    source_url=url,
                    source_hash=payload.sha256,
                    quality_flag="reconstructed_pubtime",
                )
            )
    return rows


class TreasurySource:
    """Treasury daily par yield XML `DataSource` (spec §9.6). No API key."""

    source_id = "TREASURY"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """Tenors are whatever the feed publishes (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """Download one calendar year of the par yield XML feed (spec §9.6)."""
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        year = since.year if since is not None else date.today().year
        return client.get(treasury_xml_url(year))

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse the XML feed. See `parse_par_yields` (spec §9.1)."""
        return parse_par_yields(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """The XML feed has no separate release calendar (spec §9.1). Returns []."""
        return []


class FedGswSource:
    """Fed Gürkaynak–Sack–Wright curve `DataSource` (spec §9.6). No API key."""

    source_id = "FEDGSW"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """Columns are read from the CSV header at parse time (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """Download `feds200628.csv` from the registry URL (spec §9.6). `since` is ignored; the file is complete."""
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        url = self.cfg.access.url
        if not url:
            raise RuntimeError("FEDGSW registry entry has no access.url")
        return client.get(url)

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse the GSW CSV. See `parse_gsw` (spec §9.1)."""
        return parse_gsw(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """The research CSV is posted weekly and has no calendar feed (spec §9.6). Returns []."""
        return []
