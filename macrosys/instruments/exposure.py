"""Exposure vectors from holdings, currency hedges, and portfolio weights (spec A09, §5.4, §7.4).

A09's output used here is the holdings decomposition: country, sector, currency, and asset class.
Factor loadings are not a sum of holding weights; they belong to the risk model (spec §6.2) and are
not a field on this vector. Futures-based funds expose the underlying contract, not a country.
"""

from __future__ import annotations

from datetime import date

import numpy as np
from pydantic import BaseModel, ConfigDict, field_validator

from macrosys.instruments.holdings import HoldingRow, normalize_ccy
from macrosys.instruments.master import Instrument

_EPS = 1e-12


def _trim(weights: dict[str, float]) -> dict[str, float]:
    return {key: value for key, value in weights.items() if abs(value) > _EPS}


def _add(bucket: dict[str, float], key: str | None, weight: float) -> None:
    if key is None:
        return
    bucket[key] = bucket.get(key, 0.0) + weight


class ExposureVector(BaseModel):
    """Look-through exposures as NAV shares (spec A09, §7.4).

    ``effective_n`` is ``(sum w)^2 / sum(w^2)`` over holding lines (1 / sum(w^2) when weights sum
    to 1). ``top10_weight`` is the sum of the ten largest signed line weights. ``underlying`` is set
    for a futures-based fund; ``country`` is then empty. Keys are ISO3 / ``EA``, sector labels,
    ISO 4217 codes, and the asset-class vocabulary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    country: dict[str, float]
    sector: dict[str, float]
    ccy: dict[str, float]
    asset_class: dict[str, float]
    effective_n: float
    top10_weight: float
    as_of: date
    source: str
    underlying: str | None = None

    @field_validator("country", "sector", "ccy", "asset_class", mode="before")
    @classmethod
    def _copy_bucket(cls, value: dict[str, float]) -> dict[str, float]:
        return {str(key): float(weight) for key, weight in dict(value).items()}


def _concentration(weights: np.ndarray) -> tuple[float, float]:
    if weights.size == 0:
        raise ValueError("no holdings")
    if not np.isfinite(weights).all():
        raise ValueError("holding weights must be finite")
    sum_sq = float(np.dot(weights, weights))
    if sum_sq == 0.0:
        raise ValueError("holdings have zero weight")
    total = float(weights.sum())
    effective_n = (total * total) / sum_sq
    largest = np.sort(weights)[::-1][:10]
    return effective_n, float(largest.sum())


def exposure_from_holdings(rows: list[HoldingRow], *, underlying: str | None = None) -> ExposureVector:
    """Aggregate one holdings snapshot into an exposure vector (spec A09, §4.2).

    Rows must share ``instrument_id``, ``as_of``, and ``source``. Weights are summed by country,
    sector, currency, and asset class. A null country or sector is omitted from that bucket and
    still counts toward ``effective_n`` and ``top10_weight``. When ``underlying`` is set the fund
    is futures-based: country exposure is dropped and the vector records the contract instead.
    """
    if not rows:
        raise ValueError("no holdings")
    instrument_id = rows[0].instrument_id
    as_of = rows[0].as_of
    source = rows[0].source
    for row in rows[1:]:
        if row.instrument_id != instrument_id or row.as_of != as_of or row.source != source:
            raise ValueError("holdings must be one instrument, as_of, and source")
    country: dict[str, float] = {}
    sector: dict[str, float] = {}
    ccy: dict[str, float] = {}
    asset_class: dict[str, float] = {}
    weights = np.empty(len(rows), dtype=np.float64)
    for i, row in enumerate(rows):
        weights[i] = row.weight
        if underlying is None:
            _add(country, row.country, row.weight)
        _add(sector, row.sector, row.weight)
        _add(ccy, row.ccy, row.weight)
        _add(asset_class, row.asset_class, row.weight)
    effective_n, top10 = _concentration(weights)
    name = underlying.strip() if isinstance(underlying, str) else None
    return ExposureVector(
        country=_trim(country),
        sector=_trim(sector),
        ccy=_trim(ccy),
        asset_class=_trim(asset_class),
        effective_n=effective_n,
        top10_weight=top10,
        as_of=as_of,
        source=source,
        underlying=name or None,
    )


def apply_currency_hedge(vec: ExposureVector, hedge_ratio: float, base_ccy: str) -> ExposureVector:
    """Move non-base currency exposure into ``base_ccy`` by ``hedge_ratio`` (spec A09, §5.4).

    A ratio of 1 puts the whole foreign-currency book into the base currency. A ratio of 0 leaves
    the vector unchanged. Each foreign weight ``w`` becomes ``w * (1 - hedge_ratio)`` and the base
    currency gains ``w * hedge_ratio``. Country, sector, and asset-class buckets are unchanged:
    the hedge is a currency overlay, not a change in the holdings. ``hedge_ratio`` must lie in [0, 1].
    """
    if not 0.0 <= hedge_ratio <= 1.0:
        raise ValueError(f"hedge_ratio must be in [0, 1], got {hedge_ratio}")
    base = normalize_ccy(base_ccy)
    ccy = dict(vec.ccy)
    moved = 0.0
    for code, weight in list(ccy.items()):
        if code == base:
            continue
        shift = weight * hedge_ratio
        ccy[code] = weight - shift
        moved += shift
    ccy[base] = ccy.get(base, 0.0) + moved
    return vec.model_copy(update={"ccy": _trim(ccy)})


def net_currency_exposure(vec: ExposureVector, base_ccy: str) -> dict[str, float]:
    """Currency exposures excluding the base currency (spec §5.4).

    The portfolio's weekly currency report is this residual: what is left after the base-currency
    sleeve is removed. An empty dict means the book is fully in (or fully hedged into) ``base_ccy``.
    """
    base = normalize_ccy(base_ccy)
    return {code: weight for code, weight in vec.ccy.items() if code != base}


def portfolio_exposure(positions: list[tuple[Instrument, float, ExposureVector]]) -> ExposureVector:
    """NAV-weighted sum of instrument exposure vectors (spec A09, §5.4).

    ``weight`` is the position's share of portfolio NAV. Bucket maps are the weighted sum and are
    not renormalized, so a half-invested book sums to one half. Repeated lines for the same
    ``instrument_id`` are added together first. Futures-based instruments contribute their
    underlying and no country. ``effective_n`` is the look-through Herfindahl
    ``1 / sum_i (s_i^2 / N_i)`` after renormalizing position weights to the invested book, assuming
    distinct instruments have disjoint holdings — overlapping funds overstate N. ``top10_weight``
    is the same invested-weight average of constituent top-10 weights, not a true look-through top 10.
    """
    if not positions:
        raise ValueError("no positions")
    merged: dict[str, tuple[Instrument, float, ExposureVector]] = {}
    for instrument, weight, vector in positions:
        if not np.isfinite(weight):
            raise ValueError(f"{instrument.ticker}: weight must be finite")
        current = merged.get(instrument.instrument_id)
        if current is None:
            merged[instrument.instrument_id] = (instrument, weight, vector)
            continue
        previous_instrument, previous_weight, previous_vector = current
        if previous_vector != vector:
            raise ValueError(f"{instrument.ticker}: two exposure vectors for one instrument")
        merged[instrument.instrument_id] = (previous_instrument, previous_weight + weight, previous_vector)

    items = list(merged.values())
    raw_weights = np.array([weight for _, weight, _ in items], dtype=np.float64)
    invested = float(raw_weights.sum())
    if invested == 0.0:
        raise ValueError("portfolio weights sum to zero")
    shares = raw_weights / invested

    country: dict[str, float] = {}
    sector: dict[str, float] = {}
    ccy: dict[str, float] = {}
    asset_class: dict[str, float] = {}
    underlyings: set[str] = set()
    hhi = 0.0
    top10 = 0.0
    for share, (instrument, weight, vector) in zip(shares, items, strict=True):
        if vector.effective_n <= 0.0:
            raise ValueError(f"{instrument.ticker}: effective_n must be positive")
        hhi += (share * share) / vector.effective_n
        top10 += share * vector.top10_weight
        futures = instrument.structure == "futures"
        underlying = vector.underlying or instrument.underlying
        if futures:
            if not underlying:
                raise ValueError(f"{instrument.ticker}: futures exposure needs an underlying")
            underlyings.add(underlying)
        else:
            for key, value in vector.country.items():
                _add(country, key, weight * value)
        for key, value in vector.sector.items():
            _add(sector, key, weight * value)
        for key, value in vector.ccy.items():
            _add(ccy, key, weight * value)
        for key, value in vector.asset_class.items():
            _add(asset_class, key, weight * value)

    all_futures = all(instrument.structure == "futures" for instrument, _, _ in items)
    underlying_name = next(iter(underlyings)) if all_futures and len(underlyings) == 1 else None
    return ExposureVector(
        country={} if all_futures else _trim(country),
        sector=_trim(sector),
        ccy=_trim(ccy),
        asset_class=_trim(asset_class),
        effective_n=1.0 / hhi,
        top10_weight=top10,
        as_of=min(vector.as_of for _, _, vector in items),
        source="+".join(sorted({vector.source for _, _, vector in items})),
        underlying=underlying_name,
    )
