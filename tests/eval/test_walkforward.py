"""Point-in-time walk-forward on a temp store (spec §10.2, §9.2). No network."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
import yaml

from macrosys.core.contracts import EntityKey, PriceBar
from macrosys.core.quantity import Quantity
from macrosys.decide.mandate import Mandate
from macrosys.decide.shock import Shock
from macrosys.decide.thesis import Thesis
from macrosys.eval.promotion import ResearchGateResult
from macrosys.eval.walkforward import (
    EVAL_RESEARCH,
    Approval,
    WalkForwardReport,
    run,
)
from macrosys.forward.week import ShockNote, WeekDecision, decide_week
from macrosys.pipeline.jobs import PipelineConfig, load_config
from macrosys.store.prices import PriceStore
from macrosys.store.schema import create_schema
from tests.decide.support import make_thesis

REPO = Path(__file__).resolve().parents[2]
START = date(2024, 3, 7)
MID = date(2024, 3, 14)
END = date(2024, 3, 21)
JUMP = date(2024, 3, 8)
TICKERS = ["SPY", "IEF", "GLD"]
NOT_MET = ResearchGateResult(
    evaluable=True,
    met=False,
    reasons=("held-out evidential agreement is under 60%",),
)


def test_idle_weeks_match_the_resting_book_and_the_json_round_trips(tmp_path: Path) -> None:
    db, config, mandate, replay = _prepare(tmp_path, omit=None)
    _write_replay(replay, [_financial(MID)])
    conn = duckdb.connect(str(db), read_only=True)
    out = tmp_path / "out"
    try:
        result = run(
            conn,
            mandate,
            config,
            replay,
            START,
            END,
            approver=None,
            cost_bp=5.0,
            out_dir=out,
        )
    finally:
        conn.close()

    assert list(result.weights.index) == [pd.Timestamp(START), pd.Timestamp(MID), pd.Timestamp(END)]
    for stamp in result.weights.index:
        book = result.weights.loc[stamp]
        rest = result.resting.loc[stamp]
        assert book.tolist() == pytest.approx(rest.tolist(), abs=1e-6)
        assert float(book.sum()) == pytest.approx(1.0, abs=1e-6)
        assert float(rest.sum()) == pytest.approx(1.0, abs=1e-6)
    mid = next(row for row in result.report.weeks if row.as_of == MID)
    assert mid.n_shocks == 1 and mid.n_trades == 0 and mid.n_sized == 0
    assert sum(mid.failing_gates.values()) == 1
    assert result.report.n_trades == 0
    assert result.report.rebalance == "W-THU"
    names = set(result.backtests)
    assert names == {"system", "resting", "sixty_forty", "equal_weight", "inverse_vol", "tsmom"}
    calendar = result.backtests["system"].net_returns.index
    for book in result.backtests.values():
        assert book.net_returns.index.equals(calendar)
    loaded = WalkForwardReport.model_validate(json.loads((out / "walkforward.json").read_text(encoding="utf-8")))
    assert loaded.n_weeks == result.report.n_weeks
    assert loaded.failing_gates == result.report.failing_gates
    assert loaded.books["system"].n_obs == result.report.books["system"].n_obs
    assert loaded.books["resting"].ann_return.value == pytest.approx(
        result.report.books["resting"].ann_return.value
    )
    assert "Lopez de Prado" in loaded.purge_embargo
    saved = pd.read_csv(out / "weights.csv", index_col="as_of", parse_dates=True)
    assert saved.loc[pd.Timestamp(START)].tolist() == pytest.approx(
        result.weights.loc[pd.Timestamp(START)].tolist(),
        abs=1e-6,
    )


def test_an_approved_weight_tilts_the_book_and_gross_stays_within_nav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, config, mandate, replay = _prepare(tmp_path, omit=None)
    _write_replay(replay, [_financial(END)])
    real = decide_week

    def _with_a_trade(conn: duckdb.DuckDBPyConnection, as_of: datetime, shocks: list[Shock], **kwargs: object) -> WeekDecision:
        decision = real(conn, as_of, shocks, **kwargs)  # type: ignore[arg-type]
        if as_of.date() != MID:
            return decision
        thesis = _trade_thesis(as_of)
        note = ShockNote(
            shock_id="tilt",
            action="trade",
            failing_gate=None,
            reason="all four gates passed",
            thesis_id=thesis.thesis_id,
        )
        return decision.model_copy(
            update={
                "notes": [note, *decision.notes],
                "theses": [thesis, *decision.theses],
                "n_shocks": decision.n_shocks + 1,
            }
        )

    monkeypatch.setattr("macrosys.eval.walkforward.decide_week", _with_a_trade)
    conn = duckdb.connect(str(db), read_only=True)
    try:
        result = run(conn, mandate, config, replay, START, END, approver=_TenPercent(), cost_bp=5.0)
    finally:
        conn.close()

    stamp = pd.Timestamp(MID)
    trade = result.weights.loc[stamp]
    rest = result.resting.loc[stamp]
    assert float(trade["SPY"]) > float(rest["SPY"]) + 1e-4
    assert float(trade.abs().sum()) <= 1.0 + 1e-6
    other = result.weights.drop(index=stamp)
    other_rest = result.resting.drop(index=stamp)
    assert other.to_numpy() == pytest.approx(other_rest.to_numpy(), abs=1e-6)
    mid = next(row for row in result.report.weeks if row.as_of == MID)
    assert mid.n_trades == 1 and mid.n_sized == 1


def test_a_close_the_day_after_a_week_does_not_move_that_week(tmp_path: Path) -> None:
    db, config, mandate, replay = _prepare(tmp_path, omit=JUMP)
    replay.write_text("", encoding="utf-8")
    first = _run(db, mandate, config, replay)
    _append_jump(db, multiplier=10.0)
    second = _run(db, mandate, config, replay)
    before = pd.Timestamp(START)
    after = pd.Timestamp(MID)
    assert first.weights.loc[before].tolist() == pytest.approx(second.weights.loc[before].tolist(), abs=1e-8)
    moved = np.max(np.abs(first.weights.loc[after].to_numpy() - second.weights.loc[after].to_numpy()))
    assert moved > 1e-3


def test_a_met_research_bar_does_not_replace_the_mechanism_gate() -> None:
    as_of = datetime(2024, 3, 14, 23, 59, 59, tzinfo=UTC)
    shock = _financial(as_of.date())
    mandate = _mandate()
    conn = duckdb.connect()
    try:
        met = decide_week(conn, as_of, [shock], mandate=mandate, research=EVAL_RESEARCH, book=None)
        unmet = decide_week(conn, as_of, [shock], mandate=mandate, research=NOT_MET, book=None)
    finally:
        conn.close()
    assert met.notes[0].action == "no_trade" and met.notes[0].failing_gate == "mechanism"
    assert all("sign bar" not in reason for reason in met.reasons)
    assert unmet.notes[0].failing_gate == "mechanism"
    assert any("sign bar" in reason or "60%" in reason for reason in unmet.reasons)


class _TenPercent:
    """Approver stand-in: hold 10% of NAV, long or short as the thesis says."""

    def approve(
        self,
        thesis: Thesis,
        conn: duckdb.DuckDBPyConnection,
        as_of: datetime,
        mandate: Mandate,
        current: dict[str, float],
    ) -> Approval:
        del conn, as_of, mandate, current
        return Approval(
            thesis_id=thesis.thesis_id,
            exposure_match=True,
            cost_estimate_bp=5.0,
            vol_annual=0.20,
            scenario_loss=0.05,
            regime_flag="stable",
            post_trade_ok=True,
            weight=0.1,
            reasons=["test approver"],
        )


def _prepare(tmp_path: Path, *, omit: date | None) -> tuple[Path, PipelineConfig, Mandate, Path]:
    db = tmp_path / "store.duckdb"
    config_path = tmp_path / "pipeline.yaml"
    _write_pipeline(config_path, tmp_path, db)
    _write_closes(db, omit=omit)
    return db, load_config(config_path), _mandate(), tmp_path / "replay.jsonl"


def _run(db: Path, mandate: Mandate, config: PipelineConfig, replay: Path):
    conn = duckdb.connect(str(db), read_only=True)
    try:
        return run(conn, mandate, config, replay, START, END, cost_bp=5.0)
    finally:
        conn.close()


def _mandate() -> Mandate:
    loaded = Mandate.load(REPO / "config" / "mandate.yaml")
    assert loaded.baseline is not None
    return loaded.model_copy(
        update={
            "baseline": loaded.baseline.model_copy(
                update={"instruments": list(TICKERS), "window_days": 15}
            ),
            "risk": loaded.risk.model_copy(update={"position_limit_pct": 80}),
        }
    )


def _write_pipeline(path: Path, tmp_path: Path, db: Path) -> None:
    payload = {
        "db_path": str(db),
        "raw_root": str(tmp_path / "raw"),
        "manifest_dir": str(tmp_path / "manifests"),
        "prices": [{"source_id": "TIINGO", "instrument_ids": list(TICKERS)}],
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _write_closes(db: Path, *, omit: date | None) -> None:
    index = pd.bdate_range("2024-01-02", "2024-03-21")
    if omit is not None:
        index = index[index != pd.Timestamp(omit)]
    rng = np.random.default_rng(7)
    frames = {
        ticker: pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.008 * (i + 1), size=len(index)))), index=index)
        for i, ticker in enumerate(TICKERS)
    }
    _store_bars(db, frames, ingested=datetime(2024, 3, 21, 20, 0, tzinfo=UTC))


def _append_jump(db: Path, *, multiplier: float) -> None:
    conn = duckdb.connect(str(db))
    try:
        previous = PriceStore(conn).read_closes(["TIINGO:SPY"], START, START)
        close = float(previous.iloc[-1, 0]) * multiplier
        PriceStore(conn).append_bars(
            [
                PriceBar(
                    instrument_id="TIINGO:SPY",
                    trade_date=JUMP,
                    open=None,
                    high=None,
                    low=None,
                    close=close,
                    volume=None,
                    ccy="USD",
                    source_id="TIINGO",
                    tier="record",
                    ingested_at=datetime(2024, 3, 21, 22, 0, tzinfo=UTC),
                )
            ]
        )
    finally:
        conn.close()


def _store_bars(db: Path, closes: dict[str, pd.Series], *, ingested: datetime) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db))
    try:
        create_schema(conn)
        PriceStore(conn).append_bars(
            [
                PriceBar(
                    instrument_id=f"TIINGO:{ticker}",
                    trade_date=pd.Timestamp(ts).date(),
                    open=None,
                    high=None,
                    low=None,
                    close=float(value),
                    volume=None,
                    ccy="USD",
                    source_id="TIINGO",
                    tier="record",
                    ingested_at=ingested,
                )
                for ticker, series in closes.items()
                for ts, value in series.items()
            ]
        )
    finally:
        conn.close()


def _financial(day: date) -> Shock:
    return Shock(
        id=f"us_equity_drawdown-{day.isoformat()}",
        type="financial",
        subtype="collateral",
        origin=EntityKey(entity_type="instrument", entity_id="SPY"),
        size=Quantity(value=-11.0, unit="pct", provenance="walkforward_test"),
        start=day,
        stage="observed",
        certainty=1.0,
        evidence_refs=["walkforward_test"],
        information_cutoff=datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC),
    )


def _write_replay(path: Path, shocks: list[Shock]) -> None:
    lines = []
    for shock in shocks:
        payload = {
            "as_of": shock.information_cutoff.date().isoformat(),
            "rule_id": "us_equity_drawdown",
            "series": "SPY",
            "crossed": shock.start.isoformat(),
            "last_date": shock.start.isoformat(),
            "value": shock.size.value,
            "unit": shock.size.unit,
            "shock": shock.model_dump(mode="json"),
        }
        lines.append(json.dumps(payload))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _trade_thesis(as_of: datetime):
    return make_thesis(
        thesis_id=f"T-tilt-{as_of.date().isoformat()}",
        created=as_of,
        shock_ref="tilt",
        information_cutoff=as_of,
        gap={"unit": "bp", "low": 800.0, "high": 1200.0},
        expression={
            "instrument": "SPY",
            "direction": "long",
            "exposure_vector_check": "passed",
            "structure_notes": "test tilt on the resting book",
            "cost_estimate_bp": 5.0,
        },
        sizing_inputs={
            "vol_annual": 0.20,
            "scenario_pnl": {"stress": -0.05},
            "factor_loadings": {"equity": 0.2},
        },
    )
