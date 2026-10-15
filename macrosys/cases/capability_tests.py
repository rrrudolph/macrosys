"""Named capability tests (spec §10.1 item 8).

Each test is a synthetic, labelled fixture and a structural expectation. A mechanism that still
refuses because a parameter is ``TODO_estimate`` leaves ``blocked_parameter`` set. The pytest
module marks that case ``xfail(strict=True)`` so the missing parameter stays visible.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.decide.gates import decide, gap_gate
from macrosys.decide.thesis import (
    CompetingEffect,
    EvidenceQuality,
    ExpectedEffect,
    ExpectedReturn,
    Expression,
    ForecastPrecision,
    Invalidation,
    InvalidationRule,
    PricedIn,
    Scenario,
    SizingInputs,
    Thesis,
    Versions,
)
from macrosys.events.response import load_responses, response_block
from macrosys.mechanisms.errors import MissingParameterError
from macrosys.mechanisms.eval import evaluate
from macrosys.mechanisms.library import Library
from macrosys.mechanisms.registry import get_impl
from macrosys.store.documents import Claim, ClaimStore, Document, DocumentStore
from macrosys.store.observations import ObservationStore
from macrosys.store.schema import create_schema
from macrosys.store.state import WorldStateStore

_FIXTURE = "case_fixture"
# Synthetic slope. Provenance is "test": not a payrolls or CPI estimate (spec §10.1 item 8).
_RELEASE = "fixture-release"
_MARKET = "fixture-market"
_HORIZON_DAYS = 1
_RESPONSE_YAML = """\
entries:
- name: fixture-release->fixture-market@1d
  release: fixture-release
  market: fixture-market
  horizon_days: 1
  beta:
    value: 1.0
    low: 0.5
    high: 2.0
    unit: bp
    per: 1 sd of standardized surprise
    source: test
  predicted_sign: 1
  mechanisms:
  - C09_policy_path
"""


@dataclass
class CapabilityResult:
    """What one named capability fixture produced (spec §10.1 item 8)."""

    name: str
    blocked_parameter: str | None = None
    details: dict[str, float | str | bool | None] = field(default_factory=dict)


def diversification_assumption(*, equity_duration_correlation: float, inflation_shock: bool) -> str:
    """Hedge rule under an inflation shock (spec §6.6 first row, §10.1 item 8).

    A positive equity-duration correlation means the two fall together. In an inflation or
    policy regime that co-movement is not a reason to keep a fixed Treasury hedge. The return
    value ``revised`` is that change of assumption; ``fixed_treasury_hedge`` is the rule the
    inflation shock retires.
    """
    if inflation_shock and equity_duration_correlation > 0.0:
        return "revised"
    return "fixed_treasury_hedge"


def run_capability(name: str, library: Library) -> CapabilityResult:
    """Run one named capability fixture against the loaded library (spec §10.1 item 8)."""
    try:
        runner = _RUNNERS[name]
    except KeyError as exc:
        raise KeyError(f"unknown capability {name!r}") from exc
    return runner(library)


def assert_capability(name: str, result: CapabilityResult) -> None:
    """Raise ``AssertionError`` when ``result`` misses the structural expectation (spec §10.1 item 8).

    A blocked parameter is a failure of the full capability, named in the message, so a test can
    mark it as an expected failure without treating the gap as a pass.
    """
    if result.name != name:
        raise AssertionError(f"result {result.name!r} is not capability {name!r}")
    _CHECKS[name](result)


def _q(value: float, unit: str, *, low: float | None = None, high: float | None = None) -> Quantity:
    return Quantity(value=value, unit=unit, low=low, high=high, provenance=_FIXTURE)


def _blocked(name: str, parameter: str | None, details: dict[str, float | str | bool | None]) -> CapabilityResult:
    return CapabilityResult(name=name, blocked_parameter=parameter, details=details)


def _call(spec, inputs: dict[str, Quantity]) -> tuple[dict[str, Quantity] | None, str | None]:
    try:
        produced = evaluate(spec, inputs)
    except MissingParameterError as exc:
        return None, exc.parameter
    return produced, None  # type: ignore[return-value]


def _require(result: CapabilityResult) -> None:
    if result.blocked_parameter:
        raise AssertionError(f"parameter {result.blocked_parameter} is TODO_estimate")


def _u_kink(library: Library) -> dict[str, Quantity]:
    spec = library.get("B03_bottleneck_pricing")
    param = next(item for item in spec.parameters if item.name == "u_kink")
    if param.value is None:
        raise AssertionError("u_kink has no value on the B03 record")
    return {"u_kink": Quantity(value=param.value, unit=param.unit, provenance=param.source)}


def _gate(library: Library, utilization: float):
    conn = duckdb.connect(database=":memory:")
    try:
        create_schema(conn)
        as_of = datetime(2020, 1, 1, tzinfo=UTC)
        entity = EntityKey(entity_type="industry", entity_id="SYN:cap")
        WorldStateStore(conn).write(
            as_of,
            entity,
            "capacity_utilization",
            _q(utilization, "share"),
            _FIXTURE,
        )
        state = WorldStateStore(conn).view(as_of)
        spec = library.get("B03_bottleneck_pricing")
        return get_impl(spec.id).applicable(
            state,
            {"entity_type": "industry", "entity_id": "SYN:cap"},
            _u_kink(library),
        )
    finally:
        conn.close()


def _abundant(library: Library) -> CapabilityResult:
    """Low utilization gates B03 off; B08 splits the cost with the estimated capture (spec §10.1 item 8)."""
    gate = _gate(library, 0.55)
    produced, parameter = _call(library.get("B08_pass_through"), {"d_cost": _q(10.0, "pct")})
    details: dict[str, float | str | bool | None] = {
        "gate_factor": gate.factor,
        "gate_status": gate.status,
    }
    if produced is not None:
        details["retained"] = produced["retained"].mid
        details["passed_on"] = produced["passed_on"].mid
    return _blocked("abundant_supply", parameter, details)


def _scarce(library: Library) -> CapabilityResult:
    """High utilization leaves B03 on; B07 and B05 are the adaptation paths (spec §10.1 item 8)."""
    gate = _gate(library, 0.85)
    _, b03_param = _call(
        library.get("B03_bottleneck_pricing"),
        {"d_demand": _q(4.0, "pct"), "u": _q(0.85, "share")},
    )
    _, b07_param = _call(library.get("B07_substitution"), {"d_relative_price": _q(4.0, "pct")})
    b05, b05_param = _call(
        library.get("B05_capacity_pipeline"),
        {"pipeline": _q(10.0, "USD_bn"), "demand": _q(12.0, "USD_bn"), "lead_time": _q(6.0, "q")},
    )
    parameter = b03_param or b07_param or b05_param
    details: dict[str, float | str | bool | None] = {"gate_factor": gate.factor, "gate_status": gate.status}
    if b05 is not None:
        details["arrival_q"] = b05["arrival_q"].mid
    return _blocked("scarce_complement", parameter, details)


def _productivity(library: Library) -> CapabilityResult:
    """Separate unit requirements, usage, pass-through, and capture (spec §10.1 item 8)."""
    b26, p_through = _call(library.get("B26_productivity"), {"d_tfp": _q(2.0, "pct")})
    b06, eps = _call(library.get("B06_demand_elasticity"), {"d_price": _q(-1.0, "pct")})
    b08, capture = _call(library.get("B08_pass_through"), {"d_cost": _q(-1.0, "pct")})
    details: dict[str, float | str | bool | None] = {}
    if b26 is not None:
        details["d_ulc"] = b26["d_ulc"].mid
        details["d_price"] = b26["d_price"].mid
    if b06 is not None:
        details["d_demand_sr"] = b06["d_demand_sr"].mid
    if b08 is not None:
        details["passed_on"] = b08["passed_on"].mid
        details["retained"] = b08["retained"].mid
    return _blocked("productivity", p_through or eps or capture, details)


def _currency(library: Library) -> CapabilityResult:
    """Exporters, imported inputs, and the reporting currency (spec §10.1 item 8, §5.2)."""
    income, _ = _call(
        library.get("B17_terms_of_trade"),
        {"d_tot": _q(10.0, "pct"), "trade_share": _q(0.3, "share")},
    )
    report, _ = _call(
        library.get("A09_etf_return"),
        {
            "r_local": _q(0.0, "share"),
            "r_fx": _q(-0.15, "share"),
            "fees": _q(0.0, "share"),
            "r_observed": _q(-0.15, "share"),
        },
    )
    prices, parameter = _call(
        library.get("B16_fx_passthrough"),
        {"d_fx": _q(10.0, "pct"), "invoicing_share": _q(0.5, "share")},
    )
    details: dict[str, float | str | bool | None] = {}
    if income is not None:
        details["exporter_income"] = income["d_income"].mid
    if report is not None:
        details["reporting_return"] = report["r_base"].mid
        details["reporting_residual"] = report["residual"].mid
    if prices is not None:
        details["import_price"] = prices["d_import_price"].mid
    return _blocked("currency_depreciation", parameter, details)


def _anticipated(library: Library) -> CapabilityResult:
    """Anticipated level versus the policy surprise (spec §10.1 item 8)."""
    path, premium = _call(
        library.get("C09_policy_path"),
        {"futures_rate": _q(5.0, "pct")},
    )
    demand, multiplier = _call(library.get("B11_monetary_transmission"), {"d_policy": _q(0.25, "pct")})
    details: dict[str, float | str | bool | None] = {"futures_rate": 5.0}
    if path is not None:
        details["adjusted_policy_rate"] = path["policy_rate"].mid
    if demand is not None:
        details["demand_sign"] = float(_sign_of(demand["d_demand"].mid))
    return _blocked("anticipated_rate", premium or multiplier, details)


def _funding(library: Library) -> CapabilityResult:
    """Net external balance can close while gross funding still matters (spec §10.1 item 8, A03, B27)."""
    identity, _ = _call(
        library.get("A03_balance_of_payments"),
        {
            "ca": _q(-5.0, "USD_bn"),
            "ka": _q(0.0, "USD_bn"),
            "fa": _q(5.0, "USD_bn"),
            "eo": _q(0.0, "USD_bn"),
            "diip": _q(5.0, "USD_bn"),
            "valuation": _q(0.0, "USD_bn"),
        },
    )
    squeeze, parameter = _call(library.get("B27_funding_squeeze"), {"funding_stress": _q(1.0, "bp")})
    details: dict[str, float | str | bool | None] = {"gross_flow": 5.0}
    if identity is not None:
        details["net_residual"] = identity["residual"].mid
    if squeeze is not None:
        details["spread"] = squeeze["d_spread"].mid
    return _blocked("funding_gross_vs_net", parameter, details)


def _inflation(library: Library) -> CapabilityResult:
    """An inflation shock revises the stock-bond hedge (spec §6.6, §10.1 item 8)."""
    del library
    revised = diversification_assumption(equity_duration_correlation=0.4, inflation_shock=True)
    unchanged = diversification_assumption(equity_duration_correlation=-0.3, inflation_shock=False)
    return CapabilityResult(
        name="inflation_hedge",
        details={"inflation_rule": revised, "calm_rule": unchanged},
    )


def _future_dated(library: Library) -> CapabilityResult:
    """A document or observation dated after ``as_of`` is not visible (spec §9.2, §10.1 item 8)."""
    del library
    as_of = datetime(2020, 1, 15, tzinfo=UTC)
    earlier = datetime(2020, 1, 10, tzinfo=UTC)
    later = datetime(2020, 2, 1, tzinfo=UTC)
    conn = duckdb.connect(database=":memory:")
    try:
        create_schema(conn)
        from macrosys.core.contracts import Observation

        ObservationStore(conn).append(
            [
                Observation(
                    series_id="FRED:TCU",
                    ref_period_start=date(2019, 12, 1),
                    ref_period_end=date(2019, 12, 31),
                    value=75.0,
                    unit="pct",
                    published_at=earlier,
                    ingested_at=earlier,
                    vintage_id="past",
                    source_url="https://fred.stlouisfed.org/series/TCU",
                    source_hash=_FIXTURE,
                    quality_flag="estimated",
                ),
                Observation(
                    series_id="FRED:TCU",
                    ref_period_start=date(2020, 1, 1),
                    ref_period_end=date(2020, 1, 31),
                    value=99.0,
                    unit="pct",
                    published_at=later,
                    ingested_at=later,
                    vintage_id="future",
                    source_url="https://fred.stlouisfed.org/series/TCU",
                    source_hash=_FIXTURE,
                    quality_flag="estimated",
                ),
            ]
        )
        visible = ObservationStore(conn).snapshot(as_of).get("FRED:TCU")
        entity = EntityKey(entity_type="economy", entity_id="USA")
        DocumentStore(conn).append(
            [
                Document(
                    doc_id="past-doc",
                    content_hash="past",
                    source="fixture",
                    url="https://example.test/past",
                    published_at=earlier,
                    retrieved_at=earlier,
                    retained_text_policy="metadata_only",
                ),
                Document(
                    doc_id="future-doc",
                    content_hash="future",
                    source="fixture",
                    url="https://example.test/future",
                    published_at=later,
                    retrieved_at=later,
                    retained_text_policy="metadata_only",
                ),
            ]
        )
        ClaimStore(conn).append(
            [
                Claim(
                    claim_id="past-claim",
                    doc_id="past-doc",
                    claimant="fixture",
                    entity_type="economy",
                    entity_id="USA",
                    variable="output",
                    claim_made_at=earlier,
                ),
                Claim(
                    claim_id="future-claim",
                    doc_id="future-doc",
                    claimant="fixture",
                    entity_type="economy",
                    entity_id="USA",
                    variable="output",
                    claim_made_at=earlier,
                ),
            ]
        )
        claims = ClaimStore(conn).claims_for(entity, as_of)
    finally:
        conn.close()
    ids = {claim.claim_id for claim in claims}
    return CapabilityResult(
        name="future_dated_document",
        details={
            "past_observation_visible": not visible.empty and float(visible.iloc[-1]["value"]) == 75.0,
            "future_observation_hidden": visible.empty or float(visible["value"].max()) != 99.0,
            "past_claim_visible": "past-claim" in ids,
            "future_claim_hidden": "future-claim" not in ids,
        },
    )


def _priced_in(library: Library) -> CapabilityResult:
    """A positive effect that is already in the price fails the gap gate (spec §11.3, §10.1 item 8)."""
    del library
    when = datetime(2024, 1, 15, tzinfo=UTC)
    thesis = Thesis(
        thesis_id="priced-in-fixture",
        created=when,
        shock_ref="fixture-shock",
        claim="the demand effect is economically positive and already in the valuation",
        mechanism_path=["B03_bottleneck_pricing"],
        propagation_run="fixture-run",
        expected_effect=ExpectedEffect(
            entity=EntityKey(entity_type="commodity", entity_id="Cu"),
            variable="price",
            change=_q(5.0, "pct"),
            window_q=(1.0, 4.0),
        ),
        priced_in=PricedIn(
            source=["fixture:valuation"],
            implied_change=_q(5.0, "pct", low=4.0, high=6.0),
            method_notes="synthetic valuation already contains the move",
        ),
        gap=_q(0.0, "pct", low=-1.0, high=1.0),
        competing_effects_accepted=[
            CompetingEffect(mechanism="B07_substitution", observable="substitution", threshold="fixture")
        ],
        expression=Expression(
            instrument="CPER",
            direction="long",
            exposure_vector_check="passed",
            structure_notes="fixture expression",
            cost_estimate_bp=5.0,
        ),
        sizing_inputs=SizingInputs(vol_annual=0.16, scenario_pnl={"base": 0.0}, factor_loadings={"equity": 0.2}),
        invalidation=Invalidation(
            rules=[InvalidationRule(observable="price", condition="flat", action="exit")],
            time_stop=date(2024, 7, 1),
        ),
        information_cutoff=when,
        evidence_quality=EvidenceQuality(observations="low", claims="n/a", inferences="low"),
        scenarios=[Scenario(name="base", weight=1.0, outcome_range=_q(5.0, "pct"))],
        forecast_precision=ForecastPrecision(range_width_pct=2.0, interval_level=0.8),
        expected_return=ExpectedReturn(net_of_costs_range=_q(0.0, "pct", low=-1.0, high=1.0), horizon_m=6.0),
        no_trade_alternative="hold cash",
        versions=Versions(library="fixture", parameters="fixture", code_commit="fixture", model_ids={}),
        missing_evidence=[],
        review_schedule="weekly",
    )
    gap = gap_gate(thesis, min_gap_bp=0.0)
    decision = decide(
        thesis,
        run_valid=True,
        identity_residuals_ok=True,
        min_gap_bp=0.0,
        exposure_match=True,
        horizon_ok=True,
        post_trade_ok=True,
        regime_flag="calm",
    )
    return CapabilityResult(
        name="priced_in_no_trade",
        details={
            "expected_positive": thesis.expected_effect.change.mid > 0.0,
            "gap_passed": gap.passed,
            "action": decision.action,
            "failing_gate": decision.failing_gate or "",
        },
    )


def _fixture_book():
    """Load the synthetic response slope (spec §10.1 item 8, §11.3).

    The yaml is built in memory and handed to ``load_responses`` through a temporary path.
    That path is not ``parameters/event_response.yaml``. The slope's provenance is ``test``.
    """
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "event_response.yaml"
        path.write_text(_RESPONSE_YAML, encoding="utf-8")
        return load_responses(path)


def _decide_on_response(reason: str | None):
    """Run the four gates; a release reason fails the gap gate (spec §10.1 item 8, §11.3).

    The thesis builder lives in the test tree, which is not part of the installed package, so it
    is imported here rather than at module import.
    """
    from tests.decide.support import make_thesis

    return decide(
        make_thesis(),
        run_valid=True,
        identity_residuals_ok=True,
        min_gap_bp=20.0,
        exposure_match=True,
        horizon_ok=True,
        post_trade_ok=True,
        regime_flag="stable",
        response_block=reason,
    )


def _observed(value: float) -> Quantity:
    return Quantity(value=value, unit="bp", provenance="test")


def _release_matches_forecast(library: Library) -> CapabilityResult:
    """A print that matches the forecast is not a tradable gap (spec §10.1 item 8, §11.3).

    A standardized surprise of 0 is the level-versus-surprise fact in spec §7.3 and §9.8.
    ``response_block`` returns a reason, and ``decide`` with that reason is no-trade on the gap gate.
    """
    del library
    reason = response_block(_fixture_book(), _RELEASE, _MARKET, _HORIZON_DAYS, 0.0, _observed(0.0))
    decision = _decide_on_response(reason)
    return CapabilityResult(
        name="release_matches_forecast",
        details={
            "reason": reason or "",
            "action": decision.action,
            "failing_gate": decision.failing_gate or "",
        },
    )


def _release_already_priced(library: Library) -> CapabilityResult:
    """A front end at the slope's conservative edge is already priced (spec §10.1 item 8, §11.3).

    The fixture interval excludes zero. Once the observed move reaches that edge, ``decide`` is
    no-trade. A smaller move leaves ``response_block`` empty, and a thesis that otherwise passes
    the four gates is a trade. The slope is a test fixture, not an estimate of payrolls or CPI.
    """
    del library
    book = _fixture_book()
    coeff = book.get(_RELEASE, _MARKET, _HORIZON_DAYS)
    if coeff is None:
        raise AssertionError("fixture response is missing")
    covered_reason = response_block(book, _RELEASE, _MARKET, _HORIZON_DAYS, 1.0, _observed(0.5))
    covered = _decide_on_response(covered_reason)
    open_reason = response_block(book, _RELEASE, _MARKET, _HORIZON_DAYS, 1.0, _observed(0.4))
    opened = _decide_on_response(open_reason)
    return CapabilityResult(
        name="release_already_priced",
        details={
            "slope_excludes_zero": not coeff.beta.straddles_zero(),
            "slope_provenance": coeff.beta.provenance,
            "covered_action": covered.action,
            "covered_gate": covered.failing_gate or "",
            "covered_reason": covered_reason or "",
            "open_unblocked": open_reason is None,
            "open_action": opened.action,
        },
    )


def _sign_of(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _check_abundant(result: CapabilityResult) -> None:
    if result.details.get("gate_factor") != 0.0 or result.details.get("gate_status") != "scaled":
        raise AssertionError("abundant supply should gate B03 off")
    _require(result)
    retained = float(result.details["retained"])  # type: ignore[arg-type]
    passed_on = float(result.details["passed_on"])  # type: ignore[arg-type]
    if retained <= 0.0 or passed_on <= 0.0:
        raise AssertionError("B08 should split a cost increase into a kept share and a passed-on share")
    if abs(retained + passed_on - 10.0) > 1e-6:
        raise AssertionError("B08 should account for the whole cost change")


def _check_scarce(result: CapabilityResult) -> None:
    if result.details.get("gate_status") != "on" or result.details.get("gate_factor") != 1.0:
        raise AssertionError("scarce capacity should leave the B03 gate on")
    _require(result)
    if result.details.get("arrival_q") != 6.0:
        raise AssertionError("B05 arrival should equal the lead time")


def _check_productivity(result: CapabilityResult) -> None:
    _require(result)
    if float(result.details["d_ulc"]) >= 0.0:  # type: ignore[arg-type]
        raise AssertionError("a productivity gain should lower unit requirements")
    if float(result.details["d_price"]) >= 0.0:  # type: ignore[arg-type]
        raise AssertionError("pass-through of a productivity gain should not raise price")


def _check_currency(result: CapabilityResult) -> None:
    if float(result.details.get("exporter_income") or 0.0) <= 0.0:
        raise AssertionError("an exporter terms-of-trade gain should raise income")
    if float(result.details.get("reporting_return") or 0.0) >= 0.0:
        raise AssertionError("an unhedged depreciation should reduce the reporting-currency return")
    if abs(float(result.details.get("reporting_residual") or 1.0)) > 1e-9:
        raise AssertionError("A09 should close for the reporting-currency identity")
    _require(result)
    if float(result.details.get("import_price") or 0.0) == 0.0:
        raise AssertionError("B16 should move import prices with the exchange rate")


def _check_anticipated(result: CapabilityResult) -> None:
    _require(result)
    adjusted = result.details.get("adjusted_policy_rate")
    if adjusted is None or float(adjusted) == float(result.details["futures_rate"]):  # type: ignore[arg-type]
        raise AssertionError("C09 should separate the futures level from the premium-adjusted rate")
    if result.details.get("demand_sign") != -1.0:
        raise AssertionError("B11 should weigh a policy surprise against demand")


def _check_funding(result: CapabilityResult) -> None:
    if abs(float(result.details.get("net_residual") or 1.0)) > 1e-6:
        raise AssertionError("A03 net identity should close")
    if float(result.details.get("gross_flow") or 0.0) <= 0.0:
        raise AssertionError("the gross flow should be visible beside a closed net")
    _require(result)
    if float(result.details.get("spread") or 0.0) == 0.0:
        raise AssertionError("B27 should move the spread with funding stress")


def _check_inflation(result: CapabilityResult) -> None:
    if result.details.get("inflation_rule") != "revised":
        raise AssertionError("an inflation shock should revise the stock-bond hedge")
    if result.details.get("calm_rule") != "fixed_treasury_hedge":
        raise AssertionError("the fixed hedge remains when there is no inflation shock")


def _check_future(result: CapabilityResult) -> None:
    for key in ("past_observation_visible", "future_observation_hidden", "past_claim_visible", "future_claim_hidden"):
        if result.details.get(key) is not True:
            raise AssertionError(f"{key} failed")


def _check_priced(result: CapabilityResult) -> None:
    if result.details.get("expected_positive") is not True:
        raise AssertionError("the economic assessment should be positive")
    if result.details.get("gap_passed") is not False:
        raise AssertionError("the gap gate should fail when the move is already priced")
    if result.details.get("action") != "no_trade" or result.details.get("failing_gate") != "gap":
        raise AssertionError("the decision should be no trade on the gap gate")


def _check_matches(result: CapabilityResult) -> None:
    """A zero surprise is no-trade on the gap gate (spec §10.1 item 8, §11.3)."""
    if result.details.get("action") != "no_trade":
        raise AssertionError("a zero surprise must not become a trade")
    if result.details.get("failing_gate") != "gap":
        raise AssertionError("a print that matches the forecast fails the gap gate")
    reason = result.details.get("reason")
    if not isinstance(reason, str) or "surprise is 0" not in reason:
        raise AssertionError("response_block should name the zero surprise")


def _check_already(result: CapabilityResult) -> None:
    """The conservative edge is no-trade; a smaller move can still trade (spec §10.1 item 8, §11.3)."""
    if result.details.get("slope_excludes_zero") is not True or result.details.get("slope_provenance") != "test":
        raise AssertionError("the fixture slope must exclude zero and carry provenance test")
    covered_reason = result.details.get("covered_reason")
    if not isinstance(covered_reason, str) or "already moved" not in covered_reason:
        raise AssertionError("the front end at the conservative edge should already be priced")
    if result.details.get("covered_action") != "no_trade" or result.details.get("covered_gate") != "gap":
        raise AssertionError("a front end at the conservative edge is no trade on the gap gate")
    if result.details.get("open_unblocked") is not True:
        raise AssertionError("a smaller front-end move should leave a residual")
    if result.details.get("open_action") != "trade":
        raise AssertionError("a residual front-end move should pass the four gates")


_RUNNERS = {
    "abundant_supply": _abundant,
    "scarce_complement": _scarce,
    "productivity": _productivity,
    "currency_depreciation": _currency,
    "anticipated_rate": _anticipated,
    "funding_gross_vs_net": _funding,
    "inflation_hedge": _inflation,
    "future_dated_document": _future_dated,
    "priced_in_no_trade": _priced_in,
    "release_matches_forecast": _release_matches_forecast,
    "release_already_priced": _release_already_priced,
}

_CHECKS = {
    "abundant_supply": _check_abundant,
    "scarce_complement": _check_scarce,
    "productivity": _check_productivity,
    "currency_depreciation": _check_currency,
    "anticipated_rate": _check_anticipated,
    "funding_gross_vs_net": _check_funding,
    "inflation_hedge": _check_inflation,
    "future_dated_document": _check_future,
    "priced_in_no_trade": _check_priced,
    "release_matches_forecast": _check_matches,
    "release_already_priced": _check_already,
}
