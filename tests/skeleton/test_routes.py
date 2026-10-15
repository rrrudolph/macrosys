"""Routes for shocks the trigger rules file (spec §4.1, §12.3, §12.4).

Each new pair is selected by ``route_for``. Unit continuity is checked with an empty converter
list, the same list the forward week uses. A path either emits its effect or refuses on the
parameter or the unit break the library actually has.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.core.units import compatible
from macrosys.decide.shock import Shock
from macrosys.mechanisms import MissingParameterError, Propagator, UnitContinuityError, validate_path
from macrosys.mechanisms.compose import Shock as PropagationShock
from macrosys.mechanisms.library import Library, load_library
from macrosys.skeleton.routing import Route, route_for

LIBRARY = load_library("mechanisms")
AS_OF = datetime(2026, 10, 14, 21, 0, tzinfo=UTC)


class _State:
    """Empty point-in-time state. These routes do not read a world-state variable."""

    as_of = AS_OF

    def has(self, entity: EntityKey, var: str) -> bool:
        del entity, var
        return False

    def get(self, entity: EntityKey, var: str) -> Quantity:
        del entity
        raise KeyError(var)


def _shock(
    family: str,
    subtype: str,
    origin: EntityKey,
    value: float,
    unit: str,
    *,
    stage: str = "observed",
) -> Shock:
    """A trigger-shaped shock: the size is the signal, provenance names the rule (spec §4.1)."""
    return Shock(
        id=f"t-{family}-{subtype}-{origin.entity_id}",
        type=family,  # type: ignore[arg-type]
        subtype=subtype,
        origin=origin,
        size=Quantity(value=value, unit=unit, provenance=f"trigger:test-{family}-{subtype}"),
        start=date(2026, 10, 1),
        stage=stage,
        certainty=0.5,
        evidence_refs=[f"trigger:test-{family}-{subtype}"],
        information_cutoff=AS_OF,
    )


def _run(route: Route, shock: Shock, library: Library = LIBRARY):
    path = route.propagation_path(library)
    return Propagator(library).run(
        PropagationShock(shock_id=shock.id, magnitude=shock.size, target_input=route.target_input),
        _State(),
        path,
        run_id="route-test",
    )


def test_existing_pairs_keep_their_paths() -> None:
    demand = _shock(
        "demand",
        "investment",
        EntityKey(entity_type="economy", entity_id="USA"),
        15.0,
        "USD_bn",
        stage="contracted",
    )
    transport = _shock("supply", "input_disruption", EntityKey(entity_type="commodity", entity_id="freight"), 5.0, "USD_bn")
    brl = _shock("price", "fx", EntityKey(entity_type="currency", entity_id="BRL"), 15.0, "pct")
    assert route_for(demand).mechanism_ids() == ["A04_supply_use", "B03_bottleneck_pricing"]
    assert route_for(demand).instrument == "CPER"
    assert route_for(transport).mechanism_ids() == [
        "A04_supply_use",
        "B04_inventory_cycle",
        "B03_bottleneck_pricing",
    ]
    assert route_for(transport).instrument == "BDRY"
    fx = route_for(brl)
    assert fx.mechanism_ids() == ["B16_fx_passthrough"]
    assert fx.instrument == "EMLC"
    assert fx.target_input == "d_fx"


def test_demand_and_supply_subtypes_fall_back_to_the_type_route() -> None:
    consumption = _shock("demand", "consumption", EntityKey(entity_type="economy", entity_id="USA"), 1.0, "pct")
    capacity = _shock("supply", "capacity_loss", EntityKey(entity_type="economy", entity_id="JPN"), 1.0, "pct")
    assert route_for(consumption).mechanism_ids() == ["A04_supply_use", "B03_bottleneck_pricing"]
    assert route_for(capacity).instrument == "BDRY"


def test_unknown_pair_raises_key_error() -> None:
    commodity = _shock("price", "commodity", EntityKey(entity_type="commodity", entity_id="Brent"), 10.0, "pct")
    funding = _shock("financial", "funding", EntityKey(entity_type="institution", entity_id="FED"), 50.0, "bp")
    with pytest.raises(KeyError, match="price/commodity"):
        route_for(commodity)
    with pytest.raises(KeyError, match="financial/funding"):
        route_for(funding)


def test_collateral_route_refuses_on_b13_elasticity() -> None:
    shock = _shock("financial", "collateral", EntityKey(entity_type="instrument", entity_id="XLK"), -12.0, "pct")
    route = route_for(shock)
    assert route.mechanism_ids() == ["B13_financial_accelerator"]
    assert route.target_input == "d_asset_price"
    assert route.instrument == "SPY"
    assert LIBRARY.get("B13_financial_accelerator").input_unit("d_asset_price") == "pct"
    validate_path(route.propagation_path(LIBRARY), LIBRARY, [])
    with pytest.raises(MissingParameterError) as caught:
        _run(route, shock)
    assert caught.value.parameter == "elasticity"


def test_credit_route_runs_the_sloos_range_on_the_oas_change() -> None:
    shock = _shock("financial", "credit", EntityKey(entity_type="economy", entity_id="USA"), 1.02, "pp")
    route = route_for(shock)
    assert route.mechanism_ids() == ["B12_bank_credit"]
    assert route.target_input == "standards"
    assert route.effect_output == "d_credit"
    assert route.effect_unit == "pct"
    assert route.instrument == "HYG"
    validate_path(route.propagation_path(LIBRARY), LIBRARY, [])
    run = _run(route, shock)
    credit = run.steps[0].outputs["d_credit"]
    elasticity = next(item for item in LIBRARY.get("B12_bank_credit").parameters if item.name == "elasticity")
    assert elasticity.low is not None and elasticity.high is not None
    assert credit.unit == "pct"
    assert credit.low == pytest.approx(-elasticity.high * 1.02)
    assert credit.high == pytest.approx(-elasticity.low * 1.02)
    assert credit.high < 0.0


def test_flows_route_breaks_on_the_b21_to_b27_units() -> None:
    shock = _shock("financial", "flows", EntityKey(entity_type="economy", entity_id="USA"), 30.0, "index")
    route = route_for(shock)
    assert route.mechanism_ids() == ["B21_positioning", "B27_funding_squeeze"]
    assert route.instrument == "HYG"
    b21_in = LIBRARY.get("B21_positioning").input_unit(route.target_input)
    assert b21_in == "share"
    assert not compatible(shock.size.unit, b21_in)
    with pytest.raises(UnitContinuityError) as caught:
        validate_path(route.propagation_path(LIBRARY), LIBRARY, [])
    assert caught.value.source == "B21_positioning"
    assert caught.value.dest == "B27_funding_squeeze"
    assert caught.value.from_unit == "ratio"
    assert caught.value.to_unit == "bp"
    with pytest.raises(UnitContinuityError) as propagated:
        _run(route, shock)
    assert propagated.value.from_unit == "ratio"
    assert propagated.value.to_unit == "bp"


def test_rates_route_refuses_on_c05_duration() -> None:
    shock = _shock("price", "rates", EntityKey(entity_type="economy", entity_id="USA"), -0.77, "pp")
    route = route_for(shock)
    assert route.mechanism_ids() == ["C05_equity_duration"]
    assert route.target_input == "d_discount"
    assert route.instrument == "SPY"
    assert route.effect_output == "d_price"
    assert LIBRARY.get("C05_equity_duration").input_unit("d_discount") == "pp"
    validate_path(route.propagation_path(LIBRARY), LIBRARY, [])
    with pytest.raises(MissingParameterError) as caught:
        _run(route, shock)
    assert caught.value.parameter == "duration"


def test_dollar_origin_uses_b18_and_other_currencies_keep_b16() -> None:
    usd = _shock("price", "fx", EntityKey(entity_type="currency", entity_id="USD"), 3.66, "pct")
    eur = _shock("price", "fx", EntityKey(entity_type="currency", entity_id="EUR"), 3.0, "pct")
    dollar = route_for(usd)
    assert dollar.mechanism_ids() == ["B18_global_financial_cycle"]
    assert dollar.target_input == "d_usd"
    assert dollar.instrument == "EMB"
    assert dollar.effect_output == "d_credit"
    assert LIBRARY.get("B18_global_financial_cycle").input_unit("d_usd") == "pct"
    validate_path(dollar.propagation_path(LIBRARY), LIBRARY, [])
    with pytest.raises(MissingParameterError) as caught:
        _run(dollar, usd)
    assert caught.value.parameter == "elasticity"
    assert route_for(eur).mechanism_ids() == ["B16_fx_passthrough"]
