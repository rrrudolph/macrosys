"""Records the core contracts do not carry yet (spec §9.1 holdings and corporate actions, §9.6 MT5).

`PriceBar` has no place for an MT5 session close, so `Mt5PriceBar` adds `session_note`. Promoting that
field onto `macrosys.core.contracts.PriceBar` is a requested core change, not something this package edits.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from macrosys.core.contracts import PriceBar

AssetClass = Literal["equity", "fixed_income", "cash", "derivative", "commodity", "fund", "other"]


class HoldingSnapshotRow(BaseModel):
    """One N-PORT (or issuer) holding line (spec §9.1 holdings snapshot, §9.6 SEC N-PORT)."""

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
    market_value: float | None
    ccy_mv: str | None


class CorporateActionRow(BaseModel):
    """A split or cash distribution used to rebuild total return (spec §9.1 corporate actions)."""

    model_config = ConfigDict(frozen=True)

    instrument_id: str
    ex_date: date
    type: Literal["split", "cash_div"]
    ratio: float | None = None
    amount: float | None = None
    ccy: str | None = None


class SymbolSpec(BaseModel):
    """MT5 symbol facts needed to compare a broker bar with an exchange close (spec §9.6)."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    digits: int
    trade_sessions: list[dict[str, str]]
    contract_size: float
    currency: str
    source_id: str = "MT5"


class Mt5PriceBar(PriceBar):
    """A cross-check bar plus the close time the core `PriceBar` model cannot store (spec §9.6)."""

    session_note: str
