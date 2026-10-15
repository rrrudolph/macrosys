"""CLI for the release event study (spec §10.1 item 11, E4.4; consensus input per §9.8, P2.4).

``events consensus-load`` copies a local calendar extract (SQLite ``ff_cal_raw``) into the store's
``calendar_print`` table; it makes no request. ``events build`` rebuilds the stored surprise events
from the observation store, against the statistical forecaster or the loaded consensus. ``events
study`` scores every configured release-to-market sign and, with ``--write-parameters``, writes the
estimated response coefficients with provenance, naming the forecaster the surprises were against.
``events check`` compares an observed front-end move with the slope in that file. ``events assess``
reports the surprise and the move knowable at one timestamp, and the reason the gap gate must fail
when the front end is already covered.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import duckdb

from macrosys.core.quantity import Quantity
from macrosys.events.assess import assess_release
from macrosys.events.config import EventsConfig, load_events_config
from macrosys.events.consensus import ConsensusLookup, read_legacy_sqlite
from macrosys.events.estimate import forecaster_caveat, write_parameters
from macrosys.events.response import check_response, load_responses
from macrosys.events.schema import ResponseStudy
from macrosys.events.store import ConsensusStore, SurpriseStore
from macrosys.events.study import run_study
from macrosys.events.surprise import build_surprises

Forecaster = Literal["statistical", "consensus"]


def register_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``events consensus-load``, ``events build``, ``events study``, ``events check``, and ``events assess``."""
    events = subparsers.add_parser("events", help="release event study (spec 10.1 item 11)")
    commands = events.add_subparsers(dest="events_cmd")

    load = commands.add_parser("consensus-load", help="load a local calendar extract (SQLite ff_cal_raw) into calendar_print; no request is made")
    load.add_argument("--db", type=Path, required=True, help="DuckDB store path")
    load.add_argument("--sqlite", type=Path, required=True, help="local SQLite file with the ff_cal_raw table")
    load.add_argument("--config", type=Path, default=None, help="events yaml; default config/events.yaml")
    load.set_defaults(func=_cmd_consensus_load)

    build = commands.add_parser("build", help="rebuild stored surprise events from the observation store")
    build.add_argument("--db", type=Path, required=True, help="DuckDB store path")
    build.add_argument("--config", type=Path, default=None, help="events yaml; default config/events.yaml")
    build.add_argument("--as-of", default=None, help="ISO timestamp bound on published_at; default all")
    build.add_argument("--forecaster", choices=("statistical", "consensus"), default="statistical", help="what the surprise is measured against")
    build.set_defaults(func=_cmd_build)

    study = commands.add_parser("study", help="score predicted signs and estimate response coefficients")
    study.add_argument("--db", type=Path, required=True, help="DuckDB store path")
    study.add_argument("--config", type=Path, default=None, help="events yaml; default config/events.yaml")
    study.add_argument("--as-of", default=None, help="ISO timestamp bound on published_at; default all")
    study.add_argument("--forecaster", choices=("statistical", "consensus"), default="statistical", help="what the surprise is measured against")
    study.add_argument("--write-parameters", type=Path, default=None, help="write parameters yaml to this path")
    study.set_defaults(func=_cmd_study)
    check = commands.add_parser("check", help="compare an observed front-end move with the estimated slope")
    check.add_argument("--release", required=True, help="release series id, e.g. FRED:CPIAUCSL")
    check.add_argument("--market", required=True, help="market series id, e.g. FRED:DGS2")
    check.add_argument("--horizon", type=int, required=True, help="horizon in trading days")
    check.add_argument("--surprise", type=float, required=True, help="standardized surprise, in sd")
    check.add_argument("--observed", type=float, required=True, help="front-end move already realized")
    check.add_argument("--unit", required=True, help="unit of --observed")
    check.add_argument(
        "--parameters",
        type=Path,
        default=None,
        help="event_response yaml; default parameters/event_response.yaml",
    )
    check.set_defaults(func=_cmd_check)
    assess = commands.add_parser("assess", help="point-in-time surprise, front-end move, and gap-gate block")
    assess.add_argument("--db", type=Path, required=True, help="DuckDB store path")
    assess.add_argument("--release", required=True, help="release series id, e.g. FRED:CPIAUCSL")
    assess.add_argument("--market", required=True, help="market series id, e.g. FRED:DGS2")
    assess.add_argument("--horizon", type=int, required=True, help="horizon in trading days")
    assess.add_argument("--as-of", required=True, help="ISO timestamp; nothing published later is used")
    assess.add_argument("--config", type=Path, default=None, help="events yaml; default config/events.yaml")
    assess.add_argument(
        "--parameters",
        type=Path,
        default=None,
        help="event_response yaml; default parameters/event_response.yaml",
    )
    assess.set_defaults(func=_cmd_assess)


def _as_of(text: str | None) -> datetime | None:
    if text is None:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _consensus_or_none(conn: duckdb.DuckDBPyConnection, config: EventsConfig, forecaster: str, as_of: datetime | None) -> ConsensusLookup | None:
    if forecaster == "statistical":
        return None
    if config.consensus is None:
        raise SystemExit("--forecaster consensus needs a `consensus:` block in the events config")
    lookup = ConsensusLookup(conn, config, as_of)
    if lookup.prints_loaded() == 0:
        raise SystemExit(f"no calendar prints stored for source {lookup.source_id!r}; run `events consensus-load` first")
    return lookup


def _forecaster_name(config: EventsConfig, consensus: ConsensusLookup | None) -> str:
    return config.forecaster.name if consensus is None else consensus.name


def _cmd_consensus_load(args: argparse.Namespace) -> int:
    config = load_events_config(args.config)
    if config.consensus is None:
        raise SystemExit("events config has no `consensus:` block; nothing to load")
    loaded = read_legacy_sqlite(args.sqlite, config.consensus.source_id)
    conn = duckdb.connect(str(args.db))
    try:
        stored = ConsensusStore(conn).replace(config.consensus.source_id, loaded.prints)
    finally:
        conn.close()
    print(
        f"{args.sqlite}: read {loaded.rows_read} rows, dropped {loaded.dropped_future_actual} with an actual before "
        f"their release clock and {loaded.dropped_empty} empty; stored {stored} as source {config.consensus.source_id!r} "
        f"(sha256 {loaded.source_hash[:12]})"
    )
    if loaded.prints:
        first = min(p.scheduled_at for p in loaded.prints).date()
        last = max(p.scheduled_at for p in loaded.prints).date()
        print(f"scheduled {first}..{last}")
    for mapping in config.consensus.mappings:
        rows = [p for p in loaded.prints if p.event == mapping.event]
        with_forecast = sum(1 for p in rows if p.forecast is not None)
        print(f"{mapping.release:<16} {mapping.event:<40} prints {len(rows):4d}  with forecast {with_forecast:4d}")
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    config = load_events_config(args.config)
    as_of = _as_of(args.as_of)
    conn = duckdb.connect(str(args.db))
    try:
        consensus = _consensus_or_none(conn, config, args.forecaster, as_of)
        store = SurpriseStore(conn)
        name = _forecaster_name(config, consensus)
        print(f"forecaster {name}")
        for release in config.releases:
            if consensus is not None and not consensus.mapped(release.series_id):
                print(f"{release.series_id:<18} {release.label:<28} no consensus mapping")
                continue
            events = build_surprises(conn, release, config, as_of, consensus)
            stored = store.replace(release.series_id, name, events)
            with_sigma = sum(1 for event in events if event.surprise_sigma is not None)
            line = f"{release.series_id:<18} {release.label:<28} events {stored:4d}  standardized {with_sigma:4d}"
            if consensus is not None:
                line += "  " + consensus.stats[release.series_id].as_line()
            print(line)
            if consensus is not None:
                for release_date, calendar_actual, store_change in consensus.stats[release.series_id].mismatches[:5]:
                    print(f"    mismatch {release_date}: calendar actual {calendar_actual:+.4g} vs store change {store_change:+.4g}")
    finally:
        conn.close()
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    book = load_responses(args.parameters)
    observed = Quantity(value=args.observed, unit=args.unit, provenance="cli")
    result = check_response(book, args.release, args.market, args.horizon, args.surprise, observed)
    print(result.reason)
    return 0


def _cmd_assess(args: argparse.Namespace) -> int:
    config = load_events_config(args.config)
    as_of = _as_of(args.as_of)
    if as_of is None:
        raise ValueError("events assess requires --as-of")
    conn = duckdb.connect(str(args.db), read_only=True)
    try:
        result = assess_release(
            conn,
            args.release,
            args.market,
            args.horizon,
            as_of,
            config=config,
            parameters=args.parameters,
        )
    finally:
        conn.close()
    print(result.reason)
    return 0


def _cmd_study(args: argparse.Namespace) -> int:
    config = load_events_config(args.config)
    as_of = _as_of(args.as_of)
    conn = duckdb.connect(str(args.db), read_only=True)
    try:
        consensus = _consensus_or_none(conn, config, args.forecaster, as_of)
        results = run_study(conn, config, as_of, consensus)
    finally:
        conn.close()
    name = _forecaster_name(config, consensus)
    consensus_name = None if config.consensus is None else config.consensus.forecaster_name
    for line in format_report(results, config.sign_threshold_sigma, forecaster_caveat(name, consensus_name)):
        print(line)
    if args.write_parameters is not None:
        target = write_parameters(results, args.write_parameters, forecaster=name, consensus_name=consensus_name)
        print(f"wrote {target}")
    return 0


def format_report(results: list[ResponseStudy], threshold: float, caveat: str) -> list[str]:
    """Plain-text table, one row per pair and horizon, ending with ``caveat``."""
    lines = [
        f"{'release':<16} {'market':<18} {'h':>3} {'pred':>4} {'n':>5} {'n_eff':>6} {'signed':>6} {'hit%':>5} {'p':>6} "
        f"{'beta':>9} {'t':>6} {'oos%':>5} {'oos_n':>5}"
    ]
    for item in results:
        pred = "state" if item.predicted_sign is None else f"{item.predicted_sign:+d}"
        hit = "n/a" if item.sign_accuracy is None else f"{item.sign_accuracy:.0%}"
        p = "n/a" if item.p_value is None else f"{item.p_value:.3f}"
        beta = "n/a" if item.beta is None else f"{item.beta.mid:+.3f}{item.beta.unit}"
        t = "n/a" if item.t_stat is None else f"{item.t_stat:+.2f}"
        oos = "n/a" if item.oos_sign_accuracy is None else f"{item.oos_sign_accuracy:.0%}"
        lines.append(
            f"{item.release_id:<16} {item.market_id:<18} {item.horizon_days:3d} {pred:>4} {item.n:5d} {item.n_effective:6.1f} "
            f"{item.n_signed:6d} {hit:>5} {p:>6} {beta:>9} {t:>6} {oos:>5} {item.oos_n:5d}"
        )
        for split in item.regimes:
            acc = "n/a" if split.sign_accuracy is None else f"{split.sign_accuracy:.0%}"
            b = "n/a" if split.beta is None else f"{split.beta.mid:+.3f}"
            lines.append(
                f"    {split.regime}={str(split.state):<5} pred {split.predicted_sign:+d} n {split.n:4d} signed {split.n_signed:4d} "
                f"hit {acc:>5} beta {b:>8}"
            )
    scored = [item for item in results if item.n_signed > 0]
    if scored:
        total_hits = sum(round(item.sign_accuracy * item.n_signed) for item in scored if item.sign_accuracy is not None)
        total = sum(item.n_signed for item in scored)
        lines.append(
            f"pooled sign agreement {total_hits / total:.0%} on {total} signed pair-horizon observations "
            f"(|surprise| >= {threshold:g} sd; one event counts once per pair and horizon)"
        )
    lines.append(caveat + " Coefficients are market responses per 1 sd, not structural elasticities.")
    return lines
