"""Prospective weekly decision on a temp store (spec §11.2, §10.2 item 7, §10.3). No network."""

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
from macrosys.eval.ledger import ForecastLedger
from macrosys.eval.promotion import ResearchGateResult
from macrosys.forward.week import DuplicateWeekError, forward_week, week_status_text
from macrosys.store.prices import PriceStore
from macrosys.store.schema import create_schema

AS_OF = date(2026, 10, 14)
REPO = Path(__file__).resolve().parents[2]
BASELINE_MANDATE = REPO / "config" / "mandate.yaml"
RUNBOOK = REPO / "docs" / "runbook.md"
NOT_MET = ResearchGateResult(
    evaluable=True,
    met=False,
    reasons=("held-out evidential agreement is under 60%",),
)


@pytest.fixture
def cash_mandate(tmp_path: Path) -> Path:
    """The working mandate with the resting book turned off: these tests have no price store."""
    raw = yaml.safe_load(BASELINE_MANDATE.read_text(encoding="utf-8"))
    raw["baseline"] = None
    path = tmp_path / "mandate_cash.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def test_empty_shocks_are_no_trade(tmp_path: Path, cash_mandate: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    shocks.write_text("shocks: []\n", encoding="utf-8")

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=cash_mandate,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
    )

    assert result.status == "ok"
    assert result.n_theses == 0
    assert result.baseline is None
    assert _lines(ledger) == []
    records = _records(week_log)
    assert len(records) == 1
    assert any("no open shock" in reason for reason in records[0]["reasons"])
    assert records[0]["baseline"] is None and records[0]["triggered"] == []
    text = week_status_text(week_log)
    assert "1 record." in text
    assert "1 trailing records have status ok or blocked." in text
    assert "The 8-week gate is not met." in text
    assert "The 8-week gate is met." not in text
    report = out.joinpath("report.md").read_text(encoding="utf-8")
    assert "no-trade" in report
    assert "the book is cash" in report
    assert str(ledger) in report


def test_baseline_is_stated_logged_and_named_as_the_alternative(tmp_path: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    mandate = Mandate.load(BASELINE_MANDATE)
    assert mandate.baseline is not None
    tickers = list(mandate.baseline.instruments)
    _write_pipeline(pipeline, tmp_path, prices=tickers)
    _seed_closes(db, {ticker: 0.004 + 0.002 * i for i, ticker in enumerate(tickers)}, days=120)
    _write_shocks(shocks, [_price_shock()])

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=BASELINE_MANDATE,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
        research=NOT_MET,
    )

    assert result.status == "ok"
    assert result.baseline is not None
    assert set(result.baseline) == set(tickers)
    assert sum(result.baseline.values()) == pytest.approx(1.0)
    cap = mandate.risk.position_limit_pct / 100.0  # type: ignore[operator]
    assert all(weight <= cap + 1e-9 for weight in result.baseline.values())
    # The lowest-volatility fund is the first ticker.
    assert max(result.baseline, key=result.baseline.get) == tickers[0]  # type: ignore[arg-type]
    report = out.joinpath("report.md").read_text(encoding="utf-8")
    assert "The resting book is" in report and "held when no thesis passes" in report
    record = _records(week_log)[0]
    assert record["baseline"] == result.baseline
    thesis = ForecastLedger(ledger).entries(AS_OF)[0].thesis()
    assert "Hold the resting book." in thesis.no_trade_alternative
    assert tickers[0] in thesis.no_trade_alternative


def test_store_approver_replaces_the_pending_exposure_check(tmp_path: Path) -> None:
    from macrosys.decide.approve import StoreApprover
    from macrosys.pipeline.jobs import load_config

    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    mandate = Mandate.load(BASELINE_MANDATE)
    assert mandate.baseline is not None
    tickers = [*mandate.baseline.instruments, "EMLC"]
    _write_pipeline(pipeline, tmp_path, prices=tickers)
    _seed_closes(db, {ticker: 0.004 + 0.002 * i for i, ticker in enumerate(tickers)}, days=300)
    _write_shocks(shocks, [_price_shock()])
    approver = StoreApprover(load_config(pipeline), REPO / "config" / "universe.yaml")

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=BASELINE_MANDATE,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
        research=NOT_MET,
        approver=approver,
    )

    assert result.status == "ok" and result.n_theses == 1
    note = result.decisions[0]
    assert note.weight is not None and note.approval_reasons
    thesis = ForecastLedger(ledger).entries(AS_OF)[0].thesis()
    assert thesis.expression.exposure_vector_check in ("passed", "failed")
    # The B16 route refuses on TODO_estimate, so the mechanism gate still fails first.
    assert note.action == "no_trade" and note.failing_gate == "mechanism"
    report = out.joinpath("report.md").read_text(encoding="utf-8")
    assert "The approver's size is" in report


def test_resting_book_without_prices_blocks_the_week(tmp_path: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    mandate = Mandate.load(BASELINE_MANDATE)
    assert mandate.baseline is not None
    _write_pipeline(pipeline, tmp_path, prices=list(mandate.baseline.instruments))
    _write_shocks(shocks, [_price_shock()])

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=BASELINE_MANDATE,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
        research=NOT_MET,
    )

    assert result.status == "blocked"
    assert result.n_theses == 0 and _lines(ledger) == []
    assert any("resting book cannot be stated" in reason for reason in result.reasons)
    report = out.joinpath("report.md").read_text(encoding="utf-8")
    assert "The resting book cannot be stated" in report and "the book is kept" in report


def test_triggered_shock_without_a_route_fails_the_mechanism_gate(tmp_path: Path, cash_mandate: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path, prices=["XLK"])
    shocks.write_text("shocks: []\n", encoding="utf-8")
    # Flat for months, then a 15% fall over the last three sessions.
    index = pd.bdate_range(end=pd.Timestamp(AS_OF), periods=200)
    closes = pd.Series([100.0] * 197 + [95.0, 89.0, 85.0], index=index)
    _write_closes(db, {"XLK": closes})
    triggers = tmp_path / "triggers.yaml"
    triggers.write_text(
        yaml.safe_dump(
            {
                "rules": [
                    {
                        "id": "us_tech_drawdown",
                        "series": {"kind": "price", "instrument": "XLK"},
                        "signal": {"kind": "drawdown", "lookback_days": 126},
                        "threshold": {"op": "<=", "value": -10.0, "unit": "pct"},
                        "shock": {
                            "type": "financial",
                            "subtype": "collateral",
                            "origin": {"entity_type": "instrument", "entity_id": "XLK"},
                            "stage": "observed",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=cash_mandate,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
        research=NOT_MET,
        triggers_path=triggers,
    )

    # The collateral route (B13) exists but its elasticity is TODO_estimate: the shock is filed,
    # the thesis is frozen, and the mechanism gate refuses. Nothing errors.
    assert result.status == "ok"
    assert result.n_shocks == 1 and result.n_theses == 1
    assert result.triggered == [f"us_tech_drawdown-{AS_OF.isoformat()}"]
    note = result.decisions[0]
    assert note.triggered and note.action == "no_trade" and note.failing_gate == "mechanism"
    assert not note.error and "TODO_estimate" in note.reason
    record = _records(week_log)[0]
    assert record["status"] == "ok" and record["triggered"] == result.triggered
    report = out.joinpath("report.md").read_text(encoding="utf-8")
    assert "Trigger us_tech_drawdown fired" in report
    assert "Triggered shock us_tech_drawdown" in report and "mechanism" in report


def test_shock_type_without_a_route_is_a_mechanism_gate_failure_not_an_error(
    tmp_path: Path, cash_mandate: Path
) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    unrouted = _price_shock().model_copy(
        update={
            "id": "S-funding",
            "type": "financial",
            "subtype": "funding",
            "origin": EntityKey(entity_type="economy", entity_id="USA"),
            "size": Quantity(value=50.0, unit="bp", provenance="forward_test"),
        }
    )
    _write_shocks(shocks, [unrouted])

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=cash_mandate,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
        research=NOT_MET,
    )

    assert result.status == "ok"
    assert result.n_shocks == 1 and result.n_theses == 0 and _lines(ledger) == []
    note = result.decisions[0]
    assert note.action == "no_trade" and note.failing_gate == "mechanism" and not note.error
    assert "no library route" in note.reason


def test_price_shock_refuses_todo_estimate_and_freezes(tmp_path: Path, cash_mandate: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    shock = _price_shock()
    _write_shocks(shocks, [shock])

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=cash_mandate,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
        research=NOT_MET,
    )

    assert result.status == "ok"
    assert result.n_theses == 1
    assert result.decisions[0].action == "no_trade"
    lines = _lines(ledger)
    assert len(lines) == 1
    thesis = ForecastLedger(ledger).entries(AS_OF)[0].thesis()
    assert thesis.thesis_id == f"T-{shock.id}-{AS_OF.isoformat()}"
    assert "stub:S0.1" not in thesis.claim
    assert "Stub pass" not in thesis.claim
    assert "forward week" in thesis.claim.lower()
    assert "stub:S0.1" not in lines[0]
    blocking = [item for item in thesis.missing_evidence if item.blocking]
    assert any("pt_import" in item.description for item in blocking)


def test_missing_series_blocks_without_a_thesis(tmp_path: Path, cash_mandate: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(
        pipeline,
        tmp_path,
        required=[{"series_id": "FRED:NOT_IN_STORE", "max_lag_days": 7}],
    )
    shocks.write_text("shocks: []\n", encoding="utf-8")

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=cash_mandate,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
        research=NOT_MET,
    )

    assert result.status == "blocked"
    assert any("coverage blocked the week" in reason for reason in result.reasons)
    assert _lines(ledger) == []
    assert _records(week_log)[0]["status"] == "blocked"
    assert _records(week_log)[0]["n_theses"] == 0


def test_second_call_for_the_same_day_does_not_append(tmp_path: Path, cash_mandate: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    _write_shocks(shocks, [_price_shock()])
    kwargs = dict(
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=cash_mandate,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
        research=NOT_MET,
    )

    forward_week(db, AS_OF, **kwargs)
    assert len(_lines(ledger)) == 1
    with pytest.raises(DuplicateWeekError):
        forward_week(db, AS_OF, **kwargs)
    assert len(_lines(ledger)) == 1
    assert len(_records(week_log)) == 1


def test_retry_after_a_lost_week_log_does_not_duplicate_the_thesis(tmp_path: Path, cash_mandate: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    _write_shocks(shocks, [_price_shock()])
    kwargs = dict(
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=cash_mandate,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
        research=NOT_MET,
    )

    first = forward_week(db, AS_OF, **kwargs)
    week_log.unlink()
    second = forward_week(db, AS_OF, **kwargs)

    assert first.n_theses == 1
    assert second.n_theses == 1
    assert len(_lines(ledger)) == 1
    assert len(_records(week_log)) == 1


def test_injected_research_bar_is_not_paper(tmp_path: Path, cash_mandate: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    shocks.write_text("shocks: []\n", encoding="utf-8")

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=cash_mandate,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
        research=NOT_MET,
    )

    assert result.paper_allowed is False
    assert result.research_evaluable is True
    record = _records(week_log)[0]
    assert record["paper_allowed"] is False


def _layout(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    return (
        tmp_path / "store.duckdb",
        tmp_path / "pipeline.yaml",
        tmp_path / "open_shocks.yaml",
        tmp_path / "ledger.jsonl",
        tmp_path / "weeks.jsonl",
        tmp_path / "out",
    )


def _write_pipeline(
    path: Path,
    tmp_path: Path,
    *,
    required: list[dict[str, object]] | None = None,
    series: list[dict[str, object]] | None = None,
    prices: list[str] | None = None,
) -> None:
    payload = {
        "db_path": str(tmp_path / "unused.duckdb"),
        "raw_root": str(tmp_path / "raw"),
        "manifest_dir": str(tmp_path / "manifests"),
        "series": series or [],
        "required": required or [],
        "prices": [] if not prices else [{"source_id": "TIINGO", "instrument_ids": list(prices)}],
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _seed_closes(db: Path, daily_vols: dict[str, float], *, days: int) -> None:
    """Geometric random walks ending at AS_OF, one per ticker, with the given daily volatility."""
    rng = np.random.default_rng(11)
    index = pd.bdate_range(end=pd.Timestamp(AS_OF), periods=days)
    frames = {
        ticker: pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0.0, vol, size=days))), index=index)
        for ticker, vol in daily_vols.items()
    }
    _write_closes(db, frames)


def _write_closes(db: Path, closes: dict[str, pd.Series]) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db))
    try:
        create_schema(conn)
        ingested = datetime(AS_OF.year, AS_OF.month, AS_OF.day, 22, 0, tzinfo=UTC)
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


def _price_shock() -> Shock:
    return Shock(
        id="S-fx-forward",
        type="price",
        subtype="fx",
        origin=EntityKey(entity_type="currency", entity_id="BRL"),
        size=Quantity(value=10.0, unit="pct", provenance="forward_test"),
        start=AS_OF,
        stage="observed",
        certainty=0.5,
        evidence_refs=["forward_test"],
        information_cutoff=datetime(AS_OF.year, AS_OF.month, AS_OF.day, 12, 0, tzinfo=UTC),
    )


def _write_shocks(path: Path, shocks: list[Shock]) -> None:
    payload = {"shocks": [shock.model_dump(mode="json") for shock in shocks]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in _lines(path)]
