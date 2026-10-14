"""OECD ICIO 2025 download URLs (spec §9.6).

Confirmed from the dataset page on 2026-10-14. The link labelled "ReadMe (regular csv)"
points at an xlsx workbook, not a csv. Regular-edition zips are one file per year block;
each zip holds one CSV per year. Extended-edition URLs (China and Mexico processing splits)
are a different layout and are not used by this loader.
"""

from __future__ import annotations

# [M] https://www.oecd.org/en/data/datasets/inter-country-input-output-tables.html
README_XLSX_URL = "https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/ReadMe_ICIO_small.xlsx"

# Inclusive (year_from, year_to, url). The blocks partition 1995–2022.
REGULAR_ZIP_URLS: tuple[tuple[int, int, str], ...] = (
    (1995, 2000, "https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/1995-2000_SML.zip"),
    (2001, 2005, "https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/2001-2005_SML.zip"),
    (2006, 2010, "https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/2006-2010_SML.zip"),
    (2011, 2015, "https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/2011-2015_SML.zip"),
    (2016, 2022, "https://webfs-sti.oecd.org/files/STI-PIE/ICIO/2025/2016-2022_SML.zip"),
)
