"""Append-only experiment log: hash chain and trial-count deflation (spec §10.2 item 5)."""

from __future__ import annotations

import json

import pytest

from macrosys.core.quantity import Quantity
from macrosys.eval.experiment_log import ExperimentIntegrityError, ExperimentLog, ExperimentRecord
from macrosys.eval.scoring import deflated_sharpe_ratio

_SHARPE = Quantity(value=0.8, unit="ratio", provenance="test")
_N = 80
_SKEW = -0.2
_KURT = 3.5
_VAR_SR = 0.04


def _append(
    log: ExperimentLog,
    *,
    experiment_id: str,
    hypothesis: str,
    variant: str,
) -> ExperimentRecord:
    return log.append(
        experiment_id=experiment_id,
        hypothesis=hypothesis,
        variant=variant,
        sharpe=_SHARPE,
        code_commit="abc123",
        parameter_set="default",
        n=_N,
        skew=_SKEW,
        kurt=_KURT,
        var_sr_trials=_VAR_SR,
    )


def test_tampered_trial_count_fails_verify(tmp_path) -> None:
    log = ExperimentLog(tmp_path / "experiments.jsonl")
    _append(log, experiment_id="exp-1", hypothesis="real rates lead copper", variant="lag-1")
    text = log.path.read_text(encoding="utf-8")
    needle = '\\"trial_count\\":1'
    assert needle in text
    log.path.write_text(text.replace(needle, '\\"trial_count\\":9', 1), encoding="utf-8")
    with pytest.raises(ExperimentIntegrityError, match="line 1"):
        log.verify()


def test_second_variant_on_the_same_hypothesis_deflates_more(tmp_path) -> None:
    log = ExperimentLog(tmp_path / "experiments.jsonl")
    hypothesis = "real rates lead copper"
    first = _append(log, experiment_id="exp-1", hypothesis=hypothesis, variant="lag-1")
    second = _append(log, experiment_id="exp-2", hypothesis=hypothesis, variant="lag-2")
    other = _append(log, experiment_id="exp-3", hypothesis="oil follows inventories", variant="lag-1")
    assert first.trial_count == 1
    assert second.trial_count == 2
    assert other.trial_count == 1
    assert first.sharpe.unit == "ratio"
    assert second.deflated_sharpe.unit == "ratio"
    expected = deflated_sharpe_ratio(
        0.8, n_trials=2, var_sr_trials=_VAR_SR, n=_N, skew=_SKEW, kurt=_KURT
    )
    assert second.deflated_sharpe.value == pytest.approx(expected)
    assert second.deflated_sharpe.value < first.deflated_sharpe.value
    assert other.deflated_sharpe.value == pytest.approx(first.deflated_sharpe.value)


def test_untouched_file_verifies(tmp_path) -> None:
    log = ExperimentLog(tmp_path / "experiments.jsonl")
    _append(log, experiment_id="exp-1", hypothesis="real rates lead copper", variant="lag-1")
    _append(log, experiment_id="exp-2", hypothesis="real rates lead copper", variant="lag-2")
    assert log.verify() is True
    reloaded = ExperimentLog(log.path).records()
    assert [record.trial_count for record in reloaded] == [1, 2]
    assert json.loads(log.path.read_text(encoding="utf-8").splitlines()[0])["prev_hash"] == ""
