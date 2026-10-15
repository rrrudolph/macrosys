"""Outcome windows, coefficient recovery, sign tests, regimes, store, parameters, CLI (spec §10.1 item 11)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest
import yaml

from macrosys.events.cli_commands import _cmd_build, _cmd_study, format_report
from macrosys.events.config import RegimeSpec, ResponseSpec, StateSign, load_events_config
from macrosys.events.estimate import write_parameters
from macrosys.events.outcomes import market_outcomes
from macrosys.events.store import SurpriseStore
from macrosys.events.study import run_study
from macrosys.events.surprise import build_surprises, regime_state
from macrosys.mechanisms.library import load_library
from macrosys.store.schema import create_schema
from tests.events.support import (
    INDEX,
    RATE,
    RELEASE,
    YIELD,
    business_days,
    daily_rows,
    make_config,
    month_start,
    monthly_release_rows,
    new_store,
    obs,
    release_instant,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(20261014)


YIELD_UP = ResponseSpec(release=RELEASE, market=YIELD, sign=1, mechanisms=["C09_policy_path"], reference=None, label="E")
YIELD_DOWN = ResponseSpec(release=RELEASE, market=YIELD, sign=-1, mechanisms=["C09_policy_path"], reference=None, label="H")


def _release_levels(rng: np.random.Generator, n: int) -> list[float]:
    level = 100.0
    out = []
    for _ in range(n):
        level *= 1.0 + rng.normal(0.002, 0.003)
        out.append(level)
    return out


def _seed_release(store, rng: np.random.Generator, n_months: int = 60) -> None:
    store.append(monthly_release_rows(_release_levels(rng, n_months), month_start(2016, 1)))


def _market_from_events(conn, config, rng, *, beta: float, noise: float, start: date, end: date, state_sign=None) -> dict[date, float]:
    """A daily yield series whose release-day change is ``beta * z`` (optionally flipped by state) plus noise."""
    events = build_surprises(conn, config.release(RELEASE), config)
    z_by_date = {e.event_date: e.surprise_sigma for e in events if e.surprise_sigma is not None}
    days = business_days(start, end)
    level = 2.0
    values: dict[date, float] = {}
    for day in days:
        shock = rng.normal(0.0, noise)
        # A weekend/holiday release lands on the first market day on or after the event date.
        z = next((z_by_date[d] for d in (day, day - timedelta(days=1), day - timedelta(days=2)) if d in z_by_date), None)
        if z is not None:
            sign = 1.0 if state_sign is None else state_sign(day)
            shock += sign * beta * z / 100.0  # beta is in bp; the level is in pct
        level += shock
        values[day] = level
    return values


def test_outcome_window_is_pre_close_to_hth_close_and_handles_weekends_and_units():
    conn, store = new_store()
    config = make_config(responses=[YIELD_UP], horizons=[1, 3])
    store.append(monthly_release_rows([100.0 + i for i in range(10)], month_start(2020, 1)))
    events = build_surprises(conn, config.release(RELEASE), config)
    # 12 Sep 2020 is a Saturday: the event lands on Monday 14 Sep.
    saturday = next(e for e in events if e.event_date == date(2020, 9, 12))
    days = business_days(date(2020, 3, 1), date(2020, 12, 31))
    store.append(daily_rows(YIELD, {d: 1.0 + 0.01 * i for i, d in enumerate(days)}, "pct"))
    outcomes = market_outcomes(conn, [saturday], config.market(YIELD), [1, 3])
    one = next(o for o in outcomes if o.horizon_days == 1)
    three = next(o for o in outcomes if o.horizon_days == 3)
    assert one.pre_date == date(2020, 9, 11)
    assert one.post_date == date(2020, 9, 14)
    assert three.post_date == date(2020, 9, 16)
    assert one.change.unit == "bp"
    assert one.change.value == pytest.approx(1.0)  # 0.01 pct = 1 bp
    assert three.change.value == pytest.approx(3.0)


def test_outcomes_without_a_full_window_are_omitted():
    conn, store = new_store()
    config = make_config(responses=[YIELD_UP], horizons=[1, 5])
    store.append(monthly_release_rows([100.0 + i for i in range(10)], month_start(2020, 1)))
    events = build_surprises(conn, config.release(RELEASE), config)
    last = events[-1]
    store.append(daily_rows(YIELD, {last.event_date - timedelta(days=1): 1.0, last.event_date: 1.1}, "pct"))
    outcomes = market_outcomes(conn, [last], config.market(YIELD), [1, 5])
    assert [o.horizon_days for o in outcomes] == [1]


def test_study_recovers_the_coefficient_and_scores_the_sign(rng):
    conn, store = new_store()
    config = make_config(responses=[YIELD_UP], horizons=[1])
    _seed_release(store, rng, 72)
    values = _market_from_events(conn, config, rng, beta=4.0, noise=0.01, start=date(2016, 1, 1), end=date(2022, 6, 30))
    store.append(daily_rows(YIELD, values, "pct"))
    [result] = run_study(conn, config)
    assert result.n >= 40
    assert result.beta is not None and result.beta.unit == "bp"
    assert result.beta.low is not None and result.beta.high is not None
    assert result.beta.low < 4.0 < result.beta.high
    assert result.t_stat is not None and result.t_stat > 3.0
    assert result.sign_accuracy is not None and result.sign_accuracy > 0.75
    assert result.p_value is not None and result.p_value < 0.01
    assert result.oos_sign_accuracy is not None and result.oos_sign_accuracy > 0.6
    assert result.n_effective == pytest.approx(result.n)  # monthly events, one-day horizon: no overlap
    assert "per 1sd surprise" in (result.beta.provenance or "")


def test_wrong_predicted_sign_scores_below_chance(rng):
    conn, store = new_store()
    config = make_config(responses=[YIELD_DOWN], horizons=[1])
    _seed_release(store, rng, 72)
    values = _market_from_events(conn, config, rng, beta=4.0, noise=0.01, start=date(2016, 1, 1), end=date(2022, 6, 30))
    store.append(daily_rows(YIELD, values, "pct"))
    [result] = run_study(conn, config)
    assert result.sign_accuracy is not None and result.sign_accuracy < 0.25
    # The fitted slope still has the data's sign; only the library's claim is wrong.
    assert result.beta is not None and result.beta.mid > 0


def test_no_relationship_gives_no_significant_result(rng):
    conn, store = new_store()
    config = make_config(responses=[YIELD_UP], horizons=[1])
    _seed_release(store, rng, 72)
    values = _market_from_events(conn, config, rng, beta=0.0, noise=0.03, start=date(2016, 1, 1), end=date(2022, 6, 30))
    store.append(daily_rows(YIELD, values, "pct"))
    [result] = run_study(conn, config)
    assert result.t_stat is not None and abs(result.t_stat) < 2.5
    assert result.p_value is not None and result.p_value > 0.01


def test_pairs_without_data_report_zero_events_rather_than_failing():
    conn, store = new_store()
    config = make_config(responses=[YIELD_UP], horizons=[1, 5])
    store.append(monthly_release_rows([100.0 + i for i in range(4)], month_start(2020, 1)))
    results = run_study(conn, config)
    assert [r.n for r in results] == [0, 0]
    assert all(r.beta is None and r.sign_accuracy is None for r in results)


def test_pct_change_response_on_a_level_series(rng):
    conn, store = new_store()
    spec = ResponseSpec(release=RELEASE, market=INDEX, sign=-1, mechanisms=["C05_equity_duration"], reference=None, label="H")
    config = make_config(responses=[spec], horizons=[1])
    _seed_release(store, rng, 60)
    events = build_surprises(conn, config.release(RELEASE), config)
    z_by_date = {e.event_date: e.surprise_sigma for e in events if e.surprise_sigma is not None}
    level = 1000.0
    values: dict[date, float] = {}
    for day in business_days(date(2016, 1, 1), date(2021, 6, 30)):
        ret = rng.normal(0.0, 0.002)
        z = next((z_by_date[d] for d in (day, day - timedelta(days=1), day - timedelta(days=2)) if d in z_by_date), None)
        if z is not None:
            ret -= 0.01 * z
        level *= 1.0 + ret
        values[day] = level
    store.append(daily_rows(INDEX, values, "index"))
    [result] = run_study(conn, config)
    assert result.beta is not None and result.beta.unit == "pct"
    assert result.beta.mid < 0
    assert result.sign_accuracy is not None and result.sign_accuracy > 0.7


def _unemployment_rows(months: int, start: date) -> list:
    """A rate that rises sharply in the second half so the Sahm rule flips to contraction."""
    rows = []
    period = start
    for i in range(months):
        rate = 4.0 if i < months // 2 else 4.0 + 0.3 * (i - months // 2 + 1)
        rows.append(obs(RATE, period, rate, release_instant(period, 12, 30), unit="pct"))
        period = date(period.year + period.month // 12, period.month % 12 + 1, 1)
    return rows


def test_regime_state_is_point_in_time_and_flips_under_the_sahm_rule():
    conn, store = new_store()
    store.append(_unemployment_rows(40, month_start(2016, 1)))
    regime = RegimeSpec(name="us_expansion", series_id=RATE, rule="sahm", threshold_pp=0.5)
    early = release_instant(month_start(2017, 6))
    late = release_instant(month_start(2019, 3))
    assert regime_state(conn, regime, early) is True
    assert regime_state(conn, regime, late) is False
    assert regime_state(conn, regime, release_instant(month_start(2016, 6))) is None  # fewer than 12 prints
    # The print released exactly at `at` is not visible to the state at `at`.
    conn.execute("DELETE FROM observation WHERE series_id = ? AND ref_period_start > ?", [RATE, date(2017, 5, 1)])
    assert regime_state(conn, regime, release_instant(month_start(2017, 5))) is True


def test_state_dependent_sign_splits_by_regime(rng):
    conn, store = new_store()
    regime = RegimeSpec(name="us_expansion", series_id=RATE, rule="sahm", threshold_pp=0.5)
    spec = ResponseSpec(
        release=RELEASE,
        market=YIELD,
        sign=StateSign(regime="us_expansion", when_true=1, when_false=-1),
        mechanisms=["B11_monetary_transmission"],
        reference="BoydHuJagannathan2005",
        label="E",
    )
    config = make_config(responses=[spec], regimes=[regime], horizons=[1])
    months = 96
    _seed_release(store, rng, months)
    store.append(_unemployment_rows(months, month_start(2016, 1)))
    flip_date = release_instant(month_start(2016 + months // 24, 1)).date()

    def state_sign(day: date) -> float:
        return 1.0 if day < flip_date else -1.0

    values = _market_from_events(
        conn, config, rng, beta=4.0, noise=0.01, start=date(2016, 1, 1), end=date(2024, 6, 30), state_sign=state_sign
    )
    store.append(daily_rows(YIELD, values, "pct"))
    [result] = run_study(conn, config)
    assert result.predicted_sign is None
    splits = {split.state: split for split in result.regimes}
    assert splits[True].n > 0 and splits[False].n > 0
    assert splits[True].sign_accuracy is not None and splits[True].sign_accuracy > 0.7
    assert splits[False].sign_accuracy is not None and splits[False].sign_accuracy > 0.7
    assert result.sign_accuracy is not None and result.sign_accuracy > 0.7


def test_store_roundtrip_and_latest_as_of(rng):
    conn, store = new_store()
    config = make_config(responses=[YIELD_UP])
    _seed_release(store, rng, 30)
    events = build_surprises(conn, config.release(RELEASE), config)
    surprises = SurpriseStore(conn)
    assert surprises.replace(RELEASE, config.forecaster.name, events) == len(events)
    back = surprises.read(RELEASE, config.forecaster.name)
    assert [e.model_dump() for e in back] == [e.model_dump() for e in events]
    cutoff = events[5].published_at
    latest = surprises.latest(RELEASE, config.forecaster.name, cutoff)
    assert latest is not None and latest.ref_period_start == events[5].ref_period_start
    assert surprises.latest(RELEASE, config.forecaster.name, events[0].published_at - timedelta(seconds=1)) is None
    # Replacing is idempotent.
    assert surprises.replace(RELEASE, config.forecaster.name, events) == len(events)
    assert len(surprises.read(RELEASE, config.forecaster.name)) == len(events)


def test_repo_config_loads_and_names_real_mechanisms():
    config = load_events_config()
    library = load_library(ROOT / "mechanisms")
    ids = set(library.ids())
    for spec in config.responses:
        for mechanism_id in spec.mechanisms:
            assert mechanism_id in ids, mechanism_id
    assert config.forecaster.name == "ar1:60"


def test_config_rejects_dangling_references(tmp_path):
    text = load_events_config.__globals__["default_events_config_path"]().read_text(encoding="utf-8")
    broken = text.replace("market: FRED:DGS2\n    sign: 1\n    mechanisms: [C09_policy_path, B10_phillips]", "market: FRED:NOPE\n    sign: 1\n    mechanisms: [C09_policy_path, B10_phillips]", 1)
    path = tmp_path / "events.yaml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(ValueError):
        load_events_config(path)


def test_write_parameters_and_cli(tmp_path, rng, capsys):
    db = tmp_path / "store.duckdb"
    conn = duckdb.connect(str(db))
    create_schema(conn)
    from macrosys.store.observations import ObservationStore

    store = ObservationStore(conn)
    config = make_config(responses=[YIELD_UP], horizons=[1])
    _seed_release(store, rng, 72)
    values = _market_from_events(conn, config, rng, beta=4.0, noise=0.01, start=date(2016, 1, 1), end=date(2022, 6, 30))
    store.append(daily_rows(YIELD, values, "pct"))
    conn.close()
    config_path = tmp_path / "events.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False), encoding="utf-8")

    build_args = SimpleNamespace(db=db, config=config_path, as_of=None)
    study_args = SimpleNamespace(db=db, config=config_path, as_of=None, write_parameters=tmp_path / "event_response.yaml")
    assert _cmd_build(build_args) == 0
    assert _cmd_study(study_args) == 0
    out = capsys.readouterr().out
    assert RELEASE in out and "pooled sign agreement" in out and "not a consensus" in out
    written = yaml.safe_load(study_args.write_parameters.read_text(encoding="utf-8"))
    [entry] = written["entries"]
    assert entry["name"] == f"{RELEASE}->{YIELD}@1d"
    assert entry["beta"]["unit"] == "bp"
    assert entry["beta"]["low"] < 4.0 < entry["beta"]["high"]
    assert "event_study" in entry["beta"]["source"]
    assert "consensus" in written["caveat"]

    conn = duckdb.connect(str(db), read_only=True)
    try:
        stored = SurpriseStore(conn).read(RELEASE, config.forecaster.name)
    finally:
        conn.close()
    assert len(stored) > 0


def test_format_report_handles_empty_results():
    lines = format_report([], 0.5)
    assert lines[0].startswith("release")
    assert "not a consensus" in lines[-1]


def test_write_parameters_omits_pairs_without_a_coefficient(tmp_path):
    conn, store = new_store()
    config = make_config(responses=[YIELD_UP], horizons=[1])
    store.append(monthly_release_rows([100.0 + i for i in range(4)], month_start(2020, 1)))
    results = run_study(conn, config)
    path = write_parameters(results, tmp_path / "p.yaml", generated_at=datetime(2026, 10, 14, tzinfo=UTC))
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["entries"] == []
