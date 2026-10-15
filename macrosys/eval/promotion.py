"""Research-to-paper bar (spec §10.3, ticket E4.3).

The mandate holds the thresholds. This function only applies them to counts the caller already
computed. It does not replay cases, read a store, or start the paper clock. Zero evidential
outcomes means the bar is not evaluable: formula-direction hits are not a pass.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ResearchCounts(BaseModel):
    """Evidential sign counts for one case-library run (spec §10.3).

    An evidential outcome used the recorded shock and library parameters. An author-supplied
    input sign or a replay fixture is not in these counts. ``n_cases_naming_competing`` is the
    number of cases that named a dominant competing effect. ``n_cases_competing_listed`` is how
    many of those found that effect on the mechanism.
    """

    model_config = ConfigDict(frozen=True)

    n_scored: int = Field(ge=0)
    n_evidential: int = Field(ge=0)
    n_evidential_correct: int = Field(ge=0)
    n_cases_with_evidential: int = Field(ge=0)
    held_out_evidential: int = Field(ge=0)
    held_out_evidential_correct: int = Field(ge=0)
    n_cases_naming_competing: int = Field(ge=0)
    n_cases_competing_listed: int = Field(ge=0)


class ResearchGateResult(BaseModel):
    """Whether research may exit to paper (spec §10.3).

    ``evaluable`` is false when no evidential outcome exists. ``met`` is false in that case too.
    ``reasons`` is empty only when the bar is met.
    """

    model_config = ConfigDict(frozen=True)

    evaluable: bool
    met: bool
    reasons: tuple[str, ...]


def research_to_paper(
    *,
    min_cases_scored: int,
    min_sign_accuracy: float | None,
    counts: ResearchCounts,
) -> ResearchGateResult:
    """Apply the mandate's research-to-paper thresholds (spec §10.3).

    ``min_cases_scored`` and ``min_sign_accuracy`` are ``promotion_gates.research_to_paper`` in
    the mandate file. A null accuracy checks the case count and the competing-effect rule only.
    Held-out and overall evidential agreement both have to clear ``min_sign_accuracy``. The
    competing-effect rule is "most": strictly more than half of the cases that named one.
    """
    if min_cases_scored < 0:
        raise ValueError("min_cases_scored cannot be negative")
    if min_sign_accuracy is not None and not 0.0 <= min_sign_accuracy <= 1.0:
        raise ValueError("min_sign_accuracy must be between 0 and 1")
    if counts.n_evidential == 0:
        return ResearchGateResult(
            evaluable=False,
            met=False,
            reasons=(
                f"0 of {counts.n_scored} scored outcomes are evidential; every sign came from an "
                "author-supplied input sign or a replay fixture, so this run checks formula direction only",
            ),
        )
    reasons: list[str] = []
    if counts.n_cases_with_evidential < min_cases_scored:
        reasons.append(
            f"{counts.n_cases_with_evidential} cases produced an evidential sign, the bar is {min_cases_scored}"
        )
    if min_sign_accuracy is not None:
        if counts.held_out_evidential == 0:
            reasons.append("held-out split produced no evidential signs")
        elif counts.held_out_evidential_correct / counts.held_out_evidential < min_sign_accuracy:
            reasons.append(
                "held-out evidential agreement "
                f"{_pct(counts.held_out_evidential_correct, counts.held_out_evidential)} "
                f"is under {min_sign_accuracy:.0%}"
            )
        if counts.n_evidential_correct / counts.n_evidential < min_sign_accuracy:
            reasons.append(
                "overall evidential agreement "
                f"{_pct(counts.n_evidential_correct, counts.n_evidential)} "
                f"is under {min_sign_accuracy:.0%}"
            )
    if counts.n_cases_naming_competing and counts.n_cases_competing_listed * 2 <= counts.n_cases_naming_competing:
        reasons.append(
            "dominant competing effect is on the mechanism in "
            f"{counts.n_cases_competing_listed} of {counts.n_cases_naming_competing} cases; the bar is most"
        )
    return ResearchGateResult(evaluable=True, met=not reasons, reasons=tuple(reasons))


def _pct(numerator: int, denominator: int) -> str:
    return "n/a" if denominator == 0 else f"{numerator / denominator:.0%}"
