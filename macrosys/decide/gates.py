"""Opportunity gates and the no-trade default (spec §11.3).

A thesis becomes a trade only when the mechanism, gap, expression, and risk gates all pass. Otherwise the
decision is ``no_trade`` and the first failing gate is recorded. These functions do not size, do not
fetch data, and do not read a portfolio; the caller passes the propagation and risk facts they already have.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from macrosys.core.quantity import Quantity
from macrosys.core.units import compatible, convert
from macrosys.decide.thesis import Thesis

GateName = Literal["mechanism", "gap", "expression", "risk"]


class GateResult(BaseModel):
    """One gate's verdict (spec §11.3). ``passed`` is false whenever the thesis must not trade on this gate."""

    model_config = ConfigDict(frozen=True)

    passed: bool
    gate: GateName
    reason: str


class Decision(BaseModel):
    """The weekly answer for one thesis. The default action is ``no_trade``."""

    model_config = ConfigDict(frozen=True)

    action: Literal["trade", "no_trade"] = "no_trade"
    thesis_id: str
    failing_gate: GateName | None = None
    reason: str = ""
    gates: list[GateResult] = Field(default_factory=list)


def mechanism_gate(thesis: Thesis, run_valid: bool, identity_residuals_ok: bool) -> GateResult:
    """Pass only when the propagation is usable and nothing blocking is missing (spec §11.3).

    ``run_valid`` covers units, competing-effect closure, and a finished run. ``identity_residuals_ok``
    covers the accounting identities on that run. Either false, or any ``missing_evidence`` item with
    ``blocking=True``, fails the gate.
    """
    blocking = [item.description for item in thesis.missing_evidence if item.blocking]
    if blocking:
        return GateResult(
            passed=False,
            gate="mechanism",
            reason="blocking missing evidence: " + "; ".join(blocking),
        )
    if not run_valid:
        return GateResult(passed=False, gate="mechanism", reason="propagation run is not valid")
    if not identity_residuals_ok:
        return GateResult(passed=False, gate="mechanism", reason="identity residuals are outside tolerance")
    return GateResult(passed=True, gate="mechanism", reason="propagation run valid and no blocking evidence")


def gap_gate(thesis: Thesis, min_gap_bp: float) -> GateResult:
    """Pass when the gap excludes zero and clears costs by the mandated margin (spec §11.3).

    The comparison is in basis points. Units compatible with ``bp`` (``bp``, ``pp``) convert through
    ``macrosys.core.units``. A ``pct`` gap is treated as 1 pct = 100 bp: the unit vocabulary keeps
    ``pct`` (a level) distinct from ``pp`` (a difference), but a price or return gap quoted in percent
    is the same economic object as a cost quoted in basis points. That scale is a [J] local to this gate.

    For a strictly positive gap the test uses the lower edge. For a strictly negative gap it uses the
    edge closer to zero (the absolute value of the upper edge), which is the conservative reading of
    "clears the cost". A range that straddles zero fails before the cost test.
    """
    if min_gap_bp < 0:
        raise ValueError("min_gap_bp cannot be negative")
    if thesis.gap.straddles_zero():
        return GateResult(passed=False, gate="gap", reason="gap straddles zero")
    try:
        lo_bp, hi_bp = _edges_bp(thesis.gap)
    except ValueError as exc:
        return GateResult(passed=False, gate="gap", reason=str(exc))
    conservative_bp = lo_bp if lo_bp > 0.0 else abs(hi_bp)
    need_bp = thesis.expression.cost_estimate_bp + min_gap_bp
    if conservative_bp < need_bp:
        return GateResult(
            passed=False,
            gate="gap",
            reason=(
                f"conservative gap edge {conservative_bp:.4g} bp is below cost plus margin {need_bp:.4g} bp"
            ),
        )
    return GateResult(
        passed=True,
        gate="gap",
        reason=f"conservative gap edge {conservative_bp:.4g} bp clears cost plus margin {need_bp:.4g} bp",
    )


def expression_gate(thesis: Thesis, exposure_match: bool, horizon_ok: bool) -> GateResult:
    """Pass when the instrument expresses the entity and the horizon fits (spec §11.3, §7.6).

    ``exposure_match`` is the caller's A09 check (holdings concentration and structure). ``horizon_ok``
    is true when the thesis horizon sits inside both the propagation window and the mandate's 1–12 month
    band. The gate also fails when the thesis itself records ``exposure_vector_check`` as ``failed``.
    """
    if thesis.expression.exposure_vector_check == "failed" or not exposure_match:
        return GateResult(passed=False, gate="expression", reason="exposure vector does not match the entity")
    if not horizon_ok:
        return GateResult(passed=False, gate="expression", reason="horizon is outside the path or the mandate")
    if thesis.expression.exposure_vector_check == "pending":
        return GateResult(passed=False, gate="expression", reason="exposure vector check has not been run")
    return GateResult(passed=True, gate="expression", reason="expression matches and the horizon fits")


def risk_gate(post_trade_ok: bool, regime_flag: str) -> GateResult:
    """Pass when the post-trade book is inside its limits and the correlation regime is not deteriorating.

    Spec §11.3 names the regime flag ``deteriorating`` as the failure. Other flags pass this gate; limit
    and scenario-budget breaches are the caller's ``post_trade_ok`` boolean.
    """
    if not post_trade_ok:
        return GateResult(
            passed=False,
            gate="risk",
            reason="post-trade portfolio breaches a limit or the scenario budget",
        )
    if regime_flag == "deteriorating":
        return GateResult(passed=False, gate="risk", reason="correlation regime is deteriorating")
    return GateResult(passed=True, gate="risk", reason="post-trade book is inside limits")


def decide(
    thesis: Thesis,
    *,
    run_valid: bool,
    identity_residuals_ok: bool,
    min_gap_bp: float,
    exposure_match: bool,
    horizon_ok: bool,
    post_trade_ok: bool,
    regime_flag: str,
) -> Decision:
    """Run the four gates in spec order. Trade only if every gate passes; otherwise no-trade.

    The recorded failing gate is the first failure in the order mechanism, gap, expression, risk.
    Later failures are still listed on ``gates`` so the review note can show all of them.
    """
    results = [
        mechanism_gate(thesis, run_valid, identity_residuals_ok),
        gap_gate(thesis, min_gap_bp),
        expression_gate(thesis, exposure_match, horizon_ok),
        risk_gate(post_trade_ok, regime_flag),
    ]
    failed = [result for result in results if not result.passed]
    if not failed:
        return Decision(
            action="trade",
            thesis_id=thesis.thesis_id,
            failing_gate=None,
            reason="all four gates passed",
            gates=results,
        )
    first = failed[0]
    return Decision(
        action="no_trade",
        thesis_id=thesis.thesis_id,
        failing_gate=first.gate,
        reason=first.reason,
        gates=results,
    )


def _edges_bp(gap: Quantity) -> tuple[float, float]:
    lo = gap.value if gap.low is None else gap.low
    hi = gap.value if gap.high is None else gap.high
    if lo is None or hi is None:
        raise ValueError("gap has no usable edge")
    scale = _bp_per_unit(gap.unit)
    return float(lo) * scale, float(hi) * scale


def _bp_per_unit(unit: str) -> float:
    if compatible(unit, "bp"):
        return convert(1.0, unit, "bp")
    if unit == "pct":
        return 100.0
    raise ValueError(f"gap gate needs a bp, pp, or pct gap, got {unit!r}")
