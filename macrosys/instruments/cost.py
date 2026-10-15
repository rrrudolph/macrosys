"""Round-trip implementation cost in basis points (spec §7.5)."""

from __future__ import annotations

import math


def round_trip_cost_bp(
    half_spread_bp: float,
    expense_ratio_annual: float,
    holding_months: float,
    roll_cost_annual_bp: float = 0,
    hedge_cost_annual_bp: float = 0,
    impact_bp: float = 0,
    borrow_cost_annual_bp: float = 0,
) -> float:
    """Round-trip cost in basis points over a holding period (spec §7.5).

    ``2 * half_spread_bp + impact_bp + expense_ratio_annual * years * 10000 + (roll + hedge + borrow) * years``,
    with ``years = holding_months / 12``. ``expense_ratio_annual`` is an annual NAV share (0.0009
    is 9 bp per year), not a percent. Spread is paid on the way in and the way out. Impact is a
    one-shot basis-point cost. Roll, hedge, and borrow costs are annual basis points scaled by the
    holding period (futures roll, the forward premium plus basis on a hedged share class, and the
    stock-loan fee on an outright short — ``Mandate.short_borrow_cost_bp_annual``, amended
    2026-10-15). Negative inputs raise ``ValueError``.
    """
    named = {
        "half_spread_bp": half_spread_bp,
        "expense_ratio_annual": expense_ratio_annual,
        "holding_months": holding_months,
        "roll_cost_annual_bp": roll_cost_annual_bp,
        "hedge_cost_annual_bp": hedge_cost_annual_bp,
        "impact_bp": impact_bp,
        "borrow_cost_annual_bp": borrow_cost_annual_bp,
    }
    for name, value in named.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{name} must be a non-negative number, got {value!r}")
        if not math.isfinite(float(value)) or float(value) < 0.0:
            raise ValueError(f"{name} must be a non-negative number, got {value!r}")
    years = float(holding_months) / 12.0
    return float(
        2.0 * float(half_spread_bp)
        + float(impact_bp)
        + float(expense_ratio_annual) * years * 10_000.0
        + (float(roll_cost_annual_bp) + float(hedge_cost_annual_bp) + float(borrow_cost_annual_bp)) * years
    )
