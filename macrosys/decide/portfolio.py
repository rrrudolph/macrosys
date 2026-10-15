"""Deterministic portfolio construction, order state, and reconciliation (spec §11.5, §11.7).

Nothing here talks to a broker. Tickets are a pure function of the theses, the sizes, the current
book, the mandate, and NAV. The same inputs produce the same tickets, including the same client order
id, so a resubmission cannot open a second order.

Position limits written as percents on the mandate are enforced here. Factor, country, and currency
limits need exposure data this package does not own; pass a callable for each limit that is set.
A book that fails a callable is scaled back until it passes, so a returned ticket list cannot breach
a check the caller actually supplied.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from macrosys.decide.mandate import Mandate
from macrosys.decide.thesis import Thesis, ThesisStatus

WeightCheck = Callable[[Mapping[str, float]], bool]


class LimitCheckMissingError(RuntimeError):
    """Raised when the mandate sets a factor, country, or currency limit and no callable was supplied."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        super().__init__(
            "mandate sets limits that the constructor cannot see without a callable (spec §11.3, §11.7): "
            + ", ".join(self.missing)
        )


class IllegalTransitionError(ValueError):
    """Raised when an order is moved along an edge the spec §11.7 diagram does not have."""


class OrderState(StrEnum):
    """Order lifecycle (spec §11.7). Terminal states are Filled, Rejected, and Cancelled."""

    CREATED = "Created"
    SUBMITTED = "Submitted"
    PARTIALLY_FILLED = "PartiallyFilled"
    WORKING = "Working"
    FILLED = "Filled"
    REJECTED = "Rejected"
    CANCELLED = "Cancelled"
    UNKNOWN = "Unknown"


# [J] Submitted may go straight to Working (resting, no fill yet) or Filled (complete fill).
# The diagram draws those as the PartiallyFilled loop; skipping the partial state matches a full fill.
_ALLOWED: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.SUBMITTED}),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.WORKING,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset({OrderState.WORKING, OrderState.FILLED, OrderState.CANCELLED}),
    OrderState.WORKING: frozenset({OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED}),
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.SUBMITTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.WORKING,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.CANCELLED: frozenset(),
}


class OrderStateMachine:
    """Mutable state of one order. Illegal edges raise ``IllegalTransitionError`` (spec §11.7)."""

    def __init__(self, state: OrderState = OrderState.CREATED) -> None:
        self.state = state

    def transition(self, to: OrderState) -> OrderState:
        """Move to ``to`` if the diagram allows it from the current state."""
        dest = to if isinstance(to, OrderState) else OrderState(to)
        if dest not in _ALLOWED[self.state]:
            raise IllegalTransitionError(f"cannot transition {self.state.value} -> {dest.value}")
        self.state = dest
        return self.state

    def resolve(self, observed: OrderState) -> OrderState:
        """Leave Unknown by adopting the broker's view of this client order id (spec §11.7)."""
        if self.state is not OrderState.UNKNOWN:
            raise IllegalTransitionError(f"resolve() is only valid from Unknown, not {self.state.value}")
        return self.transition(observed)


class OrderTicket(BaseModel):
    """One order the OMS may submit (spec §11.7). ``quantity`` is unsigned notional in the base currency.

    ``client_order_id`` is ``sha256(run_id|instrument|side|target_qty)[:24]`` with ``target_qty`` the
    order notional formatted to cents. Rebuilding the ticket from the same inputs repeats the id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str = Field(min_length=1)
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0.0)
    order_type: Literal["MOC", "LMT"] = "MOC"
    limit: float | None = None
    client_order_id: str = Field(min_length=24, max_length=24)
    run_id: str = Field(min_length=1)
    thesis_ids: list[str]

    @model_validator(mode="after")
    def _limit_matches_type(self) -> OrderTicket:
        if self.order_type == "LMT" and self.limit is None:
            raise ValueError("LMT orders require a limit")
        if self.order_type == "MOC" and self.limit is not None:
            raise ValueError("MOC orders do not carry a limit")
        return self


class ReconciliationBreak(BaseModel):
    """One ledger-versus-broker difference above tolerance (spec §11.7)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["position", "cash"]
    key: str
    ledger: float
    broker: float
    difference: float


class ReconciliationReport(BaseModel):
    """Daily compare result. Any break is unexplained at compare time and is class A (spec §11.7)."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    class_a: bool
    breaks: list[ReconciliationBreak]


class Reconciler:
    """Compare ledger and broker positions and cash (spec §11.7).

    This function has no memory of yesterday's break, so every difference above ``tol`` is unexplained
    and raises the class-A flag. A later human resolution lives outside this compare.
    """

    @staticmethod
    def compare(
        ledger_positions: Mapping[str, float],
        broker_positions: Mapping[str, float],
        ledger_cash: Mapping[str, float],
        broker_cash: Mapping[str, float],
        tol: float,
    ) -> ReconciliationReport:
        """Return the breaks whose absolute difference exceeds ``tol``. Missing keys are treated as 0."""
        if tol < 0:
            raise ValueError("tol cannot be negative")
        breaks = _breaks("position", ledger_positions, broker_positions, tol)
        breaks.extend(_breaks("cash", ledger_cash, broker_cash, tol))
        breaks.sort(key=lambda item: (item.kind, item.key))
        unexplained = len(breaks) > 0
        return ReconciliationReport(ok=not unexplained, class_a=unexplained, breaks=breaks)


class PortfolioConstructor:
    """Build the week's tickets (spec §11.3 no-trade default, §11.4 sizes, §11.5 hedges, §11.7 ids).

    ``sizes`` maps ``thesis_id`` to a fraction of NAV (a float or any object with a ``weight``
    attribute). Only theses in ``approved`` or ``live`` are expressed. Contributions sum across
    theses naming the same instrument. A ``long`` thesis contributes ``+weight``; a ``short`` or
    ``underweight`` thesis contributes ``-weight``. A pair puts ``+weight`` on the first leg and
    ``-hedge_ratio * weight`` on the second (``hedge_ratio`` defaults to 1).

    Without ``baseline`` a contribution is the instrument's absolute target weight, and instruments
    no thesis names are left at their current weight. With ``baseline`` — the resting book (spec
    §11.3, amended 2026-10-15) — every instrument's target starts at its baseline weight (zero when
    absent) and a contribution is a tilt added to it, so a week with no passing thesis trades toward
    the baseline and a thesis is the active bet on top of it.

    With shorting disallowed, targets are floored at 0. A pair whose hedge would be negative and
    whose hedge leg is already flat is skipped entirely, so the long leg is not put on unhedged.
    With shorting allowed, targets may be negative; when the mandate also disallows leverage, gross
    exposure (the sum of absolute weights) is held at or under 100% of NAV by scaling the tilts back.

    ``quantity`` on each ticket is the unsigned change in notional, ``abs(target - current) * nav``,
    rounded to cents. Changes smaller than ``min_notional`` are skipped.
    """

    def __init__(
        self,
        run_id: str,
        *,
        min_notional: float = 0.0,
        position_ok: WeightCheck | None = None,
        factor_ok: WeightCheck | None = None,
        country_ok: WeightCheck | None = None,
        currency_ok: WeightCheck | None = None,
    ) -> None:
        if not run_id:
            raise ValueError("run_id is required for idempotent tickets")
        if min_notional < 0:
            raise ValueError("min_notional cannot be negative")
        self.run_id = run_id
        self.min_notional = min_notional
        self.position_ok = position_ok
        self.factor_ok = factor_ok
        self.country_ok = country_ok
        self.currency_ok = currency_ok

    def propose(
        self,
        theses: list[Thesis],
        sizes: Mapping[str, object],
        current: Mapping[str, float],
        mandate: Mandate,
        nav: float,
        baseline: Mapping[str, float] | None = None,
    ) -> list[OrderTicket]:
        """Return tickets sorted by instrument, side, and client order id. Same inputs, same list."""
        if nav <= 0:
            raise ValueError("nav must be positive")
        self._require_wired_limits(mandate)
        allow_short = mandate.shorting.allowed
        cap = None if mandate.risk.position_limit_pct is None else mandate.risk.position_limit_pct / 100.0
        checks = self._checks(mandate)

        if baseline is not None and any(weight < 0.0 for weight in baseline.values()):
            raise ValueError("baseline weights are long-only shares of NAV")
        tilts, contributors = _target_weights(theses, sizes, current, allow_short=allow_short)
        names = sorted(set(current) | set(tilts) | set(baseline or {}))
        cur = {name: float(current.get(name, 0.0)) for name in names}
        if baseline is None:
            target = {name: float(tilts.get(name, cur[name])) for name in names}
        else:
            rest = {name: float(baseline.get(name, 0.0)) for name in names}
            target = _fund_tilts(rest, {name: float(tilts.get(name, 0.0)) for name in names}, mandate)
        if not allow_short:
            target = {name: max(0.0, weight) for name, weight in target.items()}
        if cap is not None:
            target = {name: _clip(weight, cap) for name, weight in target.items()}
            for name in names:
                if cur[name] > cap:
                    target[name] = min(target[name], cap)
                elif cur[name] < -cap:
                    target[name] = max(target[name], -cap)

        final = _enforce_callables(cur, target, checks)
        if cap is not None:
            final = {name: _clip(weight, cap) for name, weight in final.items()}
        if not allow_short:
            final = {name: max(0.0, weight) for name, weight in final.items()}
        if not _passes(final, checks):
            raise ValueError("limit checks reject every scaled book, including a flat portfolio")

        tickets: list[OrderTicket] = []
        for instrument in sorted(final):
            delta = final[instrument] - cur.get(instrument, 0.0)
            notional = delta * nav
            if abs(notional) < self.min_notional:
                continue
            quantity = _cents(abs(notional))
            if quantity <= 0:
                continue
            side: Literal["buy", "sell"] = "buy" if notional > 0 else "sell"
            tickets.append(
                OrderTicket(
                    instrument_id=instrument,
                    side=side,
                    quantity=quantity,
                    order_type="MOC",
                    limit=None,
                    client_order_id=client_order_id(self.run_id, instrument, side, quantity),
                    run_id=self.run_id,
                    thesis_ids=sorted(set(contributors.get(instrument, []))),
                )
            )
        tickets.sort(key=lambda ticket: (ticket.instrument_id, ticket.side, ticket.client_order_id))
        return tickets

    def _checks(self, mandate: Mandate) -> list[WeightCheck]:
        checks = [
            check for check in (self.position_ok, self.factor_ok, self.country_ok, self.currency_ok) if check
        ]
        if not mandate.leverage.allowed:
            checks.append(_gross_within_nav)
        return checks

    def _require_wired_limits(self, mandate: Mandate) -> None:
        missing: list[str] = []
        if mandate.risk.factor_limit_pct is not None and self.factor_ok is None:
            missing.append("factor")
        if mandate.risk.country_limit_pct is not None and self.country_ok is None:
            missing.append("country")
        if mandate.risk.currency_net_limit_pct is not None and self.currency_ok is None:
            missing.append("currency")
        if missing:
            raise LimitCheckMissingError(missing)


def client_order_id(run_id: str, instrument: str, side: str, target_qty: float) -> str:
    """First 24 hex characters of ``sha256(run_id|instrument|side|target_qty)`` (spec §11.7).

    ``target_qty`` is formatted to cents so the same economic quantity always hashes the same way.
    """
    payload = f"{run_id}|{instrument}|{side}|{format_target_qty(target_qty)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def format_target_qty(target_qty: float) -> str:
    """Canonical cents string used inside the idempotency hash."""
    return f"{_cents(target_qty):.2f}"


def _cents(amount: float) -> float:
    quantized = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(quantized)


def _clip(weight: float, cap: float) -> float:
    return min(max(weight, -cap), cap)


def _gross_within_nav(weights: Mapping[str, float]) -> bool:
    """No leverage: longs plus shorts may not exceed NAV (spec §11.1, amended 2026-10-15)."""
    return sum(abs(weight) for weight in weights.values()) <= 1.0 + 1e-9


def _fund_tilts(rest: dict[str, float], tilts: dict[str, float], mandate: Mandate) -> dict[str, float]:
    """Baseline plus tilts; without leverage the baseline is trimmed pro rata to make room.

    A fully invested resting book has no cash to fund a long tilt and no margin for a short, so the
    tilt is paid for by scaling the baseline down by the largest factor in [0, 1] that keeps gross
    exposure at or under NAV. Tilts are kept whole here; if the tilts alone exceed NAV the callable
    checks in ``propose`` scale them. With leverage allowed the two are simply added.
    """

    def book(scale: float) -> dict[str, float]:
        return {name: scale * rest[name] + tilts[name] for name in rest}

    if mandate.leverage.allowed or _gross_within_nav(book(1.0)):
        return book(1.0)
    if not _gross_within_nav(book(0.0)):
        return book(0.0)
    return book(_largest_scale(book, [_gross_within_nav]))


def _as_weight(sizes: Mapping[str, object], thesis_id: str) -> float:
    if thesis_id not in sizes:
        return 0.0
    value = sizes[thesis_id]
    weight = float(value.weight) if hasattr(value, "weight") else float(value)  # type: ignore[arg-type]
    if weight < 0:
        raise ValueError("sizes are unsigned fractions of NAV; direction lives on the thesis")
    return weight


def _target_weights(
    theses: list[Thesis],
    sizes: Mapping[str, object],
    current: Mapping[str, float],
    *,
    allow_short: bool,
) -> tuple[dict[str, float], dict[str, list[str]]]:
    desired: dict[str, float] = {}
    contributors: dict[str, list[str]] = {}
    for thesis in sorted(theses, key=lambda item: item.thesis_id):
        if thesis.status not in (ThesisStatus.approved, ThesisStatus.live):
            continue
        weight = _as_weight(sizes, thesis.thesis_id)
        if weight == 0:
            continue
        expression = thesis.expression
        if expression.pair is not None:
            long_leg, hedge_leg = expression.pair
            ratio = expression_hedge_ratio(thesis)
            hedge_weight = -ratio * weight
            if not allow_short and hedge_weight < 0 and float(current.get(hedge_leg, 0.0)) <= 0:
                continue
            _add(desired, contributors, long_leg, weight, thesis.thesis_id)
            _add(desired, contributors, hedge_leg, hedge_weight, thesis.thesis_id)
            continue
        assert expression.instrument is not None
        sign = -1.0 if expression.direction in ("short", "underweight") else 1.0
        if sign < 0 and not allow_short and float(current.get(expression.instrument, 0.0)) <= 0:
            continue
        _add(desired, contributors, expression.instrument, sign * weight, thesis.thesis_id)
    return desired, contributors


def expression_hedge_ratio(thesis: Thesis) -> float:
    ratio = thesis.sizing_inputs.hedge_ratio
    return 1.0 if ratio is None else ratio


def _add(
    desired: dict[str, float],
    contributors: dict[str, list[str]],
    instrument: str,
    weight: float,
    thesis_id: str,
) -> None:
    desired[instrument] = desired.get(instrument, 0.0) + weight
    contributors.setdefault(instrument, []).append(thesis_id)


def _enforce_callables(
    current: dict[str, float],
    target: dict[str, float],
    checks: list[WeightCheck],
) -> dict[str, float]:
    names = sorted(set(current) | set(target))

    def mix(scale: float) -> dict[str, float]:
        mixed: dict[str, float] = {}
        for name in names:
            now, goal = current.get(name, 0.0), target.get(name, 0.0)
            if abs(goal) > abs(now) + 1e-15:
                mixed[name] = now + scale * (goal - now)
            else:
                mixed[name] = goal
        return mixed

    if _passes(mix(1.0), checks):
        return mix(1.0)
    if _passes(mix(0.0), checks):
        return mix(_largest_scale(lambda scale: mix(scale), checks))

    def toward_flat(scale: float) -> dict[str, float]:
        return {name: scale * current.get(name, 0.0) for name in names}

    if not _passes(toward_flat(0.0), checks):
        raise ValueError("limit checks reject a flat portfolio")
    return toward_flat(_largest_scale(toward_flat, checks))


def _largest_scale(book_at: Callable[[float], dict[str, float]], checks: list[WeightCheck]) -> float:
    """Largest scale in [0, 1] whose book passes. Assumes scale 0 passes and scale 1 does not."""
    low, high = 0.0, 1.0
    for _ in range(50):
        mid = (low + high) / 2.0
        if _passes(book_at(mid), checks):
            low = mid
        else:
            high = mid
    return low


def _passes(weights: Mapping[str, float], checks: list[WeightCheck]) -> bool:
    return all(check(weights) for check in checks)


def _breaks(
    kind: Literal["position", "cash"],
    ledger: Mapping[str, float],
    broker: Mapping[str, float],
    tol: float,
) -> list[ReconciliationBreak]:
    found: list[ReconciliationBreak] = []
    for key in set(ledger) | set(broker):
        left = float(ledger.get(key, 0.0))
        right = float(broker.get(key, 0.0))
        difference = left - right
        if abs(difference) > tol:
            found.append(
                ReconciliationBreak(kind=kind, key=key, ledger=left, broker=right, difference=difference)
            )
    return found
