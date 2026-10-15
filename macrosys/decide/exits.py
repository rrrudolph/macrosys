"""Exit priority for an open thesis (spec §11.6).

First match wins: invalidation, time stop, portfolio risk limit, gap closed. Otherwise hold.
The caller passes the facts. Nothing here fetches data or sizes a position.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

ExitAction = Literal["exit", "hold"]
ExitTrigger = Literal["invalidation", "time_stop", "risk_limit", "gap_closed"]


class ExitDecision(BaseModel):
    """Frozen monitor result (spec §11.6).

    ``action`` is ``exit`` or ``hold``. ``trigger`` names the first match and is null on a hold.
    ``reason`` is one sentence for the ledger.
    """

    model_config = ConfigDict(frozen=True)

    action: ExitAction
    trigger: ExitTrigger | None
    reason: str

    @model_validator(mode="after")
    def _paired(self) -> Self:
        if self.action == "exit" and self.trigger is None:
            raise ValueError("an exit needs a trigger")
        if self.action == "hold" and self.trigger is not None:
            raise ValueError("a hold has no trigger")
        if not self.reason.strip():
            raise ValueError("reason must be a sentence")
        return self


def exit_priority(
    *,
    fired_observables: Sequence[str],
    today: date,
    time_stop: date,
    risk_breach: bool,
    gap_closed: bool,
) -> ExitDecision:
    """Return the first matching exit, or hold, from facts the caller already has (spec §11.6).

    Priority is invalidation, then time stop, then a portfolio risk-limit breach, then a closed gap.
    ``fired_observables`` are ``InvalidationRule.observable`` names whose conditions have already
    fired; a downgrade rule is left out of that sequence because it is not an exit, and this function
    does not evaluate conditions or read a store. ``time_stop`` is ``Invalidation.time_stop`` and is
    reached on that date, inclusive. ``risk_breach`` is a portfolio limit, separate from the thesis.
    ``gap_closed`` means the thesis worked. Price stops stay portfolio risk controls and are never an
    exit trigger. The function does not size a position.
    """
    if not isinstance(risk_breach, bool) or not isinstance(gap_closed, bool):
        raise TypeError("risk_breach and gap_closed must be bool")
    names = _observable_names(fired_observables)
    if names:
        noun = "observable" if len(names) == 1 else "observables"
        verb = "has" if len(names) == 1 else "have"
        return ExitDecision(
            action="exit",
            trigger="invalidation",
            reason=f"Invalidation {noun} {_name_list(names)} {verb} fired, so the thesis is wrong.",
        )
    # [J] The stop date itself counts as reached: the thesis is late on that date, not the day after.
    if today >= time_stop:
        return ExitDecision(
            action="exit",
            trigger="time_stop",
            reason=(
                f"The time stop {time_stop.isoformat()} has been reached as of {today.isoformat()}, "
                "so the thesis is late."
            ),
        )
    if risk_breach:
        return ExitDecision(
            action="exit",
            trigger="risk_limit",
            reason=(
                "A portfolio risk limit has been breached, which is a portfolio fact "
                "and not a verdict on the thesis."
            ),
        )
    if gap_closed:
        return ExitDecision(
            action="exit",
            trigger="gap_closed",
            reason="The gap has closed, so the thesis worked.",
        )
    return ExitDecision(
        action="hold",
        trigger=None,
        reason="No exit trigger has fired, so the position is held.",
    )


def _observable_names(fired_observables: Sequence[str]) -> list[str]:
    if isinstance(fired_observables, (str, bytes)) or not isinstance(fired_observables, Sequence):
        raise TypeError("fired_observables must be a sequence of observable names")
    names: list[str] = []
    seen: set[str] = set()
    for item in fired_observables:
        if not isinstance(item, str):
            raise TypeError("each fired observable must be a string")
        name = item.strip()
        if not name:
            raise ValueError("each fired observable must be a non-empty string")
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _name_list(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + ", and " + names[-1]
