"""Seams between layers (spec §8.5). Other packages implement these; nothing here has behaviour.

Records (Pydantic) are the wire format between packages. Protocols are structural — any object with the
right methods satisfies them, which keeps tests cheap (fakes instead of mocks).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from macrosys.core.quantity import Quantity

QualityFlag = Literal["ok", "estimated", "parser_changed", "imputed", "unofficial", "reconstructed_pubtime"]
SourceTier = Literal["record", "cross_check", "research"]
AssetClass = Literal["equity", "fixed_income", "cash", "derivative", "commodity", "fund", "other"]
ActionType = Literal["split", "cash_div", "capital_gain"]


# ---------------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------------


class RawPayload(BaseModel):
    """An immutable captured response. Stored under raw/<source>/<yyyy>/<mm>/<sha256>; parsers run over it."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    request: dict[str, Any]  # url, params, headers (secrets redacted)
    fetched_at: datetime
    status: int
    content_type: str | None
    sha256: str
    path: str  # relative path inside the raw store
    size_bytes: int


class SeriesMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    series_id: str  # '<SOURCE>:<native id>' e.g. 'FRED:GDPC1', 'ECB:EXR.D.USD.EUR.SP00.A'
    source_id: str
    native_id: str
    title: str
    unit: str
    frequency: Literal["D", "W", "M", "Q", "A", "irregular"]
    seasonal_adj: str | None = None
    geography: str | None = None  # ISO-3166 alpha-3 or 'EA'
    concept: str | None = None  # maps to a mechanism input name
    release_id: str | None = None
    typical_lag_days: int | None = None
    revision_policy: str | None = None
    license: str | None = None
    tier: SourceTier = "record"


class Observation(BaseModel):
    """One (series, reference period, vintage) row. Append-only; a revision is a new vintage (spec §9.1)."""

    model_config = ConfigDict(frozen=True)

    series_id: str
    ref_period_start: date
    ref_period_end: date
    value: float | None
    unit: str
    published_at: datetime
    ingested_at: datetime
    vintage_id: str  # ALFRED realtime_start, or ingest hash for sources without vintages
    source_url: str
    source_hash: str  # sha256 of the RawPayload this row was parsed from
    quality_flag: QualityFlag = "ok"


class ReleaseEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    release_id: str
    scheduled_at: datetime
    actual_at: datetime | None = None
    source_id: str


class PriceBar(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: str
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: float | None
    nav: float | None = None
    ccy: str
    source_id: str
    tier: SourceTier
    ingested_at: datetime
    quality_flag: QualityFlag = "ok"
    session_note: str | None = None  # e.g. MT5 bar close time / session; None for exchange closes


class HoldingRow(BaseModel):
    """One line of a holdings snapshot (spec §4.2 exposure, §9.1). `weight` is a share of NAV (0..1);
    `country` is ISO3 or 'EA' after normalisation, or None for cash and futures (exposure is the
    underlying); `published_at` is timezone-aware; `market_value` is in `ccy_mv` when present."""

    model_config = ConfigDict(frozen=True)

    instrument_id: str
    as_of: date
    published_at: datetime
    source: str
    holding_key: str
    name: str
    weight: float
    country: str | None
    sector: str | None
    ccy: str
    asset_class: AssetClass
    market_value: float | None = None
    ccy_mv: str | None = None


class CorporateAction(BaseModel):
    """A split or distribution used to rebuild total return (spec §9.1). `ratio` is new shares per old
    share for a split (2.0 = 2-for-1), else None; `amount` is cash per ex-date share in `ccy` for
    `cash_div` / `capital_gain`."""

    model_config = ConfigDict(frozen=True)

    instrument_id: str
    ex_date: date
    type: ActionType
    ratio: float | None = None
    amount: float | None = None
    ccy: str | None = None


class SymbolSpec(BaseModel):
    """Broker/exchange symbol facts needed to compare a quote with an exchange close (spec §9.6 tiering)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    digits: int
    trade_sessions: list[dict[str, str]]
    contract_size: float
    currency: str
    source_id: str


class EntityKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_type: Literal["economy", "industry", "commodity", "institution", "currency", "instrument", "portfolio"]
    entity_id: str  # e.g. 'USA', 'USA:C26' (ICIO industry), 'Cu', 'FED', 'USD', 'CPER'


class Gate(BaseModel):
    """Result of a mechanism's applicability check against the world state (spec §3.4 rule 5)."""

    model_config = ConfigDict(frozen=True)

    status: Literal["on", "off", "scaled"]
    factor: float = 1.0
    reason: str


# ---------------------------------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------------------------------


@runtime_checkable
class RawStore(Protocol):
    def put(self, source_id: str, request: dict[str, Any], content: bytes, *, status: int, content_type: str | None, fetched_at: datetime) -> RawPayload: ...
    def get(self, payload: RawPayload) -> bytes: ...
    def exists(self, sha256: str) -> bool: ...


@runtime_checkable
class ObservationSink(Protocol):
    def append(self, rows: list[Observation]) -> int:
        """Append rows; duplicates on the natural key are ignored. Returns rows actually inserted."""
        ...


@runtime_checkable
class DataSource(Protocol):
    source_id: str
    tier: SourceTier

    def discover(self) -> list[SeriesMeta]: ...
    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload: ...
    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Pure function of the payload bytes. Versioned; a schema change must raise ParserChangedError."""
        ...
    def schedule(self) -> list[ReleaseEvent]: ...


class ParserChangedError(RuntimeError):
    """Raised when a payload's shape does not match the parser version (spec §9.4 quarantine rule)."""


@runtime_checkable
class StateView(Protocol):
    """Point-in-time view of the world state (spec §4.3). Everything a mechanism may read."""

    as_of: datetime

    def get(self, entity: EntityKey, var: str) -> Quantity: ...
    def has(self, entity: EntityKey, var: str) -> bool: ...


@runtime_checkable
class Mechanism(Protocol):
    mechanism_id: str

    def applicable(self, state: StateView, bindings: dict[str, str], params: dict[str, Quantity] | None = None) -> Gate:
        """State gate (spec §3.4 rule 5). `params` are the bound mechanism parameters so thresholds such
        as a utilization kink come from the YAML record, not from code constants."""
        ...

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]: ...


class PathStepResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    mechanism_id: str
    inputs: dict[str, Quantity]
    outputs: dict[str, Quantity]
    gate: Gate
    competing_accounted: list[str] = Field(default_factory=list)
