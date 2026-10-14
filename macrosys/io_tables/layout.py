"""ICIO edition layout: codes live in data, not in the parser (spec §9.3, §9.6).

The 2025 regular edition, confirmed against the OECD ReadMe workbook
(``ReadMe_ICIO_small.xlsx``, sheets Structure, Area_Activities, RowItems, ColItems),
has 80 economies plus Rest of the World and 50 industries. The spec's "45 industries"
is the pre-2025 classification; this edition splits agriculture, mining, basic metals,
and ships. Gross output is labelled ``OUT`` in the CSV. The structure diagram's ``(X)``
is the accounting symbol for that same row and column, not the cell code.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from macrosys.core.units import assert_unit
from macrosys.io_tables.urls import README_XLSX_URL, REGULAR_ZIP_URLS

_DATA = Path(__file__).resolve().parent / "data"
_ECON_RE = re.compile(r"^[A-Z0-9]{2,4}$")
_IND_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FD_RE = re.compile(r"^[A-Z][A-Z0-9]*$")


@dataclass(frozen=True)
class Economy:
    """One ICIO reference area. ``code`` is the prefix of ``<ECON>_<IND>`` labels."""

    code: str
    name: str


@dataclass(frozen=True)
class Industry:
    """One ICIO industry, with the ReadMe's ISIC Rev.4 crosswalk text.

    ``isic_rev4`` is the division (or group) string as published, including ranges
    such as ``302 to 309`` and lists such as ``241, 2431``. It is not parsed further.
    """

    code: str
    description: str
    isic_rev4: str


@dataclass(frozen=True)
class FinalDemand:
    """One final-demand category. Columns are labelled ``<ECON>_<code>``."""

    code: str
    description: str


@dataclass(frozen=True)
class ZipSource:
    """Inclusive year block and the URL of the zip that contains those CSVs."""

    year_from: int
    year_to: int
    url: str


@dataclass(frozen=True)
class IcioLayout:
    """Row and column vocabulary for one ICIO edition (spec §9.3, §9.6).

    A later edition should be a new ``IcioLayout`` (and, when the industry list
    changes, a new CSV under ``data/``). The parser does not hard-code dimensions.
    ``output_code`` is the CSV label of gross output; in the 2025 ReadMe that label
    is ``OUT``. ``unit`` is a ``macrosys.core.units`` string (``USD_mn`` for OECD).
    """

    edition: str
    vintage: str
    unit: str
    economies: tuple[Economy, ...]
    industries: tuple[Industry, ...]
    final_demand: tuple[FinalDemand, ...]
    tax_code: str
    value_added_code: str
    output_code: str
    year_from: int
    year_to: int
    zip_sources: tuple[ZipSource, ...]
    readme_url: str

    def __post_init__(self) -> None:
        assert_unit(self.unit)
        if self.year_from > self.year_to:
            raise ValueError(f"year_from {self.year_from} > year_to {self.year_to}")
        _unique("economy", [e.code for e in self.economies])
        _unique("industry", [i.code for i in self.industries])
        _unique("final demand", [f.code for f in self.final_demand])
        for econ in self.economies:
            if not _ECON_RE.fullmatch(econ.code):
                raise ValueError(f"economy code {econ.code!r} is not a short alphanumeric prefix")
            if not econ.name:
                raise ValueError(f"economy {econ.code} has no name")
        for ind in self.industries:
            if not _IND_RE.fullmatch(ind.code):
                raise ValueError(f"industry code {ind.code!r} is not a usable ICIO code")
            if not ind.description or not ind.isic_rev4:
                raise ValueError(f"industry {ind.code} needs a description and an ISIC Rev.4 crosswalk")
        for fd in self.final_demand:
            if not _FD_RE.fullmatch(fd.code):
                raise ValueError(f"final-demand code {fd.code!r} must not contain '_'")
            if not fd.description:
                raise ValueError(f"final-demand code {fd.code} has no description")
        overlap = {i.code for i in self.industries} & {f.code for f in self.final_demand}
        if overlap:
            raise ValueError(f"industry and final-demand codes overlap: {sorted(overlap)}")
        for code, role in (
            (self.tax_code, "tax"),
            (self.value_added_code, "value added"),
            (self.output_code, "output"),
        ):
            if not code or "_" in code:
                raise ValueError(f"{role} code {code!r} must be a non-empty token without '_'")
        extras = {self.tax_code, self.value_added_code, self.output_code}
        if len(extras) != 3:
            raise ValueError("tax, value-added, and output codes must be distinct")
        covered: set[int] = set()
        for src in self.zip_sources:
            if src.year_from > src.year_to:
                raise ValueError(f"zip range {src.year_from}-{src.year_to} is reversed")
            span = set(range(src.year_from, src.year_to + 1))
            if covered & span:
                raise ValueError(f"zip ranges overlap on {src.year_from}-{src.year_to}")
            covered |= span

    @property
    def economy_codes(self) -> tuple[str, ...]:
        return tuple(e.code for e in self.economies)

    @property
    def industry_codes(self) -> tuple[str, ...]:
        return tuple(i.code for i in self.industries)

    @property
    def final_demand_codes(self) -> tuple[str, ...]:
        return tuple(f.code for f in self.final_demand)

    def industry_labels(self) -> tuple[str, ...]:
        """``<ECON>_<IND>`` labels in economy-major, industry-minor order."""
        return tuple(f"{econ}_{ind}" for econ in self.economy_codes for ind in self.industry_codes)

    def final_demand_labels(self) -> tuple[str, ...]:
        """``<ECON>_<FDCODE>`` labels in economy-major, category-minor order."""
        return tuple(f"{econ}_{fd}" for econ in self.economy_codes for fd in self.final_demand_codes)

    def zip_url(self, year: int) -> str:
        """URL of the OECD zip whose year block contains ``year``."""
        for src in self.zip_sources:
            if src.year_from <= year <= src.year_to:
                return src.url
        raise ValueError(f"no ICIO zip URL for year {year} in edition {self.edition}")


def _unique(kind: str, codes: list[str]) -> None:
    seen: set[str] = set()
    for code in codes:
        if code in seen:
            raise ValueError(f"duplicate {kind} code {code!r}")
        seen.add(code)


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} has no header")
        return [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def load_industries(path: Path) -> tuple[Industry, ...]:
    """Read ``code,description,isic_rev4`` into industry records (spec §9.3)."""
    rows = _load_csv(path)
    return tuple(Industry(r["code"], r["description"], r["isic_rev4"]) for r in rows)


def load_economies(path: Path) -> tuple[Economy, ...]:
    """Read ``code,name`` into economy records, preserving file order (spec §9.3)."""
    rows = _load_csv(path)
    return tuple(Economy(r["code"], r["name"]) for r in rows)


# [M] Final-demand codes and descriptions: ReadMe sheet Structure, 2025 edition.
_FINAL_DEMAND_2025: tuple[FinalDemand, ...] = (
    FinalDemand("HFCE", "Household Final Consumption Expenditure"),
    FinalDemand("NPISH", "Non-Profit Institutions Serving Households"),
    FinalDemand("GGFC", "General Government Final Consumption"),
    FinalDemand("GFCF", "Gross Fixed Capital Formation"),
    FinalDemand("INVNT", "Changes in Inventories and Valuables"),
    FinalDemand("DPABR", "Direct purchases abroad by residents"),
)

ICIO_2025 = IcioLayout(
    edition="2025",
    vintage="OECD_ICIO_2025",
    unit="USD_mn",
    economies=load_economies(_DATA / "icio_2025_economies.csv"),
    industries=load_industries(_DATA / "icio_2025_industries.csv"),
    final_demand=_FINAL_DEMAND_2025,
    tax_code="TLS",
    value_added_code="VA",
    output_code="OUT",
    year_from=1995,
    year_to=2022,
    zip_sources=tuple(ZipSource(a, b, url) for a, b, url in REGULAR_ZIP_URLS),
    readme_url=README_XLSX_URL,
)
