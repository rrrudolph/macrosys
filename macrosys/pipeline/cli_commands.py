"""CLI subcommands for ingest, replay, coverage, and the daily/weekly jobs (spec §9.4).

The CLI package owns ``macrosys.cli``. Call ``register_cli(subparsers)`` from there. Each parser
sets ``args.func``; dispatch with ``args.func(args)``, which returns a process exit code.
Scheduling stays outside this process (cron or Windows Task Scheduler), for example:

    macrosys daily --config config/pipeline.yaml
    macrosys weekly --config config/pipeline.yaml
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from macrosys.pipeline.ingest import replay
from macrosys.pipeline.jobs import daily_job, load_config, open_source, weekly_job
from macrosys.sources import SOURCES
from macrosys.store.coverage import coverage_report
from macrosys.store.observations import ObservationStore
from macrosys.store.raw import FileRawStore
from macrosys.store.schema import create_schema


def register_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``ingest``, ``replay``, ``coverage``, ``daily``, and ``weekly`` (spec §9.4)."""
    ingest = subparsers.add_parser("ingest", help="fetch configured series or one source into the store")
    ingest.add_argument("--config", type=Path, required=True)
    ingest.add_argument("--source", help="limit ingest to this source_id")
    ingest.set_defaults(func=_cmd_ingest)

    replay_parser = subparsers.add_parser("replay", help="re-parse stored raw payloads for one source")
    replay_parser.add_argument("--config", type=Path, required=True)
    replay_parser.add_argument("--source", required=True)
    replay_parser.set_defaults(func=_cmd_replay)

    coverage = subparsers.add_parser("coverage", help="print the decision-blocking coverage report")
    coverage.add_argument("--config", type=Path, required=True)
    coverage.add_argument("--as-of", help="ISO timestamp; default is now UTC")
    coverage.set_defaults(func=_cmd_coverage)

    daily = subparsers.add_parser("daily", help="ingest, coverage, cross-check, manifest")
    daily.add_argument("--config", type=Path, required=True)
    daily.set_defaults(func=_cmd_daily)

    weekly = subparsers.add_parser("weekly", help="daily steps plus manifest; refresh hook is in-process")
    weekly.add_argument("--config", type=Path, required=True)
    weekly.set_defaults(func=_cmd_weekly)


def _cmd_ingest(args: argparse.Namespace) -> int:
    from macrosys.pipeline.ingest import ingest_prices, ingest_series
    from macrosys.store.actions import CorporateActionStore
    from macrosys.store.prices import PriceStore

    config = load_config(args.config)
    series_groups = config.series
    price_groups = config.prices
    if args.source:
        series_groups = [group for group in series_groups if group.source_id == args.source]
        price_groups = [group for group in price_groups if group.source_id == args.source]
    conn = duckdb.connect(str(config.db_path))
    failed = False
    try:
        create_schema(conn)
        raw = FileRawStore(config.raw_root)
        now = datetime.now(UTC)
        end = now.date()
        start = end.fromordinal(end.toordinal() - config.price_lookback_days)
        for group in series_groups:
            report = ingest_series(
                open_source(group.source_id, raw), group.series_ids, raw, ObservationStore(conn), now=now
            )
            failed = failed or report.failed
            print(f"{report.source_id} series={len(report.results)} errors={sum(1 for item in report.results if item.error)}")
        for group in price_groups:
            report = ingest_prices(
                open_source(group.source_id, raw),
                group.instrument_ids,
                raw,
                PriceStore(conn),
                CorporateActionStore(conn),
                start=start,
                end=end,
                now=now,
            )
            failed = failed or report.failed
            print(f"{report.source_id} prices={len(report.results)} errors={sum(1 for item in report.results if item.error)}")
    finally:
        conn.close()
    return 1 if failed else 0


def _cmd_replay(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.source not in SOURCES:
        raise SystemExit(f"unknown source {args.source}")
    conn = duckdb.connect(str(config.db_path))
    try:
        create_schema(conn)
        raw = FileRawStore(config.raw_root)
        report = replay(raw, open_source(args.source, raw), ObservationStore(conn))
    finally:
        conn.close()
    print(f"payloads={report.payloads} inserted={report.inserted} quarantined={report.quarantined}")
    return 1 if report.errors else 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    required = [(item.series_id, item.max_lag_days) for item in config.required]
    for group in config.series:
        required.extend((series_id, group.max_lag_days) for series_id in group.series_ids)
    conn = duckdb.connect(str(config.db_path))
    try:
        create_schema(conn)
        report = coverage_report(conn, as_of, required)
    finally:
        conn.close()
    for row in report.rows:
        print(f"{row.series_id} {row.status}")
    print("blocking" if report.blocking else "clear")
    return 2 if report.blocking else 0


def _cmd_daily(args: argparse.Namespace) -> int:
    report = daily_job(load_config(args.config))
    _print_job(report.status, report.manifest_digest)
    return _exit(report.status)


def _cmd_weekly(args: argparse.Namespace) -> int:
    report = weekly_job(load_config(args.config))
    _print_job(report.status, report.manifest_digest)
    return _exit(report.status)


def _print_job(status: str, digest: str) -> None:
    print(f"{status} {digest}")


def _exit(status: str) -> int:
    if status == "error":
        return 1
    if status == "blocked":
        return 2
    return 0
