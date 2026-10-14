"""UN Comtrade Plus connector (spec §9.1, §9.2, §9.6).

`GET {base}get/C/{freq}/HS` with header `Ocp-Apim-Subscription-Key` from env `COMTRADE_KEY`.
The registry budget is 500 calls/day, a 1 call/second burst, and 100_000 records/call; the HTTP
client's token bucket enforces the first two and `fetch` does not ask for more than the third.

The primary observation is `COMTRADE:<reporter>:<partner>:<flow>:<cmd>` in `USD` (`primaryValue`).
`netWgt` is kilograms. `kg` is not in the unit vocabulary, so the weight series
`COMTRADE:<reporter>:<partner>:<flow>:<cmd>:netWgt` is stored in metric tonnes (`t`) after dividing
by 1000. There is no publication clock, so `published_at` is `fetched_at` and flagged
`reconstructed_pubtime`. Env: `COMTRADE_KEY`.
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


def _period_token(period: Any) -> tuple[date, date]:
    text = str(period)
    if len(text) == 6 and text.isdigit():
        return sdmx_period(f"{text[:4]}-{text[4:]}")
    if len(text) == 4 and text.isdigit():
        return sdmx_period(text)
    return sdmx_period(text)


def parse_comtrade(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse a Comtrade `data` array into value and weight observations (spec §9.1, §9.2)."""
    from macrosys.sources.common import load_json, require_mapping

    doc = require_mapping(load_json(content, what="Comtrade"), what="Comtrade")
    rows_in = doc.get("data")
    if not isinstance(rows_in, list):
        raise ParserChangedError("Comtrade payload is missing data")
    cap = payload.request.get("records_per_call")
    if isinstance(cap, int) and len(rows_in) > cap:
        raise ParserChangedError(f"Comtrade page has {len(rows_in)} rows, above the registry cap {cap}")
    ingested = fetched_at(payload)
    url = source_url(payload)
    assert_unit("USD")
    assert_unit("t")
    rows: list[Observation] = []
    for item in rows_in:
        if not isinstance(item, dict):
            raise ParserChangedError("Comtrade row is not an object")
        for key in ("reporterCode", "partnerCode", "flowCode", "cmdCode", "period", "primaryValue"):
            if key not in item:
                raise ParserChangedError(f"Comtrade row is missing {key}")
        reporter = str(item.get("reporterISO") or item["reporterCode"])
        partner = str(item.get("partnerISO") or item["partnerCode"])
        flow = str(item["flowCode"])
        cmd = str(item["cmdCode"])
        start, end = _period_token(item["period"])
        base = f"COMTRADE:{reporter}:{partner}:{flow}:{cmd}"
        primary = item["primaryValue"]
        rows.append(
            Observation(
                series_id=base,
                ref_period_start=start,
                ref_period_end=end,
                value=None if primary is None else float(primary),
                unit="USD",
                published_at=ingested,
                ingested_at=ingested,
                vintage_id=payload.sha256,
                source_url=url,
                source_hash=payload.sha256,
                quality_flag="reconstructed_pubtime",
            )
        )
        weight = item.get("netWgt")
        if weight not in (None, ""):
            # [M] Comtrade netWgt is kilograms. The vocabulary has tonnes, not kg.
            rows.append(
                Observation(
                    series_id=f"{base}:netWgt",
                    ref_period_start=start,
                    ref_period_end=end,
                    value=float(weight) / 1000.0,
                    unit="t",
                    published_at=ingested,
                    ingested_at=ingested,
                    vintage_id=payload.sha256,
                    source_url=url,
                    source_hash=payload.sha256,
                    quality_flag="reconstructed_pubtime",
                )
            )
    return rows


class ComtradeSource:
    """UN Comtrade `DataSource` (spec §9.6). Env: `COMTRADE_KEY`."""

    source_id = "COMTRADE"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """Comtrade queries are named by the caller (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """GET `get/C/{freq}/HS` for one reporter-partner-flow-commodity key (spec §9.6).

        `series_id` is `COMTRADE:<reporter>:<partner>:<flow>:<cmd>`.
        """
        parts = series_id.removeprefix("COMTRADE:").split(":")
        if len(parts) < 4:
            raise ValueError(f"Comtrade series id {series_id!r} is not COMTRADE:<reporter>:<partner>:<flow>:<cmd>")
        reporter, partner, flow, cmd = parts[:4]
        freq = "A" if since is None or (since.month == 1 and since.day == 1) else "M"
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        cap = self.cfg.rate_limit.records_per_call if self.cfg.rate_limit else None
        params: dict[str, Any] = {
            "reporterCode": reporter,
            "partnerCode": partner,
            "flowCode": flow,
            "cmdCode": cmd,
        }
        if since is not None:
            params["period"] = f"{since.year:04d}" if freq == "A" else f"{since.year:04d}{since.month:02d}"
        return client.get(
            urljoin(self.cfg.access.base_url or "", f"get/C/{freq}/HS"),
            params=params,
            extra_request={"records_per_call": cap},
        )

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse a Comtrade page. See `parse_comtrade` (spec §9.1)."""
        return parse_comtrade(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """Comtrade has no release calendar in this connector (spec §9.1). Returns []."""
        return []
