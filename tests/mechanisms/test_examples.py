"""Each YAML example reproduces its expected output (spec §10.1 items 3–4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from macrosys.core.quantity import Quantity
from macrosys.mechanisms import (
    ArrayQuantity,
    MissingParameterError,
    evaluate,
    get_impl,
    load_example,
    load_library,
)

LIB = load_library(Path(__file__).resolve().parents[2] / "mechanisms")
IDS = LIB.ids()


def _close(actual, expected) -> None:
    if isinstance(expected, ArrayQuantity):
        assert isinstance(actual, ArrayQuantity)
        assert actual.unit == expected.unit
        assert actual.rows == expected.rows
        assert actual.cols == expected.cols
        assert actual.values == pytest.approx(expected.values, rel=1e-9, abs=1e-9)
        return
    assert isinstance(actual, Quantity)
    assert actual.unit == expected.unit
    if expected.value is not None:
        assert actual.mid == pytest.approx(expected.value, rel=1e-9, abs=1e-9)
    if expected.low is not None:
        assert actual.low == pytest.approx(expected.low, rel=1e-9, abs=1e-9)
    if expected.high is not None:
        assert actual.high == pytest.approx(expected.high, rel=1e-9, abs=1e-9)


@pytest.mark.parametrize("mech_id", IDS, ids=IDS)
def test_yaml_example(mech_id: str):
    spec = LIB.get(mech_id)
    inputs, params, expected = load_example(spec)
    if not expected:
        with pytest.raises(MissingParameterError):
            evaluate(spec, inputs, params)
        return
    actual = evaluate(spec, inputs, params)
    for name, exp in expected.items():
        _close(actual[name], exp)


def test_b16_names_the_missing_parameter():
    impl = get_impl("B16_fx_passthrough")
    inputs = {
        "d_fx": Quantity(value=10.0, unit="pct"),
        "invoicing_share": Quantity(value=0.5, unit="share"),
    }
    with pytest.raises(MissingParameterError) as caught:
        impl.apply(inputs, {})
    assert caught.value.parameter == "pt_import"


def test_b16_runs_when_an_override_supplies_the_coefficients():
    impl = get_impl("B16_fx_passthrough")
    inputs = {
        "d_fx": Quantity(value=10.0, unit="pct"),
        "invoicing_share": Quantity(value=0.5, unit="share"),
    }
    params = {
        "pt_import": Quantity(value=0.8, unit="share", provenance="scenario"),
        "pt_cpi": Quantity(value=0.4, unit="share", provenance="scenario"),
    }
    out = impl.apply(inputs, params)
    assert out["d_import_price"].value == pytest.approx(4.0)
    assert out["d_cpi"].value == pytest.approx(2.0)


def test_b01_without_import_shares_returns_only_gross_output():
    impl = get_impl("B01_derived_demand")
    demand = ArrayQuantity(values=[4.0, 2.0], unit="USD_bn", rows=("steel", "autos"))
    inverse = ArrayQuantity(
        values=[[1.0, 0.5], [0.0, 1.0]],
        unit="ratio",
        rows=("steel", "autos"),
        cols=("steel", "autos"),
    )
    out = impl.apply({"d_final_demand": demand, "leontief": inverse}, {})
    assert "d_domestic" not in out
    assert out["d_gross_output"].values == pytest.approx([5.0, 2.0])
