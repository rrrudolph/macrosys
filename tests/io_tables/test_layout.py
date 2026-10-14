"""ICIO 2025 vocabulary matches the OECD ReadMe, and zip URLs match the dataset page."""

from __future__ import annotations

import pytest

from macrosys.io_tables import ICIO_2025


def test_edition_covers_readme_economies_and_industries() -> None:
    layout = ICIO_2025
    assert layout.edition == "2025"
    assert layout.vintage == "OECD_ICIO_2025"
    assert layout.unit == "USD_mn"
    assert layout.year_from == 1995
    assert layout.year_to == 2022
    assert len(layout.economies) == 81
    assert layout.economies[0].code == "AGO"
    assert layout.economies[-1].code == "ROW"
    assert {e.code for e in layout.economies} >= {"AGO", "ARE", "COD", "STP", "USA", "DEU", "CHN", "MEX"}
    assert len(layout.industries) == 50
    by_code = {ind.code: ind for ind in layout.industries}
    assert by_code["C26"].isic_rev4 == "26"
    assert by_code["C26"].description.startswith("Manufacture of computer")
    assert by_code["D"].description.startswith("Electricity")
    assert by_code["D"].isic_rev4 == "35"
    assert by_code["C24A"].isic_rev4 == "241, 2431"
    assert by_code["C301"].description.startswith("Building of ships")
    assert by_code["C302T309"].isic_rev4 == "302 to 309"
    assert all(ind.isic_rev4 and ind.description for ind in layout.industries)
    assert [fd.code for fd in layout.final_demand] == ["HFCE", "NPISH", "GGFC", "GFCF", "INVNT", "DPABR"]
    assert layout.tax_code == "TLS"
    assert layout.value_added_code == "VA"
    assert layout.output_code == "OUT"
    assert len(layout.industry_labels()) == 81 * 50
    assert layout.industry_labels()[0] == "AGO_A01"
    assert layout.industry_labels()[-1] == "ROW_T"
    assert layout.final_demand_labels()[0] == "AGO_HFCE"
    assert layout.final_demand_labels()[-1] == "ROW_DPABR"


def test_zip_urls_partition_1995_through_2022() -> None:
    assert ICIO_2025.zip_url(1995).endswith("/1995-2000_SML.zip")
    assert ICIO_2025.zip_url(2000).endswith("/1995-2000_SML.zip")
    assert ICIO_2025.zip_url(2001).endswith("/2001-2005_SML.zip")
    assert ICIO_2025.zip_url(2005).endswith("/2001-2005_SML.zip")
    assert ICIO_2025.zip_url(2006).endswith("/2006-2010_SML.zip")
    assert ICIO_2025.zip_url(2010).endswith("/2006-2010_SML.zip")
    assert ICIO_2025.zip_url(2011).endswith("/2011-2015_SML.zip")
    assert ICIO_2025.zip_url(2015).endswith("/2011-2015_SML.zip")
    assert ICIO_2025.zip_url(2016).endswith("/2016-2022_SML.zip")
    assert ICIO_2025.zip_url(2022).endswith("/2016-2022_SML.zip")
    assert ICIO_2025.readme_url.endswith("/ReadMe_ICIO_small.xlsx")
    with pytest.raises(ValueError):
        ICIO_2025.zip_url(1994)
    with pytest.raises(ValueError):
        ICIO_2025.zip_url(2023)
