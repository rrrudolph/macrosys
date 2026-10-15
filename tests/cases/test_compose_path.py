"""Composed case replay as one propagation (spec §3.4, §10.1).

The cases here are built in memory. Historical YAML under ``cases/`` stays on one-step replay.
The wired pair is A04 ``total_use`` into B04 ``d_sales``: both are ``USD_bn`` in the library, so
the path needs no converter. Input levels are the mechanism example blocks, which close A04's
identity and give B04 the companions its form requires.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from macrosys.cases.runner import CaseRunner
from macrosys.cases.schema import (
    Case,
    FixtureLevel,
    ObservedPoint,
    ReplayStep,
    StructureEntity,
    StructurePoint,
    load_cases,
)
from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.core.units import compatible
from macrosys.decide.shock import Shock
from macrosys.mechanisms.compose import Propagator, Wire
from macrosys.mechanisms.library import Library, load_library

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = load_library(ROOT / "mechanisms")
CASES = load_cases(ROOT / "cases")
CUTOFF = datetime(2021, 3, 11, 23, 59, 59, tzinfo=UTC)
A04 = "A04_supply_use"
B04 = "B04_inventory_cycle"
WIRE = Wire(input="d_sales", from_step=0, output="total_use")

# Mechanism example blocks (mechanisms/A04_supply_use.yaml, mechanisms/B04_inventory_cycle.yaml).
_A04_FIXED = {
    "domestic_output": FixtureLevel(value=80, unit="USD_bn"),
    "intermediate_use": FixtureLevel(value=30, unit="USD_bn"),
    "household": FixtureLevel(value=25, unit="USD_bn"),
    "government": FixtureLevel(value=10, unit="USD_bn"),
    "investment": FixtureLevel(value=15, unit="USD_bn"),
    "d_inventories": FixtureLevel(value=5, unit="USD_bn"),
    "exports": FixtureLevel(value=15, unit="USD_bn"),
}
_B04_FIXED = {
    "inventory": FixtureLevel(value=100, unit="USD_bn"),
    "sales": FixtureLevel(value=200, unit="USD_bn"),
    "target_ratio": FixtureLevel(value=0.5, unit="share"),
}


def _skip_if_units_differ() -> None:
    source = LIBRARY.get(A04).output_unit("total_use")
    dest = LIBRARY.get(B04).input_unit("d_sales")
    if source == dest or compatible(source, dest):
        return
    pytest.skip(
        f"no unit-compatible pair without a new coefficient: A04 total_use is {source}, B04 d_sales is {dest}"
    )


def _observed(mechanism_id: str, output: str) -> ObservedPoint:
    return ObservedPoint(
        entity=StructureEntity(type="economy", id="USA"),
        variable=output,
        sign=1,
        horizon_q=(0.0, 4.0),
        source_url="https://example.com/compose-path",
        source_note="In-memory sign check for the composed replay. Not a published reading.",
        mechanism_id=mechanism_id,
        output=output,
    )


def _case(
    hint: list[str],
    replay: dict[str, ReplayStep],
    *,
    compose_path: bool,
    observed: list[ObservedPoint] | None = None,
) -> Case:
    points = observed or [_observed(A04, "total_use"), _observed(B04, "production_response")]
    expected = [
        StructurePoint(
            entity=point.entity,
            variable=point.variable,
            sign=point.sign,
            horizon_q=point.horizon_q,
        )
        for point in points
    ]
    return Case(
        case_id="compose_path_memory",
        title="In-memory composed replay",
        shock=Shock(
            id="compose-a04-b04",
            type="demand",
            subtype="consumption",
            origin=EntityKey(entity_type="economy", entity_id="USA"),
            size=Quantity(value=20, unit="USD_bn", provenance="case_fixture"),
            start=date(2021, 3, 11),
            stage="observed",
            certainty=1.0,
            evidence_refs=["case_fixture"],
            information_cutoff=CUTOFF,
        ),
        information_cutoff=CUTOFF,
        held_out_group="compose",
        expected_structure=expected,
        observed_structure=points,
        mechanism_path_hint=hint,
        compose_path=compose_path,
        fixture_observations="fixtures/us_fiscal_2021.csv",
        notes="The shock size and observation file are case_fixture, not measurements.",
        replay=replay,
    )


def _wired_replay(*, later_shock: float | None = None, first_shock: float | None = None) -> dict[str, ReplayStep]:
    later = ReplayStep(
        inputs=dict(_B04_FIXED),
        consumes=[WIRE],
        shock_value=later_shock,
        shock_unit="USD_bn" if later_shock is not None else None,
    )
    first = ReplayStep(
        shock_input="imports",
        inputs=dict(_A04_FIXED),
        shock_value=first_shock,
        shock_unit="USD_bn" if first_shock is not None else None,
    )
    return {A04: first, B04: later}


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    real = Propagator.run

    def wrapped(self, shock, state, path, run_id=None):
        record = {"converters": list(self.converters), "shock": shock, "path": path}
        calls.append(record)
        run = real(self, shock, state, path, run_id=run_id)
        record["run"] = run
        return run

    monkeypatch.setattr("macrosys.cases.runner.Propagator.run", wrapped)
    return calls


def _replay(case: Case, library: Library = LIBRARY):
    return CaseRunner(replay_parameters=False).replay(case, library)


def test_compose_path_runs_a04_total_use_into_b04_once(monkeypatch: pytest.MonkeyPatch):
    _skip_if_units_differ()
    loaded = ReplayStep.model_validate({"consumes": [{"input": "d_sales", "from_step": 0, "output": "total_use"}]})
    assert loaded.consumes == [WIRE]
    # 999 would unbalance A04 if it replaced the case shock. It must not.
    case = _case([A04, B04], _wired_replay(first_shock=999), compose_path=True)
    calls = _capture(monkeypatch)
    result = _replay(case)

    assert result.errors == []
    assert result.n_blocked == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["converters"] == []
    path = call["path"]
    assert [step.mechanism_id for step in path.steps] == [A04, B04]
    assert path.steps[1].consumes == [WIRE]
    assert path.steps[0].bindings == {"entity_type": "economy", "entity_id": "USA"}
    accepted = {item.mechanism: item for item in path.accepted_competing}
    assert "B04_inventory_cycle" not in accepted
    assert accepted["B03_bottleneck_pricing"].observable == "not on this case path"
    assert accepted["B03_bottleneck_pricing"].threshold.unit == "share"
    assert accepted["B03_bottleneck_pricing"].threshold.provenance == "case_replay"
    shock = call["shock"]
    assert shock.target_input == "imports"
    assert shock.magnitude.value == 20
    assert shock.magnitude.unit == "USD_bn"
    run = call["run"]
    assert [step.mechanism_id for step in run.steps] == [A04, B04]
    assert run.steps[0].inputs["imports"].value == 20
    assert run.steps[1].inputs["d_sales"].value == run.steps[0].outputs["total_use"].value
    assert case.replay[B04].shock_value is None
    assert result.n_scored == 2
    assert result.n_sign_correct == 2
    assert result.n_author_fed == 0
    assert result.n_fixture_params == 0
    assert result.n_evidential == 2
    assert result.n_evidential_correct == 2


def test_compose_path_without_a_wire_records_a_unit_error(monkeypatch: pytest.MonkeyPatch):
    case = _case(
        [A04, B04],
        {
            A04: ReplayStep(shock_input="imports", inputs=dict(_A04_FIXED)),
            B04: ReplayStep(inputs=dict(_B04_FIXED)),
        },
        compose_path=True,
    )
    calls = _capture(monkeypatch)
    result = _replay(case)

    assert result.n_scored == 0
    assert result.n_sign_correct == 0
    assert result.n_sign_wrong == 0
    assert result.blocked == []
    assert len(result.errors) == 1
    assert "UnitContinuityError" in result.errors[0]
    assert "no converter" in result.errors[0]
    assert len(calls) == 1
    assert calls[0]["converters"] == []
    assert calls[0]["path"].steps[1].consumes == []


def test_compose_path_false_keeps_one_step_replay(monkeypatch: pytest.MonkeyPatch):
    assert all(case.compose_path is False for case in CASES)
    assert all(step.consumes == [] for case in CASES for step in case.replay.values())
    fiscal = next(case for case in CASES if case.case_id == "us_fiscal_2021")
    assert fiscal.mechanism_path_hint is not None
    assert len(fiscal.mechanism_path_hint) >= 2
    calls = _capture(monkeypatch)
    result = CaseRunner().replay(fiscal, LIBRARY)
    assert result.errors == []
    assert result.n_scored >= 1
    assert [step.mechanism_id for call in calls for step in call["path"].steps] == list(fiscal.mechanism_path_hint)
    assert all(len(call["path"].steps) == 1 for call in calls)
    assert all("compose_path requires" not in err for err in result.errors)


@pytest.mark.parametrize("hint", [[], [A04]])
def test_compose_path_with_fewer_than_two_mechanisms_does_not_score(
    monkeypatch: pytest.MonkeyPatch, hint: list[str]
):
    calls = _capture(monkeypatch)
    result = _replay(_case(hint, {}, compose_path=True))
    assert calls == []
    assert result.n_scored == 0
    assert result.n_sign_wrong == 0
    assert result.blocked == []
    assert result.errors == ["compose_path requires at least two mechanism ids"]


def test_later_shock_value_is_not_evidential_and_does_not_replace_the_wire(
    monkeypatch: pytest.MonkeyPatch,
):
    _skip_if_units_differ()
    case = _case([A04, B04], _wired_replay(later_shock=5), compose_path=True)
    calls = _capture(monkeypatch)
    result = _replay(case)
    assert result.errors == []
    assert len(calls) == 1
    run = calls[0]["run"]
    wired = run.steps[0].outputs["total_use"].value
    assert run.steps[1].inputs["d_sales"].value == wired
    assert run.steps[1].inputs["d_sales"].value != 5
    assert result.n_scored == 2
    assert result.n_author_fed == 1
    assert result.n_evidential == 1
    assert result.n_evidential_correct == 1
    assert result.n_fixture_params == 0
