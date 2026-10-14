"""Position sizing (spec §11.4).

Size is the minimum of three constraints, and a fractional-Kelly figure is only a ceiling. Volatility,
gap, and scenario loss are plain floats in annualized portfolio-weight units: 0.10 is 10% of NAV.
The mandate supplies the numeric limits; this module does not read the YAML itself.

Correlated theses — a shared mechanism id, or a shared factor loading at or above the threshold — are
grouped so the caller sizes the group once instead of adding the positions up.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from macrosys.decide.thesis import EvidenceQuality, Thesis

Binding = Literal["vol_parity", "scenario_budget", "position_cap", "factor_cap", "kelly_ceiling"]

_QUALITY = {"low": 0.0, "medium": 0.5, "high": 1.0, "n/a": None}
_BINDING_ORDER: dict[str, int] = {
    "vol_parity": 0,
    "scenario_budget": 1,
    "position_cap": 2,
    "factor_cap": 3,
}


class SizedPosition(BaseModel):
    """The weight that survives every cap (spec §11.4). ``weight`` is a fraction of NAV."""

    model_config = ConfigDict(frozen=True)

    weight: float = Field(ge=0.0)
    vol_parity_scaled: float
    scenario_cap: float
    position_cap: float
    factor_cap: float
    kelly_ceiling: float
    binding: Binding


def vol_parity_weight(vol_annual: float, target_vol: float) -> float:
    """Weight that brings an asset's volatility to the portfolio target: ``target_vol / vol_annual``.

    Both arguments are annualized fractions of NAV (spec §11.4). A 20% asset against a 10% target
    gets weight 0.5 before the conviction multiplier and the caps.
    """
    if vol_annual <= 0:
        raise ValueError("vol_annual must be positive")
    if target_vol < 0:
        raise ValueError("target_vol cannot be negative")
    return target_vol / vol_annual


def conviction_multiplier(
    gap_low: float,
    gap_high: float,
    evidence_quality: EvidenceQuality,
    bounds: tuple[float, float] = (0.5, 1.5),
) -> float:
    """Map the gap range and evidence labels into the mandate's conviction band (spec §11.4).

    The band defaults to the example mandate's ``[0.5, 1.5]``. Sharpness is the conservative edge
    divided by that edge plus the width, so a wide range or a range near zero stays near the floor.
    Evidence quality is the mean of the low/medium/high labels, ignoring ``n/a``. The multiplier is
    ``low + (high - low) * sharpness * quality``, then clipped to the band. A gap that straddles zero
    has sharpness 0 and sits on the floor. This formula is a [J]: the spec requires a bounded
    multiplier derived from the gap and the labels, and does not prescribe the arithmetic.
    """
    lo_b, hi_b = bounds
    if not 0 < lo_b <= hi_b:
        raise ValueError("conviction bounds must satisfy 0 < low <= high")
    if gap_low > gap_high:
        raise ValueError("gap_low cannot exceed gap_high")
    if gap_low <= 0.0 <= gap_high:
        sharpness = 0.0
    else:
        width = gap_high - gap_low
        conservative = min(abs(gap_low), abs(gap_high))
        sharpness = conservative / (conservative + width) if conservative + width > 0 else 0.0
    scores = [
        _QUALITY[label]
        for label in (evidence_quality.observations, evidence_quality.claims, evidence_quality.inferences)
        if _QUALITY[label] is not None
    ]
    quality = sum(scores) / len(scores) if scores else 0.0
    raw = lo_b + (hi_b - lo_b) * sharpness * quality
    return float(min(hi_b, max(lo_b, raw)))


def scenario_budget_cap(marginal_scenario_loss: float, remaining_budget: float) -> float:
    """Largest weight whose scenario loss stays inside the remaining budget (spec §11.4, §6.5).

    ``marginal_scenario_loss`` is the loss fraction of NAV at weight 1 (a positive number).
    ``remaining_budget`` is the loss fraction still available. A non-positive loss does not constrain
    the weight and returns infinity. An exhausted budget returns 0.
    """
    if marginal_scenario_loss < 0:
        raise ValueError("marginal_scenario_loss is a loss magnitude and cannot be negative")
    if remaining_budget < 0:
        raise ValueError("remaining_budget cannot be negative")
    if marginal_scenario_loss == 0:
        return math.inf
    if remaining_budget == 0:
        return 0.0
    return remaining_budget / marginal_scenario_loss


def fractional_kelly_ceiling(gap_lower_edge_annual: float, vol_annual: float, fraction: float) -> float:
    """Sanity ceiling ``fraction * edge / vol^2`` from the gap's lower edge (spec §11.4).

    ``gap_lower_edge_annual`` is an annualized expected excess return (a fraction, not a price change
    in basis points). ``fraction`` is the mandate's fractional-Kelly coefficient (0.25 on the example
    file). A non-positive edge ceilings the weight at 0. This is never the target; ``size_position``
    takes the minimum of the other constraints and then this ceiling.
    """
    if vol_annual <= 0:
        raise ValueError("vol_annual must be positive")
    if fraction <= 0:
        raise ValueError("fraction must be positive")
    if gap_lower_edge_annual <= 0:
        return 0.0
    return fraction * gap_lower_edge_annual / (vol_annual**2)


def size_position(
    *,
    vol_annual: float,
    target_vol: float,
    gap_low: float,
    gap_high: float,
    evidence_quality: EvidenceQuality,
    marginal_scenario_loss: float,
    remaining_budget: float,
    position_cap: float,
    factor_cap: float,
    gap_lower_edge_annual: float,
    kelly_fraction: float,
    conviction_bounds: tuple[float, float] = (0.5, 1.5),
) -> SizedPosition:
    """Combine the spec §11.4 constraints. The returned weight is a fraction of NAV.

    The three sizing inputs are the conviction-scaled volatility-parity weight, the scenario-budget
    cap, and the tighter of the position and factor caps. The fractional-Kelly ceiling then cuts the
    result further when it is smaller; it does not raise it. ``factor_cap`` is the caller's remaining
    room on the tightest factor (budget divided by the absolute loading), already in weight units.
    """
    if position_cap < 0 or factor_cap < 0:
        raise ValueError("position and factor caps cannot be negative")
    scaled = vol_parity_weight(vol_annual, target_vol) * conviction_multiplier(
        gap_low, gap_high, evidence_quality, conviction_bounds
    )
    scenario = scenario_budget_cap(marginal_scenario_loss, remaining_budget)
    kelly = fractional_kelly_ceiling(gap_lower_edge_annual, vol_annual, kelly_fraction)
    candidates: dict[str, float] = {
        "vol_parity": scaled,
        "scenario_budget": scenario,
        "position_cap": position_cap,
        "factor_cap": factor_cap,
    }
    binding_name = min(candidates, key=lambda name: (candidates[name], _BINDING_ORDER[name]))
    raw = candidates[binding_name]
    if kelly < raw:
        weight = max(0.0, kelly)
        binding: Binding = "kelly_ceiling"
    else:
        weight = max(0.0, raw)
        binding = binding_name  # type: ignore[assignment]
    return SizedPosition(
        weight=weight,
        vol_parity_scaled=scaled,
        scenario_cap=scenario,
        position_cap=position_cap,
        factor_cap=factor_cap,
        kelly_ceiling=kelly,
        binding=binding,
    )


def group_correlated(theses: list[Thesis], shared_factor_threshold: float) -> list[list[Thesis]]:
    """Group theses that must be sized as one position budget (spec §11.4).

    Two theses are linked when their ``mechanism_path`` lists share an id, or when both have an
    absolute factor loading at or above ``shared_factor_threshold`` on the same factor. Groups are
    sorted by thesis id. The caller runs ``size_position`` once per group and splits that single
    weight across the group's expressions; adding a fresh size per thesis would double-count the bet.
    """
    if shared_factor_threshold < 0:
        raise ValueError("shared_factor_threshold cannot be negative")
    parent = list(range(len(theses)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left == root_right:
            return
        if theses[root_left].thesis_id <= theses[root_right].thesis_id:
            parent[root_right] = root_left
        else:
            parent[root_left] = root_right

    for i in range(len(theses)):
        for j in range(i + 1, len(theses)):
            if _correlated(theses[i], theses[j], shared_factor_threshold):
                union(i, j)
    buckets: dict[int, list[Thesis]] = {}
    for index, thesis in enumerate(theses):
        buckets.setdefault(find(index), []).append(thesis)
    groups = [sorted(members, key=lambda thesis: thesis.thesis_id) for members in buckets.values()]
    groups.sort(key=lambda group: group[0].thesis_id)
    return groups


def _correlated(left: Thesis, right: Thesis, threshold: float) -> bool:
    if set(left.mechanism_path) & set(right.mechanism_path):
        return True
    left_factors = left.sizing_inputs.factor_loadings
    right_factors = right.sizing_inputs.factor_loadings
    for name in left_factors.keys() & right_factors.keys():
        if abs(left_factors[name]) >= threshold and abs(right_factors[name]) >= threshold:
            return True
    return False
