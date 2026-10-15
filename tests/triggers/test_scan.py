"""Rules to shocks, point-in-time, with a fake reader and with the DuckDB store. No network."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from macrosys.core.contracts import Observation, PriceBar, SeriesMeta
from macrosys.store.observations import ObservationStore
from macrosys.store.prices import PriceStore
from macrosys.store.schema import create_schema
from macrosys.triggers.rules import SeriesRef, TriggerConfig, load_triggers
from macrosys.triggers.scan import ReadSeries, StoreReader, replay, scan, write_replay

REPO = Path(__file__).resolve().parents[2]
AS_OF = datetime(2026, 3, 6, 23, 59, 59, tzinfo=UTC)


def _rules(**overrides: object) -> TriggerConfig:
    payload: dict[str, object] = {
        "fresh_days": 7,
        "rearm_days": 20,
        "stale_days": 7,
        "rules": [
            {
                "id": "tech_drawdown",
                "series": {"kind": "price", "instrument": "XLK"},
                "signal": {"kind": "drawdown", "lookback_days": 60},
                "threshold": {"op": "<=", "value": -10.0, "unit": "pct"},
                "shock": {
                    "type": "financial",
                    "subtype": "collateral",
                    "origin": {"entity_type": "instrument", "entity_id": "XLK"},
                    "stage": "observed",
                },
            },
            {
                "id": "vol_spike",
                "series": {"kind": "observation", "series_id": "FRED:VIXCLS"},
                "signal": {"kind": "level"},
                "threshold": {"op": ">=", "value": 30.0, "unit": "index"},
                "shock": {
                    "type": "financial",
                    "subtype": "flows",
                    "origin": {"entity_type": "economy", "entity_id": "USA"},
                    "stage": "observed",
                },
            },
        ],
    }
    payload.update(overrides)
    return TriggerConfig.model_validate(payload)


class _FakeReader:
    def __init__(self, series: dict[str, ReadSeries]) -> None:
        self._series = series

    def read(self, ref: SeriesRef, start: date, as_of: datetime) -> ReadSeries:
        found = self._series.get(ref.label)
        if found is None:
            return ReadSeries(values=pd.Series(dtype=float))
        keep = [pd.Timestamp(ts).date() <= as_of.date() for ts in found.values.index]
        values = found.values.loc[keep]
        published = None if found.published is None else found.published.loc[keep]
        return ReadSeries(values=values, published=published, unit=found.unit)


def _bdays(end: date, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(end=pd.Timestamp(end), periods=n)


def test_scan_builds_a_shock_from_the_crossing() -> None:
    index = _bdays(AS_OF.date(), 70)
    closes = pd.Series([100.0] * 65 + [100.0, 95.0, 89.0, 87.0, 86.0], index=index)
    vix = pd.Series([15.0] * 70, index=index)
    reader = _FakeReader(
        {
            "XLK": ReadSeries(values=closes),
            "FRED:VIXCLS": ReadSeries(values=vix, published=pd.Series(index, index=index), unit="index"),
        }
    )
    fired = scan(reader, _rules(), AS_OF)
    assert [item.rule_id for item in fired] == ["tech_drawdown"]
    hit = fired[0]
    assert hit.crossed == pd.Timestamp(index[67]).date()
    assert hit.value == pytest.approx(-14.0)
    shock = hit.shock
    assert shock.id == "tech_drawdown-2026-03-06"
    assert shock.type == "financial" and shock.subtype == "collateral"
    assert shock.size.value == pytest.approx(-14.0) and shock.size.unit == "pct"
    assert shock.size.provenance == "trigger:tech_drawdown"
    assert shock.start == hit.crossed
    assert shock.information_cutoff == AS_OF
    assert shock.evidence_refs == [f"trigger:tech_drawdown:XLK:{hit.crossed.isoformat()}"]
    assert "XLK reads -14 pct" in hit.sentence()


def test_scan_requires_an_aware_instant_and_a_fitting_unit() -> None:
    index = _bdays(AS_OF.date(), 40)
    vix = pd.Series([35.0] * 40, index=index)
    reader = _FakeReader({"FRED:VIXCLS": ReadSeries(values=vix, unit="pct")})
    with pytest.raises(ValueError, match="timezone-aware"):
        scan(reader, _rules(), AS_OF.replace(tzinfo=None))
    with pytest.raises(ValueError, match="level threshold is 'index' but the series is 'pct'"):
        scan(reader, _rules(), AS_OF)


def test_replay_is_weekly_and_write_counts_by_rule(tmp_path: Path) -> None:
    index = _bdays(AS_OF.date(), 90)
    # Quiet, then a spike that stays: the replay must file it once.
    vix = pd.Series([15.0] * 60 + [40.0] * 30, index=index)
    reader = _FakeReader(
        {"FRED:VIXCLS": ReadSeries(values=vix, published=pd.Series(index, index=index), unit="index")}
    )
    start = pd.Timestamp(index[0]).date()
    runs = list(replay(reader, _rules(), start, AS_OF.date(), step_days=7))
    assert all(runs[i + 1][0] - runs[i][0] == timedelta(days=7) for i in range(len(runs) - 1))
    fires = [(day, fired) for day, fired in runs if fired]
    assert len(fires) == 1
    assert fires[0][1][0].crossed == pd.Timestamp(index[60]).date()
    counts = write_replay(tmp_path / "replay.jsonl", iter(runs))
    assert counts == {"vol_spike": 1}
    lines = (tmp_path / "replay.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and '"rule_id": "vol_spike"' in lines[0]


def test_store_reader_is_point_in_time() -> None:
    conn = duckdb.connect(":memory:")
    create_schema(conn)
    prices = PriceStore(conn)
    obs = ObservationStore(conn)
    index = _bdays(AS_OF.date(), 10)
    ingested = datetime(2026, 3, 7, tzinfo=UTC)
    prices.append_bars(
        [
            PriceBar(
                instrument_id="TIINGO:XLK",
                trade_date=pd.Timestamp(ts).date(),
                open=None,
                high=None,
                low=None,
                close=100.0 + i,
                volume=None,
                ccy="USD",
                source_id="TIINGO",
                tier="record",
                ingested_at=ingested,
            )
            for i, ts in enumerate(index)
        ]
    )
    obs.upsert_series(
        [
            SeriesMeta(
                series_id="FRED:VIXCLS",
                source_id="FRED",
                native_id="VIXCLS",
                title="VIX",
                unit="index",
                frequency="D",
            )
        ]
    )
    obs.append(
        [
            Observation(
                series_id="FRED:VIXCLS",
                ref_period_start=pd.Timestamp(ts).date(),
                ref_period_end=pd.Timestamp(ts).date(),
                value=20.0 + i,
                unit="index",
                # Each print is published the day after its reference date.
                published_at=datetime.combine(
                    pd.Timestamp(ts).date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC
                ),
                ingested_at=ingested,
                vintage_id=f"v{i}",
                source_url="https://example.test/vix",
                source_hash="a" * 64,
            )
            for i, ts in enumerate(index)
        ]
    )
    reader = StoreReader(conn, lambda ticker: f"TIINGO:{ticker}")
    early = datetime.combine(pd.Timestamp(index[4]).date(), datetime.max.time(), tzinfo=UTC)
    closes = reader.read(SeriesRef(kind="price", instrument="XLK"), pd.Timestamp(index[0]).date(), early)
    assert len(closes.values) == 5 and closes.unit is None and closes.published is None
    vix = reader.read(
        SeriesRef(kind="observation", series_id="FRED:VIXCLS"), pd.Timestamp(index[0]).date(), early
    )
    # The print for index[4] is published on index[4] + 1 day, so it is not visible yet.
    assert len(vix.values) == 4 and vix.unit == "index"
    assert vix.published is not None and len(vix.published) == 4
    conn.close()


def test_checked_in_rules_load_and_name_known_tickers() -> None:
    config = load_triggers(REPO / "config" / "triggers.yaml")
    assert len(config.rules) >= 4
    ids = [rule.id for rule in config.rules]
    assert "us_tech_drawdown" in ids
    import yaml

    pipeline = yaml.safe_load((REPO / "config" / "pipeline.yaml").read_text(encoding="utf-8"))
    priced = {ticker for group in pipeline["prices"] for ticker in group["instrument_ids"]}
    series = {sid for group in pipeline["series"] for sid in group["series_ids"]}
    for rule in config.rules:
        if rule.series.kind == "price":
            assert rule.series.instrument in priced, rule.id
        else:
            assert rule.series.series_id in series, rule.id
