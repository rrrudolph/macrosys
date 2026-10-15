"""Prospective weekly decision on a temp store (spec §11.2, §10.2 item 7, §10.3). No network."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.decide.shock import Shock
from macrosys.eval.ledger import ForecastLedger
from macrosys.eval.promotion import ResearchGateResult
from macrosys.forward.week import DuplicateWeekError, forward_week, week_status_text

AS_OF = date(2026, 10, 14)
REPO = Path(__file__).resolve().parents[2]
MANDATE = REPO / "config" / "mandate.yaml"
RUNBOOK = REPO / "docs" / "runbook.md"
NOT_MET = ResearchGateResult(
    evaluable=True,
    met=False,
    reasons=("held-out evidential agreement is under 60%",),
)


def test_empty_shocks_are_no_trade(tmp_path: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    shocks.write_text("shocks: []\n", encoding="utf-8")

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=MANDATE,
        ledger_path=ledger,
        week_log_path=week_log,
        out_dir=out,
        runbook_path=RUNBOOK,
    )

    assert result.status == "ok"
    assert result.n_theses == 0
    assert _lines(ledger) == []
    records = _records(week_log)
    assert len(records) == 1
    assert any("no open shock" in reason for reason in records[0]["reasons"])
    text = week_status_text(week_log)
    assert "1 record." in text
    assert "1 trailing records have status ok or blocked." in text
    assert "The 8-week gate is not met." in text
    assert "The 8-week gate is met." not in text
    report = out.joinpath("report.md").read_text(encoding="utf-8")
    assert "no-trade" in report
    assert str(ledger) in report


def test_price_shock_refuses_todo_estimate_and_freezes(tmp_path: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    shock = _price_shock()
    _write_shocks(shocks, [shock])

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=MANDATE,
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


def test_missing_series_blocks_without_a_thesis(tmp_path: Path) -> None:
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
        mandate_path=MANDATE,
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


def test_second_call_for_the_same_day_does_not_append(tmp_path: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    _write_shocks(shocks, [_price_shock()])
    kwargs = dict(
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=MANDATE,
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


def test_retry_after_a_lost_week_log_does_not_duplicate_the_thesis(tmp_path: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    _write_shocks(shocks, [_price_shock()])
    kwargs = dict(
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=MANDATE,
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


def test_injected_research_bar_is_not_paper(tmp_path: Path) -> None:
    db, pipeline, shocks, ledger, week_log, out = _layout(tmp_path)
    _write_pipeline(pipeline, tmp_path)
    shocks.write_text("shocks: []\n", encoding="utf-8")

    result = forward_week(
        db,
        AS_OF,
        pipeline_config=pipeline,
        shocks_path=shocks,
        mandate_path=MANDATE,
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
) -> None:
    payload = {
        "db_path": str(tmp_path / "unused.duckdb"),
        "raw_root": str(tmp_path / "raw"),
        "manifest_dir": str(tmp_path / "manifests"),
        "series": series or [],
        "required": required or [],
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


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
