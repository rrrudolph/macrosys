"""Daily release monitor (spec §11.2, §11.3). Synthetic store only; no network."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import duckdb
import pytest
import yaml
from pydantic import ValidationError

from macrosys.cli import main
from macrosys.events.assess import assess_release
from macrosys.events.config import ResponseSpec
from macrosys.events.response import implied_revision, load_responses
from macrosys.events.surprise import build_surprises
from macrosys.pipeline.cli_commands import register_cli
from macrosys.pipeline.release_monitor import monitor_releases
from macrosys.store.observations import ObservationStore
from macrosys.store.schema import create_schema
from tests.events.support import (
    INDEX,
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
        ResponseSpec(release=RELEASE, market=YIELD, sign=1, mechanisms=["C09_policy_path"], reference=None, label="E"),
        ResponseSpec(
            release=RELEASE, market=INDEX, sign=-1, mechanisms=["C05_equity_duration"], reference=None, label="H"
        ),
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
    return next(item for item in events if item.surprise_sigma is not None and abs(item.surprise_sigma) >= 0.2)


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


def _populate(store: ObservationStore, params: Path) -> tuple[datetime, date, date]:
    store.append(monthly_release_rows(_levels(), month_start(2020, 1)))
    event = _target(store.conn)
    assert event.surprise_sigma is not None
    move = _edge(params, event.surprise_sigma) * 0.25
    values = _closes(event.event_date, move, extra=4)
    store.append(daily_rows(YIELD, values, "pct"))
    return _close(max(values)), _on_or_after(event.event_date), event.ref_period_start


def test_one_call_per_response_and_horizon_passes_as_of_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, store = new_store()
    params = _parameters(tmp_path)
    as_of, post, period = _populate(store, params)
    calls: list[tuple[object, ...]] = []
    real = assess_release

    def wrapped(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("macrosys.pipeline.release_monitor.assess_release", wrapped)
    result = monitor_releases(conn, as_of, CONFIG, params)
    expected = [
        (spec.release, spec.market, horizon) for spec in CONFIG.responses for horizon in CONFIG.horizons_days
    ]
    assert [(call[0][1], call[0][2], call[0][3]) for call in calls] == expected
    assert all(call[0][4] is as_of and call[1]["config"] is CONFIG and call[1]["parameters"] is params for call in calls)
    assert isinstance(result.rows, tuple)
    assert len(result.rows) == len(expected)
    for row, (release, market, horizon) in zip(result.rows, expected, strict=True):
        direct = assess_release(conn, release, market, horizon, as_of, config=CONFIG, parameters=params)
        assert (row.release, row.market, row.horizon, row.blocked, row.reason) == (
            release,
            market,
            horizon,
            direct.blocked,
            direct.reason,
        )
    assert result.rows[0].blocked is False
    assert "has not covered" in result.rows[0].reason
    assert result.rows[1].blocked is True
    assert "no estimated response" in result.rows[1].reason
    assert all(row.blocked and "front end has not printed" in row.reason for row in result.rows[2:])
    assert as_of.isoformat() in result.note
    assert "does not place a trade" in result.note
    for row in result.rows:
        assert row.reason in result.note
    with pytest.raises(ValidationError):
        result.rows[0].blocked = True  # type: ignore[misc]
    with pytest.raises(ValidationError):
        result.note = "rewritten"  # type: ignore[misc]

    later = as_of + timedelta(days=1)
    store.append([obs(YIELD, post, 40.0, later, unit="pct", end=post)])
    store.append([obs(RELEASE, period, 500.0, later)])
    again = monitor_releases(conn, as_of, CONFIG, params)
    assert again.rows == result.rows
    assert again.note == result.note
    before = len(calls)
    moved = monitor_releases(conn, later, CONFIG, params)
    assert [call[0][4] for call in calls[before:]] == [later] * len(expected)
    assert moved.rows[0].reason != result.rows[0].reason


def test_naive_as_of_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, _store = new_store()
    called = False

    def wrapped(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("assess_release must not run")

    monkeypatch.setattr("macrosys.pipeline.release_monitor.assess_release", wrapped)
    with pytest.raises(ValueError, match="timezone-aware"):
        monitor_releases(conn, datetime(2026, 10, 14), CONFIG, _parameters(tmp_path))
    assert called is False


def test_releases_command_prints_the_note_and_leaves_other_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "store.duckdb"
    conn = duckdb.connect(str(db))
    create_schema(conn)
    store = ObservationStore(conn)
    params = _parameters(tmp_path)
    as_of, _post, _period = _populate(store, params)
    conn.close()
    config_path = tmp_path / "events.yaml"
    config_path.write_text(yaml.safe_dump(CONFIG.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    code = main(
        [
            "pipeline",
            "releases",
            "--db",
            str(db),
            "--as-of",
            as_of.isoformat(),
            "--config",
            str(config_path),
            "--parameters",
            str(params),
        ]
    )
    assert code == 0
    note = capsys.readouterr().out
    assert "Daily release monitor" in note
    assert "has not covered" in note
    assert "does not place a trade" in note

    parser = argparse.ArgumentParser()
    register_cli(parser.add_subparsers(dest="cmd"))
    for argv in (
        ["ingest", "--config", "c.yaml"],
        ["replay", "--config", "c.yaml", "--source", "FRED"],
        ["coverage", "--config", "c.yaml"],
        ["daily", "--config", "c.yaml"],
        ["weekly", "--config", "c.yaml"],
    ):
        parsed = parser.parse_args(argv)
        assert parsed.cmd == argv[0]
        assert callable(parsed.func)
    releases = parser.parse_args(
        ["pipeline", "releases", "--db", "s.duckdb", "--as-of", "2026-10-14T21:00:00Z"]
    )
    assert releases.cmd == "pipeline"
    assert releases.pipeline_cmd == "releases"
    assert callable(releases.func)
