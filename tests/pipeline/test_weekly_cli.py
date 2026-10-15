"""The weekly command records the forward decision after a clean ingest (spec §11.2)."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from macrosys.pipeline.cli_commands import _cmd_weekly
from macrosys.pipeline.jobs import JobReport


def test_ingest_error_does_not_record_a_forward_week(monkeypatch, tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    monkeypatch.setattr("macrosys.pipeline.cli_commands.weekly_job", lambda loaded: _report("error"))
    monkeypatch.setattr("macrosys.forward.week.forward_week", _forbid_forward)

    code = _cmd_weekly(_args(config))

    assert code == 1


def test_clean_ingest_records_the_forward_week(monkeypatch, tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    started = datetime(2026, 10, 14, 15, tzinfo=UTC)
    monkeypatch.setattr("macrosys.pipeline.cli_commands.weekly_job", lambda loaded: _report("ok", started))
    seen: dict[str, object] = {}

    def _record(db_path, as_of, **kwargs):
        seen["as_of"] = as_of
        seen["db_path"] = db_path
        seen["out"] = kwargs["out_dir"]
        return type("Week", (), {"status": "ok", "n_theses": 0, "paper_allowed": False})()

    monkeypatch.setattr("macrosys.forward.week.forward_week", _record)

    code = _cmd_weekly(_args(config))

    assert code == 0
    assert seen["as_of"] == started
    assert seen["out"] == Path("data/forward/2026-10-14")


def _forbid_forward(*args, **kwargs):
    raise AssertionError("forward week must not run after an ingest error")


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "pipeline.yaml"
    db = (tmp_path / "store.duckdb").as_posix()
    raw = (tmp_path / "raw").as_posix()
    manifests = (tmp_path / "manifests").as_posix()
    path.write_text(
        f"db_path: {db}\nraw_root: {raw}\nmanifest_dir: {manifests}\n",
        encoding="utf-8",
    )
    return path


def _args(config: Path) -> argparse.Namespace:
    return argparse.Namespace(config=config)


def _report(status: str, started: datetime | None = None) -> JobReport:
    stamp = started or datetime(2026, 10, 14, 15, tzinfo=UTC)
    return JobReport(
        kind="weekly",
        status=status,  # type: ignore[arg-type]
        started_at=stamp,
        ended_at=stamp,
        manifest_digest="abc",
        manifest_path="manifest.json",
        coverage_blocking=False,
        ingests=[],
        discrepancies={},
    )
