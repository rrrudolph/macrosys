"""Consensus calendar: legacy SQLite reader, store, conversion, matching, and the CLI (spec §9.8, P2.4). No network."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import yaml

from macrosys.core.contracts import ParserChangedError
from macrosys.core.quantity import Quantity
from macrosys.events.cli_commands import _cmd_build, _cmd_consensus_load, _cmd_study
from macrosys.events.config import ConsensusConfig, ConsensusMapping, EventsConfig, ResponseSpec
from macrosys.events.consensus import ConsensusLookup, printed_to_change, read_legacy_sqlite
from macrosys.events.schema import ConsensusPrint
from macrosys.events.store import ConsensusStore, SurpriseStore
from macrosys.events.study import run_study
from macrosys.events.surprise import build_surprises, headline_change
from macrosys.store.observations import ObservationStore
from macrosys.store.schema import create_schema
from tests.events.support import (
    RATE,
    RELEASE,
    YIELD,
    business_days,
    daily_rows,
    make_config,
    month_start,
    monthly_release_rows,
    new_store,
    next_month,
    release_instant,
)

SOURCE = "test_calendar"
CPI_EVENT = "USD Test CPI m/m"
RATE_EVENT = "USD Test Unemployment Rate"
LOADED_AT = datetime(2030, 6, 1, tzinfo=UTC)


def _legacy_db(path: Path, rows: list[tuple[str, str, str, float | None, float | None, float | None]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ff_cal_raw (datetime TEXT, ccy TEXT, ccy_event TEXT, forecast REAL, actual REAL, previous REAL)")
    conn.executemany("INSERT INTO ff_cal_raw VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def _consensus_config(responses: list[ResponseSpec] | None = None) -> EventsConfig:
    base = make_config(responses=responses or [ResponseSpec(release=RELEASE, market=YIELD, sign=1, mechanisms=["C09_policy_path"], reference=None, label="E")], horizons=[1])
    consensus = ConsensusConfig(
        source_id=SOURCE,
        forecaster_name="test_consensus",
        mappings=[
            ConsensusMapping(release=RELEASE, event=CPI_EVENT, kind="change", unit="pct", tolerance=0.051),
            ConsensusMapping(release=RATE, event=RATE_EVENT, kind="level", unit="pct", tolerance=0.051),
        ],
    )
    return base.model_copy(update={"consensus": consensus})


def _print(event: str, when: datetime, forecast: float | None, actual: float | None, previous: float | None = None) -> ConsensusPrint:
    return ConsensusPrint(
        source=SOURCE,
        currency=event.split(" ", 1)[0],
        event=event,
        scheduled_at=when,
        forecast=forecast,
        actual=actual,
        previous=previous,
        ingested_at=LOADED_AT,
        source_hash="f" * 64,
    )


def _cpi_prints(levels: list[float], first: date, *, round_to: float = 0.1, skip: set[date] = frozenset(), wrong_actual: set[date] = frozenset(), offset: float = 0.05) -> list[ConsensusPrint]:
    """Calendar rows whose printed actual is the true month-on-month change rounded to the print resolution; the forecast sits ``offset`` above it."""
    out: list[ConsensusPrint] = []
    period = first
    for index in range(1, len(levels)):
        period = next_month(period)
        if period in skip:
            continue
        change = (levels[index] / levels[index - 1] - 1.0) * 100.0
        printed = round(change / round_to) * round_to
        actual = printed + 1.0 if period in wrong_actual else printed
        out.append(_print(CPI_EVENT, release_instant(period), printed + offset, actual, None))
    return out


# -- legacy reader ----------------------------------------------------------------------------------


def test_read_legacy_sqlite_types_rows_and_drops_impossible_ones(tmp_path):
    db = _legacy_db(
        tmp_path / "cal.db",
        [
            ("2024-03-12 12:30:00", "USD", "USD CPI m/m", 0.3, 0.4, 0.2),
            ("2024-03-12 12:30:00", "USD", "USD Core CPI m/m", 0.3, None, 0.4),
            ("2031-01-01 13:30:00", "USD", "USD Nonfarm Payrolls", 170.0, 180.0, 165.0),  # an actual before its release clock: a test row
            ("2031-01-02 13:30:00", "EUR", "EUR Flash CPI y/y", 2.1, None, None),  # a genuine future row keeps its forecast
            ("2024-03-13 00:00:00", "JPY", "JPY Tankan", None, None, None),
        ],
    )
    loaded = read_legacy_sqlite(db, SOURCE, loaded_at=LOADED_AT)
    assert loaded.rows_read == 5
    assert loaded.dropped_future_actual == 1
    assert loaded.dropped_empty == 1
    assert [p.event for p in loaded.prints] == ["USD CPI m/m", "USD Core CPI m/m", "EUR Flash CPI y/y"]
    cpi = loaded.prints[0]
    assert cpi.scheduled_at == datetime(2024, 3, 12, 12, 30, tzinfo=UTC)
    assert (cpi.forecast, cpi.actual, cpi.previous) == (0.3, 0.4, 0.2)
    assert cpi.quality_flag == "unofficial"
    assert cpi.ingested_at == LOADED_AT
    assert len(cpi.source_hash) == 64 and cpi.source_hash == loaded.source_hash
    assert loaded.prints[1].actual is None


def test_legacy_schema_change_is_rejected(tmp_path):
    conn = sqlite3.connect(tmp_path / "bad.db")
    conn.execute("CREATE TABLE ff_cal_raw (datetime TEXT, ccy TEXT, ccy_event TEXT, forecast REAL, actual REAL, previous REAL, impact TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ParserChangedError, match="columns"):
        read_legacy_sqlite(tmp_path / "bad.db", SOURCE, loaded_at=LOADED_AT)
    with pytest.raises(FileNotFoundError):
        read_legacy_sqlite(tmp_path / "missing.db", SOURCE, loaded_at=LOADED_AT)


def test_print_record_rejects_naive_clocks_and_currency_mismatch():
    with pytest.raises(ValueError, match="timezone-aware"):
        _print(CPI_EVENT, datetime(2024, 3, 12, 12, 30), 0.3, 0.4)
    with pytest.raises(ValueError, match="does not start with its currency"):
        ConsensusPrint(source=SOURCE, currency="EUR", event=CPI_EVENT, scheduled_at=LOADED_AT, forecast=0.1, actual=None, previous=None, ingested_at=LOADED_AT, source_hash="f" * 64)


# -- store ------------------------------------------------------------------------------------------


def test_consensus_store_replaces_per_source_and_bounds_by_as_of():
    conn, _ = new_store()
    store = ConsensusStore(conn)
    early = _print(CPI_EVENT, datetime(2024, 3, 12, 12, 30, tzinfo=UTC), 0.3, 0.4)
    late = _print(CPI_EVENT, datetime(2024, 4, 10, 12, 30, tzinfo=UTC), 0.2, 0.3)
    other = _print(RATE_EVENT, datetime(2024, 4, 5, 12, 30, tzinfo=UTC), 3.9, 3.8)
    assert store.replace(SOURCE, [early, late, other]) == 3
    assert [p.event for p in store.read(SOURCE)] == [CPI_EVENT, RATE_EVENT, CPI_EVENT]
    assert store.read(SOURCE, {CPI_EVENT}, as_of=datetime(2024, 3, 31, tzinfo=UTC)) == [early]
    assert store.read(SOURCE, set()) == []
    assert store.read("another_source") == []
    assert store.replace(SOURCE, [late]) == 1
    assert store.read(SOURCE) == [late]
    with pytest.raises(ValueError, match="does not belong"):
        store.replace("another_source", [late])


# -- conversion -------------------------------------------------------------------------------------


def test_printed_to_change_kinds():
    previous = Quantity(value=3.9, unit="pct")
    change = ConsensusMapping(release=RELEASE, event=CPI_EVENT, kind="change", unit="pct", tolerance=0.05)
    level = ConsensusMapping(release=RATE, event=RATE_EVENT, kind="level", unit="pct", tolerance=0.05)
    annual = ConsensusMapping(release=RELEASE, event="USD GDP q/q", kind="annualized_q_pct", unit="pct", tolerance=0.03)
    assert printed_to_change(0.3, change, previous, "pct_change", "p").model_dump() == Quantity(value=0.3, unit="pct", provenance="p").model_dump()
    as_level = printed_to_change(4.1, level, previous, "diff", "p")
    assert as_level.unit == "pp" and as_level.value == pytest.approx(0.2)
    quarterly = printed_to_change(2.1, annual, previous, "pct_change", "p")
    assert quarterly.unit == "pct" and quarterly.value == pytest.approx(((1.021) ** 0.25 - 1.0) * 100.0)
    with pytest.raises(ValueError, match="percent"):
        printed_to_change(2.1, annual.model_copy(update={"unit": "pp"}), previous, "pct_change", "p")


# -- matching inside build_surprises ----------------------------------------------------------------


def _levels(n: int, start: float = 100.0, step: float = 0.3) -> list[float]:
    return [start + step * i + (0.2 if i % 4 == 0 else 0.0) for i in range(n)]


def test_consensus_forecast_replaces_the_statistical_one_and_mismatches_are_dropped():
    conn, store = new_store()
    first = month_start(2020, 1)
    levels = _levels(16)
    store.append(monthly_release_rows(levels, first))
    config = _consensus_config()
    missing = month_start(2020, 6)
    wrong = month_start(2020, 9)
    ConsensusStore(conn).replace(SOURCE, _cpi_prints(levels, first, skip={missing}, wrong_actual={wrong}))
    lookup = ConsensusLookup(conn, config)
    events = build_surprises(conn, config.release(RELEASE), config, consensus=lookup)
    statistical = build_surprises(conn, config.release(RELEASE), config)

    periods = {event.ref_period_start for event in events}
    assert missing not in periods and wrong not in periods
    # Every period from the second one on is an event: a consensus needs no warm-up history.
    expected = [month_start(2020, m) for m in range(2, 13)] + [month_start(2021, m) for m in range(1, 5)]
    assert periods == {p for p in expected if p not in {missing, wrong}}
    for event in events:
        assert event.forecaster == "test_consensus"
        assert event.forecast_change.provenance == f"{SOURCE}:{CPI_EVENT}"
        # The forecast is the printed consensus (rounded actual + 0.05), not a function of earlier prints.
        printed = round(event.actual_change.value / 0.1) * 0.1
        assert event.forecast_change.value == pytest.approx(printed + 0.05)
        assert event.surprise.value == pytest.approx(event.actual_change.value - event.forecast_change.value)
    stats = lookup.stats[RELEASE]
    assert stats.matched == len(events) and stats.no_print == 1 and stats.mismatch == 1
    assert stats.mismatches[0][0] == release_instant(wrong).date()
    assert all(event.forecaster == config.forecaster.name for event in statistical)
    # Standardisation uses consensus surprises only.
    with_sigma = [e for e in events if e.surprise_sigma is not None]
    assert with_sigma and all(e.n_history >= config.min_history_events for e in with_sigma)


def test_level_kind_forms_the_consensus_change_against_the_prior_print():
    conn, store = new_store()
    first = month_start(2020, 1)
    rates = [3.5, 3.6, 3.6, 3.7, 3.9, 3.8, 3.8, 4.0]
    store.append(monthly_release_rows(rates, first, unit="pct"))
    config = _consensus_config()
    prints = []
    period = first
    for index in range(1, len(rates)):
        period = next_month(period)
        prints.append(_print(RATE_EVENT, release_instant(period), rates[index - 1], rates[index], rates[index - 1]))  # consensus: unchanged rate
    ConsensusStore(conn).replace(SOURCE, prints)
    lookup = ConsensusLookup(conn, config)
    events = build_surprises(conn, config.release(RATE), config, consensus=lookup)
    assert len(events) == len(rates) - 1
    for event in events:
        assert event.forecast_change.unit == "pp" and event.forecast_change.value == pytest.approx(0.0)
        assert event.surprise.value == pytest.approx(event.actual_change.value)
    assert lookup.stats[RATE].matched == len(events)


def test_ambiguous_missing_forecast_and_unmapped_release_are_counted_or_ignored():
    conn, store = new_store()
    first = month_start(2020, 1)
    levels = _levels(6)
    store.append(monthly_release_rows(levels, first))
    config = _consensus_config()
    prints = _cpi_prints(levels, first)
    second = month_start(2020, 3)
    duplicate = _print(CPI_EVENT, release_instant(second, hour=14), 0.1, 0.1)
    no_forecast = month_start(2020, 4)
    prints = [p if p.scheduled_at.date() != release_instant(no_forecast).date() else p.model_copy(update={"forecast": None}) for p in prints]
    ConsensusStore(conn).replace(SOURCE, [*prints, duplicate])
    lookup = ConsensusLookup(conn, config)
    events = build_surprises(conn, config.release(RELEASE), config, consensus=lookup)
    periods = {event.ref_period_start for event in events}
    assert second not in periods and no_forecast not in periods
    stats = lookup.stats[RELEASE]
    assert stats.ambiguous == 1 and stats.no_forecast == 1
    # A release with no mapping yields no events and no stats under the consensus forecaster.
    unmapped = config.model_copy(update={"consensus": config.consensus.model_copy(update={"mappings": config.consensus.mappings[1:]})})
    assert build_surprises(conn, unmapped.release(RELEASE), unmapped, consensus=ConsensusLookup(conn, unmapped)) == []
    assert not ConsensusLookup(conn, unmapped).mapped(RELEASE)


def test_a_unit_mismatch_between_mapping_and_store_is_a_configuration_error():
    conn, store = new_store()
    first = month_start(2020, 1)
    levels = _levels(5)
    store.append(monthly_release_rows(levels, first))
    config = _consensus_config()
    bad = config.consensus.mappings[0].model_copy(update={"unit": "pp"})
    config = config.model_copy(update={"consensus": config.consensus.model_copy(update={"mappings": [bad, config.consensus.mappings[1]]})})
    ConsensusStore(conn).replace(SOURCE, _cpi_prints(levels, first))
    with pytest.raises(ValueError, match="does not match the store's change unit"):
        build_surprises(conn, config.release(RELEASE), config, consensus=ConsensusLookup(conn, config))


def test_as_of_bounds_the_consensus_too():
    conn, store = new_store()
    first = month_start(2020, 1)
    levels = _levels(12)
    store.append(monthly_release_rows(levels, first))
    config = _consensus_config()
    ConsensusStore(conn).replace(SOURCE, _cpi_prints(levels, first))
    cutoff = release_instant(month_start(2020, 6))
    bounded = build_surprises(conn, config.release(RELEASE), config, as_of=cutoff, consensus=ConsensusLookup(conn, config, cutoff))
    assert bounded and all(event.published_at <= cutoff for event in bounded)
    assert ConsensusLookup(conn, config, cutoff).prints_loaded() < ConsensusLookup(conn, config).prints_loaded()


def test_later_calendar_or_vintage_rows_cannot_change_an_earlier_consensus_event():
    conn, store = new_store()
    first = month_start(2020, 1)
    levels = _levels(10)
    store.append(monthly_release_rows(levels, first))
    config = _consensus_config()
    prints = _cpi_prints(levels, first)
    ConsensusStore(conn).replace(SOURCE, prints)
    before = build_surprises(conn, config.release(RELEASE), config, consensus=ConsensusLookup(conn, config))
    store.append([monthly_release_rows(levels, first)[3].model_copy(update={"value": 150.0, "published_at": datetime(2025, 6, 1, 13, 30, tzinfo=UTC), "vintage_id": "2025-06-01"})])
    ConsensusStore(conn).replace(SOURCE, [*prints, _print(CPI_EVENT, datetime(2025, 7, 1, 12, 30, tzinfo=UTC), 0.2, 0.2)])
    after = build_surprises(conn, config.release(RELEASE), config, consensus=ConsensusLookup(conn, config))
    assert [e.model_dump() for e in before] == [e.model_dump() for e in after]


# -- config -----------------------------------------------------------------------------------------


def test_consensus_config_cross_references():
    base = make_config(responses=[ResponseSpec(release=RELEASE, market=YIELD, sign=1, mechanisms=["C09_policy_path"], reference=None, label="E")])
    good = ConsensusMapping(release=RELEASE, event=CPI_EVENT, kind="change", unit="pct", tolerance=0.05)
    with pytest.raises(ValueError, match="unknown release"):
        EventsConfig.model_validate({**base.model_dump(), "consensus": {"source_id": SOURCE, "forecaster_name": "c", "mappings": [good.model_copy(update={"release": "FRED:NOPE"}).model_dump()]}})
    with pytest.raises(ValueError, match="duplicate consensus mapping"):
        EventsConfig.model_validate({**base.model_dump(), "consensus": {"source_id": SOURCE, "forecaster_name": "c", "mappings": [good.model_dump(), good.model_copy(update={"event": "USD Other"}).model_dump()]}})
    with pytest.raises(ValueError, match="mapped to two releases"):
        EventsConfig.model_validate({**base.model_dump(), "consensus": {"source_id": SOURCE, "forecaster_name": "c", "mappings": [good.model_dump(), good.model_copy(update={"release": RATE}).model_dump()]}})
    with pytest.raises(ValueError, match="collides"):
        EventsConfig.model_validate({**base.model_dump(), "consensus": {"source_id": SOURCE, "forecaster_name": base.forecaster.name, "mappings": [good.model_dump()]}})
    with pytest.raises(ValueError, match="unknown unit"):
        ConsensusMapping(release=RELEASE, event=CPI_EVENT, kind="change", unit="thousands", tolerance=0.05)


def test_repository_config_consensus_block_is_consistent():
    from macrosys.events.config import load_events_config

    config = load_events_config()
    assert config.consensus is not None
    assert config.consensus.mapping_for("FRED:PAYEMS").unit == "persons_k"
    assert config.consensus.mapping_for("FRED:GDPC1").kind == "annualized_q_pct"
    assert config.consensus.mapping_for("FRED:PCEPI") is None


# -- CLI --------------------------------------------------------------------------------------------


def test_consensus_load_build_and_study_cli(tmp_path, capsys):
    db = tmp_path / "store.duckdb"
    conn = duckdb.connect(str(db))
    create_schema(conn)
    store = ObservationStore(conn)
    first = month_start(2016, 1)
    levels = _levels(40)
    store.append(monthly_release_rows(levels, first))
    days = business_days(date(2016, 1, 1), date(2019, 8, 31))
    store.append(daily_rows(YIELD, {day: 2.0 + 0.001 * i for i, day in enumerate(days)}, "pct"))
    conn.close()

    config = _consensus_config()
    config_path = tmp_path / "events.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    rows = []
    period = first
    for index in range(1, len(levels)):
        period = next_month(period)
        change = round((levels[index] / levels[index - 1] - 1.0) * 1000.0) / 10.0
        when = release_instant(period).replace(tzinfo=None).isoformat(sep=" ")
        rows.append((when, "USD", CPI_EVENT, change + 0.05, change, None))
    sqlite_path = _legacy_db(tmp_path / "cal.db", rows)

    assert _cmd_consensus_load(SimpleNamespace(db=db, sqlite=sqlite_path, config=config_path)) == 0
    out = capsys.readouterr().out
    assert f"stored {len(rows)}" in out and CPI_EVENT in out

    assert _cmd_build(SimpleNamespace(db=db, config=config_path, as_of=None, forecaster="consensus")) == 0
    out = capsys.readouterr().out
    assert "forecaster test_consensus" in out and f"matched {len(rows)}" in out and "no consensus mapping" not in out

    params = tmp_path / "params.yaml"
    assert _cmd_study(SimpleNamespace(db=db, config=config_path, as_of=None, forecaster="consensus", write_parameters=params)) == 0
    out = capsys.readouterr().out
    assert "survey consensus (test_consensus)" in out
    written = yaml.safe_load(params.read_text(encoding="utf-8"))
    assert written["forecaster"] == "test_consensus"
    assert "survey consensus" in written["caveat"]

    conn = duckdb.connect(str(db), read_only=True)
    try:
        stored = SurpriseStore(conn).read(RELEASE, "test_consensus")
        assert len(stored) == len(rows)
        assert ConsensusStore(conn).read(SOURCE)
        results = run_study(conn, config, consensus=ConsensusLookup(conn, config))
    finally:
        conn.close()
    assert results and results[0].n > 0
    assert results[0].beta is None or "forecaster=test_consensus" in results[0].beta.provenance


def test_consensus_cli_refuses_without_loaded_prints(tmp_path, capsys):
    db = tmp_path / "store.duckdb"
    conn = duckdb.connect(str(db))
    create_schema(conn)
    conn.close()
    config_path = tmp_path / "events.yaml"
    config_path.write_text(yaml.safe_dump(_consensus_config().model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="consensus-load"):
        _cmd_build(SimpleNamespace(db=db, config=config_path, as_of=None, forecaster="consensus"))
    plain = tmp_path / "plain.yaml"
    plain.write_text(yaml.safe_dump(make_config(responses=[ResponseSpec(release=RELEASE, market=YIELD, sign=1, mechanisms=["C09_policy_path"], reference=None, label="E")]).model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="consensus"):
        _cmd_study(SimpleNamespace(db=db, config=plain, as_of=None, forecaster="consensus", write_parameters=None))
    with pytest.raises(SystemExit, match="nothing to load"):
        _cmd_consensus_load(SimpleNamespace(db=db, sqlite=tmp_path / "x.db", config=plain))


def test_level_kind_uses_the_store_arithmetic():
    # The level kind must use the same arithmetic as the store's actual, or the cross-check would fail on rounding alone.
    previous = Quantity(value=100.0, unit="index")
    mapping = ConsensusMapping(release=RELEASE, event=CPI_EVENT, kind="level", unit="index", tolerance=0.05)
    via_store = headline_change(Quantity(value=100.3, unit="index"), previous, "pct_change")
    via_calendar = printed_to_change(100.3, mapping, previous, "pct_change", "p")
    assert via_calendar.unit == via_store.unit and via_calendar.value == pytest.approx(via_store.value)
