"""Holdings snapshots and the country/currency vocabularies they must use (spec §4.2, §9.1, §9.3).

Holdings are claims about a fund's book, not market observations. ``HoldingRow`` is the core
record in ``macrosys.core.contracts``, with country, currency, and timestamp checks added on the
way in so N-PORT and issuer files land on the same grain. Weights are NAV shares in 0..1, not
percentages. ``ingested_at`` is intentionally absent: the field list is the core record's, and
ingestion time belongs on the loader that writes the row.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from macrosys.core.contracts import AssetClass
from macrosys.core.contracts import HoldingRow as _CoreHoldingRow

# Re-exported so callers share the core asset-class vocabulary (spec §9.1).
__all__ = [
    "AssetClass",
    "HoldingRow",
    "ISO3_COUNTRIES",
    "WeightCheck",
    "check_weights",
    "normalize_ccy",
    "normalize_country",
]

WeightFlag = Literal["ok", "weight_sum_out_of_band", "derivatives_present"]

# Curated economies that show up in ETF holdings, plus every current euro-area member.
# ``EA`` is the euro area aggregate from spec §9.3 (not an ISO 3166 code).
# Columns: iso3, iso2, canonical English name, extra aliases.
_COUNTRY_ROWS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("USA", "US", "united states", ("united states of america", "america")),
    ("CHN", "CN", "china", ("peoples republic of china", "prc", "mainland china")),
    ("JPN", "JP", "japan", ()),
    ("DEU", "DE", "germany", ("deutschland",)),
    ("GBR", "GB", "united kingdom", ("uk", "great britain", "britain")),
    ("FRA", "FR", "france", ()),
    ("IND", "IN", "india", ()),
    ("ITA", "IT", "italy", ()),
    ("BRA", "BR", "brazil", ()),
    ("CAN", "CA", "canada", ()),
    ("RUS", "RU", "russia", ("russian federation",)),
    ("KOR", "KR", "south korea", ("korea", "republic of korea", "korea republic")),
    ("AUS", "AU", "australia", ()),
    ("ESP", "ES", "spain", ()),
    ("MEX", "MX", "mexico", ()),
    ("IDN", "ID", "indonesia", ()),
    ("NLD", "NL", "netherlands", ("holland", "the netherlands")),
    ("SAU", "SA", "saudi arabia", ()),
    ("TUR", "TR", "turkey", ("turkiye", "türkiye")),
    ("CHE", "CH", "switzerland", ()),
    ("TWN", "TW", "taiwan", ("chinese taipei",)),
    ("POL", "PL", "poland", ()),
    ("BEL", "BE", "belgium", ()),
    ("SWE", "SE", "sweden", ()),
    ("ARG", "AR", "argentina", ()),
    ("THA", "TH", "thailand", ()),
    ("NOR", "NO", "norway", ()),
    ("ISR", "IL", "israel", ()),
    ("IRL", "IE", "ireland", ()),
    ("AUT", "AT", "austria", ()),
    ("NGA", "NG", "nigeria", ()),
    ("ARE", "AE", "united arab emirates", ("uae",)),
    ("ZAF", "ZA", "south africa", ()),
    ("DNK", "DK", "denmark", ()),
    ("SGP", "SG", "singapore", ()),
    ("MYS", "MY", "malaysia", ()),
    ("HKG", "HK", "hong kong", ()),
    ("PHL", "PH", "philippines", ()),
    ("COL", "CO", "colombia", ()),
    ("EGY", "EG", "egypt", ()),
    ("VNM", "VN", "vietnam", ("viet nam",)),
    ("CHL", "CL", "chile", ()),
    ("FIN", "FI", "finland", ()),
    ("CZE", "CZ", "czech republic", ("czechia",)),
    ("ROU", "RO", "romania", ()),
    ("PRT", "PT", "portugal", ()),
    ("NZL", "NZ", "new zealand", ()),
    ("PER", "PE", "peru", ()),
    ("GRC", "GR", "greece", ("el",)),
    ("HUN", "HU", "hungary", ()),
    ("QAT", "QA", "qatar", ()),
    ("KAZ", "KZ", "kazakhstan", ()),
    ("UKR", "UA", "ukraine", ()),
    ("DZA", "DZ", "algeria", ()),
    ("KWT", "KW", "kuwait", ()),
    ("MAR", "MA", "morocco", ()),
    ("SVK", "SK", "slovakia", ()),
    ("BGR", "BG", "bulgaria", ()),
    ("HRV", "HR", "croatia", ()),
    ("PAK", "PK", "pakistan", ()),
    ("BGD", "BD", "bangladesh", ()),
    ("LUX", "LU", "luxembourg", ()),
    ("SVN", "SI", "slovenia", ()),
    ("EST", "EE", "estonia", ()),
    ("LVA", "LV", "latvia", ()),
    ("LTU", "LT", "lithuania", ()),
    ("CYP", "CY", "cyprus", ()),
    ("MLT", "MT", "malta", ()),
    ("ISL", "IS", "iceland", ()),
    ("SRB", "RS", "serbia", ()),
    ("EA", "", "euro area", ("eurozone", "euro zone", "euro-area", "ea")),
)

# ISO 4217 codes plus unambiguous English aliases. Bare "dollar" is not mapped.
# [J] The yen sign maps to JPY; offshore yuan stays CNH rather than collapsing into CNY.
_CCY_ALIASES: dict[str, tuple[str, ...]] = {
    "USD": ("usd", "us dollar", "us dollars", "u.s. dollar"),
    "EUR": ("eur", "euro", "euros"),
    "JPY": ("jpy", "yen", "japanese yen"),
    "GBP": ("gbp", "pound", "sterling", "pound sterling"),
    "CNY": ("cny", "yuan", "renminbi", "rmb"),
    "CNH": ("cnh", "offshore yuan", "offshore renminbi"),
    "CHF": ("chf", "swiss franc"),
    "CAD": ("cad", "canadian dollar"),
    "AUD": ("aud", "australian dollar"),
    "KRW": ("krw", "won", "korean won"),
    "TWD": ("twd", "new taiwan dollar", "taiwan dollar"),
    "INR": ("inr", "rupee", "indian rupee"),
    "BRL": ("brl", "real", "brazilian real"),
    "MXN": ("mxn", "mexican peso"),
    "SEK": ("sek", "swedish krona"),
    "NOK": ("nok", "norwegian krone"),
    "DKK": ("dkk", "danish krone"),
    "ZAR": ("zar", "rand", "south african rand"),
    "TRY": ("try", "turkish lira"),
    "SGD": ("sgd", "singapore dollar"),
    "HKD": ("hkd", "hong kong dollar"),
    "NZD": ("nzd", "new zealand dollar"),
    "PLN": ("pln", "zloty", "polish zloty"),
    "IDR": ("idr", "rupiah", "indonesian rupiah"),
    "SAR": ("sar", "saudi riyal"),
    "AED": ("aed", "dirham", "uae dirham"),
    "THB": ("thb", "baht", "thai baht"),
    "MYR": ("myr", "ringgit", "malaysian ringgit"),
    "PHP": ("php", "philippine peso"),
    "ILS": ("ils", "shekel", "israeli shekel"),
    "CLP": ("clp", "chilean peso"),
    "COP": ("cop", "colombian peso"),
    "PEN": ("pen", "sol", "peruvian sol"),
    "ARS": ("ars", "argentine peso"),
    "VND": ("vnd", "dong", "vietnamese dong"),
    "EGP": ("egp", "egyptian pound"),
    "NGN": ("ngn", "naira"),
    "QAR": ("qar", "qatari riyal"),
    "KWD": ("kwd", "kuwaiti dinar"),
    "MAD": ("mad", "moroccan dirham"),
    "DZD": ("dzd", "algerian dinar"),
    "RUB": ("rub", "ruble", "russian ruble"),
    "KZT": ("kzt", "tenge"),
    "UAH": ("uah", "hryvnia"),
    "RON": ("ron", "romanian leu"),
    "BGN": ("bgn", "bulgarian lev"),
    "HRK": ("hrk", "kuna"),
    "PKR": ("pkr", "pakistani rupee"),
    "BDT": ("bdt", "taka", "bangladeshi taka"),
    "CZK": ("czk", "czech koruna"),
    "HUF": ("huf", "forint", "hungarian forint"),
    "ISK": ("isk", "icelandic krona"),
}
_CCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}

_WEIGHT_LO = 0.95
_WEIGHT_HI = 1.05


def _fold(value: str) -> str:
    cleaned = value.strip().casefold().replace("'", "").replace("’", "").replace(".", "")
    cleaned = cleaned.replace("_", " ").replace("-", " ")
    return " ".join(cleaned.split())


def _build_countries() -> tuple[frozenset[str], dict[str, str]]:
    iso3s: set[str] = set()
    lookup: dict[str, str] = {}
    for iso3, iso2, name, extras in _COUNTRY_ROWS:
        iso3s.add(iso3)
        keys = {iso3, name, *extras}
        if iso2:
            keys.add(iso2)
        for key in keys:
            folded = _fold(key)
            previous = lookup.get(folded)
            if previous is not None and previous != iso3:
                raise RuntimeError(f"country alias {key!r} maps to both {previous} and {iso3}")
            lookup[folded] = iso3
    return frozenset(iso3s), lookup


def _build_ccy() -> tuple[frozenset[str], dict[str, str]]:
    lookup: dict[str, str] = {}
    for code, aliases in _CCY_ALIASES.items():
        for alias in aliases:
            folded = _fold(alias)
            previous = lookup.get(folded)
            if previous is not None and previous != code:
                raise RuntimeError(f"currency alias {alias!r} maps to both {previous} and {code}")
            lookup[folded] = code
    return frozenset(_CCY_ALIASES), lookup


ISO3_COUNTRIES, _COUNTRY_LOOKUP = _build_countries()
ISO4217_CCY, _CCY_LOOKUP = _build_ccy()


def normalize_country(value: str) -> str:
    """Map an ISO2 code or English name to ISO3, or ``EA`` for the euro area (spec §9.3).

    The table is a curated set of economies that appear in ETF holdings (the euro-area members
    plus the large developed and emerging markets), not the full ISO 3166 list. Unknown names
    raise ``ValueError`` rather than passing through, so a new spelling becomes a data change.
    Bare ``Korea`` means the Republic of Korea. ``EU`` is not the euro area and is rejected.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"country must be a non-empty string, got {value!r}")
    folded = _fold(value)
    try:
        return _COUNTRY_LOOKUP[folded]
    except KeyError:
        raise ValueError(f"unknown country {value!r}") from None


def normalize_ccy(value: str) -> str:
    """Map a currency alias or symbol to an ISO 4217 code (spec §9.3).

    Accepts the curated code list and unambiguous names (``euro``, ``yen``, ``RMB``).
    ``CNH`` stays distinct from ``CNY``. A bare dollar sign is USD; the word ``dollar`` alone
    is rejected because it is shared. Unknown codes raise ``ValueError``.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"currency must be a non-empty string, got {value!r}")
    symbol = value.strip()
    if symbol in _CCY_SYMBOLS:
        return _CCY_SYMBOLS[symbol]
    folded = _fold(symbol)
    try:
        return _CCY_LOOKUP[folded]
    except KeyError:
        raise ValueError(f"unknown currency {value!r}") from None


class HoldingRow(_CoreHoldingRow):
    """Core holdings record, plus the instruments checks on the way in (spec §4.2, §9.1, §9.3).

    The fields are ``macrosys.core.contracts.HoldingRow``. Country and currency are normalised to
    ISO3 / ``EA`` and ISO 4217, ``published_at`` must be timezone-aware, and unknown fields are
    rejected. ``weight`` is a share of NAV. ``country`` is null when the line has no country
    (cash, or a futures contract whose exposure is the underlying). Market value, when present,
    is in ``ccy_mv``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("country", mode="before")
    @classmethod
    def _country(cls, value: str | None) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return normalize_country(value)

    @field_validator("ccy", mode="before")
    @classmethod
    def _ccy(cls, value: str) -> str:
        return normalize_ccy(value)

    @field_validator("ccy_mv", mode="before")
    @classmethod
    def _ccy_mv(cls, value: str | None) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return normalize_ccy(value)

    @field_validator("weight", "market_value")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError(f"expected a finite number, got {value!r}")
        return float(value)

    @model_validator(mode="after")
    def _timestamps(self) -> HoldingRow:
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        if not self.instrument_id or not self.holding_key or not self.source:
            raise ValueError("instrument_id, holding_key, and source are required")
        return self


class WeightCheck(BaseModel):
    """Result of the holdings weight sanity check (spec §9.1).

    Long-only physical books should sum to a NAV share inside [0.95, 1.05]. Derivative lines
    (futures notionals, offsets) are allowed to break that band; their presence is itself the flag.
    """

    model_config = ConfigDict(frozen=True)

    total: float
    in_band: bool
    derivatives_present: bool
    flag: WeightFlag


def check_weights(rows: list[HoldingRow]) -> WeightCheck:
    """Sum NAV-share weights and flag a book that is not a partition of NAV (spec §9.1, §7.4).

    With no derivative lines, a total outside [0.95, 1.05] sets ``weight_sum_out_of_band``.
    If any line has ``asset_class='derivative'``, the band is waived and the flag is
    ``derivatives_present`` whether or not the total is inside the band. An empty book raises.
    """
    if not rows:
        raise ValueError("no holdings to check")
    total = float(sum(row.weight for row in rows))
    derivatives = any(row.asset_class == "derivative" for row in rows)
    in_band = _WEIGHT_LO <= total <= _WEIGHT_HI
    if derivatives:
        flag: WeightFlag = "derivatives_present"
    elif not in_band:
        flag = "weight_sum_out_of_band"
    else:
        flag = "ok"
    return WeightCheck(total=total, in_band=in_band, derivatives_present=derivatives, flag=flag)
