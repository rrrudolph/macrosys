"""Aliases for holdings, corporate actions, and symbol specs (spec §9.1, §9.6).

The records live in `macrosys.core.contracts`. These names stay so existing connector tests keep
importing `HoldingSnapshotRow` and `CorporateActionRow`.
"""

from macrosys.core.contracts import AssetClass, CorporateAction, HoldingRow, SymbolSpec

HoldingSnapshotRow = HoldingRow
CorporateActionRow = CorporateAction

__all__ = [
    "AssetClass",
    "CorporateAction",
    "CorporateActionRow",
    "HoldingRow",
    "HoldingSnapshotRow",
    "SymbolSpec",
]
