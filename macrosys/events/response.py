"""Read estimated release responses and say whether the front end has already moved (spec §7.3, §11.3, §10.1 item 11).

``parameters/event_response.yaml`` holds the first data-estimated parameter family: the HC1 slope of a
market change on a standardized surprise, in the market's unit per one standard deviation. The pricing
use of B23 is this revision — how far the front end typically moves per unit of surprise — which tempers
what is already priced. It is not B23's Bordalo overreaction ratio. That coefficient stays
``TODO_estimate`` on the mechanism card until consensus revisions exist (spec §3.3 B23, §9.8).

The gap gate fails closed on two facts from this file. A slope whose interval contains zero cannot
approve a trade. A front end that has already moved by at least the edge of the implied interval
closer to zero has priced the news the slope identifies, so the residual is not a gap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from macrosys.core.quantity import Quantity
from macrosys.core.units import UnitError, compatible
from macrosys.events.estimate import default_parameters_path

Sign = Literal[-1, 1]


class ResponseCoefficient(BaseModel):
    """One stored slope, with the interval the regression reported (spec §8.4 "estimated", §10.1 item 11)."""

    model_config = ConfigDict(frozen=True)

    name: str
    release: str
    market: str
    horizon_days: int = Field(ge=1)
    beta: Quantity
    predicted_sign: Sign | None
    mechanisms: tuple[str, ...]


class ResponseCheck(BaseModel):
    """Whether a release still has an unpriced front-end move (spec §7.3, §11.3).

    ``blocked`` is true when the gap gate must fail: no slope, a zero surprise, an interval that
    contains zero, or a front end that has already covered the conservative edge. ``implied`` is
    the slope times the standardized surprise, in the market unit. ``residual`` is implied minus
    the observed move when both exist and share a unit.
    """

    model_config = ConfigDict(frozen=True)

    blocked: bool
    reason: str
    implied: Quantity | None = None
    residual: Quantity | None = None


class ResponseBook:
    """The coefficient file, keyed by release, market, and horizon."""

    def __init__(self, entries: list[ResponseCoefficient]) -> None:
        keyed: dict[tuple[str, str, int], ResponseCoefficient] = {}
        for item in entries:
            key = (item.release, item.market, item.horizon_days)
            if key in keyed:
                raise ValueError(f"duplicate response for {item.release} -> {item.market} @{item.horizon_days}d")
            keyed[key] = item
        self._by = keyed
        self.entries = tuple(entries)

    def get(self, release: str, market: str, horizon_days: int) -> ResponseCoefficient | None:
        """The stored slope, or ``None`` when this pair was not estimated."""
        return self._by.get((release, market, horizon_days))


class _Beta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float
    low: float | None
    high: float | None
    unit: str
    per: str
    source: str

    @model_validator(mode="after")
    def _both_edges(self) -> _Beta:
        if (self.low is None) != (self.high is None):
            raise ValueError("beta low and high must both be set or both be absent")
        return self


class _Entry(BaseModel):
    """Fields the decision reads. The study writes more; those stay on the file and are ignored here."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    release: str
    market: str
    horizon_days: int = Field(ge=1)
    beta: _Beta
    predicted_sign: Sign | None = None
    mechanisms: list[str] = Field(default_factory=list)


class _Document(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    entries: list[_Entry]


def load_responses(path: str | Path | None = None) -> ResponseBook:
    """Load ``parameters/event_response.yaml`` into slopes with units and provenance (spec §10.1 item 11).

    The default path is the file the study writes. A pair the study omitted, because the sample was
    too short, is simply absent. Duplicate keys are an error: one file must not carry two slopes for
    the same release, market, and horizon.
    """
    file_path = Path(path) if path is not None else default_parameters_path()
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{file_path} must be a mapping")
    document = _Document.model_validate(loaded)
    entries = [
        ResponseCoefficient(
            name=item.name,
            release=item.release,
            market=item.market,
            horizon_days=item.horizon_days,
            beta=Quantity(
                value=item.beta.value,
                unit=item.beta.unit,
                low=item.beta.low,
                high=item.beta.high,
                provenance=item.beta.source,
            ),
            predicted_sign=item.predicted_sign,
            mechanisms=tuple(item.mechanisms),
        )
        for item in document.entries
    ]
    return ResponseBook(entries)


def implied_revision(coeff: ResponseCoefficient, surprise_sigma: float) -> Quantity:
    """Market revision the slope implies for this standardized surprise (spec §7.3, B23's pricing use).

    ``surprise_sigma`` is dimensionless: the surprise divided by its trailing scale, the same object
    the study regresses on. The result is in the slope's unit. A negative surprise flips the interval.
    This is the front-end move per unit of surprise, not a forecast-revision ratio.
    """
    return coeff.beta.scale(surprise_sigma, provenance=coeff.beta.provenance)


def check_response(
    book: ResponseBook,
    release: str,
    market: str,
    horizon_days: int,
    surprise_sigma: float,
    observed: Quantity,
) -> ResponseCheck:
    """Compare the observed front-end move with the move the slope implies (spec §7.3, §11.3).

    The conservative implied move is the edge of ``beta × surprise`` closer to zero: the smallest
    move the interval still supports. The observed move is read the same way, so a wide observed
    range cannot claim the news is priced off its far edge. The front end has already moved when
    that observed edge has the slope's sign and is at least as large. Units must be compatible;
    a mismatch is an error, not a soft no-trade, because it is a caller bug.

    A missing pair, a zero surprise, or an interval that contains zero blocks. A print that matches
    the forecast is not a gap, and a slope indistinguishable from zero is not a decision.
    """
    coeff = book.get(release, market, horizon_days)
    label = f"{release}->{market}@{horizon_days}d"
    if coeff is None:
        return ResponseCheck(blocked=True, reason=f"no estimated response for {label}")
    if surprise_sigma == 0.0:
        return ResponseCheck(
            blocked=True,
            reason=f"standardized surprise is 0; the slope implies no revision ({label})",
            implied=implied_revision(coeff, 0.0),
        )
    implied = implied_revision(coeff, surprise_sigma)
    if implied.straddles_zero():
        return ResponseCheck(
            blocked=True,
            reason=f"response interval contains zero ({label}); the slope cannot gate a decision",
            implied=implied,
        )
    if not compatible(observed.unit, implied.unit):
        raise UnitError(
            f"observed move unit {observed.unit!r} is incompatible with the slope unit {implied.unit!r} ({label})"
        )
    seen = observed.to(implied.unit) if observed.unit != implied.unit else observed
    residual = _closed(implied).sub(_closed(seen), provenance=implied.provenance)
    edge = _conservative_edge(implied)
    observed_edge = _conservative_edge(seen)
    if _covers(observed_edge, edge):
        return ResponseCheck(
            blocked=True,
            reason=(
                f"front end already moved by {observed_edge:.4f} {implied.unit}; "
                f"the slope's conservative edge is {edge:.4f} {implied.unit} "
                f"(implied {_fmt(implied)}; {label})"
            ),
            implied=implied,
            residual=residual,
        )
    return ResponseCheck(
        blocked=False,
        reason=(
            f"front end has not covered the slope: observed {observed_edge:.4f} {implied.unit}, "
            f"conservative edge {edge:.4f} {implied.unit} (implied {_fmt(implied)}; {label})"
        ),
        implied=implied,
        residual=residual,
    )


def response_block(
    book: ResponseBook,
    release: str,
    market: str,
    horizon_days: int,
    surprise_sigma: float,
    observed: Quantity,
) -> str | None:
    """Reason the gap gate must fail, or ``None`` when a residual remains (spec §11.3).

    ``decide`` passes a non-empty reason through as a gap-gate failure. ``None`` leaves the gap
    gate to its other tests: the range still has to exclude zero and clear costs.
    """
    result = check_response(book, release, market, horizon_days, surprise_sigma, observed)
    if result.blocked:
        return result.reason
    return None


def _closed(qty: Quantity) -> Quantity:
    """A point as ``[value, value]``, so subtracting it keeps the other side's interval (spec §7.7)."""
    if qty.low is not None and qty.high is not None:
        return qty
    assert qty.value is not None
    return Quantity(value=qty.value, unit=qty.unit, low=qty.value, high=qty.value, provenance=qty.provenance)


def _fmt(qty: Quantity) -> str:
    if qty.low is None or qty.high is None:
        return f"{qty.mid:.4f} {qty.unit}"
    return f"{qty.mid:.4f} {qty.unit} [{qty.low:.4f}, {qty.high:.4f}]"


def _conservative_edge(qty: Quantity) -> float:
    """The edge closer to zero. A range that contains zero contributes nothing."""
    lo = qty.value if qty.low is None else qty.low
    hi = qty.value if qty.high is None else qty.high
    assert lo is not None and hi is not None
    if lo <= 0.0 <= hi:
        return 0.0
    if hi < 0.0:
        return hi
    return lo


def _covers(observed_edge: float, implied_edge: float) -> bool:
    """True when the observed edge has reached the implied edge, same sign."""
    if implied_edge > 0.0:
        return observed_edge >= implied_edge
    if implied_edge < 0.0:
        return observed_edge <= implied_edge
    return False
