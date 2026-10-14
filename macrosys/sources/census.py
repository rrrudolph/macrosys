"""Census Bureau timeseries API (spec §9.1, §9.2, §9.6).

Requests are `GET {base}<dataset>` with `get=` and `time=`. The key is env `CENSUS_API_KEY`
(registry `query:key`, optional). A keyless call currently returns an HTML "Missing Key" page;
the registry still marks the key optional. Responses are a JSON array whose first row is the
header. There is no unit column, so `request["unit"]` must be a controlled-vocabulary label.
There is no publication clock, so `published_at` is `fetched_at` with
`quality_flag='reconstructed_pubtime'`. Series ids are `CENSUS:<dataset>:<category>:<data_type>`.
Env: `CENSUS_API_KEY` (optional).
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
from macrosys.core.units import assert_unit
from macrosys.sources.common import fetched_at, sdmx_period, source_url
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"


def parse_census(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse a Census timeseries JSON matrix into observations (spec §9.1, §9.2)."""
    from macrosys.sources.common import load_json

    doc = load_json(content, what="Census")
    if not isinstance(doc, list) or len(doc) < 2 or not isinstance(doc[0], list):
        raise ParserChangedError("Census payload is not a header-plus-rows JSON array")
    header = [str(name) for name in doc[0]]
    if "time" not in header or "cell_value" not in header:
        raise ParserChangedError("Census payload is missing time or cell_value")
    unit_label = payload.request.get("unit")
    if not isinstance(unit_label, str) or not unit_label:
        raise ValueError("Census parse needs request.unit; the timeseries matrix has no unit column")
    unit = assert_unit(unit_label).label
    dataset = str(payload.request.get("dataset") or "timeseries")
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for raw in doc[1:]:
        if not isinstance(raw, list) or len(raw) != len(header):
            raise ParserChangedError("Census data row does not match the header")
        item = dict(zip(header, raw, strict=True))
        start, end = sdmx_period(str(item["time"])[:10])
        text = str(item["cell_value"]).strip()
        value = None if text in {"", "null", "NA"} else float(text)
        category = str(item.get("category_code") or item.get("NAICS") or item.get("CTY_CODE") or "ALL")
        data_type = str(item.get("data_type_code") or item.get("COMM_LVL") or "value")
        rows.append(
            Observation(
                series_id=f"CENSUS:{dataset}:{category}:{data_type}",
                ref_period_start=start,
                ref_period_end=end,
                value=value,
                unit=unit,
                published_at=ingested,
                ingested_at=ingested,
                vintage_id=payload.sha256,
                source_url=url,
                source_hash=payload.sha256,
                quality_flag="reconstructed_pubtime",
            )
        )
    return rows


class CensusSource:
    """Census timeseries `DataSource` (spec §9.6). Env: `CENSUS_API_KEY` (optional)."""

    source_id = "CENSUS"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """Census datasets are named by the caller (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """GET one timeseries dataset such as `eits/marts` (spec §9.6).

        `series_id` is `CENSUS:<dataset path>`, for example `CENSUS:eits/marts`.
        """
        dataset = series_id.removeprefix("CENSUS:").split(":")[0]
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        params: dict[str, Any] = {
            "get": "cell_value,time_slot_id,category_code,data_type_code,seasonally_adj,time",
        }
        if since is not None:
            params["time"] = f"{since.year:04d}-{since.month:02d}"
        return client.get(
            urljoin(self.cfg.access.base_url or "", dataset),
            params=params,
            extra_request={"dataset": dataset, "unit": "USD_mn"},
        )

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse a timeseries matrix. See `parse_census` (spec §9.1)."""
        return parse_census(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """The timeseries matrix has no release calendar (spec §9.1). Returns []."""
        return []
