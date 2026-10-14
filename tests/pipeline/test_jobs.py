import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from macrosys.pipeline.cli_commands import register_cli
from macrosys.pipeline.jobs import PipelineConfig, RequiredSeries, daily_job, load_config, weekly_job


def test_daily_job_writes_run_and_manifest_and_blocks(tmp_path):
    config = PipelineConfig(
        db_path=tmp_path / "db" / "macrosys.duckdb",
        raw_root=tmp_path / "raw",
        manifest_dir=tmp_path / "manifests",
        required=[RequiredSeries(series_id="FRED:GDPC1", max_lag_days=5)],
    )
    report = daily_job(config, now=datetime(2026, 10, 14, tzinfo=UTC))
    assert report.status == "blocked"
    assert report.coverage_blocking is True
    assert report.manifest_digest
    path = tmp_path / "manifests"
    assert any(path.glob("daily-*.json"))

    conn = duckdb.connect(str(config.db_path))
    try:
        row = conn.execute("SELECT status, manifest_digest, kind FROM job_run").fetchone()
    finally:
        conn.close()
    assert row == ("blocked", report.manifest_digest, "daily")


def test_weekly_job_calls_refresh_before_manifest(tmp_path):
    seen: list[str] = []

    def refresh(conn, as_of):
        seen.append(as_of.isoformat())
        conn.execute("SELECT 1")

    config = PipelineConfig(
        db_path=tmp_path / "db" / "macrosys.duckdb",
        raw_root=tmp_path / "raw",
        manifest_dir=tmp_path / "manifests",
    )
    report = weekly_job(config, refresh, now=datetime(2026, 10, 14, tzinfo=UTC))
    assert report.status == "ok"
    assert seen
    assert report.kind == "weekly"


def test_example_config_loads():
    config = load_config(Path("config/pipeline.example.yaml"))
    assert "FRED:GDPC1" in config.series[0].series_ids
    assert "SPY" in config.prices[0].instrument_ids


def test_register_cli_adds_job_commands():
    parser = argparse.ArgumentParser()
    register_cli(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["coverage", "--config", "config/pipeline.example.yaml"])
    assert args.cmd == "coverage"
    assert callable(args.func)
