"""BEA API connector (spec §9.1, §9.2, §9.6).

GetData requests use the parameters published for `https://apps.bea.gov/api/data/`: `UserID`
(env `BEA_API_KEY`), `method=GetData`, `DataSetName`, `ResultFormat=JSON`, plus dataset fields.
NIPA uses `TableName`, `Frequency`, and `Year`. GDPbyIndustry and InputOutput use `TableID`,
`Frequency`/`Year`, and GDPbyIndustry also `Industry`. Confirmed against the BEA API user guide
(`https://apps.bea.gov/api/_pdf/bea_web_service_api_user_guide.pdf`) and the data endpoint's
parameter names: UserID, method, DataSetName, TableName, TableID, Frequency, Year, Industry,
ResultFormat. The PDF is compressed, so the names were checked against that documented GetData
contract rather than a plaintext extract.

Series ids are `BEA:<dataset>:<table>:<line>`. `UTCProductionTime` is a UTC clock when present
(`quality_flag='ok'`). Otherwise `published_at` is `fetched_at` and flagged. `DataValue` commas
are stripped. Percent-like `CL_UNIT`/`METRIC_NAME` map to `pct`; dollar levels use `UNIT_MULT`
(6 → `USD_mn`). Env: `BEA_API_KEY`.
"""

from __future__ import annotations

from datetime import date
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
_MULT = {0: "", 3: "_k", 6: "_mn", 9: "_bn", 12: "_tn"}


def map_bea_unit(metric: str, cl_unit: str, unit_mult: int) -> str:
    """Map a BEA metric and unit multiplier onto the controlled vocabulary (spec §9.1)."""
    text = f"{metric} {cl_unit}".lower()
    if "percent" in text:
        return "pct"
    if unit_mult not in _MULT:
        raise ValueError(f"BEA UNIT_MULT {unit_mult} has no scale mapping")
    scale = _MULT[unit_mult]
    if "chained" in text:
        year = "".join(ch for ch in metric if ch.isdigit())
        label = f"USD{scale}_real{year}" if len(year) == 4 else f"USD{scale}_real"
    elif "dollar" in text or cl_unit.lower() == "level":
        label = f"USD{scale}" if scale else "USD"
    else:
        raise ValueError(f"BEA metric {metric!r} / {cl_unit!r} has no unit mapping")
    try:
        assert_unit(label)
    except UnitError as exc:
        raise ValueError(f"BEA unit {label!r} is not in the vocabulary") from exc
    return label


def _bea_period(token: str) -> tuple[date, date]:
    text = token.strip().upper()
    if len(text) >= 6 and text[4] == "Q":
        return sdmx_period(f"{text[:4]}-Q{text[5]}")
    if len(text) >= 6 and text[4] == "M":
        return sdmx_period(f"{text[:4]}-{int(text[5:]):02d}")
    return sdmx_period(text)


def parse_bea(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse a BEA GetData JSON body (spec §9.1, §9.2)."""
    from macrosys.sources.common import load_json, require_mapping

    doc = require_mapping(load_json(content, what="BEA"), what="BEA")
    api = doc.get("BEAAPI")
    if not isinstance(api, dict) or not isinstance(api.get("Results"), dict):
        raise ParserChangedError("BEA payload is missing BEAAPI.Results")
    results = api["Results"]
    data = results.get("Data")
    if not isinstance(data, list):
        raise ParserChangedError("BEA Results.Data is not a list")
    dataset = str(payload.request.get("dataset") or payload.request.get("params", {}).get("DataSetName") or "")
    if not dataset:
        raise ParserChangedError("BEA request has no DataSetName")
    produced = parse_instant(str(results.get("UTCProductionTime") or ""))
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for item in data:
        if not isinstance(item, dict):
            raise ParserChangedError("BEA data row is not an object")
        for key in ("TableName", "LineNumber", "TimePeriod", "DataValue"):
            if key not in item:
                raise ParserChangedError(f"BEA row is missing {key}")
        table = str(item.get("TableName") or item.get("TableID") or "")
        line = str(item["LineNumber"])
        start, end = _bea_period(str(item["TimePeriod"]))
        mult = int(str(item.get("UNIT_MULT") or "0") or "0")
        unit = map_bea_unit(str(item.get("METRIC_NAME") or ""), str(item.get("CL_UNIT") or ""), mult)
        raw = str(item["DataValue"]).replace(",", "").strip()
        value = None if raw in {"", "(NA)", "NA"} else float(raw)
        rows.append(
            Observation(
                series_id=f"BEA:{dataset}:{table}:{line}",
                ref_period_start=start,
                ref_period_end=end,
                value=value,
                unit=unit,
                published_at=produced or ingested,
                ingested_at=ingested,
                vintage_id=payload.sha256,
                source_url=url,
                source_hash=payload.sha256,
                quality_flag="ok" if produced is not None else "reconstructed_pubtime",
            )
        )
    return rows


class BeaSource:
    """BEA GetData `DataSource` (spec §9.6). Env: `BEA_API_KEY`."""

    source_id = "BEA"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """BEA tables are named by the caller (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """GET one NIPA, GDPbyIndustry, or InputOutput table (spec §9.6).

        `series_id` is `BEA:<dataset>:<table>` or the full line id; the line is not a request
        filter. `since` selects `Year`.
        """
        parts = series_id.removeprefix("BEA:").split(":")
        if len(parts) < 2:
            raise ValueError(f"BEA series id {series_id!r} is not BEA:<dataset>:<table>")
        dataset, table = parts[0], parts[1]
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        params: dict[str, Any] = {
            "method": "GetData",
            "DataSetName": dataset,
            "ResultFormat": "JSON",
            "Year": str(since.year) if since is not None else "X",
        }
        if dataset == "NIPA":
            params["TableName"] = table
            params["Frequency"] = "Q"
        elif dataset == "GDPbyIndustry":
            params["TableID"] = table
            params["Frequency"] = "A"
            params["Industry"] = "ALL"
        elif dataset == "InputOutput":
            params["TableID"] = table
        else:
            params["TableName"] = table
        return client.get(urljoin(self.cfg.access.base_url or "", ""), params=params, extra_request={"dataset": dataset})

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse GetData JSON. See `parse_bea` (spec §9.1)."""
        return parse_bea(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """BEA GetData is not a release calendar (spec §9.1). Returns []."""
        return []
