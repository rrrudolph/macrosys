"""The resting book held when no thesis passes (spec §11.3, amended 2026-10-15).

Before the amendment a week with no passing thesis left the book flat, which for a USD account is a
cash position nobody decided on. The owner chose a baseline instead: ``inverse_vol`` weights each
instrument named on the mandate by the inverse of its trailing realized volatility, renormalizes the
weights to one, and clips any weight above the position limit with the excess spread over the rest.
Theses are tilts on top of this book (``PortfolioConstructor.propose`` with ``baseline``). Closes are
read point-in-time: nothing dated after ``as_of`` enters the volatility.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime, timedelta

import duckdb
import pandas as pd
from pydantic import BaseModel, ConfigDict

from macrosys.decide.mandate import BaselineConfig, Mandate
from macrosys.store.prices import PriceStore

# [J] Trading days in a year, for annualizing a daily standard deviation.
TRADING_DAYS = 252
# [J] A close older than this at as_of means the price feed is stale and the book cannot be stated.
MAX_STALE_DAYS = 7
_EPS = 1e-12


class BaselineUnavailableError(RuntimeError):
    """The resting book cannot be computed from the closes visible at ``as_of`` (class B, spec §11.7)."""


class RestingBook(BaseModel):
    """Baseline weights at one decision instant. ``weights`` are shares of NAV and sum to one."""

    model_config = ConfigDict(frozen=True)

    as_of: date
    method: str
    window_days: int
    weights: dict[str, float]
    vol_annual: dict[str, float]

    def sentence(self) -> str:
        """One plain sentence naming the book, largest weight first."""
        ordered = sorted(self.weights.items(), key=lambda item: (-item[1], item[0]))
        parts = ", ".join(f"{ticker} {weight * 100:.1f}%" for ticker, weight in ordered)
        return f"The resting book is {parts}."


def inverse_vol_book(
    closes: pd.DataFrame,
    config: BaselineConfig,
    as_of: date,
    position_cap: float | None,
) -> RestingBook:
    """Inverse-volatility weights from a wide close panel (spec §11.3, amended 2026-10-15).

    ``closes`` is indexed by trade date with one column per ticker; rows after ``as_of`` are dropped.
    Each instrument needs ``config.window_days`` consecutive daily simple returns ending at the last
    row, and that last close must be within ``MAX_STALE_DAYS`` of ``as_of``. Volatility is the sample
    standard deviation annualized by ``sqrt(TRADING_DAYS)``. ``position_cap`` is a share of NAV; a
    weight above it is clipped and the excess is spread over the uncapped names in proportion. Any
    shortfall raises ``BaselineUnavailableError`` naming the instruments.
    """
    missing = [ticker for ticker in config.instruments if ticker not in closes.columns]
    if missing:
        raise BaselineUnavailableError(f"no closes for baseline instruments {missing}")
    panel = closes.loc[:, config.instruments].sort_index()
    panel = panel.loc[pd.to_datetime(panel.index).date <= as_of]  # type: ignore[index]
    if panel.empty:
        raise BaselineUnavailableError(f"no closes at or before {as_of.isoformat()}")
    last_day = pd.Timestamp(panel.index[-1]).date()
    if (as_of - last_day).days > MAX_STALE_DAYS:
        raise BaselineUnavailableError(
            f"latest close is {last_day.isoformat()}, more than {MAX_STALE_DAYS} days before {as_of.isoformat()}"
        )
    returns = panel.pct_change().iloc[1:]
    window = returns.tail(config.window_days)
    short = [
        ticker
        for ticker in config.instruments
        if len(window) < config.window_days or window[ticker].isna().any()
    ]
    if short:
        raise BaselineUnavailableError(
            f"fewer than {config.window_days} daily returns at {as_of.isoformat()} for {short}"
        )
    vol = window.std(ddof=1) * math.sqrt(TRADING_DAYS)
    flat = [ticker for ticker in config.instruments if not (vol[ticker] > _EPS)]
    if flat:
        raise BaselineUnavailableError(f"zero or undefined volatility for {flat}")
    inverse = 1.0 / vol
    raw = {ticker: float(inverse[ticker] / inverse.sum()) for ticker in config.instruments}
    weights = _cap(raw, position_cap)
    return RestingBook(
        as_of=as_of,
        method=config.method,
        window_days=config.window_days,
        weights=weights,
        vol_annual={ticker: float(vol[ticker]) for ticker in config.instruments},
    )


def resting_book(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    mandate: Mandate,
    price_id: Callable[[str], str],
) -> RestingBook | None:
    """Read record-tier closes and build the mandate's baseline at ``as_of`` (spec §11.3, §9.6).

    Returns ``None`` when the mandate has no baseline (cash). ``price_id`` maps a universe ticker to
    the store's instrument id (``TIINGO:SPY`` for the Tiingo feed). Only closes dated at or before
    ``as_of`` are read; the lookback is wide enough to hold the window through holidays.
    """
    config = mandate.baseline
    if config is None:
        return None
    end = as_of.date()
    start = end - timedelta(days=config.window_days * 2 + 30)
    try:
        ids = [price_id(ticker) for ticker in config.instruments]
    except KeyError as exc:
        raise BaselineUnavailableError(
            f"baseline instrument is not a configured price: {exc.args[0]}"
        ) from exc
    closes = PriceStore(conn).read_closes(ids, start, end)
    closes = closes.rename(columns=dict(zip(ids, config.instruments, strict=True)))
    cap = None if mandate.risk.position_limit_pct is None else mandate.risk.position_limit_pct / 100.0
    return inverse_vol_book(closes, config, end, cap)


def _cap(weights: dict[str, float], cap: float | None) -> dict[str, float]:
    """Clip weights at ``cap`` and spread the excess over the names still under it."""
    if cap is None:
        return dict(weights)
    if cap <= 0.0:
        raise BaselineUnavailableError("position limit is zero, so no resting book can be held")
    if cap * len(weights) < 1.0 - _EPS:
        raise BaselineUnavailableError(
            f"{len(weights)} instruments at a {cap:.0%} position limit cannot sum to 100%"
        )
    out = dict(weights)
    for _ in range(len(out)):
        over = {name: weight for name, weight in out.items() if weight > cap + _EPS}
        if not over:
            break
        excess = sum(weight - cap for weight in over.values())
        for name in over:
            out[name] = cap
        free = {name: weight for name, weight in out.items() if name not in over and weight < cap - _EPS}
        total_free = sum(free.values())
        if total_free <= _EPS:
            # Every uncapped name is at the cap; cap * n >= 1 guarantees this only happens at the limit.
            share = excess / len(free) if free else 0.0
            for name in free:
                out[name] += share
            break
        for name, weight in free.items():
            out[name] = weight + excess * weight / total_free
    total = sum(out.values())
    return {name: float(weight / total) for name, weight in out.items()}
