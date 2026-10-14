"""NY Fed Markets Data API (spec §9.1, §9.2, §9.6).

Live checks on 2026-10-14, against `https://markets.newyorkfed.org/static/docs/markets-api.html`
(the OpenAPI document is `markets-api.yml` next to that page):

- `rates/secured/sofr/last/{n}.json` and `rates/unsecured/effr/last/{n}.json` return `refRates`
  with `effectiveDate`, `percentRate`, and `volumeInBillions`.
- `rp/reverserepo/all/results/last/{n}.json` returns `repo.operations`.

No API key. Series ids are `NYFED:<type>:<field>`. Rates are `pct`; `volumeInBillions` is `USD_bn`.
The payload has a date and no clock, so `published_at` is the end of `effectiveDate` in
America/New_York and `quality_flag='reconstructed_pubtime'`.
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
from macrosys.sources.common import ET, end_of_day, fetched_at, source_url
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"

_RATE_FIELDS = {
    "percentRate": "pct",
    "percentPercentile1": "pct",
    "percentPercentile25": "pct",
    "percentPercentile75": "pct",
    "percentPercentile99": "pct",
    "targetRateFrom": "pct",
    "targetRateTo": "pct",
    "volumeInBillions": "USD_bn",
}


def _row(payload: RawPayload, series_id: str, day: date, value: float, unit: str) -> Observation:
    assert_unit(unit)
    ingested = fetched_at(payload)
    return Observation(
        series_id=series_id,
        ref_period_start=day,
        ref_period_end=day,
        value=value,
        unit=unit,
        published_at=end_of_day(day, ET),
        ingested_at=ingested,
        vintage_id=payload.sha256,
        source_url=source_url(payload),
        source_hash=payload.sha256,
        quality_flag="reconstructed_pubtime",
    )


def parse_markets(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse a reference-rate or repo-results JSON body (spec §9.1, §9.2)."""
    from macrosys.sources.common import load_json, require_mapping

    doc = require_mapping(load_json(content, what="NY Fed"), what="NY Fed")
    rows: list[Observation] = []
    rates = doc.get("refRates")
    operations = (doc.get("repo") or {}).get("operations") if isinstance(doc.get("repo"), dict) else None
    timeseries = (doc.get("pd") or {}).get("timeseries") if isinstance(doc.get("pd"), dict) else None
    if rates is None and operations is None and timeseries is None:
        raise ParserChangedError("NY Fed payload has neither refRates, repo.operations, nor pd.timeseries")
    if isinstance(rates, list):
        for item in rates:
            if not isinstance(item, dict) or "effectiveDate" not in item or "type" not in item:
                raise ParserChangedError("NY Fed refRates row is missing effectiveDate or type")
            day = date.fromisoformat(str(item["effectiveDate"])[:10])
            kind = str(item["type"])
            for field, unit in _RATE_FIELDS.items():
                if item.get(field) in (None, ""):
                    continue
                rows.append(_row(payload, f"NYFED:{kind}:{field}", day, float(item[field]), unit))
    if isinstance(operations, list):
        for item in operations:
            if not isinstance(item, dict) or "operationDate" not in item:
                raise ParserChangedError("NY Fed repo operation is missing operationDate")
            day = date.fromisoformat(str(item["operationDate"])[:10])
            amount = item.get("totalAmtAccepted")
            if amount in (None, ""):
                continue
            rows.append(_row(payload, "NYFED:RRP:totalAmtAccepted", day, float(amount), "USD_bn"))
    if isinstance(timeseries, list):
        for item in timeseries:
            if not isinstance(item, dict) or "asOfDate" not in item or "value" not in item:
                raise ParserChangedError("NY Fed dealer row is missing asOfDate or value")
            day = date.fromisoformat(str(item["asOfDate"])[:10])
            key = str(item.get("keyId") or "PD")
            rows.append(_row(payload, f"NYFED:PD:{key}", day, float(item["value"]), "USD_bn"))
    return rows


class NyFedSource:
    """NY Fed Markets `DataSource` (spec §9.6). No API key."""

    source_id = "NYFED"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """Rate types are SOFR, EFFR, and the repo/dealer endpoints (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """GET the last observations for a rate, repo, or dealer series (spec §9.6).

        `NYFED:SOFR` and `NYFED:EFFR` hit the reference-rate paths. `NYFED:RRP` hits reverse-repo
        results. `NYFED:PD` hits `pd/get/PDSOMA.json` (a live call returned an empty timeseries).
        """
        native = series_id.removeprefix("NYFED:").split(":")[0].upper()
        paths = {
            "SOFR": "rates/secured/sofr/last/10.json",
            "EFFR": "rates/unsecured/effr/last/10.json",
            "RRP": "rp/reverserepo/all/results/last/10.json",
            "PD": "pd/get/PDSOMA.json",
        }
        if native not in paths:
            raise ValueError(f"NY Fed series {series_id!r} is not SOFR, EFFR, RRP, or PD")
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        return client.get(urljoin(self.cfg.access.base_url or "", paths[native]))

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse a Markets API body. See `parse_markets` (spec §9.1)."""
        return parse_markets(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """Reference rates have no separate calendar feed here (spec §9.1). Returns []."""
        return []
