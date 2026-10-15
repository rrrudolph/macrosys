"""First prints, as-reported changes, point-in-time forecasts, and event dates (spec §9.2, §10.1 item 11)."""

from datetime import UTC, date, datetime, time

import pytest

from macrosys.core.quantity import Quantity
from macrosys.events.config import ResponseSpec
from macrosys.events.surprise import build_surprises, event_date_for, headline_change
from tests.events.support import (
    RELEASE,
    YIELD,
    make_config,
    month_start,
    monthly_release_rows,
    new_store,
    next_month,
    obs,
    release_instant,
)

CONFIG = make_config(responses=[ResponseSpec(release=RELEASE, market=YIELD, sign=1, mechanisms=["C09_policy_path"], reference=None, label="E")])
RELEASE_SPEC = CONFIG.release(RELEASE)


def _levels(n: int, start: float = 100.0, step: float = 0.3) -> list[float]:
    return [start + step * i for i in range(n)]


def test_first_print_and_prior_revision_visible_at_release_form_the_headline():
    conn, store = new_store()
    first = month_start(2020, 1)
    store.append(monthly_release_rows(_levels(10), first, revise_by=0.1))
    events = build_surprises(conn, RELEASE_SPEC, CONFIG)
    # Period 0 has no prior; period 1 has a prior but no forecast history; period 2 is the first event.
    assert events[0].ref_period_start == month_start(2020, 3)
    event = events[0]
    assert event.actual.value == pytest.approx(100.6)
    # The revision of February (+0.1) shipped with the March release, so the headline uses it.
    assert event.previous.value == pytest.approx(100.3 + 0.1)
    assert event.actual_change.unit == "pct"
    assert event.actual_change.value == pytest.approx((100.6 / 100.4 - 1.0) * 100.0)
    assert event.published_at == release_instant(month_start(2020, 3))
    assert event.event_date == event.published_at.date()


def test_later_vintage_cannot_change_an_earlier_event():
    conn, store = new_store()
    first = month_start(2020, 1)
    store.append(monthly_release_rows(_levels(12), first))
    before = build_surprises(conn, RELEASE_SPEC, CONFIG)
    # A benchmark revision of March 2020 published years later.
    store.append([obs(RELEASE, month_start(2020, 3), 150.0, datetime(2025, 6, 1, 13, 30, tzinfo=UTC))])
    after = build_surprises(conn, RELEASE_SPEC, CONFIG)
    assert [e.model_dump() for e in before] == [e.model_dump() for e in after]


def test_as_of_bounds_the_event_set():
    conn, store = new_store()
    store.append(monthly_release_rows(_levels(12), month_start(2020, 1)))
    cutoff = release_instant(month_start(2020, 6))
    bounded = build_surprises(conn, RELEASE_SPEC, CONFIG, as_of=cutoff)
    assert bounded
    assert all(event.published_at <= cutoff for event in bounded)


def test_sigma_needs_history_and_uses_prior_surprises_only():
    conn, store = new_store()
    store.append(monthly_release_rows([100.0 + (i % 3) * 0.5 + i * 0.2 for i in range(20)], month_start(2020, 1)))
    events = build_surprises(conn, RELEASE_SPEC, CONFIG)
    without = [e for e in events if e.surprise_sigma is None]
    with_sigma = [e for e in events if e.surprise_sigma is not None]
    assert len(without) == CONFIG.min_history_events
    assert with_sigma
    assert all(e.n_history >= CONFIG.min_history_events for e in with_sigma)
    # Recompute the first standardized surprise by hand from earlier surprises.
    target = with_sigma[0]
    prior = [e.surprise.value for e in events if e.published_at < target.published_at]
    import numpy as np

    assert target.surprise_sigma == pytest.approx(target.surprise.value / np.std(prior[-CONFIG.sigma_window :], ddof=1))


def test_ar1_forecaster_extrapolates_persistence_and_falls_back_when_short():
    from macrosys.events.config import ForecasterSpec
    from macrosys.events.surprise import forecast_change

    ar1 = ForecasterSpec(method="ar1", window=60, min_fit=12)
    mean = ForecasterSpec(method="trailing_mean", window=60)
    # An exact AR(1): c_t = 0.1 + 0.8 c_{t-1}; the one-step forecast must reproduce it.
    series = [1.0]
    for _ in range(30):
        series.append(0.1 + 0.8 * series[-1])
    expected = 0.1 + 0.8 * series[-1]
    assert forecast_change(series, ar1) == pytest.approx(expected, rel=1e-6)
    assert forecast_change(series, mean) != pytest.approx(expected, rel=1e-3)
    short = series[:5]
    assert forecast_change(short, ar1) == pytest.approx(forecast_change(short, mean))
    assert forecast_change([], ar1) is None
    assert ar1.name == "ar1:60"


def test_mad_scale_resists_one_outlier():
    from macrosys.events.surprise import surprise_scale

    calm = [0.1, -0.1, 0.2, -0.2, 0.15, -0.15, 0.05, -0.05, 0.1, -0.1]
    with_outlier = [*calm, 10.0]
    mad_calm = surprise_scale(calm, "mad")
    mad_outlier = surprise_scale(with_outlier, "mad")
    std_outlier = surprise_scale(with_outlier, "std")
    assert mad_calm is not None and mad_outlier is not None and std_outlier is not None
    assert mad_outlier < 2 * mad_calm
    assert std_outlier > 10 * mad_outlier
    assert surprise_scale([0.0, 0.0, 0.0], "mad") is None


def test_stale_first_vintage_is_not_a_first_print():
    conn, store = new_store()
    rows = monthly_release_rows(_levels(8), month_start(2020, 1))
    # Replace the March 2020 first print with one "released" a year late (store history starts late).
    rows = [r for r in rows if r.ref_period_start != month_start(2020, 3)]
    rows.append(obs(RELEASE, month_start(2020, 3), 100.6, datetime(2021, 4, 12, 13, 30, tzinfo=UTC)))
    store.append(rows)
    events = build_surprises(conn, RELEASE_SPEC, CONFIG)
    assert all(e.ref_period_start != month_start(2020, 3) for e in events)


def test_event_date_cutoff_moves_late_releases_to_the_next_day():
    morning = datetime(2024, 3, 12, 13, 30, tzinfo=UTC)
    evening = datetime(2024, 3, 12, 20, 30, tzinfo=UTC)
    midnight = datetime(2024, 3, 12, 0, 0, tzinfo=UTC)
    assert event_date_for(morning, 20) == date(2024, 3, 12)
    assert event_date_for(evening, 20) == date(2024, 3, 13)
    assert event_date_for(midnight, 20) == date(2024, 3, 12)


def test_headline_change_units():
    pct = headline_change(Quantity(value=4.1, unit="pct"), Quantity(value=3.9, unit="pct"), "diff")
    assert pct.unit == "pp" and pct.value == pytest.approx(0.2)
    growth = headline_change(Quantity(value=101.0, unit="index"), Quantity(value=100.0, unit="index"), "pct_change")
    assert growth.unit == "pct" and growth.value == pytest.approx(1.0)
    with pytest.raises(ValueError):
        headline_change(Quantity(value=1.0, unit="pct"), Quantity(value=1.0, unit="index"), "diff")


def test_quarterly_periods_use_the_previous_stored_period():
    conn, store = new_store()
    rows = []
    period = month_start(2020, 1)
    for i in range(10):
        end = next_month(next_month(next_month(period))) - __import__("datetime").timedelta(days=1)
        released = datetime.combine(next_month(end).replace(day=28), time(12, 30), tzinfo=UTC)
        rows.append(obs(RELEASE, period, 100.0 + i, released, end=end))
        period = next_month(next_month(next_month(period)))
    store.append(rows)
    events = build_surprises(conn, RELEASE_SPEC, CONFIG)
    assert events
    assert events[0].previous.value == pytest.approx(101.0)
