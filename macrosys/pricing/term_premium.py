"""Published term premia and the short-rate path they imply (spec §3.3 C03).

ACM is not loaded by ``macrosys.sources.nyfed`` (that connector is SOFR, EFFR, RRP,
and primary dealers). The expected series id is ``NYFED:ACM:TP<year>``, the ACM
spreadsheet's ACMTPnn column in percent. Kim–Wright is FRED ``THREEFYTP10``.
"""

from __future__ import annotations

import re

from macrosys.core.quantity import Quantity
from macrosys.pricing._interval import sub
from macrosys.pricing._map import term_premium_series
from macrosys.pricing.curves import ZeroCurve
from macrosys.pricing.errors import CurveError
from macrosys.pricing.types import PathWithRange
from macrosys.store.observations import PointInTime

_TP = re.compile(r"TP(\d+)")


def term_premium_from_series(
    snapshot: PointInTime,
    tenor: float,
    model: str = "ACM",
) -> Quantity:
    """Published term premium at ``tenor`` years, in percent (spec §3.3 C03).

    ``model='ACM'`` reads ``NYFED:ACM:TP<year>`` (``NYFED:ACM:TP10`` for the 10-year).
    That id is not produced by the NY Fed connector today; it is the id this layer
    expects. ``model='KW'`` reads FRED ``THREEFYTP10`` and only the 10-year is mapped.
    The snapshot's point-in-time rule applies. A missing series raises ``CurveError``
    naming the id.
    """
    if model not in {"ACM", "KW"}:
        raise CurveError(f"term-premium model {model!r} is not ACM or KW")
    try:
        series_id = term_premium_series(model, tenor)
    except KeyError as exc:
        raise CurveError(str(exc)) from exc
    got = snapshot.latest_value(series_id)
    if got is None:
        raise CurveError(f"term premium series {series_id} is not in the snapshot ({model}, {tenor:g}y)")
    return got


def expected_short_rate_path(zero: ZeroCurve, term_premium: Quantity) -> PathWithRange:
    """Horizon-average expected short rate, ``zero − term premium`` (spec §3.3 C03).

    The C03 identity is ``y_n = average expected short rate + TP_n``, so the average
    over the premium's tenor is the zero at that tenor minus the premium. The tenor
    is read from the premium's provenance (``TP10``, including FRED ``THREEFYTP10``).
    One path point is returned, at ``tenor × 4`` quarters. A premium whose provenance
    names no tenor is subtracted from every knot; that flat-premium reading is an
    assumption and is marked ``C03:flat_premium_assumption``. Subtraction is in the
    curve's native percent. ACM is estimated on the GSW continuous curve; pass that
    curve when the premium is ACM. No compounding conversion is applied.
    """
    tenor = _tenor(term_premium)
    if tenor is None:
        tenors = [point.tenor_y for point in zero.points]
        code = "C03:flat_premium_assumption"
    else:
        tenors = [tenor]
        code = f"C03:{term_premium.provenance or 'term_premium'}"
    path: PathWithRange = []
    for knot in tenors:
        average = sub(zero.zero_at(knot), term_premium, provenance=code, unshifted=term_premium.value is None)
        path.append((knot * 4.0, average))
    return path


def _tenor(premium: Quantity) -> float | None:
    if not premium.provenance:
        return None
    found = _TP.findall(premium.provenance)
    if not found:
        return None
    return float(found[-1])
