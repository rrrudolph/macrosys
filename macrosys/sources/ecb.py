"""ECB Data Portal SDMX 2.1 CSV connector (spec §9.1, §9.2, §9.6).

Requests use `{base}/data/{flow}/{key}?format=csvdata`. There is no API key. The csvdata body is parsed
for `KEY`, `TIME_PERIOD`, `OBS_VALUE`, and the unit / multiplier columns (`UNIT` or `UNIT_MEASURE`, and
`UNIT_MULT`). Exchange-rate flows (`EXR`) are ratios: the `UNIT` column names the quoted currency, not
a currency amount ([M], ECB reference-rate definition). `UNIT_MULT` selects the scale suffix
(6 → `_mn`); `OBS_VALUE` is stored as published, not rescaled.

`published_at` is a last-update timestamp when `COMPILATION`, `LAST_UPDATE`, or `OBS_COM` carries a
clock time. Otherwise it is the payload's `fetched_at` with `quality_flag='reconstructed_pubtime'`
(spec §9.2). The EXR sample captured 2026-10-14 left `COMPILATION` blank. Env: none.
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
from macrosys.sources.common import fetched_at, parse_instant, sdmx_period, source_url
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"

_UPDATE_COLUMNS = ("LAST_UPDATE", "COMPILATION", "OBS_COM")
_MULT_SCALE = {0: "", 3: "_k", 6: "_mn", 9: "_bn", 12: "_tn"}


def split_series_id(series_id: str) -> tuple[str, str]:
    """Split `ECB:<flow>.<key>` into the SDMX flow and the remainder of the key (spec §9.6)."""
    native = series_id.removeprefix("ECB:")
    flow, dot, key = native.partition(".")
    if not flow or not dot or not key:
        raise ValueError(f"ECB series id {series_id!r} is not ECB:<flow>.<key>")
    return flow, key


def map_ecb_unit(unit: str, unit_mult: int, *, flow: str) -> str:
    """Map SDMX UNIT and UNIT_MULT onto the controlled vocabulary (spec §9.1).

    EXR observations are reference rates, so the unit is `ratio` even when UNIT names a currency.
    """
    if flow == "EXR":
        return "ratio"
    if unit_mult not in _MULT_SCALE:
        raise ValueError(f"ECB UNIT_MULT {unit_mult} has no scale mapping in parser {PARSER_VERSION}")
    scale = _MULT_SCALE[unit_mult]
    token = (unit or "").strip().upper()
    if token in {"", "PURE"}:
        label = "ratio"
    elif token in {"PC", "PCT", "PERCENT"}:
        label = "pct"
    elif token in {"INDEX", "I15"} or token.startswith("IX"):
        label = "index"
    else:
        label = f"{token}{scale}" if scale else token
    try:
        assert_unit(label)
    except UnitError as exc:
        raise ValueError(
            f"ECB unit {unit!r} with UNIT_MULT={unit_mult} has no mapping in parser {PARSER_VERSION}"
        ) from exc
    return label


def _unit_column(fieldnames: list[str]) -> str:
    if "UNIT" in fieldnames:
        return "UNIT"
    if "UNIT_MEASURE" in fieldnames:
        return "UNIT_MEASURE"
    raise ParserChangedError("ECB csvdata is missing UNIT / UNIT_MEASURE")


def _float_or_none(text: str) -> float | None:
    raw = text.strip()
    if raw in {"", "NA", "NaN"}:
        return None
    return float(raw)


def parse_csvdata(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse an ECB `format=csvdata` body into observations (spec §9.1, §9.2)."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParserChangedError("ECB csvdata is not UTF-8") from exc
    reader = csv.DictReader(StringIO(text))
    fieldnames = list(reader.fieldnames or [])
    missing = [name for name in ("KEY", "TIME_PERIOD", "OBS_VALUE", "UNIT_MULT") if name not in fieldnames]
    if missing:
        raise ParserChangedError(f"ECB csvdata is missing {missing}")
    unit_col = _unit_column(fieldnames)
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for record in reader:
        key = (record.get("KEY") or "").strip()
        period = (record.get("TIME_PERIOD") or "").strip()
        if not key or not period:
            raise ParserChangedError("ECB csvdata row is missing KEY or TIME_PERIOD")
        flow = key.split(".", 1)[0]
        start, end = sdmx_period(period)
        mult_text = (record.get("UNIT_MULT") or "").strip()
        try:
            mult = int(mult_text) if mult_text else 0
        except ValueError as exc:
            raise ParserChangedError(f"ECB UNIT_MULT {mult_text!r} is not an integer") from exc
        unit = map_ecb_unit(record.get(unit_col) or "", mult, flow=flow)
        published = ingested
        flag = "reconstructed_pubtime"
        for column in _UPDATE_COLUMNS:
            stamp = parse_instant(record.get(column) or "")
            if stamp is not None:
                published = stamp
                flag = "ok"
                break
        rows.append(
            Observation(
                series_id=f"ECB:{key}",
                ref_period_start=start,
                ref_period_end=end,
                value=_float_or_none(record.get("OBS_VALUE") or ""),
                unit=unit,
                published_at=published,
                ingested_at=ingested,
                vintage_id=payload.sha256,
                source_url=url,
                source_hash=payload.sha256,
                quality_flag=flag,  # type: ignore[arg-type]
            )
        )
    return rows


class EcbSource:
    """ECB SDMX 2.1 CSV `DataSource` (spec §9.6). No API key."""

    source_id = "ECB"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """ECB has no local catalogue; discovery is the caller's key list (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """Download one `data/{flow}/{key}` csvdata resource (spec §9.6)."""
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        flow, key = split_series_id(series_id)
        base = self.cfg.access.base_url or ""
        params: dict[str, Any] = {"format": "csvdata"}
        if since is not None:
            params["startPeriod"] = since.isoformat()
        return client.get(urljoin(base, f"data/{flow}/{key}"), params=params)

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse csvdata. See `parse_csvdata` (spec §9.1)."""
        return parse_csvdata(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """ECB csvdata carries no release calendar (spec §9.1). Returns []."""
        return []
