"""Market-implied policy path from futures settlements (spec §3.3 C09, §7.3).

Fed-funds contracts settle to the average effective rate over the contract month, so
the path point is that average, not the post-meeting rate. The rate is ``100 − price``
in percent. A risk premium the card leaves as ``TODO_estimate`` widens each point and
does not move it. Meeting probabilities undo the monthly average under an explicit
no-premium assumption.
"""

from __future__ import annotations

import calendar
import math
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from macrosys.core.quantity import Quantity
from macrosys.pricing._interval import provenance_code, sub
from macrosys.pricing._params import band
from macrosys.pricing.errors import CurveError
from macrosys.pricing.types import PathWithRange

Convention = Literal["fedfunds", "sofr3m"]
_NODE = re.compile(r"C09:(fedfunds|sofr3m):(\d{4})-(\d{2})")


class RateStep(BaseModel):
    """One policy step and its probability under the no-premium assumption."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    change_bp: int
    probability: Quantity


class MeetingDistribution(BaseModel):
    """Per-meeting change distribution (spec §3.3 C09). ``assumption`` is always ``no_premium``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    meeting: date
    expected_change_bp: Quantity
    outcomes: tuple[RateStep, ...] = Field(min_length=1)
    assumption: Literal["no_premium"]
    notes: str


def implied_policy_path(
    futures: list[tuple[date, float]],
    convention: Convention = "fedfunds",
    as_of: date | None = None,
    risk_premium: Quantity | None = None,
) -> PathWithRange:
    """Futures-implied policy path in percent, one point per contract (spec §3.3 C09).

    ``futures`` are ``(contract month, settlement price)`` with the price in index
    points. The implied average rate is ``100 − price``, percent. Fed-funds contracts
    are monthly averages; a 3-month SOFR contract is the compounded rate for the
    quarter that starts in the contract month, with no intra-month split. Horizons
    are quarters from ``as_of`` to the 15th of the contract month (the centre of a
    monthly average). If ``as_of`` is omitted, the origin is the first contract
    month. The default risk premium is the ``TODO_estimate`` band in ``params.yaml``:
    the point stays at ``100 − price`` and the band widens it. A premium that itself
    has a point value is subtracted, which is the C09 form ``policy_rate = futures
    rate − risk premium``.
    """
    if convention not in ("fedfunds", "sofr3m"):
        raise CurveError(f"policy convention {convention!r} is not fedfunds or sofr3m")
    if not futures:
        raise CurveError("policy path needs at least one futures settlement")
    premium = risk_premium if risk_premium is not None else band("c09_risk_premium_pct")
    if premium.value is None:
        lo, hi = premium.low, premium.high
        if lo is None or hi is None or lo > 0.0 or hi < 0.0:
            raise CurveError("a valueless policy premium must be a band that contains zero")
    ordered = sorted(futures, key=lambda item: (item[0].year, item[0].month))
    seen: set[tuple[int, int]] = set()
    origin = as_of if as_of is not None else date(ordered[0][0].year, ordered[0][0].month, 1)
    path: PathWithRange = []
    for contract, price in ordered:
        key = (contract.year, contract.month)
        if key in seen:
            raise CurveError(f"duplicate policy contract {contract.year:04d}-{contract.month:02d}")
        seen.add(key)
        if not math.isfinite(price):
            raise CurveError(f"settlement {price} is not finite")
        rate = 100.0 - float(price)
        month = f"{contract.year:04d}-{contract.month:02d}"
        point = Quantity(value=rate, unit="pct", provenance=f"C09:{convention}:{month}")
        widened = sub(
            point,
            premium,
            provenance=f"{point.provenance}|{provenance_code('C09', premium)}",
            unshifted=premium.value is None,
        )
        path.append((_horizon_q(origin, contract), widened))
    return path


def meeting_probabilities(
    path: PathWithRange,
    meeting_dates: list[date],
    step_bp: int = 25,
) -> list[MeetingDistribution]:
    """Per-meeting step distribution from a fed-funds path (spec §3.3 C09).

    The path points are month-average rates. The new rate is assumed to be in effect
    on the announcement day, so the weight on the post-meeting rate is
    ``(days in month − day + 1) / days``. The rate entering the month is the previous
    month's average when that month has no meeting, otherwise that month's solved
    post-meeting rate. The expected change is ``(average − entering) / weight``,
    placed on the two ``step_bp`` grid points that bracket it. This uses the path's
    point values and ignores the premium band. The result is flagged
    ``assumption='no_premium'``. SOFR paths and months with no contract raise.
    """
    if step_bp <= 0:
        raise CurveError(f"step_bp must be positive, got {step_bp}")
    rates = _monthly_rates(path)
    if not rates:
        raise CurveError("meeting probabilities need a fed-funds path with contract months")
    meetings = sorted(meeting_dates)
    by_month: dict[tuple[int, int], date] = {}
    for meeting in meetings:
        key = (meeting.year, meeting.month)
        if key in by_month:
            raise CurveError(f"more than one meeting in {meeting.year:04d}-{meeting.month:02d} is not supported")
        by_month[key] = meeting
    post: dict[tuple[int, int], float] = {}
    out: list[MeetingDistribution] = []
    for meeting in meetings:
        key = (meeting.year, meeting.month)
        if key not in rates:
            raise CurveError(f"no fed-funds contract for the {meeting.isoformat()} meeting month")
        entering = _entering_rate(key, rates, by_month, post)
        weight = _post_weight(meeting)
        average = rates[key]
        if weight <= 0.0:
            raise CurveError(f"meeting {meeting.isoformat()} has no post-meeting days")
        delta_pct = (average - entering) / weight
        post[key] = entering + delta_pct
        delta_bp = delta_pct * 100.0
        outcomes = tuple(
            RateStep(
                change_bp=step,
                probability=Quantity(value=prob, unit="share", provenance="C09:no_premium"),
            )
            for step, prob in _bracket(delta_bp, step_bp)
        )
        days = calendar.monthrange(meeting.year, meeting.month)[1]
        out.append(
            MeetingDistribution(
                meeting=meeting,
                expected_change_bp=Quantity(value=delta_bp, unit="bp", provenance="C09:no_premium"),
                outcomes=outcomes,
                assumption="no_premium",
                notes=(
                    f"No risk premium is subtracted. The contract is the month average; "
                    f"the post-meeting weight is {(days - meeting.day + 1)}/{days} of {meeting:%B %Y}, "
                    f"counting the announcement day at the new rate. Steps are {step_bp} bp."
                ),
            )
        )
    return out


def _horizon_q(as_of: date, contract: date) -> float:
    anchor = date(contract.year, contract.month, 15)
    return (anchor - as_of).days / (365.25 / 4.0)


def _monthly_rates(path: PathWithRange) -> dict[tuple[int, int], float]:
    rates: dict[tuple[int, int], float] = {}
    for _horizon, quantity in path:
        match = _NODE.search(quantity.provenance or "")
        if match is None:
            raise CurveError("policy path point has no C09:fedfunds:YYYY-MM provenance")
        if match.group(1) != "fedfunds":
            raise CurveError("meeting probabilities are defined for the fed-funds monthly-average convention")
        key = (int(match.group(2)), int(match.group(3)))
        rates[key] = float(quantity.value if quantity.value is not None else quantity.mid)
    return rates


def _entering_rate(
    month: tuple[int, int],
    rates: dict[tuple[int, int], float],
    meetings: dict[tuple[int, int], date],
    post: dict[tuple[int, int], float],
) -> float:
    previous = _previous_month(month)
    if previous not in rates:
        raise CurveError(
            f"no contract for {previous[0]:04d}-{previous[1]:02d}, needed as the rate entering "
            f"{month[0]:04d}-{month[1]:02d}"
        )
    if previous in meetings:
        if previous not in post:
            raise CurveError(f"meeting in {previous[0]:04d}-{previous[1]:02d} was not solved in date order")
        return post[previous]
    return rates[previous]


def _previous_month(month: tuple[int, int]) -> tuple[int, int]:
    year, mon = month
    if mon == 1:
        return year - 1, 12
    return year, mon - 1


def _post_weight(meeting: date) -> float:
    days = calendar.monthrange(meeting.year, meeting.month)[1]
    return (days - meeting.day + 1) / days


def _bracket(delta_bp: float, step: int) -> list[tuple[int, float]]:
    k = math.floor(delta_bp / step)
    base = k * step
    if abs(delta_bp - base) <= 1e-8:
        return [(base, 1.0)]
    frac = (delta_bp - base) / step
    return [(base, 1.0 - frac), (base + step, frac)]

