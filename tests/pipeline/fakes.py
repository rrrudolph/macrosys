"""Offline stand-in for a DataSource. Parses bytes already stored by fetch."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from macrosys.core.contracts import Observation, ParserChangedError, RawPayload, RawStore

NOW = datetime(2026, 10, 14, 15, tzinfo=UTC)


class FakeSource:
    """One good series and one payload whose shape the parser rejects."""

    source_id = "FAKE"
    tier = "record"

    def discover(self) -> list:
        return []

    def schedule(self) -> list:
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        body = b"not-a-schema" if series_id == "BAD" else b'{"value": 1}'
        return raw.put(
            self.source_id,
            {"series_id": series_id},
            body,
            status=200,
            content_type="application/json",
            fetched_at=NOW,
        )

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        if content == b"not-a-schema":
            raise ParserChangedError("schema changed")
        series_id = str(payload.request["series_id"])
        return [
            Observation(
                series_id=f"FAKE:{series_id}",
                ref_period_start=date(2026, 7, 1),
                ref_period_end=date(2026, 9, 30),
                value=1.0,
                unit="index",
                published_at=NOW,
                ingested_at=NOW,
                vintage_id=series_id,
                source_url="https://example.test/fake",
                source_hash=payload.sha256,
            )
        ]


class BarSource:
    """Price producer with the ``bars`` method ingest_prices prefers."""

    source_id = "BARS"

    def bars(self, instrument_id: str, start: date, end: date) -> list[Any]:
        from macrosys.core.contracts import PriceBar

        return [
            PriceBar(
                instrument_id=instrument_id,
                trade_date=start,
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=None,
                ccy="USD",
                source_id=self.source_id,
                tier="record",
                ingested_at=NOW,
            )
        ]
