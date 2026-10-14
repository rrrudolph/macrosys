"""Controlled unit vocabulary (spec §2 "Units and accounting consistency", §3.4 rule 1).

A unit string is `<base>[_<scale>][_real[<year>]]` for currency amounts, or a bare physical / rate / ratio
unit. Only strings registered in `UNITS` are legal. Mechanisms refuse mismatched units; `convert` handles
scale changes within a dimension and nothing else (currency translation and deflation are explicit
operations elsewhere, with provenance — never implicit conversions here).

Dimensions
----------
- ``currency``: ``USD``, ``EUR``, ... with optional scale ``_k``/``_mn``/``_bn``/``_tn`` and optional
  ``_real`` or ``_real2017`` suffix. ``ccy`` is a placeholder dimension-only unit used in mechanism specs
  before binding to a concrete currency.
- ``mass``: ``t``, ``kt``, ``Mt`` (metric tonnes).
- ``energy``: ``MWh``, ``GWh``, ``TWh``; ``power``: ``MW``, ``GW``.
- ``volume_oil``: ``bbl``, ``kbbl``, ``mbbl``; ``flow_oil``: ``kbd`` (thousand barrels per day).
- ``rate``: ``pct`` (percent), ``pp`` (percentage points), ``bp`` (basis points) — these are *not*
  interconvertible with each other except pp<->bp (both are differences); pct is a level or growth rate.
- ``ratio``: ``share`` (0..1), ``ratio``; ``index``: ``index``; ``count``: ``count``.
- ``time``: ``d``, ``w``, ``m``, ``q``, ``y``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class UnitError(ValueError):
    """Raised when a unit string is unknown or two units are incompatible."""


@dataclass(frozen=True)
class Unit:
    label: str
    dimension: str
    scale: float = 1.0  # multiplier to the dimension's base unit
    currency: str | None = None
    real: bool = False
    base_year: int | None = None


_CURRENCIES = {
    "USD", "EUR", "JPY", "GBP", "CNY", "CHF", "CAD", "AUD", "KRW", "TWD", "INR", "BRL", "MXN", "SEK",
    "NOK", "ZAR", "TRY", "SGD", "HKD", "NZD", "PLN", "IDR", "SAR", "AED", "ccy",
}
_SCALES = {"": 1.0, "k": 1e3, "mn": 1e6, "bn": 1e9, "tn": 1e12}
_CCY_RE = re.compile(r"^(?P<ccy>[A-Za-z]{3})(?:_(?P<scale>k|mn|bn|tn))?(?:_real(?P<year>\d{4})?)?$")

_STATIC: dict[str, Unit] = {
    # mass
    "t": Unit("t", "mass", 1.0),
    "kt": Unit("kt", "mass", 1e3),
    "Mt": Unit("Mt", "mass", 1e6),
    # energy / power
    "MWh": Unit("MWh", "energy", 1.0),
    "GWh": Unit("GWh", "energy", 1e3),
    "TWh": Unit("TWh", "energy", 1e6),
    "MW": Unit("MW", "power", 1.0),
    "GW": Unit("GW", "power", 1e3),
    # oil
    "bbl": Unit("bbl", "volume_oil", 1.0),
    "kbbl": Unit("kbbl", "volume_oil", 1e3),
    "mbbl": Unit("mbbl", "volume_oil", 1e6),
    "kbd": Unit("kbd", "flow_oil", 1.0),
    # rates and differences
    "pct": Unit("pct", "rate_level", 1.0),
    "pp": Unit("pp", "rate_diff", 1.0),
    "bp": Unit("bp", "rate_diff", 0.01),
    # ratios, indices, counts
    "share": Unit("share", "ratio", 1.0),
    "ratio": Unit("ratio", "ratio", 1.0),
    "index": Unit("index", "index", 1.0),
    "count": Unit("count", "count", 1.0),
    # time
    "d": Unit("d", "time", 1.0),
    "w": Unit("w", "time", 7.0),
    "m": Unit("m", "time", 30.4375),
    "q": Unit("q", "time", 91.3125),
    "y": Unit("y", "time", 365.25),
}


def _parse_currency(label: str) -> Unit | None:
    m = _CCY_RE.match(label)
    if not m:
        return None
    ccy = m.group("ccy")
    if ccy not in _CURRENCIES:
        return None
    scale = _SCALES[m.group("scale") or ""]
    real = "_real" in label
    year = int(m.group("year")) if m.group("year") else None
    return Unit(label, "currency", scale, currency=ccy, real=real, base_year=year)


class _UnitRegistry(dict[str, Unit]):
    """Dict of static units plus on-demand parsing of currency units."""

    def __missing__(self, label: str) -> Unit:
        unit = _parse_currency(label)
        if unit is None:
            raise UnitError(f"unknown unit {label!r}")
        self[label] = unit
        return unit

    def __contains__(self, label: object) -> bool:  # type: ignore[override]
        if not isinstance(label, str):
            return False
        if dict.__contains__(self, label):
            return True
        return _parse_currency(label) is not None


UNITS: _UnitRegistry = _UnitRegistry(_STATIC)


def assert_unit(label: str) -> Unit:
    """Return the Unit for `label` or raise UnitError. Use at every boundary where a unit string enters."""
    if not isinstance(label, str) or not label:
        raise UnitError(f"unit must be a non-empty string, got {label!r}")
    return UNITS[label]


def compatible(a: str, b: str) -> bool:
    """True if `a` and `b` share a dimension and, for currencies, the same currency and real/nominal flag.

    Scale differences (bn vs mn) are compatible; USD vs EUR, nominal vs real, and pct vs pp are not.
    """
    ua, ub = assert_unit(a), assert_unit(b)
    if ua.dimension != ub.dimension:
        return False
    if ua.dimension == "currency":
        if "ccy" in (ua.currency, ub.currency):
            return ua.real == ub.real  # placeholder binds to any currency
        return ua.currency == ub.currency and ua.real == ub.real and ua.base_year == ub.base_year
    return True


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Scale conversion within one dimension (e.g. USD_bn -> USD_mn, kt -> t, bp -> pp).

    Refuses cross-dimension, cross-currency, and real/nominal conversions — those need explicit
    operations with provenance.
    """
    if not compatible(from_unit, to_unit):
        raise UnitError(f"cannot convert {from_unit!r} to {to_unit!r}")
    ua, ub = assert_unit(from_unit), assert_unit(to_unit)
    return value * ua.scale / ub.scale
