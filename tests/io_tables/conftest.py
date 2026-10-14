"""Shared ICIO fixture: 3 economies × 3 industries in the 2025 column layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from macrosys.io_tables import IcioLayout, IoTable, parse_icio_csv
from macrosys.io_tables.layout import Economy, FinalDemand, Industry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "io_tables" / "2020.csv"

SMALL_LAYOUT = IcioLayout(
    edition="fixture",
    vintage="fixture",
    unit="USD_mn",
    economies=(
        Economy("USA", "United States"),
        Economy("DEU", "Germany"),
        Economy("ROW", "Rest of the World"),
    ),
    industries=(
        Industry("A01", "Agriculture", "01"),
        Industry("C26", "Electronics", "26"),
        Industry("D", "Electricity", "35"),
    ),
    final_demand=tuple(
        FinalDemand(code, code) for code in ("HFCE", "NPISH", "GGFC", "GFCF", "INVNT", "DPABR")
    ),
    tax_code="TLS",
    value_added_code="VA",
    output_code="OUT",
    year_from=1995,
    year_to=2022,
    zip_sources=(),
    readme_url="",
)


@pytest.fixture(scope="module")
def small_layout() -> IcioLayout:
    return SMALL_LAYOUT


@pytest.fixture(scope="module")
def icio_table(small_layout: IcioLayout) -> IoTable:
    return parse_icio_csv(FIXTURE, small_layout)
