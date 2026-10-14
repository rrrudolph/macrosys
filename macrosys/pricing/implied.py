"""Store-backed implied paths (spec §8.5 ``ImpliedPaths``, §7.3, §9.2).

Series ids come from ``series_map.yaml``. The snapshot has already applied
``published_at``. Each method also drops reference periods that start after the
requested date.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from macrosys.core.quantity import Quantity
from macrosys.pricing._map import (
    commodity_for_name,
    inflation_for_ccy,
    instrument_block,
    policy_for_ccy,
)
from macrosys.pricing._params import band
from macrosys.pricing._series import latest, month_starts
from macrosys.pricing.commodities import Curve, CurvePoint
from macrosys.pricing.equity import implied_growth_from_multiple
from macrosys.pricing.errors import NoPricedInError
from macrosys.pricing.inflation import decompose_breakeven
from macrosys.pricing.policy_path import implied_policy_path
from macrosys.pricing.types import PathWithRange
from macrosys.store.observations import PointInTime

_HORIZON_MONTHS = 36


@runtime_checkable
class ImpliedPaths(Protocol):
    """Market-implied paths the thesis layer can read (spec §8.5)."""

    def policy_path(self, ccy: str, as_of: date) -> PathWithRange:
        """Policy rate path for a currency."""
        ...

    def breakevens(self, ccy: str, as_of: date) -> PathWithRange:
        """Inflation breakevens, decomposed for missing premia."""
        ...

    def futures_curve(self, commodity: str, as_of: date) -> Curve:
        """Futures settlements for a commodity."""
        ...

    def implied_growth(self, instrument_id: str, as_of: date) -> Quantity:
        """Implied growth for one instrument."""
        ...


class StoreImpliedPaths:
    """``ImpliedPaths`` over an observation snapshot (spec §8.5, §9.2)."""

    def __init__(self, snapshot: PointInTime) -> None:
        self.snapshot = snapshot

    def policy_path(self, ccy: str, as_of: date) -> PathWithRange:
        """Fed-funds or SOFR settlements mapped for ``ccy``, as a C09 path (spec §3.3 C09).

        The series are ``{prefix}YYYY-MM`` settlement prices. EUR has no futures
        prefix: OIS and €STR futures are a spec §9.8 gap, and the €STR level is not
        used as a stand-in path.
        """
        block = policy_for_ccy(ccy)
        if block is None:
            raise NoPricedInError(f"no policy-path mapping for currency {ccy}")
        prefix = block.get("futures_prefix")
        if not prefix:
            raise NoPricedInError(str(block.get("gap") or f"no policy futures mapped for {ccy}"))
        convention = block.get("convention")
        if convention not in {"fedfunds", "sofr3m"}:
            raise NoPricedInError(f"{ccy} policy convention {convention!r} is not fedfunds or sofr3m")
        expected_unit = str(block.get("price_unit") or "index")
        futures: list[tuple[date, float]] = []
        for month in month_starts(as_of, _HORIZON_MONTHS):
            series_id = f"{prefix}{month:%Y-%m}"
            got = latest(self.snapshot, series_id, as_of)
            if got is None or got.value is None:
                continue
            if got.unit != expected_unit:
                raise NoPricedInError(
                    f"{series_id} is in {got.unit}; the settlement price must be in {expected_unit} "
                    "(100 minus the rate in percent)"
                )
            futures.append((month, float(got.value)))
        if not futures:
            raise NoPricedInError(
                f"no {prefix}YYYY-MM settlements in the snapshot on {as_of.isoformat()} for {ccy}"
            )
        return implied_policy_path(futures, convention, as_of=as_of)

    def breakevens(self, ccy: str, as_of: date) -> PathWithRange:
        """Published breakevens, then the C04 premium bands (spec §3.3 C04).

        USD uses FRED ``T5YIE`` and ``T10YIE``, which are par breakevens (nominal
        CMT minus real CMT), not zero-coupon gaps. A real zero bootstrap is not
        available from the connectors: real CMT quotes start at 5 years and the
        Treasury parser does not read the real curve. Each print is passed through
        ``decompose_breakeven``, so missing premia widen the range and are not
        entered as zero. The horizon in quarters is the tenor in years times 4.
        """
        block = inflation_for_ccy(ccy)
        if block is None:
            raise NoPricedInError(f"no breakeven mapping for currency {ccy}")
        series = block.get("series") or {}
        if not series:
            raise NoPricedInError(str(block.get("gap") or f"no breakeven series mapped for {ccy}"))
        path: PathWithRange = []
        missing: list[str] = []
        for tenor_text, series_id in series.items():
            got = latest(self.snapshot, str(series_id), as_of)
            if got is None:
                missing.append(str(series_id))
                continue
            tenor_y = float(tenor_text)
            path.extend(decompose_breakeven(got, horizon_q=tenor_y * 4.0))
        if not path:
            names = ", ".join(missing) if missing else "the mapped breakeven series"
            raise NoPricedInError(f"no breakeven prints in the snapshot for {ccy}; missing: {names}")
        path.sort(key=lambda item: item[0])
        return path

    def futures_curve(self, commodity: str, as_of: date) -> Curve:
        """Futures settlements ``{prefix}YYYY-MM`` for a mapped commodity (spec §3.3 C07).

        The spot series in the map (FRED Brent, FRED copper) is a level. It is not
        turned into a curve when the futures are absent.
        """
        block = commodity_for_name(commodity)
        if block is None:
            raise NoPricedInError(f"no futures-curve mapping for commodity {commodity}")
        prefix = block.get("futures_prefix")
        if not prefix:
            raise NoPricedInError(f"no futures prefix mapped for {commodity}")
        points: list[CurvePoint] = []
        for month in month_starts(as_of, _HORIZON_MONTHS):
            series_id = f"{prefix}{month:%Y-%m}"
            got = latest(self.snapshot, series_id, as_of)
            if got is None:
                continue
            points.append(CurvePoint(expiry=month, price=got))
        if not points:
            spot = block.get("spot")
            raise NoPricedInError(
                f"no {prefix}YYYY-MM settlements on {as_of.isoformat()} for {commodity}. "
                f"Spot series {spot} is a level, not a curve."
            )
        return Curve(commodity=commodity, points=tuple(points))

    def implied_growth(self, instrument_id: str, as_of: date) -> Quantity:
        """Reverse Gordon growth from the mapped earnings yield (spec §3.3 C05, §7.3).

        Required return is the ``TODO_estimate`` band in ``params.yaml``. Payout is
        the mapped series when the snapshot has it, otherwise the payout band. A
        missing earnings yield raises ``NoPricedInError`` naming the series. The
        result is a perpetuity growth rate in percent, not a finite-horizon DCF.
        """
        block = instrument_block(instrument_id)
        if block is None:
            raise NoPricedInError(f"no implied-growth series mapped for instrument {instrument_id}")
        series_id = block.get("earnings_yield")
        if not series_id:
            raise NoPricedInError(f"instrument {instrument_id} has no earnings-yield series in the map")
        earnings = latest(self.snapshot, str(series_id), as_of)
        if earnings is None:
            raise NoPricedInError(
                f"earnings yield series {series_id} is not in the snapshot on {as_of.isoformat()}"
            )
        payout_id = block.get("payout")
        payout = latest(self.snapshot, str(payout_id), as_of) if payout_id else None
        if payout is None:
            payout = band("c05_payout_share")
        return implied_growth_from_multiple(earnings, band("c05_required_return_pct"), payout)
