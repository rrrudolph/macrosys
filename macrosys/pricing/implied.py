"""Store-backed implied paths (spec §8.5 ``ImpliedPaths``, §7.3, §9.2).

Series ids come from ``series_map.yaml``. The snapshot has already applied
``published_at``. Each method also drops reference periods that start after the
requested date.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable

from macrosys.core.quantity import Quantity
from macrosys.pricing._map import (
    commodity_for_name,
    inflation_for_ccy,
    instrument_block,
    policy_for_ccy,
    spread_for_entity,
)
from macrosys.pricing._params import band
from macrosys.pricing._series import history, latest, month_starts
from macrosys.pricing.commodities import Curve, CurvePoint
from macrosys.pricing.equity import implied_growth_from_multiple
from macrosys.pricing.errors import NoPricedInError
from macrosys.pricing.inflation import decompose_breakeven
from macrosys.pricing.policy_path import implied_policy_path
from macrosys.pricing.spreads import implied_spread_change, quarter_window_days
from macrosys.pricing.types import PathWithRange
from macrosys.store.actions import CorporateActionStore
from macrosys.store.observations import PointInTime
from macrosys.store.prices import PriceStore

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

    def spread_change(self, entity_id: str, as_of: date, horizon_q: tuple[float, float]) -> Quantity:
        """Mean-reversion-free credit-spread change, in basis points."""
        ...


class StoreImpliedPaths:
    """``ImpliedPaths`` over an observation snapshot (spec §8.5, §9.2).

    Record closes and corporate actions are not observations. Pass ``prices`` and
    ``actions``, or omit them and the snapshot's store connection is used, which
    is how the week constructs this object. A close is visible when its trade date
    is on or before the as-of date, and a distribution when its ex-date is. Ingestion
    time does not hide either one. ``corporate_action`` has no ``published_at``.
    """

    def __init__(
        self,
        snapshot: PointInTime,
        prices: PriceStore | None = None,
        actions: CorporateActionStore | None = None,
    ) -> None:
        """Bind the snapshot and, optionally, the price and corporate-action readers (spec §8.5, §9.2)."""
        self.snapshot = snapshot
        self._prices = prices
        self._actions = actions

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
        missing earnings yield uses the mapped trailing cash-dividend yield when
        the instrument declares one, and otherwise raises ``NoPricedInError`` naming
        the series. The result is a perpetuity growth rate in percent, not a
        finite-horizon DCF and not a price change compounded over the thesis window.
        """
        block = instrument_block(instrument_id)
        if block is None:
            raise NoPricedInError(f"no implied-growth series mapped for instrument {instrument_id}")
        series_id = block.get("earnings_yield")
        earnings = latest(self.snapshot, str(series_id), as_of) if series_id else None
        if earnings is not None:
            payout_id = block.get("payout")
            payout = latest(self.snapshot, str(payout_id), as_of) if payout_id else None
            if payout is None:
                payout = band("c05_payout_share")
            return implied_growth_from_multiple(earnings, band("c05_required_return_pct"), payout)
        if isinstance(block.get("trailing_dividend"), dict):
            return self._growth_from_trailing_dividend(instrument_id, as_of, block)
        if series_id:
            raise NoPricedInError(
                f"earnings yield series {series_id} is not in the snapshot on {as_of.isoformat()}"
            )
        raise NoPricedInError(f"instrument {instrument_id} has no earnings-yield series in the map")

    def spread_change(self, entity_id: str, as_of: date, horizon_q: tuple[float, float]) -> Quantity:
        """Zero point change in the mapped OAS, with a percentile band of history (spec §3.3 C06, §7.3).

        The series level is the market spread. The point change is zero basis points.
        The range is the declared percentile band of overlapping changes whose length
        is the thesis window. Prints are those ``history`` returns, so ``published_at``
        and the reference period are both on or before ``as_of``.
        """
        block = spread_for_entity(entity_id)
        if block is None:
            raise NoPricedInError(f"no credit-spread mapping for {entity_id}")
        series_id = str(block.get("series") or "")
        if not series_id:
            raise NoPricedInError(f"credit-spread mapping for {entity_id} has no series")
        level_unit = str(block.get("level_unit") or "")
        if not level_unit:
            raise NoPricedInError(f"credit-spread mapping for {series_id} has no level_unit")
        points, unit = history(self.snapshot, series_id, as_of)
        if not points or unit is None:
            raise NoPricedInError(f"no {series_id} prints in the snapshot on {as_of.isoformat()}")
        if unit != level_unit:
            raise NoPricedInError(f"{series_id} is in {unit}; the credit-spread map expects {level_unit}")
        return implied_spread_change(
            points,
            window_days=quarter_window_days(horizon_q[0], horizon_q[1]),
            level_unit=level_unit,
            low_percentile=float(block["low_percentile"]),
            high_percentile=float(block["high_percentile"]),
            min_changes=int(block["min_changes"]),
            tolerance_days=int(block["match_tolerance_days"]),
            series_id=series_id,
        )

    def _growth_from_trailing_dividend(self, instrument_id: str, as_of: date, block: dict) -> Quantity:
        """Gordon growth from the trailing cash yield when no earnings yield is stored (spec §3.3 C05).

        [M] The yield is the sum of cash dividends with ex-date in the lookback, restated
        into current shares by later split ratios, divided by the latest record close.
        [J] That trailing yield is passed to ``implied_growth_from_multiple`` as the
        earnings yield with payout 1, so payout times the input equals the cash yield.
        Payout 1 is that identity. It is not an estimate that earnings are fully paid
        out. The trailing yield stands in for the forward yield. The required-return
        band is the ``TODO_estimate`` in ``params.yaml``. A mapped earnings yield
        replaces this path when the snapshot has it.
        """
        trail = block["trailing_dividend"]
        price_id = str(trail["price_id"])
        action_id = str(trail["action_id"])
        lookback = int(trail["lookback_days"])
        minimum = int(trail["min_payments"])
        max_age = int(trail["close_max_age_days"])
        ccy = str(trail["ccy"])
        earnings_id = block.get("earnings_yield")
        named = f" Earnings yield series {earnings_id} is not in the snapshot." if earnings_id else ""
        close, _close_day = self._latest_close(price_id, as_of, max_age, named)
        window_start = as_of - timedelta(days=lookback) + timedelta(days=1)
        actions = self._action_reader().for_instrument(action_id, start=window_start, end=as_of)
        splits = [_split(row) for row in actions if row.type == "split"]
        cash: list[tuple[date, float]] = []
        for row in actions:
            if row.type != "cash_div":
                continue
            ex_date = _as_date(row.ex_date)
            if ex_date < window_start or ex_date > as_of:
                continue
            row_ccy = (row.ccy or "").upper()
            if row_ccy != ccy.upper():
                raise NoPricedInError(
                    f"{action_id} cash dividend on {ex_date.isoformat()} is in {row.ccy!r}; the map expects {ccy}.{named}"
                )
            if row.amount is None:
                raise NoPricedInError(f"{action_id} cash dividend on {ex_date.isoformat()} has no amount.{named}")
            if any(split_date == ex_date for split_date, _ratio in splits):
                raise NoPricedInError(
                    f"{action_id} has a split and a cash dividend on {ex_date.isoformat()}; "
                    f"the cash per current share is not restated.{named}"
                )
            factor = 1.0
            for split_date, ratio in splits:
                if split_date > ex_date:
                    factor *= ratio
            cash.append((ex_date, float(row.amount) / factor))
        if len(cash) < minimum:
            raise NoPricedInError(
                f"{instrument_id} via {action_id} has {len(cash)} cash dividends with ex_date in the "
                f"{lookback} days through {as_of.isoformat()}; need {minimum} to sum a trailing year. "
                f"A partial year is not annualized.{named} FRED:SP500 is an index level, not an earnings yield."
            )
        yield_pct = sum(amount for _day, amount in cash) / close * 100.0
        growth = implied_growth_from_multiple(
            Quantity(value=yield_pct, unit="pct", provenance=price_id),
            band("c05_required_return_pct"),
            Quantity(value=1.0, unit="share", provenance="trailing_dividend"),
        )
        return Quantity(
            value=growth.value,
            unit=growth.unit,
            low=growth.low,
            high=growth.high,
            provenance=f"{growth.provenance}|trailing_dividend|{price_id}",
        )

    def _latest_close(self, price_id: str, as_of: date, max_age: int, named: str) -> tuple[float, date]:
        frame = self._price_reader().read_closes([price_id], as_of - timedelta(days=366), as_of)
        if frame.empty or price_id not in frame.columns:
            raise NoPricedInError(f"no record close for {price_id} on or before {as_of.isoformat()}.{named}")
        column = frame[price_id].dropna()
        if column.empty:
            raise NoPricedInError(f"no record close for {price_id} on or before {as_of.isoformat()}.{named}")
        close_day = _as_date(column.index[-1])
        age = (as_of - close_day).days
        if age > max_age:
            raise NoPricedInError(
                f"newest record close for {price_id} is {close_day.isoformat()}, "
                f"{age} days before {as_of.isoformat()}; the map allows {max_age}.{named}"
            )
        close = float(column.iloc[-1])
        if close <= 0.0:
            raise NoPricedInError(f"{price_id} close on {close_day.isoformat()} is not positive.{named}")
        return close, close_day

    def _price_reader(self) -> PriceStore:
        if self._prices is not None:
            return self._prices
        return PriceStore(_store_connection(self.snapshot))

    def _action_reader(self) -> CorporateActionStore:
        if self._actions is not None:
            return self._actions
        return CorporateActionStore(_store_connection(self.snapshot))


def _store_connection(snapshot: PointInTime):
    """Connection the snapshot reads, when the week did not pass a price reader."""
    conn = getattr(snapshot, "_conn", None)
    if conn is None:
        raise NoPricedInError("no price reader is attached and the snapshot has no store connection")
    return conn


def _split(row) -> tuple[date, float]:
    if row.ratio is None or float(row.ratio) <= 0.0:
        raise NoPricedInError(f"{row.instrument_id} split on {_as_date(row.ex_date).isoformat()} has no positive ratio")
    return _as_date(row.ex_date), float(row.ratio)


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value
