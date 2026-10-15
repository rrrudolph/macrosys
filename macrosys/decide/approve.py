"""Expression, cost, risk inputs, and a size for one thesis (spec §7.4, §7.5, §6, §11.4).

``StoreApprover`` reads the store at ``as_of`` and the universe file. It does not approve by
calling ``decide``. The weekly caller copies ``cost_estimate_bp`` and the exposure check onto the
thesis, then passes ``exposure_match``, ``post_trade_ok``, and ``regime_flag`` into ``decide``.
``decide`` still reads ``expression.exposure_vector_check`` and ``expression.cost_estimate_bp``
off the thesis, so those two fields have to be updated or the expression and gap gates keep the
placeholder result. ``Approval.weight`` is the position; ``decide`` does not return a size.

Conventions, each marked where it is a judgment:

* Volatility is the trailing 63 trading days of simple closes, sample standard deviation
  (``ddof=1``) times ``sqrt(252)``. That is the resting book's window (``macrosys.decide.baseline``).
  Closes with ``trade_date`` after ``as_of`` are not read.
* The regime flag is ``macrosys.risk.regime.equity_regime_flag`` on SPY, or on ACWI when SPY has
  no closes. ``deteriorating`` is the absorption-ratio rule in spec §6.6. ``stressed`` is the
  equity factor's high-variance state and does not by itself zero the weight.
* Scenario loss is the worst loss from ``scenario_pnl`` at a signed weight of 1. Uncalibrated
  factor shocks and historical windows with no closes are skipped. A futures fund's roll is not
  invented: with no curve in the store the round trip is uncostable.
* Factor loadings are a 252-day ridge regression (``ridge=1e-4``) on the universe roles
  ``factor_*``. 252 days is the short end of the one-to-two-year window in spec §6.2.
* An exposure matches when the relevant bucket is at least half of NAV. Spec §7.2 says a 3
  percent constituent is not the ETF; half is the line used here. Futures drop country. A hedged
  share class is moved into its base currency.
* Half-spreads come from the liquidity file beside the universe. Impact is zero because spec §7.5
  calls it negligible at this size. The currency-hedge forward points are left at zero: the
  WisdomTree DXJ page (2026-10-15) says that cost is inside the NAV and is not a separate fee.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

import duckdb
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.core.units import compatible, convert
from macrosys.decide.baseline import TRADING_DAYS
from macrosys.decide.mandate import Mandate
from macrosys.decide.sizing import size_position
from macrosys.decide.thesis import Expression, Thesis
from macrosys.instruments.align import aligned_returns
from macrosys.instruments.cost import round_trip_cost_bp
from macrosys.instruments.exposure import apply_currency_hedge, exposure_from_holdings
from macrosys.instruments.holdings import HoldingRow, normalize_ccy, normalize_country
from macrosys.instruments.master import Instrument, load_universe
from macrosys.pipeline.jobs import PipelineConfig
from macrosys.risk.factors import FactorModel, exposures_from_loadings
from macrosys.risk.regime import equity_regime_flag
from macrosys.risk.stress import Scenario, load_scenarios, scenario_pnl
from macrosys.store.holdings import HoldingStore
from macrosys.store.prices import PriceStore

# [J] Same 63-day window as the resting book. See the module docstring.
VOL_WINDOW = 63
# [J] Short end of the spec §6.2 one-to-two-year regression window.
FACTOR_WINDOW = 252
# [J] Small ridge on daily decimal returns. A ridge of 1 would wipe the loadings.
FACTOR_RIDGE = 1e-4
# [J] Spec §7.2: 3% of an ETF is not a thesis about the ETF. Half of NAV is the match line.
MATCH_WEIGHT = 0.50
_EPS = 1e-12

# Scenario factor names are the role suffix. The scenario file uses these spellings.
_ROLE_TO_FACTOR: dict[str, str] = {
    "factor_global_equity": "global_equity",
    "factor_us_duration": "us_duration",
    "factor_ig_credit": "credit_ig",
    "factor_hy_credit": "credit_hy",
    "factor_em_equity": "em_equity",
    "factor_gold": "gold",
    "factor_energy": "energy",
    "factor_industrial_metals": "industrial_metals",
    "factor_usd": "usd",
}

# Entity ids in the thesis (spec §7.7 uses Cu for copper) and the issuer's word.
_COMMODITY: dict[str, str] = {
    "cu": "copper",
    "copper": "copper",
    "au": "gold",
    "gold": "gold",
    "ag": "silver",
    "silver": "silver",
    "wti": "wti",
    "oil": "wti",
    "crude": "wti",
    "light sweet crude": "wti",
}

_SKIP_SCENARIO = (
    "null factor moves",
    "no factor returns",
    "no complete factor-return",
    "missing factor columns",
    "refusing to price",
)


class Approval(BaseModel):
    """What the approver can say about one thesis at one ``as_of`` (spec §7.4, §7.5, §11.3, §11.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thesis_id: str
    exposure_match: bool
    cost_estimate_bp: float
    vol_annual: float
    scenario_loss: float
    regime_flag: str
    post_trade_ok: bool
    weight: float
    reasons: list[str]


class Approver(Protocol):
    """The seam the walk-forward calls. ``current`` is signed weight by ticker, as a share of NAV."""

    def approve(
        self,
        thesis: Thesis,
        conn: duckdb.DuckDBPyConnection,
        as_of: datetime,
        mandate: Mandate,
        current: Mapping[str, float],
    ) -> Approval:
        """Return the expression, cost, risk inputs, and a size at ``as_of`` (spec §11.3, §11.4)."""


class _Liquidity(BaseModel):
    """Half-spread ceilings by tier (spec §7.5). Values are basis points of half the quoted spread."""

    model_config = ConfigDict(extra="forbid")

    default_tier: str
    half_spread_bp: dict[str, float]
    tiers: dict[str, list[str]] = Field(default_factory=dict)

    def half_spread(self, ticker: str) -> float:
        """Half-spread in basis points for ``ticker``, or the default tier when it is unlisted."""
        for tier, names in self.tiers.items():
            if ticker in names:
                return float(self.half_spread_bp[tier])
        return float(self.half_spread_bp[self.default_tier])


@dataclass(frozen=True)
class _Exposure:
    """One instrument's buckets as NAV shares. Commodity is the futures or bullion underlying."""

    country: dict[str, float]
    sector: dict[str, float]
    ccy: dict[str, float]
    asset_class: dict[str, float]
    commodity: dict[str, float]
    source: str
    futures: bool


class StoreApprover:
    """Build an ``Approval`` from the store, the universe file, and the mandate (spec §7.4–§7.5, §11.4).

    ``pipeline_config`` maps a ticker to the price-bar id (``TIINGO:SPY``). ``universe_path`` is
    the instrument master. Scenarios and half-spreads default to ``scenarios.yaml`` and
    ``liquidity.yaml`` in the same directory, which is ``config/`` for the checked-in universe.
    """

    def __init__(
        self,
        pipeline_config: PipelineConfig,
        universe_path: str | Path,
        *,
        scenarios_path: str | Path | None = None,
        liquidity_path: str | Path | None = None,
    ) -> None:
        root = Path(universe_path)
        self._config = pipeline_config
        self._by_ticker = {row.ticker: row for row in load_universe(root)}
        self._scenarios_path = Path(scenarios_path) if scenarios_path is not None else root.parent / "scenarios.yaml"
        self._liquidity_path = Path(liquidity_path) if liquidity_path is not None else root.parent / "liquidity.yaml"

    def approve(
        self,
        thesis: Thesis,
        conn: duckdb.DuckDBPyConnection,
        as_of: datetime,
        mandate: Mandate,
        current: Mapping[str, float],
    ) -> Approval:
        """Return the gate inputs and a size for ``thesis`` using only data visible at ``as_of``."""
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        reasons: list[str] = []
        expression = thesis.expression
        hedge_ratio = thesis.sizing_inputs.hedge_ratio or 1.0
        legs = _leg_names(expression)
        missing = [ticker for ticker in legs if ticker not in self._by_ticker]
        exposures = {
            ticker: self._exposure(conn, self._by_ticker[ticker], as_of)
            for ticker in legs
            if ticker in self._by_ticker
        }
        if missing:
            exposure_match = False
            _add(reasons, f"{', '.join(missing)} is not in the universe, so the exposure check cannot pass.")
        else:
            exposure_match, why = _match(
                expression, thesis.expected_effect.entity, exposures, hedge_ratio
            )
            if not exposure_match and why:
                _add(reasons, why)

        cost, cost_reasons = self._cost(thesis, mandate)
        for sentence in cost_reasons:
            _add(reasons, sentence)
        uncostable = not math.isfinite(cost)
        if expression.is_outright_short and mandate.shorting.borrow_cost_bp_annual is None:
            _add(reasons, "borrow cost not entered on the mandate")

        market = self._market(conn, thesis, as_of, mandate, current, exposures, hedge_ratio)
        for sentence in market.reasons:
            _add(reasons, sentence)

        post_trade_ok = not uncostable
        weight = 0.0
        blocked = (
            not exposure_match
            or uncostable
            or market.vol is None
            or market.scenario_loss is None
            or market.regime_flag in ("deteriorating", "unknown")
            or market.limits is None
        )
        if not blocked and market.limits is not None and market.vol is not None and market.scenario_loss is not None:
            weight, sizing_reasons = _size(thesis, mandate, market, current, exposures, hedge_ratio)
            for sentence in sizing_reasons:
                _add(reasons, sentence)
        book_ok, book_why = _book_ok(
            expression, weight, hedge_ratio, current, exposures, market, mandate
        )
        if not book_ok:
            post_trade_ok = False
            weight = 0.0
            if book_why:
                _add(reasons, book_why)
        if (not exposure_match or not post_trade_ok or weight == 0.0) and not reasons:
            _add(reasons, "The position is not approved.")
        return Approval(
            thesis_id=thesis.thesis_id,
            exposure_match=exposure_match,
            cost_estimate_bp=cost,
            vol_annual=float("nan") if market.vol is None else market.vol,
            scenario_loss=float("nan") if market.scenario_loss is None else market.scenario_loss,
            regime_flag=market.regime_flag,
            post_trade_ok=post_trade_ok,
            weight=weight,
            reasons=reasons,
        )

    def _exposure(self, conn: duckdb.DuckDBPyConnection, instrument: Instrument, as_of: datetime) -> _Exposure:
        rows = _holding_rows(conn, instrument.ticker, self._price_id(instrument.ticker), as_of)
        if rows:
            try:
                held = [row if isinstance(row, HoldingRow) else HoldingRow.model_validate(row.model_dump()) for row in rows]
                underlying = instrument.underlying if instrument.structure == "futures" else None
                vector = exposure_from_holdings(held, underlying=underlying)
                if instrument.structure == "hedged":
                    vector = apply_currency_hedge(vector, 1.0, instrument.base_ccy)
            except ValueError as exc:
                return _blank(f"holdings:{exc}", futures=instrument.structure == "futures")
            commodity: dict[str, float] = {}
            if vector.underlying:
                commodity[_canonical_commodity(vector.underlying)] = 1.0
            return _Exposure(
                country={} if instrument.structure == "futures" else dict(vector.country),
                sector=dict(vector.sector),
                ccy=dict(vector.ccy),
                asset_class=dict(vector.asset_class),
                commodity=commodity,
                source="holdings",
                futures=instrument.structure == "futures",
            )
        return _declared(instrument)

    def _price_id(self, ticker: str) -> str | None:
        try:
            return self._config.price_instrument_id(ticker)
        except KeyError:
            return None

    def _cost(self, thesis: Thesis, mandate: Mandate) -> tuple[float, list[str]]:
        """Round trip in basis points, or infinity when a required piece was not entered."""
        if not self._liquidity_path.is_file():
            return math.inf, ["No liquidity schedule was found, so the half-spread cannot be costed."]
        liquidity = _Liquidity.model_validate(yaml.safe_load(self._liquidity_path.read_text(encoding="utf-8")))
        expression = thesis.expression
        months = thesis.expected_return.horizon_m
        total = 0.0
        problems: list[str] = []
        outright = expression.is_outright_short
        borrow = 0.0
        if outright:
            if mandate.shorting.borrow_cost_bp_annual is None:
                problems.append("borrow cost not entered on the mandate")
            else:
                borrow = float(mandate.shorting.borrow_cost_bp_annual)
        for ticker in _leg_names(expression):
            instrument = self._by_ticker.get(ticker)
            if instrument is None:
                problems.append(f"{ticker} is not in the universe, so the round trip cannot be costed.")
                continue
            if instrument.expense_ratio is None:
                problems.append(
                    f"{ticker} has no expense ratio on the universe record, so the round trip cannot be costed."
                )
                continue
            if instrument.structure == "futures":
                problems.append(
                    f"{ticker} is futures-based and the roll cost is not in the store, so the round trip cannot be costed."
                )
                continue
            leg_borrow = borrow if outright and ticker == expression.instrument else 0.0
            total += round_trip_cost_bp(
                liquidity.half_spread(ticker),
                instrument.expense_ratio,
                months,
                roll_cost_annual_bp=0.0,
                hedge_cost_annual_bp=0.0,
                borrow_cost_annual_bp=leg_borrow,
            )
        if problems:
            return math.inf, problems
        return total, []

    def _market(
        self,
        conn: duckdb.DuckDBPyConnection,
        thesis: Thesis,
        as_of: datetime,
        mandate: Mandate,
        current: Mapping[str, float],
        exposures: dict[str, _Exposure],
        hedge_ratio: float,
    ) -> _Market:
        reasons: list[str] = []
        expression = thesis.expression
        wanted = set(_leg_names(expression)) | set(current) | set(self._factor_tickers())
        for extra in ("SPY", "ACWI"):
            if extra in self._by_ticker:
                wanted.add(extra)
        for ticker in list(current):
            if ticker not in exposures and ticker in self._by_ticker:
                exposures[ticker] = self._exposure(conn, self._by_ticker[ticker], as_of)
        closes = self._closes(conn, sorted(wanted), as_of)
        if closes.empty:
            reasons.append("No closes were found at this as_of, so volatility cannot be computed.")
            return _Market(None, None, "unknown", None, reasons, None, None)

        bars = {
            ticker: closes[[ticker]].rename(columns={ticker: "close"}).dropna()
            for ticker in closes.columns
            if closes[ticker].notna().any()
        }
        aligned = aligned_returns(bars, calendar="union", max_gap_days=2, method="simple")
        unit = _leg_weights(expression, 1.0, hedge_ratio)
        vol = _expression_vol(aligned, unit)
        if vol is None:
            reasons.append(
                "There are not 63 daily closes at or before this as_of, so volatility cannot be computed "
                "and the weight is zero."
            )

        equity = _equity_series(aligned)
        factor_map = {
            name: ticker
            for ticker, name in self._factor_names().items()
            if ticker in aligned.returns.columns and ticker not in aligned.stale
        }
        factor_frame = (
            pd.DataFrame({name: aligned.returns[ticker] for name, ticker in factor_map.items()})
            if factor_map
            else None
        )
        if equity is None:
            regime = "unknown"
            reasons.append("SPY and ACWI have no closes, so the correlation regime cannot be estimated.")
        else:
            regime = equity_regime_flag(equity, factor_frame)
            if regime == "deteriorating":
                reasons.append("The correlation regime is deteriorating, so the weight is zero.")
            elif regime == "unknown":
                reasons.append("The correlation regime could not be estimated, so the weight is zero.")

        tickers = sorted(set(unit) | set(current))
        loadings = _fit_loadings(aligned, tickers, factor_map)
        scenarios = self._scenarios()
        scenario_loss: float | None = None
        book_loss: float | None = None
        if loadings is None or scenarios is None:
            if scenarios is None:
                reasons.append("No scenario file was found, so scenario loss cannot be computed.")
            else:
                reasons.append("Factor loadings could not be estimated, so scenario loss cannot be computed.")
        else:
            scenario_loss = _worst_loss(unit, loadings, factor_frame, scenarios)
            held = {ticker: float(weight) for ticker, weight in current.items() if abs(weight) > _EPS}
            book_loss = 0.0 if not held else _worst_loss(held, loadings, factor_frame, scenarios)
            if scenario_loss is None or book_loss is None:
                reasons.append(
                    "No scenario could be priced from the closes at this as_of, so the weight is zero."
                )
                scenario_loss = None
                book_loss = None

        limits = _limits(mandate)
        if limits is None:
            reasons.append("A mandate risk limit is null, so the post-trade book cannot be checked.")
        return _Market(
            vol,
            scenario_loss,
            regime,
            book_loss,
            reasons,
            loadings,
            factor_frame,
            limits,
            scenarios,
        )

    def _scenarios(self) -> list[Scenario] | None:
        if not self._scenarios_path.is_file():
            return None
        return load_scenarios(self._scenarios_path)

    def _factor_names(self) -> dict[str, str]:
        """Ticker to scenario-factor name for roles marked ``factor_*``."""
        named: dict[str, str] = {}
        for ticker, instrument in self._by_ticker.items():
            factor = _ROLE_TO_FACTOR.get(instrument.role or "")
            if factor is not None:
                named[ticker] = factor
        return named

    def _factor_tickers(self) -> list[str]:
        return list(self._factor_names())

    def _closes(self, conn: duckdb.DuckDBPyConnection, tickers: list[str], as_of: datetime) -> pd.DataFrame:
        ids = {ticker: self._price_id(ticker) for ticker in tickers}
        ids = {ticker: iid for ticker, iid in ids.items() if iid is not None}
        if not ids:
            return pd.DataFrame()
        end = as_of.date()
        wide = PriceStore(conn).read_closes(list(ids.values()), date(1990, 1, 1), end)
        wide = wide.rename(columns={iid: ticker for ticker, iid in ids.items()})
        if wide.empty:
            return wide
        index = pd.to_datetime(wide.index)
        if getattr(index, "tz", None) is not None:
            index = index.tz_convert("UTC").tz_localize(None)
        wide.index = index
        return wide.loc[wide.index.normalize() <= pd.Timestamp(end)].sort_index()


@dataclass
class _Limits:
    vol_target: float
    position: float
    factor: float
    country: float
    currency: float
    scenario: float


@dataclass
class _Market:
    vol: float | None
    scenario_loss: float | None
    regime_flag: str
    book_loss: float | None
    reasons: list[str]
    loadings: pd.DataFrame | None = None
    factor_returns: pd.DataFrame | None = None
    limits: _Limits | None = None
    scenarios: list[Scenario] | None = None


def _limits(mandate: Mandate) -> _Limits | None:
    risk = mandate.risk
    values = (
        risk.vol_target_annual_pct,
        risk.position_limit_pct,
        risk.factor_limit_pct,
        risk.country_limit_pct,
        risk.currency_net_limit_pct,
        risk.scenario_loss_budget_pct,
    )
    if any(value is None for value in values):
        return None
    assert risk.vol_target_annual_pct is not None
    assert risk.position_limit_pct is not None
    assert risk.factor_limit_pct is not None
    assert risk.country_limit_pct is not None
    assert risk.currency_net_limit_pct is not None
    assert risk.scenario_loss_budget_pct is not None
    return _Limits(
        vol_target=risk.vol_target_annual_pct / 100.0,
        position=risk.position_limit_pct / 100.0,
        factor=risk.factor_limit_pct / 100.0,
        country=risk.country_limit_pct / 100.0,
        currency=risk.currency_net_limit_pct / 100.0,
        scenario=risk.scenario_loss_budget_pct / 100.0,
    )


def _size(
    thesis: Thesis,
    mandate: Mandate,
    market: _Market,
    current: Mapping[str, float],
    exposures: dict[str, _Exposure],
    hedge_ratio: float,
) -> tuple[float, list[str]]:
    """One ``size_position`` call, then country and currency room the sizer does not know about."""
    assert market.vol is not None and market.scenario_loss is not None and market.limits is not None
    limits = market.limits
    unit = _leg_weights(thesis.expression, 1.0, hedge_ratio)
    reasons: list[str] = []
    try:
        gap_lo, gap_hi = _position_edges_annual(thesis)
    except ValueError as exc:
        return 0.0, [f"The gap cannot be converted to a return ({exc}), so the weight is zero."]
    pos_room = min(
        (_room(float(current.get(ticker, 0.0)), weight, limits.position) for ticker, weight in unit.items()),
        default=0.0,
    )
    factor_room = _factor_room(current, unit, market.loadings, limits.factor)
    country_room = _bucket_room(current, unit, exposures, "country", limits.country)
    currency_room = _bucket_room(
        current, unit, exposures, "ccy", limits.currency, ignore={mandate.base_ccy}
    )
    remaining = max(0.0, limits.scenario - (market.book_loss or 0.0))
    sized = size_position(
        vol_annual=market.vol,
        target_vol=limits.vol_target,
        gap_low=gap_lo,
        gap_high=gap_hi,
        evidence_quality=thesis.evidence_quality,
        marginal_scenario_loss=market.scenario_loss,
        remaining_budget=remaining,
        position_cap=pos_room,
        factor_cap=factor_room,
        gap_lower_edge_annual=gap_lo,
        kelly_fraction=mandate.sizing.fractional_kelly_ceiling,
        conviction_bounds=mandate.sizing.conviction_multiplier_bounds,
    )
    weight = sized.weight
    if country_room < weight:
        weight = country_room
    if currency_room < weight:
        weight = currency_room
    if weight == 0.0:
        if sized.binding == "scenario_budget" or remaining <= _EPS:
            reasons.append("The scenario budget has no room for this position, so the weight is zero.")
        elif country_room <= _EPS:
            reasons.append("The country limit has no room for this position, so the weight is zero.")
        elif currency_room <= _EPS:
            reasons.append("The currency limit has no room for this position, so the weight is zero.")
        elif sized.binding == "kelly_ceiling":
            reasons.append("The Kelly ceiling is zero, so the weight is zero.")
        elif sized.binding == "position_cap":
            reasons.append("The position limit has no room for this position, so the weight is zero.")
        elif sized.binding == "factor_cap":
            reasons.append("The factor limit has no room for this position, so the weight is zero.")
        else:
            reasons.append("The volatility-parity weight is zero, so the weight is zero.")
    return weight, reasons


def _book_ok(
    expression: Expression,
    weight: float,
    hedge_ratio: float,
    current: Mapping[str, float],
    exposures: dict[str, _Exposure],
    market: _Market,
    mandate: Mandate,
) -> tuple[bool, str]:
    """True when ``current`` plus this weight is inside the mandate limits and the scenario budget."""
    if market.limits is None:
        return False, "A mandate risk limit is null, so the post-trade book cannot be checked."
    limits = market.limits
    combined = dict(current)
    for ticker, leg in _leg_weights(expression, weight, hedge_ratio).items():
        combined[ticker] = combined.get(ticker, 0.0) + leg
    combined = {ticker: value for ticker, value in combined.items() if abs(value) > _EPS}
    for ticker, held in combined.items():
        if abs(held) > limits.position + 1e-9:
            return False, f"The post-trade weight of {ticker} is outside the position limit, so the weight is zero."
        if ticker not in exposures or exposures[ticker].source.startswith("missing"):
            return False, (
                f"{ticker} has no exposure, so the country and currency limits cannot be checked "
                "and the weight is zero."
            )
    if market.loadings is not None and combined:
        missing = [ticker for ticker in combined if ticker not in market.loadings.index]
        if missing:
            return False, (
                f"Factor loadings are missing for {', '.join(sorted(missing))}, so the factor limit "
                "cannot be checked and the weight is zero."
            )
        try:
            factor = exposures_from_loadings(combined, market.loadings)
        except ValueError as exc:
            return False, f"The factor limit cannot be checked ({exc}), so the weight is zero."
        if any(abs(float(value)) > limits.factor + 1e-9 for value in factor.to_numpy()):
            return False, "The post-trade book is outside a factor limit, so the weight is zero."
    country = _bucket_exposure(combined, exposures, "country")
    if any(abs(value) > limits.country + 1e-9 for value in country.values()):
        return False, "The post-trade book is outside a country limit, so the weight is zero."
    currency = _bucket_exposure(combined, exposures, "ccy")
    foreign = {code: value for code, value in currency.items() if code != mandate.base_ccy}
    if any(abs(value) > limits.currency + 1e-9 for value in foreign.values()):
        return False, "The post-trade book is outside the currency limit, so the weight is zero."
    if not combined:
        return True, ""
    loss = _worst_loss(combined, market.loadings, market.factor_returns, market.scenarios)
    if loss is None:
        return False, "Scenario loss could not be priced, so the post-trade book cannot be checked."
    if loss > limits.scenario + 1e-9:
        return False, "The post-trade book is outside the scenario budget, so the weight is zero."
    return True, ""


def _worst_loss(
    weights: Mapping[str, float],
    loadings: pd.DataFrame | None,
    factor_returns: pd.DataFrame | None,
    scenarios: list[Scenario] | None,
) -> float | None:
    """Worst loss fraction of NAV. Empty weights lose nothing. None when nothing can be priced."""
    held = {ticker: float(weight) for ticker, weight in weights.items() if abs(weight) > _EPS}
    if not held:
        return 0.0
    if loadings is None or factor_returns is None or not scenarios:
        return None
    losses: list[float] = []
    for scenario in scenarios:
        try:
            frame = scenario_pnl(held, loadings, factor_returns, scenario)
        except ValueError as exc:
            if any(mark in str(exc) for mark in _SKIP_SCENARIO):
                continue
            raise
        losses.append(max(0.0, -float(frame["portfolio"].min())))
    if not losses:
        return None
    return max(losses)


def _factor_room(
    current: Mapping[str, float], unit: Mapping[str, float], loadings: pd.DataFrame | None, limit: float
) -> float:
    if loadings is None:
        return 0.0
    names = list(loadings.index)
    if any(ticker not in names for ticker in list(current) + list(unit)):
        return 0.0
    current_w = {ticker: float(current.get(ticker, 0.0)) for ticker in names}
    try:
        have = exposures_from_loadings(current_w, loadings)
        add = exposures_from_loadings(dict(unit), loadings)
    except ValueError:
        return 0.0
    rooms = [
        _room(float(have[name]), float(add[name]), limit)
        for name in have.index
    ]
    return min(rooms) if rooms else math.inf


def _bucket_room(
    current: Mapping[str, float],
    unit: Mapping[str, float],
    exposures: dict[str, _Exposure],
    field: str,
    limit: float,
    ignore: set[str] | None = None,
) -> float:
    skipped = ignore or set()
    try:
        existing = _bucket_exposure(current, exposures, field)
        per_unit = _bucket_exposure(unit, exposures, field)
    except KeyError:
        return 0.0
    rooms = [
        _room(existing.get(key, 0.0), per_unit.get(key, 0.0), limit)
        for key in set(existing) | set(per_unit)
        if key not in skipped
    ]
    return min(rooms) if rooms else math.inf


def _bucket_exposure(weights: Mapping[str, float], exposures: dict[str, _Exposure], field: str) -> dict[str, float]:
    total: dict[str, float] = {}
    for ticker, weight in weights.items():
        if abs(weight) <= _EPS:
            continue
        exposure = exposures[ticker]
        for key, share in getattr(exposure, field).items():
            total[key] = total.get(key, 0.0) + float(weight) * float(share)
    return {key: value for key, value in total.items() if abs(value) > _EPS}


def _room(existing: float, per_unit: float, limit: float) -> float:
    """Largest non-negative weight such that ``existing + per_unit * weight`` stays inside ±limit."""
    if abs(per_unit) <= _EPS:
        return math.inf if abs(existing) <= limit + 1e-9 else 0.0
    if per_unit > 0:
        upper = (limit - existing) / per_unit
        lower = (-limit - existing) / per_unit
    else:
        upper = (-limit - existing) / per_unit
        lower = (limit - existing) / per_unit
    if upper < -1e-12 or lower > upper + 1e-12:
        return 0.0
    return max(0.0, upper)


def _expression_vol(aligned: object, unit: Mapping[str, float]) -> float | None:
    returns = aligned.returns  # type: ignore[attr-defined]
    stale = set(aligned.stale)  # type: ignore[attr-defined]
    if any(ticker in stale or ticker not in returns.columns for ticker in unit):
        return None
    total = None
    for ticker, weight in unit.items():
        part = returns[ticker] * weight
        total = part if total is None else total + part
    assert total is not None
    window = total.dropna().iloc[-VOL_WINDOW:]
    if len(window) < VOL_WINDOW or not bool(window.map(math.isfinite).all()):
        return None
    vol = float(window.std(ddof=1) * math.sqrt(TRADING_DAYS))
    if not math.isfinite(vol) or vol <= 0.0:
        return None
    return vol


def _equity_series(aligned: object) -> pd.Series | None:
    returns = aligned.returns  # type: ignore[attr-defined]
    stale = set(aligned.stale)  # type: ignore[attr-defined]
    for ticker in ("SPY", "ACWI"):
        if ticker in returns.columns and ticker not in stale:
            return returns[ticker].dropna()
    return None


def _fit_loadings(
    aligned: object, tickers: list[str], factor_map: dict[str, str]
) -> pd.DataFrame | None:
    if not factor_map:
        return None
    returns = aligned.returns  # type: ignore[attr-defined]
    stale = set(aligned.stale)  # type: ignore[attr-defined]
    usable = {
        name: ticker
        for name, ticker in factor_map.items()
        if ticker in returns.columns and ticker not in stale
    }
    if not usable:
        return None
    factors = pd.DataFrame({name: returns[ticker] for name, ticker in usable.items()})
    rows: list[pd.Series] = []
    index: list[str] = []
    for ticker in tickers:
        if ticker not in returns.columns or ticker in stale:
            continue
        own = next((name for name, src in usable.items() if src == ticker), None)
        rhs = factors.drop(columns=[own]) if own is not None and own in factors.columns else factors
        loading = pd.Series(0.0, index=factors.columns, dtype=float)
        if rhs.shape[1] == 0:
            if own is not None:
                loading[own] = 1.0
            rows.append(loading)
            index.append(ticker)
            continue
        asset = returns[[ticker]]
        complete = asset.join(rhs, how="inner").dropna()
        window: int | None = FACTOR_WINDOW if len(complete) >= FACTOR_WINDOW else None
        if window is None and len(complete) <= rhs.shape[1] + 1:
            continue
        try:
            fitted = FactorModel().fit(asset, rhs, ridge=FACTOR_RIDGE, window=window)
        except ValueError:
            continue
        loading.loc[fitted.loadings_.columns] = fitted.loadings_.loc[ticker].astype(float)
        if own is not None:
            loading[own] = 1.0
        rows.append(loading)
        index.append(ticker)
    if not rows:
        return None
    frame = pd.DataFrame(rows, index=index)
    frame.index.name = None
    return frame


def _match(
    expression: Expression,
    entity: EntityKey,
    exposures: dict[str, _Exposure],
    hedge_ratio: float,
) -> tuple[bool, str]:
    """A09 check: the expression's buckets match ``entity`` (spec §7.4, §11.3)."""
    label = expression.instrument or f"{expression.pair[0]}/{expression.pair[1]}"  # type: ignore[index]
    if expression.instrument is not None:
        parts = [(exposures[expression.instrument], 1.0)]
        primary = expression.instrument
    else:
        assert expression.pair is not None
        sign = 1.0 if expression.direction == "long" else -1.0
        primary = expression.pair[0]
        parts = [
            (exposures[expression.pair[0]], sign),
            (exposures[expression.pair[1]], -sign * hedge_ratio),
        ]
    if all(part.source == "missing" or part.source.startswith("holdings:") for part, _weight in parts):
        return False, f"{label} has no holdings and no declared exposure, so the exposure check cannot pass."
    net = _combine(parts)
    futures_only = all(part.futures for part, _weight in parts) and expression.pair is None
    return _rules(entity, expression, net, primary, label, futures_only=futures_only)


def _rules(
    entity: EntityKey,
    expression: Expression,
    net: _Exposure,
    primary: str,
    label: str,
    *,
    futures_only: bool,
) -> tuple[bool, str]:
    """Match rules. Direction does not matter for a single name; a pair uses the net of the hedge."""
    kind = entity.entity_type
    ident = entity.entity_id
    if kind == "instrument":
        if ident.casefold() == primary.casefold():
            return True, ""
        return False, f"{label} does not express instrument {ident}."
    if kind == "portfolio":
        return False, "A portfolio entity is not an instrument exposure."
    if kind == "institution":
        return False, f"No exposure on {label} expresses institution {ident}."
    if kind == "economy":
        if futures_only:
            return False, f"{label} is futures-based, so it exposes the underlying and not a country."
        code = _as_country(ident)
        weight = abs(net.country.get(code, 0.0))
        equity = abs(net.asset_class.get("equity", 0.0)) >= MATCH_WEIGHT
        bonds = abs(net.asset_class.get("fixed_income", 0.0)) >= MATCH_WEIGHT
        if weight >= MATCH_WEIGHT and (equity or bonds):
            return True, ""
        return False, f"{label} does not express economy {ident}."
    if kind == "industry":
        keys = _sector_keys(ident)
        found = max(
            (abs(value) for name, value in net.sector.items() if name.casefold() in keys),
            default=0.0,
        )
        if found >= MATCH_WEIGHT and abs(net.asset_class.get("equity", 0.0)) >= MATCH_WEIGHT:
            return True, ""
        return False, f"{label} does not express industry {ident}."
    if kind == "commodity":
        want = _canonical_commodity(ident)
        if abs(net.commodity.get(want, 0.0)) >= MATCH_WEIGHT:
            return True, ""
        return False, f"{label} does not express commodity {ident}."
    if kind == "currency":
        code = _as_ccy(ident)
        if abs(net.ccy.get(code, 0.0)) < MATCH_WEIGHT:
            return False, f"{label} does not express currency {ident}."
        if expression.instrument is not None:
            equity = abs(net.asset_class.get("equity", 0.0)) >= MATCH_WEIGHT
            bonds = abs(net.asset_class.get("fixed_income", 0.0)) >= MATCH_WEIGHT
            if equity or bonds:
                return False, f"{label} is an equity or bond fund, so it does not express currency {ident}."
        elif any(abs(value) >= MATCH_WEIGHT for value in net.country.values()):
            return False, f"{label} still has a country exposure, so it does not isolate currency {ident}."
        return True, ""
    return False, f"{label} does not express {kind} {ident}."


def _as_country(entity_id: str) -> str:
    try:
        return normalize_country(entity_id)
    except ValueError:
        return entity_id


def _as_ccy(entity_id: str) -> str:
    try:
        return normalize_ccy(entity_id)
    except ValueError:
        return entity_id


def _sector_keys(entity_id: str) -> set[str]:
    folded = " ".join(entity_id.casefold().replace("_", " ").split())
    keys = {folded}
    if ":" in folded:
        keys.add(folded.split(":")[-1].strip())
    return keys


def _combine(parts: list[tuple[_Exposure, float]]) -> _Exposure:
    buckets = {
        "country": {},
        "sector": {},
        "ccy": {},
        "asset_class": {},
        "commodity": {},
    }
    for exposure, weight in parts:
        for field in buckets:
            for key, share in getattr(exposure, field).items():
                buckets[field][key] = buckets[field].get(key, 0.0) + weight * share
    trimmed = {
        field: {key: value for key, value in values.items() if abs(value) > _EPS}
        for field, values in buckets.items()
    }
    return _Exposure(
        country=trimmed["country"],
        sector=trimmed["sector"],
        ccy=trimmed["ccy"],
        asset_class=trimmed["asset_class"],
        commodity=trimmed["commodity"],
        source="net",
        futures=all(part.futures for part, _weight in parts),
    )


def _declared(instrument: Instrument) -> _Exposure:
    stated = instrument.exposure
    country: dict[str, float] = {}
    sector: dict[str, float] = {}
    ccy: dict[str, float] = {}
    asset_class: dict[str, float] = {}
    commodity: dict[str, float] = {}
    if stated is not None:
        if stated.country and instrument.structure != "futures":
            country[stated.country] = 1.0
        if stated.sector:
            sector[stated.sector] = 1.0
        if stated.currency:
            ccy[stated.currency] = 1.0
        if stated.asset_class:
            asset_class[stated.asset_class] = 1.0
        if stated.commodity:
            commodity[_canonical_commodity(stated.commodity)] = 1.0
    if instrument.structure == "futures":
        country = {}
        if instrument.underlying and not commodity:
            commodity[_canonical_commodity(instrument.underlying)] = 1.0
    if instrument.structure == "hedged":
        ccy = {instrument.base_ccy: sum(ccy.values()) or 1.0}
    source = "declared" if stated is not None or commodity or ccy or asset_class else "missing"
    return _Exposure(country, sector, ccy, asset_class, commodity, source, instrument.structure == "futures")


def _blank(source: str, *, futures: bool) -> _Exposure:
    return _Exposure({}, {}, {}, {}, {}, source, futures)


def _holding_rows(
    conn: duckdb.DuckDBPyConnection, ticker: str, price_id: str | None, as_of: datetime
) -> list[HoldingRow]:
    store = HoldingStore(conn)
    for instrument_id in (ticker, price_id):
        if not instrument_id:
            continue
        rows = store.latest_snapshot(instrument_id, as_of)
        if rows:
            return rows
    return []


def _leg_names(expression: Expression) -> list[str]:
    if expression.instrument is not None:
        return [expression.instrument]
    assert expression.pair is not None
    return [expression.pair[0], expression.pair[1]]


def _leg_weights(expression: Expression, weight: float, hedge_ratio: float) -> dict[str, float]:
    sign = 1.0 if expression.direction == "long" else -1.0
    if expression.instrument is not None:
        return {expression.instrument: sign * weight}
    assert expression.pair is not None
    return {
        expression.pair[0]: sign * weight,
        expression.pair[1]: -sign * hedge_ratio * weight,
    }


def _canonical_commodity(label: str) -> str:
    folded = " ".join(label.casefold().replace("_", " ").split())
    return _COMMODITY.get(folded, folded)


def _add(reasons: list[str], sentence: str) -> None:
    if sentence and sentence not in reasons:
        reasons.append(sentence)


def _position_edges_annual(thesis: Thesis) -> tuple[float, float]:
    """Annualized position gap as a fraction of NAV (spec §11.4, the same bp scale as the gap gate).

    A long uses the gap as quoted. A short or an underweight profits when that gap is negative,
    so the edges are flipped. Basis points become a fraction, then the horizon in
    ``expected_return.horizon_m`` is scaled to a year.
    """
    lo_bp, hi_bp = _gap_edges_bp(thesis.gap)
    if thesis.expression.direction == "long":
        pos_lo, pos_hi = lo_bp, hi_bp
    else:
        pos_lo, pos_hi = -hi_bp, -lo_bp
    per_year = (12.0 / thesis.expected_return.horizon_m) / 10_000.0
    return pos_lo * per_year, pos_hi * per_year


def _gap_edges_bp(gap: Quantity) -> tuple[float, float]:
    """Same edge scale as ``macrosys.decide.gates._edges_bp`` (bp, pp, or pct = 100 bp)."""
    lo = gap.value if gap.low is None else gap.low
    hi = gap.value if gap.high is None else gap.high
    if lo is None or hi is None:
        raise ValueError("gap has no usable edge")
    if compatible(gap.unit, "bp"):
        scale = convert(1.0, gap.unit, "bp")
    elif gap.unit == "pct":
        scale = 100.0
    else:
        raise ValueError(f"gap needs a bp, pp, or pct unit, got {gap.unit!r}")
    return float(lo) * scale, float(hi) * scale
