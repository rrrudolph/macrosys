"""Loader rejections (spec §3.1, W1.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from macrosys.mechanisms import load_library

ROOT = Path(__file__).resolve().parents[2] / "mechanisms"


def _write(directory: Path, name: str, data: dict) -> None:
    directory.joinpath(name).write_text(yaml.safe_dump(data), encoding="utf-8")


def _valid() -> dict:
    return {
        "id": "A01_expenditure_identity",
        "name": "Expenditure identity",
        "class": "identity",
        "scope": ["economy"],
        "inputs": [{"name": "Y", "unit": "USD_bn"}, {"name": "C", "unit": "USD_bn"}],
        "outputs": [{"name": "residual", "unit": "USD_bn"}],
        "form": "residual = Y - C",
        "parameters": [{"name": "tolerance", "unit": "USD_bn", "value": 1.0e-6, "source": "accounting_identity"}],
        "horizon": {"impact": 0, "peak": 0, "decay": None},
        "state_dependence": [],
        "assumptions": ["test"],
        "competing": [],
        "evidence": ["national accounts"],
        "falsification": "a residual outside tolerance is a data error",
        "uncertainty": {"type": "measurement", "note": "coverage"},
        "confidence": "M",
        "references": [],
        "monotone": {},
        "tests": {"example": {"inputs": {}, "params": {}, "expected": {}}},
    }


def test_library_loads_shipped_records():
    lib = load_library(ROOT)
    assert len(lib.ids()) == 19
    assert len(lib.version) == 64
    assert lib.get("B09_okun").parameters[0].source == "BallLeighLoungani2017"


def test_missing_falsification_rejected(tmp_path: Path):
    data = _valid()
    del data["falsification"]
    _write(tmp_path, "A01_expenditure_identity.yaml", data)
    with pytest.raises(ValidationError):
        load_library(tmp_path)


def test_unknown_unit_rejected(tmp_path: Path):
    data = _valid()
    data["inputs"][0]["unit"] = "widgets"
    _write(tmp_path, "A01_expenditure_identity.yaml", data)
    with pytest.raises(ValidationError):
        load_library(tmp_path)


def test_bad_id_rejected(tmp_path: Path):
    data = _valid()
    data["id"] = "not_an_id"
    _write(tmp_path, "not_an_id.yaml", data)
    with pytest.raises(ValidationError):
        load_library(tmp_path)


def test_todo_estimate_with_a_value_rejected(tmp_path: Path):
    data = _valid()
    data["parameters"].append({"name": "kappa", "unit": "ratio", "value": 0.4, "source": "TODO_estimate"})
    _write(tmp_path, "A01_expenditure_identity.yaml", data)
    with pytest.raises(ValidationError):
        load_library(tmp_path)


def test_unresolved_competing_id_rejected(tmp_path: Path):
    data = _valid()
    data["competing"] = ["B99_missing"]
    _write(tmp_path, "A01_expenditure_identity.yaml", data)
    with pytest.raises(ValueError, match="unresolved competing"):
        load_library(tmp_path)
