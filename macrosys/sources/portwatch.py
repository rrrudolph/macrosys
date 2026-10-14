"""IMF PortWatch feature services (spec §9.1, §9.2, §9.6).

Confirmed 2026-10-14 from the ArcGIS items titled on portwatch.imf.org's data org
(`imf-dataviz`):

- ports: `.../PortWatch_ports_database/FeatureServer/0`
- chokepoints: `.../PortWatch_chokepoints_database/FeatureServer/0`

Those layers are current snapshots (`portid`, `vessel_count_*`, `share_*`). They do not carry a
daily date, so this is not a historical daily-calls archive. `published_at` is `fetched_at` and
`quality_flag='reconstructed_pubtime'`, matching the weekly update cadence in spec §9.6 rather than
a timestamp on the feature. The reference period is that same fetch date. Series ids are
`PORTWATCH:<portid>:<metric>`. Vessel counts are `count`; `share_*` fields are `share`. No API key.
"""

from __future__ import annotations

from datetime import date
from typing import Any

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
from macrosys.sources.common import fetched_at, source_url
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"

# [M] FeatureServer layer 0 URLs returned by the PortWatch ArcGIS org search on 2026-10-14.
LAYERS = {
    "ports": (
        "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
        "PortWatch_ports_database/FeatureServer/0/query"
    ),
    "chokepoints": (
        "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
        "PortWatch_chokepoints_database/FeatureServer/0/query"
    ),
}


def parse_features(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse one ArcGIS feature query into vessel-count and share observations (spec §9.1, §9.2)."""
    from macrosys.sources.common import load_json, require_mapping

    doc = require_mapping(load_json(content, what="PortWatch"), what="PortWatch")
    features = doc.get("features")
    if not isinstance(features, list):
        raise ParserChangedError("PortWatch payload is missing features")
    ingested = fetched_at(payload)
    ref = ingested.date()
    url = source_url(payload)
    rows: list[Observation] = []
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("attributes"), dict):
            raise ParserChangedError("PortWatch feature has no attributes")
        attrs = feature["attributes"]
        port_id = str(attrs.get("portid") or "").strip()
        if not port_id:
            raise ParserChangedError("PortWatch feature is missing portid")
        for name, raw in attrs.items():
            if name.startswith("vessel_count_"):
                unit = "count"
            elif name.startswith("share_"):
                unit = "share"
            else:
                continue
            if raw is None:
                continue
            assert_unit(unit)
            rows.append(
                Observation(
                    series_id=f"PORTWATCH:{port_id}:{name}",
                    ref_period_start=ref,
                    ref_period_end=ref,
                    value=float(raw),
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


class PortWatchSource:
    """PortWatch ArcGIS `DataSource` (spec §9.6). No API key."""

    source_id = "PORTWATCH"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """Port ids come from the feature layer (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """Query the ports or chokepoints layer (spec §9.6). `since` is unused; the layer is a snapshot.

        `series_id` `PORTWATCH:chokepoints` selects the chokepoint layer; anything else selects ports.
        """
        layer = "chokepoints" if "chokepoint" in series_id else "ports"
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        return client.get(
            LAYERS[layer],
            params={"where": "1=1", "outFields": "*", "f": "json"},
        )

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse a feature query. See `parse_features` (spec §9.1)."""
        return parse_features(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """The feature snapshot has no release calendar (spec §9.1). Returns []."""
        return []
