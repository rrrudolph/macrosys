"""Data-source connectors (spec §9.1–9.6).

`SOURCES` maps each implemented registry `source_id` to its connector class. Rate limits and tiers
are read from `config/source_registry.yaml` inside those classes.
"""

from macrosys.sources.cftc import CftcSource
from macrosys.sources.ecb import EcbSource
from macrosys.sources.fred import FredSource
from macrosys.sources.mt5 import MT5Source
from macrosys.sources.sec import SecSource
from macrosys.sources.tiingo import TiingoSource
from macrosys.sources.tradingview import TradingViewSource
from macrosys.sources.treasury import FedGswSource, TreasurySource

SOURCES: dict[str, type] = {
    FredSource.source_id: FredSource,
    EcbSource.source_id: EcbSource,
    CftcSource.source_id: CftcSource,
    SecSource.source_id: SecSource,
    TreasurySource.source_id: TreasurySource,
    FedGswSource.source_id: FedGswSource,
    TiingoSource.source_id: TiingoSource,
    MT5Source.source_id: MT5Source,
    TradingViewSource.source_id: TradingViewSource,
}

__all__ = [
    "SOURCES",
    "CftcSource",
    "EcbSource",
    "FedGswSource",
    "FredSource",
    "MT5Source",
    "SecSource",
    "TiingoSource",
    "TradingViewSource",
    "TreasurySource",
]
