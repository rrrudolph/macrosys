"""CLI for the release event study (spec §10.1 item 11, E4.4).

``events build`` rebuilds the stored surprise events from the observation store.
``events study`` scores every configured release-to-market sign and, with ``--write-parameters``,
writes the estimated response coefficients with provenance. ``events check`` compares an observed
front-end move with the slope in that file. ``events assess`` reports the surprise and the move
knowable at one timestamp, and the reason the gap gate must fail when the front end is already covered.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from macrosys.core.quantity import Quantity
from macrosys.events.assess import assess_release
from macrosys.events.config import load_events_config
from macrosys.events.estimate import write_parameters
from macrosys.events.response import check_response, load_responses
from macrosys.events.schema import ResponseStudy
from macrosys.events.store import SurpriseStore
from macrosys.events.study import run_study
from macrosys.events.surprise import build_surprises


def register_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``events build``, ``events study``, ``events check``, and ``events assess``."""
    events = subparsers.add_parser("events", help="release event study (spec 10.1 item 11)")
    commands = events.add_subparsers(dest="events_cmd")
    build = commands.add_parser("build", help="rebuild stored surprise events from the observation store")
    build.add_argument("--db", type=Path, required=True, help="DuckDB store path")
    build.add_argument("--config", type=Path, default=None, help="events yaml; default config/events.yaml")
    build.add_argument("--as-of", default=None, help="ISO timestamp bound on published_at; default all")
    build.set_defaults(func=_cmd_build)
    study = commands.add_parser("study", help="score predicted signs and estimate response coefficients")
    study.add_argument("--db", type=Path, required=True, help="DuckDB store path")
    study.add_argument("--config", type=Path, default=None, help="events yaml; default config/events.yaml")
    study.add_argument("--as-of", default=None, help="ISO timestamp bound on published_at; default all")
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


def _cmd_build(args: argparse.Namespace) -> int:
    config = load_events_config(args.config)
    as_of = _as_of(args.as_of)
    conn = duckdb.connect(str(args.db))
    try:
        store = SurpriseStore(conn)
        for release in config.releases:
            events = build_surprises(conn, release, config, as_of)
            stored = store.replace(release.series_id, config.forecaster.name, events)
            with_sigma = sum(1 for event in events if event.surprise_sigma is not None)
            print(f"{release.series_id:<18} {release.label:<28} events {stored:4d}  standardized {with_sigma:4d}")
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
        results = run_study(conn, config, as_of)
    finally:
        conn.close()
    for line in format_report(results, config.sign_threshold_sigma):
        print(line)
    if args.write_parameters is not None:
        target = write_parameters(results, args.write_parameters)
        print(f"wrote {target}")
    return 0


def format_report(results: list[ResponseStudy], threshold: float) -> list[str]:
    """Plain-text table, one row per pair and horizon."""
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
    lines.append(
        "Surprises are against a statistical forecast from earlier prints, not a consensus. "
        "Coefficients are market responses per 1 sd, not structural elasticities."
    )
    return lines
