"""Exit priority: first match in spec order, otherwise hold (spec §11.6)."""

from __future__ import annotations

import inspect
from datetime import date

import pytest
from pydantic import ValidationError

from macrosys.decide.exits import ExitDecision, exit_priority
from macrosys.decide.thesis import Invalidation, InvalidationRule

TODAY = date(2026, 10, 14)
STOP = date(2027, 4, 14)


def _exit(**overrides: object) -> ExitDecision:
    args: dict[str, object] = {
        "fired_observables": (),
        "today": TODAY,
        "time_stop": STOP,
        "risk_breach": False,
        "gap_closed": False,
    }
    args.update(overrides)
    return exit_priority(**args)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("fired", "today", "time_stop", "risk_breach", "gap_closed", "action", "trigger", "reason"),
    [
        pytest.param(
            ("exchange stocks",),
            date(2026, 10, 1),
            date(2026, 10, 14),
            True,
            True,
            "exit",
            "invalidation",
            "Invalidation observable exchange stocks has fired, so the thesis is wrong.",
            id="invalidation-first",
        ),
        pytest.param(
            (),
            date(2026, 10, 14),
            date(2026, 10, 14),
            True,
            True,
            "exit",
            "time_stop",
            "The time stop 2026-10-14 has been reached as of 2026-10-14, so the thesis is late.",
            id="time-stop-on-the-date",
        ),
        pytest.param(
            (),
            date(2026, 10, 15),
            date(2026, 10, 14),
            True,
            True,
            "exit",
            "time_stop",
            "The time stop 2026-10-14 has been reached as of 2026-10-15, so the thesis is late.",
            id="time-stop-after-the-date",
        ),
        pytest.param(
            (),
            date(2026, 10, 13),
            date(2026, 10, 14),
            True,
            True,
            "exit",
            "risk_limit",
            "A portfolio risk limit has been breached, which is a portfolio fact and not a verdict on the thesis.",
            id="risk-limit-before-time-stop",
        ),
        pytest.param(
            (),
            date(2026, 10, 13),
            date(2026, 10, 14),
            False,
            True,
            "exit",
            "gap_closed",
            "The gap has closed, so the thesis worked.",
            id="gap-closed-last",
        ),
        pytest.param(
            (),
            date(2026, 10, 13),
            date(2026, 10, 14),
            False,
            False,
            "hold",
            None,
            "No exit trigger has fired, so the position is held.",
            id="hold",
        ),
    ],
)
def test_first_match_wins(
    fired: tuple[str, ...],
    today: date,
    time_stop: date,
    risk_breach: bool,
    gap_closed: bool,
    action: str,
    trigger: str | None,
    reason: str,
) -> None:
    decision = exit_priority(
        fired_observables=fired,
        today=today,
        time_stop=time_stop,
        risk_breach=risk_breach,
        gap_closed=gap_closed,
    )
    assert decision.action == action
    assert decision.trigger == trigger
    assert decision.reason == reason


def test_several_fired_observables_are_named_once() -> None:
    decision = _exit(fired_observables=("exchange stocks", "exchange stocks", "utilization proxy"))
    assert decision.action == "exit"
    assert decision.trigger == "invalidation"
    assert decision.reason == (
        "Invalidation observables exchange stocks and utilization proxy have fired, so the thesis is wrong."
    )


def test_facts_can_come_from_invalidation_without_a_thesis() -> None:
    record = Invalidation(
        rules=[
            InvalidationRule(observable="exchange stocks", condition="stocks rise", action="exit"),
            InvalidationRule(observable="utilization proxy", condition="falls", action="downgrade"),
        ],
        time_stop=STOP,
    )
    # The caller reports only the exit-action observable. A downgrade is not an exit reason.
    decision = exit_priority(
        fired_observables=(record.rules[0].observable,),
        today=TODAY,
        time_stop=record.time_stop,
        risk_breach=False,
        gap_closed=False,
    )
    assert decision.trigger == "invalidation"
    assert "exchange stocks" in decision.reason
    assert "utilization proxy" not in decision.reason


def test_price_stop_is_not_an_exit_reason() -> None:
    """Price stops stay portfolio risk controls and are never a trigger (spec §11.6)."""
    params = set(inspect.signature(exit_priority).parameters)
    assert not any("price" in name for name in params)
    decision = _exit()
    assert decision.action == "hold"
    assert decision.trigger is None


def test_a_single_string_is_not_a_sequence_of_names() -> None:
    with pytest.raises(TypeError, match="sequence of observable names"):
        _exit(fired_observables="exchange stocks")


def test_blank_observable_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _exit(fired_observables=("  ",))


def test_result_is_frozen() -> None:
    decision = _exit()
    with pytest.raises(ValidationError):
        decision.action = "exit"  # type: ignore[misc]


def test_exit_without_a_trigger_is_rejected() -> None:
    with pytest.raises(ValidationError, match="needs a trigger"):
        ExitDecision(action="exit", trigger=None, reason="Missing trigger.")
