"""Inter-country input-output tables: OECD ICIO loader and Leontief algebra (spec §3.3, §4.2).

W1.2. Currency unit strings live on ``IoTable`` (``USD_mn``); convert scale with
``macrosys.core.units.convert``. ``download_icio`` is a network call and is not for tests.
"""

from macrosys.io_tables.download import download_icio
from macrosys.io_tables.icio import (
    IoTable,
    IoTableError,
    check_column_identity,
    check_row_identity,
    parse_icio_csv,
)
from macrosys.io_tables.layout import (
    ICIO_2025,
    Economy,
    FinalDemand,
    IcioLayout,
    Industry,
    ZipSource,
)
from macrosys.io_tables.leontief import (
    LeontiefError,
    aggregate,
    cost_shares,
    domar_weights,
    import_content,
    leontief_inverse,
    technical_coefficients,
    total_requirements,
    upstreamness,
    value_added_multipliers,
)

__all__ = [
    "ICIO_2025",
    "Economy",
    "FinalDemand",
    "IcioLayout",
    "Industry",
    "IoTable",
    "IoTableError",
    "LeontiefError",
    "ZipSource",
    "aggregate",
    "check_column_identity",
    "check_row_identity",
    "cost_shares",
    "domar_weights",
    "download_icio",
    "import_content",
    "leontief_inverse",
    "parse_icio_csv",
    "technical_coefficients",
    "total_requirements",
    "upstreamness",
    "value_added_multipliers",
]
