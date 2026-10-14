"""EIA API v2 connector (spec §9.1, §9.2, §9.6).

`GET {base}<route>/data/` with query `api_key` from env `EIA_API_KEY`. Series ids are
`EIA:<route>:<series>`. The v2 `units` text is mapped explicitly: `MBBL` → `kbbl` (EIA's thousand
barrels), `MBBL/D` → `kbd`, and `thousand megawatthours` → `GWh`. Anything else raises. The data
rows have no publication clock, so `published_at` is `fetched_at` with
`quality_flag='reconstructed_pubtime'`. Env: `EIA_API_KEY`.
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

EIA_UNIT_MAP = {
    "MBBL": "kbbl",
    "MBBL/D": "kbd",
    "thousand megawatthours": "GWh",
    "thousand megawatthour": "GWh",
}


def map_eia_unit(text: str) -> str:
    """Map an EIA `units` string onto the controlled vocabulary, or raise (spec §9.1)."""
    try:
        label = EIA_UNIT_MAP[text]
    except KeyError as exc:
        raise ValueError(f"EIA unit {text!r} has no mapping in EIA_UNIT_MAP (parser {PARSER_VERSION})") from exc
    assert_unit(label)
    return label


def parse_eia(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse an EIA v2 `response.data` array (spec §9.1, §9.2)."""
    from macrosys.sources.common import load_json, require_mapping

    doc = require_mapping(load_json(content, what="EIA"), what="EIA")
    response = doc.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise ParserChangedError("EIA payload is missing response.data")
    route = str(payload.request.get("route") or "")
    if not route:
        raise ParserChangedError("EIA request has no route")
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for item in response["data"]:
        if not isinstance(item, dict):
            raise ParserChangedError("EIA data row is not an object")
        for key in ("period", "series", "value", "units"):
            if key not in item:
                raise ParserChangedError(f"EIA row is missing {key}")
        start, end = sdmx_period(str(item["period"]))
        raw = item["value"]
        rows.append(
            Observation(
                series_id=f"EIA:{route}:{item['series']}",
                ref_period_start=start,
                ref_period_end=end,
                value=None if raw is None else float(raw),
                unit=map_eia_unit(str(item["units"])),
                published_at=ingested,
                ingested_at=ingested,
                vintage_id=payload.sha256,
                source_url=url,
                source_hash=payload.sha256,
                quality_flag="reconstructed_pubtime",
            )
        )
    return rows


class EiaSource:
    """EIA API v2 `DataSource` (spec §9.6). Env: `EIA_API_KEY`."""

    source_id = "EIA"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """EIA routes are named by the caller (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """GET `v2/<route>/data/` for one series facet (spec §9.6).

        `series_id` is `EIA:<route>:<series>`, and `<route>` may contain slashes
        (`petroleum/stoc/wstk`).
        """
        native = series_id.removeprefix("EIA:")
        route, sep, series = native.rpartition(":")
        if not sep:
            raise ValueError(f"EIA series id {series_id!r} is not EIA:<route>:<series>")
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        params: dict[str, Any] = {"data[]": "value", "facets[series][]": series, "length": "5000"}
        if since is not None:
            params["start"] = since.isoformat()
        return client.get(
            urljoin(self.cfg.access.base_url or "", f"{route}/data/"),
            params=params,
            extra_request={"route": route},
        )

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse an EIA data page. See `parse_eia` (spec §9.1)."""
        return parse_eia(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """EIA data rows carry no release calendar (spec §9.1). Returns []."""
        return []
