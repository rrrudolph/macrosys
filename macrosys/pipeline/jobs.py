"""Daily and weekly decision jobs (spec §9.4).

This module does not schedule itself. The owner wires cron or Windows Task Scheduler to the CLI
once ``macrosys.pipeline.cli_commands.register_cli`` is attached:

    macrosys daily --config config/pipeline.yaml
    macrosys weekly --config config/pipeline.yaml

``daily_job`` ingests the series and prices named in the config, runs the coverage report and
price cross-check, and writes a ``job_run`` row plus a manifest. ``weekly_job`` does that and
then calls a world-state refresh hook supplied by the caller before the manifest is hashed, so
the digest includes the refreshed state. A blocking coverage report sets the job status to
``blocked``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field

from macrosys.pipeline.ingest import IngestReport, ingest_prices, ingest_series
from macrosys.sources import SOURCES
from macrosys.store.actions import CorporateActionStore
from macrosys.store.coverage import coverage_report
from macrosys.store.manifest import build_manifest, write_manifest
from macrosys.store.observations import ObservationStore
from macrosys.store.prices import PriceStore
from macrosys.store.raw import FileRawStore
from macrosys.store.schema import create_schema, ensure_utc

Refresh = Callable[[duckdb.DuckDBPyConnection, datetime], None]
JobStatus = Literal["ok", "blocked", "error"]


class SeriesGroup(BaseModel):
    """Series to ingest from one source, and the lag that makes each of them coverage-required."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    series_ids: list[str]
    max_lag_days: int = 7


class PriceGroup(BaseModel):
    """Instruments whose bars are loaded from one price source (spec §9.6)."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    instrument_ids: list[str]


class RequiredSeries(BaseModel):
    """An extra coverage requirement that this job does not itself ingest."""

    model_config = ConfigDict(frozen=True)

    series_id: str
    max_lag_days: int


class PipelineConfig(BaseModel):
    """Paths, series, and price universe for ``daily_job`` / ``weekly_job`` (spec §9.4)."""

    model_config = ConfigDict(frozen=True)

    db_path: Path
    raw_root: Path
    manifest_dir: Path
    library_version: str = "0.0.1"
    parameter_set: str = "default"
    code_commit: str = "local"
    price_lookback_days: int = 10
    cross_check_tol: float = 0.01
    cross_check_instruments: list[str] = Field(default_factory=list)
    series: list[SeriesGroup] = Field(default_factory=list)
    prices: list[PriceGroup] = Field(default_factory=list)
    required: list[RequiredSeries] = Field(default_factory=list)


class JobReport(BaseModel):
    """What one daily or weekly run wrote (spec §9.4)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["daily", "weekly"]
    status: JobStatus
    started_at: datetime
    ended_at: datetime
    manifest_digest: str
    manifest_path: str
    coverage_blocking: bool
    ingests: list[IngestReport]
    discrepancies: dict[str, list[date]]


def load_config(path: Path) -> PipelineConfig:
    """Load a pipeline YAML file such as ``config/pipeline.example.yaml`` (spec §9.4)."""
    file_path = Path(path)
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{file_path} must be a mapping")
    return PipelineConfig.model_validate(loaded)


def daily_job(
    config: PipelineConfig,
    *,
    sources: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> JobReport:
    """Ingest, check coverage and price cross-checks, and record the run (spec §9.4).

    ``sources`` overrides connector construction for tests. ``now`` is the coverage ``as_of``;
    it defaults to the current UTC time and must be timezone-aware when passed.
    """
    return _run(config, kind="daily", refresh=None, sources=sources, now=now)


def weekly_job(
    config: PipelineConfig,
    refresh: Refresh | None = None,
    *,
    sources: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> JobReport:
    """Run the daily steps, call ``refresh(conn, as_of)``, then hash the manifest (spec §9.4).

    ``refresh`` is the world-state hook. The caller supplies it; this module does not build state.
    The manifest is written after ``refresh`` returns so the digest includes those rows.
    """
    return _run(config, kind="weekly", refresh=refresh, sources=sources, now=now)


def open_source(source_id: str, raw: FileRawStore) -> Any:
    """Construct the registry connector for ``source_id`` (spec §9.6).

    Tiingo also needs a path for its monthly unique-symbol counter. Other connectors take the
    raw store as their first argument.
    """
    try:
        cls = SOURCES[source_id]
    except KeyError as exc:
        raise KeyError(f"no connector registered for {source_id}") from exc
    if source_id == "TIINGO":
        state = raw.root / "state" / "tiingo_symbols.json"
        return cls(raw, state)
    return cls(raw)


def _run(
    config: PipelineConfig,
    *,
    kind: Literal["daily", "weekly"],
    refresh: Refresh | None,
    sources: Mapping[str, Any] | None,
    now: datetime | None,
) -> JobReport:
    as_of = now if now is not None else datetime.now(UTC)
    if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("now must be timezone-aware")
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.manifest_dir.mkdir(parents=True, exist_ok=True)
    config.raw_root.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(config.db_path))
    try:
        create_schema(conn)
        ensure_utc(conn)
        raw = FileRawStore(config.raw_root)
        obs = ObservationStore(conn)
        prices = PriceStore(conn)
        actions = CorporateActionStore(conn)
        ingests: list[IngestReport] = []
        ingests.extend(_ingest_configured(config, raw, obs, prices, actions, sources, as_of))
        required = [(group.series_id, group.max_lag_days) for group in config.required]
        for group in config.series:
            required.extend((series_id, group.max_lag_days) for series_id in group.series_ids)
        coverage = coverage_report(conn, as_of, required)
        discrepancies: dict[str, list[date]] = {}
        end = as_of.date()
        start = end - timedelta(days=config.price_lookback_days)
        for instrument_id in config.cross_check_instruments:
            discrepancies[instrument_id] = prices.cross_check(
                instrument_id, start, end, config.cross_check_tol
            )
        status: JobStatus = "ok"
        detail_error: str | None = None
        if any(report.failed for report in ingests):
            status = "error"
        elif coverage.blocking:
            status = "blocked"
        if refresh is not None:
            try:
                refresh(conn, as_of)
            except Exception as exc:
                status = "error"
                detail_error = f"{type(exc).__name__}: {exc}"
        job_id = uuid.uuid4().hex
        detail = json.dumps(
            {
                "coverage_blocking": coverage.blocking,
                "ingest_errors": [
                    {"source_id": report.source_id, "item_id": item.item_id, "error": item.error}
                    for report in ingests
                    for item in report.results
                    if item.error
                ],
                "discrepancies": {
                    instrument_id: [day.isoformat() for day in days]
                    for instrument_id, days in discrepancies.items()
                },
                "refresh_error": detail_error,
            },
            sort_keys=True,
        )
        conn.execute(
            """
            INSERT INTO job_run (job_id, kind, started_at, ended_at, status, manifest_digest, detail)
            VALUES (?, ?, ?, ?, ?, NULL, ?)
            """,
            [job_id, kind, as_of, as_of, status, detail],
        )
        manifest = build_manifest(
            conn,
            config.raw_root,
            config.library_version,
            config.parameter_set,
            config.code_commit,
        )
        manifest_path = config.manifest_dir / f"{kind}-{job_id}.json"
        write_manifest(manifest_path, manifest)
        conn.execute(
            "UPDATE job_run SET manifest_digest = ? WHERE job_id = ?",
            [manifest.digest, job_id],
        )
        return JobReport(
            kind=kind,
            status=status,
            started_at=as_of,
            ended_at=as_of,
            manifest_digest=manifest.digest,
            manifest_path=str(manifest_path),
            coverage_blocking=coverage.blocking,
            ingests=ingests,
            discrepancies=discrepancies,
        )
    finally:
        conn.close()


def _ingest_configured(
    config: PipelineConfig,
    raw: FileRawStore,
    obs: ObservationStore,
    prices: PriceStore,
    actions: CorporateActionStore,
    sources: Mapping[str, Any] | None,
    as_of: datetime,
) -> list[IngestReport]:
    reports: list[IngestReport] = []
    for group in config.series:
        source = _resolve_source(group.source_id, raw, sources)
        reports.append(ingest_series(source, group.series_ids, raw, obs, now=as_of))
    end = as_of.date()
    start = end - timedelta(days=config.price_lookback_days)
    for group in config.prices:
        source = _resolve_source(group.source_id, raw, sources)
        reports.append(
            ingest_prices(
                source,
                group.instrument_ids,
                raw,
                prices,
                actions,
                start=start,
                end=end,
                now=as_of,
            )
        )
    return reports


def _resolve_source(source_id: str, raw: FileRawStore, sources: Mapping[str, Any] | None) -> Any:
    if sources is not None and source_id in sources:
        return sources[source_id]
    return open_source(source_id, raw)
