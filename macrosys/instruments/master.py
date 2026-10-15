"""Instrument master and OpenFIGI ticker mapping (spec §7.4, §9.1, §9.6, §11.1).

The universe file is configuration. This module loads it, rejects products the mandate does not
hold, and can fill FIGI identifiers from OpenFIGI. The API key is the environment variable
``OPENFIGI_API_KEY`` and is optional; OpenFIGI allows a smaller batch size without it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from macrosys.core.contracts import AssetClass
from macrosys.instruments.holdings import normalize_ccy, normalize_country

InstrumentType = Literal["ETF", "index", "future", "bond", "fx_pair"]
Structure = Literal["physical", "futures", "synthetic", "hedged"]

_OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
_BATCH_WITH_KEY = 100
_BATCH_WITHOUT_KEY = 10

# Exact tickers. Name and structure checks catch the rest; this list is the short, common set
# so a futures ticker like USO or UUP is not inferred to be leveraged. [J]
_BANNED_TICKERS = frozenset(
    {
        "AGQ",
        "BOIL",
        "DDM",
        "DOG",
        "DRIP",
        "DUG",
        "DUST",
        "DXD",
        "ERY",
        "ERX",
        "FAZ",
        "FAS",
        "GLL",
        "GUSH",
        "JDST",
        "JNUG",
        "KOLD",
        "LABD",
        "LABU",
        "NUGT",
        "PSQ",
        "QID",
        "QLD",
        "RWM",
        "SCO",
        "SDOW",
        "SDS",
        "SH",
        "SOXL",
        "SOXS",
        "SPXL",
        "SPXS",
        "SPXU",
        "SQQQ",
        "SRTY",
        "SSO",
        "SVIX",
        "SVXY",
        "TBT",
        "TECL",
        "TECS",
        "TMF",
        "TMV",
        "TNA",
        "TQQQ",
        "TVIX",
        "TWM",
        "TZA",
        "UBT",
        "UCO",
        "UDOW",
        "UGL",
        "UPRO",
        "URTY",
        "UVIX",
        "UVXY",
        "UWM",
        "VIX",
        "VIXM",
        "VIXY",
        "VXZ",
        "VXX",
        "ZSL",
    }
)
_BANNED_STRUCTURES = frozenset(
    {"leveraged", "levered", "inverse", "short", "vix", "vix_linked", "volatility"}
)
_VIX_RE = re.compile(r"vix", re.IGNORECASE)
_LEV_RE = re.compile(r"leverag|inverse|ultra|[23]x", re.IGNORECASE)


class DeclaredExposure(BaseModel):
    """Issuer-stated objective exposure used when no N-PORT snapshot is in the store (spec §7.4, A09).

    Each label is a provider fact about what the fund is built to hold, not a guessed holdings
    weight. A label that is present is weight 1 of that bucket. Leave a field off when the issuer
    page does not support it; the expression check then fails closed instead of inventing a share.
    ``expense_ratio`` stays on ``Instrument``. ``duration_bucket`` is a maturity label such as
    ``1_3y`` from the fund's name, not a measured duration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_class: AssetClass | None = None
    country: str | None = None
    sector: str | None = None
    commodity: str | None = None
    currency: str | None = None
    duration_bucket: str | None = None

    @field_validator("country", mode="before")
    @classmethod
    def _country(cls, value: object) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return normalize_country(str(value))

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: object) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return normalize_ccy(str(value))

    @field_validator("sector", "commodity", "duration_bucket", mode="before")
    @classmethod
    def _label(cls, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip().casefold()
        return text or None


class MandateExclusionError(ValueError):
    """Raised when an instrument is leveraged, inverse, or VIX-linked (spec §7.4, §11.1)."""


class MandateExclusions:
    """Mandate screen for the v0 universe (spec §7.4, §11.1).

    Leveraged and inverse products are excluded because their path dependence breaks the weekly
    decision assumption. VIX-linked ETNs are excluded with them. Physical, futures, synthetic,
    and currency-hedged structures are allowed; futures-based commodity funds are not leverage.
    """

    @staticmethod
    def check(row: Mapping[str, Any]) -> None:
        """Raise ``MandateExclusionError`` if ``row`` is outside the mandate. Accepts a mapping."""
        ticker = str(row.get("ticker") or "").upper().strip()
        structure = str(row.get("structure") or "").casefold().strip()
        name = str(row.get("name") or "")
        reasons: list[str] = []
        if structure in _BANNED_STRUCTURES:
            reasons.append(f"structure {structure}")
        if ticker in _BANNED_TICKERS or (ticker and _VIX_RE.search(ticker)):
            reasons.append(f"ticker {ticker}")
        if name and (_VIX_RE.search(name) or _LEV_RE.search(name)):
            reasons.append("name")
        for key in ("leveraged", "inverse", "vix_linked"):
            if row.get(key) is True:
                reasons.append(key)
        leverage = row.get("leverage", row.get("leverage_factor"))
        if leverage is not None and abs(float(leverage) - 1.0) > 1e-9:
            reasons.append(f"leverage {leverage}")
        if reasons:
            label = ticker or "?"
            raise MandateExclusionError(
                f"{label}: excluded by mandate §7.4 ({', '.join(reasons)})"
            )

    @staticmethod
    def check_instrument(instrument: Instrument) -> None:
        """Screen an already-built instrument (ticker denylist and VIX spelling)."""
        MandateExclusions.check(instrument.model_dump())


class Instrument(BaseModel):
    """One tradable listing in the master (spec §4.1, §7.4, §9.1).

    ``structure`` is physical, futures, synthetic, or hedged. A hedged share class points at the
    unhedged listing via ``hedged_pair_of`` and is a different instrument (spec §5.4).
    ``underlying`` is the commodity or contract a futures-based fund tracks; country exposure for
    that fund is empty (A09). ``expense_ratio`` is an annual NAV share, not a percent.
    ``exposure`` is the issuer objective used only when the store has no holdings snapshot.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    ticker: str
    isin: str | None = None
    figi: str | None = None
    type: InstrumentType = "ETF"
    structure: Structure
    hedged_pair_of: str | None = None
    base_ccy: str = "USD"
    expense_ratio: float | None = None
    benchmark: str | None = None
    issuer: str | None = None
    listing_tz: str | None = None
    role: str | None = None
    underlying: str | None = None
    exposure: DeclaredExposure | None = None

    @field_validator("base_ccy", mode="before")
    @classmethod
    def _base_ccy(cls, value: str) -> str:
        return normalize_ccy(value)

    @model_validator(mode="after")
    def _consistent(self) -> Instrument:
        if not self.instrument_id or not self.ticker:
            raise ValueError("instrument_id and ticker are required")
        if self.structure == "hedged" and not self.hedged_pair_of:
            raise ValueError(f"{self.ticker}: hedged structure requires hedged_pair_of")
        if self.structure != "hedged" and self.hedged_pair_of:
            raise ValueError(f"{self.ticker}: hedged_pair_of is only valid when structure is hedged")
        if self.expense_ratio is not None and not 0.0 <= self.expense_ratio < 1.0:
            raise ValueError(
                f"{self.ticker}: expense_ratio must be an annual NAV share in [0, 1), got {self.expense_ratio}"
            )
        return self


def load_universe(path: str | Path) -> list[Instrument]:
    """Load instruments from the universe YAML format in ``config/universe.example.yaml`` (spec §9.1, §11.1).

    Each row needs ``ticker`` and ``structure``. ``instrument_id`` defaults to the ticker, ``type``
    to ETF, and ``base_ccy`` to USD. An ``exposure`` mapping becomes ``DeclaredExposure``. Rows are
    screened with ``MandateExclusions`` before they become instruments. A hedged row's
    ``hedged_pair_of`` must name another ticker in the same file.
    """
    file_path = Path(path)
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("instruments"), list):
        raise ValueError(f"{file_path}: expected a mapping with an instruments list")
    rows: list[Mapping[str, Any]] = []
    for raw in loaded["instruments"]:
        if not isinstance(raw, dict):
            raise ValueError(f"{file_path}: each instrument must be a mapping")
        if "ticker" not in raw or "structure" not in raw:
            raise ValueError(f"{file_path}: each instrument needs ticker and structure")
        MandateExclusions.check(raw)
        rows.append(raw)
    tickers = {str(row["ticker"]) for row in rows}
    instruments: list[Instrument] = []
    for row in rows:
        ticker = str(row["ticker"])
        pair = row.get("hedged_pair_of")
        if pair is not None and str(pair) not in tickers:
            raise ValueError(f"{ticker}: hedged_pair_of {pair} is not in the universe file")
        raw_exposure = row.get("exposure")
        exposure = None if raw_exposure is None else DeclaredExposure.model_validate(raw_exposure)
        instruments.append(
            Instrument(
                instrument_id=str(row.get("instrument_id") or ticker),
                ticker=ticker,
                isin=row.get("isin"),
                figi=row.get("figi"),
                type=row.get("type") or "ETF",
                structure=str(row["structure"]).casefold(),
                hedged_pair_of=None if pair is None else str(pair),
                base_ccy=str(row.get("base_ccy") or "USD"),
                expense_ratio=row.get("expense_ratio"),
                benchmark=row.get("benchmark"),
                issuer=row.get("issuer"),
                listing_tz=row.get("listing_tz"),
                role=row.get("role"),
                underlying=row.get("underlying"),
                exposure=exposure,
            )
        )
    return instruments


def _pick_figi(hits: list[dict[str, Any]]) -> str | None:
    """Prefer an ETP/ETF composite FIGI when OpenFIGI returns several matches."""
    if not hits:
        return None

    def rank(hit: dict[str, Any]) -> int:
        label = str(hit.get("securityType") or hit.get("securityType2") or "").upper()
        return 0 if any(token in label for token in ("ETP", "ETF", "FUND")) else 1

    best = sorted(hits, key=rank)[0]
    figi = best.get("compositeFIGI") or best.get("figi")
    return str(figi) if figi else None


class OpenFigiClient:
    """Map tickers to composite FIGIs via OpenFIGI ``/v3/mapping`` (spec §9.1, §9.6).

    The API key is read from the argument or from the environment variable ``OPENFIGI_API_KEY``.
    Without a key, requests are batched at 10 jobs; with a key, at 100. Pass an ``httpx``
    transport (tests use ``httpx.MockTransport``) so this client is never required to open a
    socket. Jobs send ``idType=TICKER`` and, by default, ``exchCode=US``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout

    def _key(self) -> str | None:
        key = self._api_key if self._api_key is not None else os.environ.get("OPENFIGI_API_KEY")
        if key is not None and not key.strip():
            return None
        return key

    def map_tickers(self, tickers: list[str], *, exch_code: str | None = "US") -> dict[str, str | None]:
        """Return ``{ticker: composite FIGI or None}`` for each requested ticker (spec §9.1).

        ``None`` means OpenFIGI returned no identifier. The composite FIGI is preferred over the
        listing-level FIGI. Tickers are returned in request order; duplicates are queried once.
        """
        if not tickers:
            return {}
        key = self._key()
        batch_size = _BATCH_WITH_KEY if key else _BATCH_WITHOUT_KEY
        unique: list[str] = []
        seen: set[str] = set()
        for ticker in tickers:
            if ticker not in seen:
                unique.append(ticker)
                seen.add(ticker)
        found: dict[str, str | None] = {}
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-OPENFIGI-APIKEY"] = key
        with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
            for start in range(0, len(unique), batch_size):
                chunk = unique[start : start + batch_size]
                jobs: list[dict[str, str]] = []
                for ticker in chunk:
                    job = {"idType": "TICKER", "idValue": ticker}
                    if exch_code:
                        job["exchCode"] = exch_code
                    jobs.append(job)
                response = client.post(_OPENFIGI_URL, json=jobs, headers=headers)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list) or len(payload) != len(chunk):
                    raise RuntimeError(
                        f"OpenFIGI returned {len(payload) if isinstance(payload, list) else 'non-list'} "
                        f"rows for {len(chunk)} jobs"
                    )
                for ticker, item in zip(chunk, payload, strict=True):
                    if not isinstance(item, dict) or "data" not in item:
                        found[ticker] = None
                        continue
                    data = item.get("data")
                    found[ticker] = _pick_figi(data) if isinstance(data, list) else None
        return {ticker: found[ticker] for ticker in tickers}
