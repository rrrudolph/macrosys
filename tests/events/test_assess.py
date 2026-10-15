"""Point-in-time release assessment for the gap gate (spec §7.3, §9.2, §10.1 item 11, §11.3)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import yaml

from macrosys.events.assess import assess_release
from macrosys.events.cli_commands import _cmd_assess
from macrosys.events.config import ResponseSpec
from macrosys.events.response import implied_revision, load_responses
from macrosys.events.surprise import build_surprises
from macrosys.store.observations import ObservationStore
from macrosys.store.schema import create_schema
from tests.events.support import (
    RELEASE,
    YIELD,
    daily_rows,
    make_config,
    month_start,
    monthly_release_rows,
    new_store,
    obs,
)

CONFIG = make_config(
    responses=[
        ResponseSpec(release=RELEASE, market=YIELD, sign=1, mechanisms=["C09_policy_path"], reference=None, label="E")
    ],
    horizons=[1, 5],
)


def _parameters(tmp_path: Path) -> Path:
    path = tmp_path / "event_response.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "entries": [
                    {
                        "name": f"{RELEASE}->{YIELD}@1d",
                        "release": RELEASE,
                        "market": YIELD,
                        "horizon_days": 1,
                        "beta": {
                            "value": 1.0,
                            "low": 0.5,
                            "high": 2.0,
                            "unit": "bp",
                            "per": "1 sd of standardized surprise",
                            "source": "event_study:fixture",
                        },
                        "predicted_sign": 1,
                        "mechanisms": ["C09_policy_path"],
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _levels() -> list[float]:
    return [100.0 + (i % 3) * 0.5 + i * 0.2 for i in range(18)]


def _on_or_after(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _before(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _close(day: date) -> datetime:
    return datetime.combine(day, time(22, 0), tzinfo=UTC)


def _edge(path: Path, sigma: float) -> float:
    coeff = load_responses(path).get(RELEASE, YIELD, 1)
    assert coeff is not None
    implied = implied_revision(coeff, sigma)
    assert implied.low is not None and implied.high is not None
    assert implied.straddles_zero() is False
    return implied.high if implied.high < 0.0 else implied.low


def _target(conn: duckdb.DuckDBPyConnection):
    events = build_surprises(conn, CONFIG.release(RELEASE), CONFIG)
    event = next(item for item in events if item.surprise_sigma is not None and abs(item.surprise_sigma) >= 0.2)
    return events, event


def _closes(event_date: date, move_bp: float, extra: int = 0) -> dict[date, float]:
    pre = _before(event_date)
    post = _on_or_after(event_date)
    base = 1.5
    values = {pre: base, post: base + move_bp / 100.0}
    day = post
    for _ in range(extra):
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        values[day] = base + 5.0
    return values


def test_later_vintage_does_not_change_an_earlier_assessment(tmp_path: Path) -> None:
    conn, store = new_store()
    store.append(monthly_release_rows(_levels(), month_start(2020, 1)))
    events, event = _target(conn)
    assert event.surprise_sigma is not None
    params = _parameters(tmp_path)
    post = _on_or_after(event.event_date)
    store.append(daily_rows(YIELD, _closes(event.event_date, _edge(params, event.surprise_sigma) * 0.25), "pct"))
    as_of = _close(post)
    earlier = assess_release(conn, RELEASE, YIELD, 1, as_of, config=CONFIG, parameters=params)
    assert earlier.event_date == event.event_date
    assert earlier.event_date != events[-1].event_date
    assert earlier.observed is not None and earlier.observed.unit == "bp"
    later = as_of + timedelta(days=1)
    store.append([obs(YIELD, post, 40.0, later, unit="pct", end=post)])
    store.append([obs(RELEASE, event.ref_period_start, 500.0, later)])
    again = assess_release(conn, RELEASE, YIELD, 1, as_of, config=CONFIG, parameters=params)
    assert again.model_dump() == earlier.model_dump()
    seen = assess_release(conn, RELEASE, YIELD, 1, later, config=CONFIG, parameters=params)
    assert seen.observed is not None
    assert seen.observed.value != pytest.approx(earlier.observed.value)


def test_a_move_past_the_conservative_edge_blocks(tmp_path: Path) -> None:
    conn, store = new_store()
    store.append(monthly_release_rows(_levels(), month_start(2020, 1)))
    _, event = _target(conn)
    assert event.surprise_sigma is not None
    params = _parameters(tmp_path)
    edge = _edge(params, event.surprise_sigma)
    move = edge + (1.0 if edge > 0.0 else -1.0)
    post = _on_or_after(event.event_date)
    store.append(daily_rows(YIELD, _closes(event.event_date, move), "pct"))
    result = assess_release(conn, RELEASE, YIELD, 1, _close(post), config=CONFIG, parameters=params)
    assert result.blocked
    assert "already moved" in result.reason
    assert result.observed is not None and result.observed.unit == "bp"
    assert result.observed.value == pytest.approx(move)
    assert result.implied is not None and result.implied.unit == "bp"
    assert result.event_date == event.event_date
    assert result.surprise_sigma == pytest.approx(event.surprise_sigma)


def test_a_smaller_move_does_not_block_and_the_horizon_caps_later_closes(tmp_path: Path) -> None:
    conn, store = new_store()
    store.append(monthly_release_rows(_levels(), month_start(2020, 1)))
    _, event = _target(conn)
    assert event.surprise_sigma is not None
    params = _parameters(tmp_path)
    move = _edge(params, event.surprise_sigma) * 0.25
    values = _closes(event.event_date, move, extra=4)
    store.append(daily_rows(YIELD, values, "pct"))
    result = assess_release(conn, RELEASE, YIELD, 1, _close(max(values)), config=CONFIG, parameters=params)
    assert result.blocked is False
    assert "has not covered" in result.reason
    assert result.observed is not None and result.observed.unit == "bp"
    assert result.observed.value == pytest.approx(move)
    assert result.residual is not None and result.residual.unit == "bp"


def test_as_of_before_the_first_standardized_event_blocks(tmp_path: Path) -> None:
    conn, store = new_store()
    store.append(monthly_release_rows(_levels(), month_start(2020, 1)))
    events = build_surprises(conn, CONFIG.release(RELEASE), CONFIG)
    first = next(item for item in events if item.surprise_sigma is not None)
    result = assess_release(
        conn,
        RELEASE,
        YIELD,
        1,
        first.published_at - timedelta(seconds=1),
        config=CONFIG,
        parameters=_parameters(tmp_path),
    )
    assert result.blocked
    assert "not yet standardized" in result.reason
    assert result.surprise_sigma is None
    assert result.observed is None


def test_a_front_end_that_has_not_printed_blocks_without_a_zero_move(tmp_path: Path) -> None:
    conn, store = new_store()
    store.append(monthly_release_rows(_levels(), month_start(2020, 1)))
    _, event = _target(conn)
    assert event.surprise_sigma is not None
    params = _parameters(tmp_path)
    store.append(daily_rows(YIELD, _closes(event.event_date, _edge(params, event.surprise_sigma)), "pct"))
    result = assess_release(conn, RELEASE, YIELD, 1, event.published_at, config=CONFIG, parameters=params)
    assert result.blocked
    assert "front end has not printed" in result.reason
    assert result.observed is None
    assert result.event_date == event.event_date
    assert result.surprise_sigma == pytest.approx(event.surprise_sigma)


def test_assess_command_prints_the_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "store.duckdb"
    conn = duckdb.connect(str(db))
    create_schema(conn)
    store = ObservationStore(conn)
    store.append(monthly_release_rows(_levels(), month_start(2020, 1)))
    _, event = _target(conn)
    assert event.surprise_sigma is not None
    params = _parameters(tmp_path)
    edge = _edge(params, event.surprise_sigma)
    post = _on_or_after(event.event_date)
    store.append(daily_rows(YIELD, _closes(event.event_date, edge + (1.0 if edge > 0.0 else -1.0)), "pct"))
    as_of = _close(post)
    conn.close()
    config_path = tmp_path / "events.yaml"
    config_path.write_text(yaml.safe_dump(CONFIG.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    args = SimpleNamespace(
        db=db,
        config=config_path,
        parameters=params,
        release=RELEASE,
        market=YIELD,
        horizon=1,
        as_of=as_of.isoformat(),
    )
    assert _cmd_assess(args) == 0
    assert "already moved" in capsys.readouterr().out
