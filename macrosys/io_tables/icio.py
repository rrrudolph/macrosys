"""Parse one OECD ICIO year CSV into arrays and check the supply-use identity (spec §3.3 A04).

The file is read once. Column and row membership come from an ``IcioLayout``, so a
future edition does not require a new parser. Algebra on the resulting arrays lives
in ``leontief.py`` and does not use pandas.
"""

# Public arrays keep input-output names (Z, F, X, VA).
# ruff: noqa: N803, N806

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from macrosys.io_tables.layout import IcioLayout

# [M] OECD ReadMe 2025, RowItems note: "na = Not available / Empty / Not applicable".
_NA_TOKENS = frozenset({"", "na", "n/a", "nan", "<na>"})

# atol is in the table unit. 1e-3 USD_mn is 1,000 USD, below published rounding.
DEFAULT_RTOL = 1e-6
DEFAULT_ATOL = 1e-3

_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


class IoTableError(ValueError):
    """An ICIO file broke an accounting identity or did not match its layout.

    ``label`` is the worst offending row or column. ``residual`` is X minus the
    identity's right-hand side, in the table unit. Identities are checks: this
    exception is the residual, not an explanation of it (spec §3.2).
    """

    def __init__(self, message: str, *, label: str | None = None, residual: float | None = None) -> None:
        super().__init__(message)
        self.label = label
        self.residual = residual


@dataclass(eq=False, slots=True)
class IoTable:
    """One year of an inter-country input-output table in current prices (spec §3.3 A04, §4.2).

    ``Z`` is intermediate use (n×n), ``F`` is final demand (n×k) with columns
    ``fd_labels`` of the form ``<ECON>_<FDCODE>``, ``X`` is gross output, ``VA`` is
    value added at basic prices, and ``tls`` is taxes less subsidies on products
    (the ReadMe's TLS row, which is not part of VA). ``labels`` are ``<ECON>_<IND>``
    in layout order. ``unit`` is a macrosys unit string; OECD ICIO is ``USD_mn``.
    ``vintage`` names the edition (``OECD_ICIO_2025``). Arrays are float64.
    """

    Z: NDArray[np.float64]
    F: NDArray[np.float64]
    X: NDArray[np.float64]
    VA: NDArray[np.float64]
    tls: NDArray[np.float64]
    labels: list[str]
    fd_labels: list[str]
    year: int
    unit: str
    vintage: str


def parse_icio_csv(
    path: str | Path,
    layout: IcioLayout,
    *,
    rtol: float = DEFAULT_RTOL,
    atol: float = DEFAULT_ATOL,
) -> IoTable:
    """Parse one ICIO year CSV into an ``IoTable`` and reject a broken identity (spec §3.3 A04, §9.6).

    ``layout`` supplies economy, industry, final-demand, tax, value-added, and output
    codes. The file is read once as strings (explicit dtype) and converted to float64;
    the OECD sentinel ``na`` is stored as zero. The year is the four-digit stem of
    ``path``, or the single 19xx/20xx token in the stem that falls inside the layout's
    year span. Selection is by label, not by column position. The row identity
    ``X = Z·1 + F·1`` is checked within ``atol + rtol * |reference|``, and so is the
    column identity ``X = 1'Z + TLS + VA``. ``IoTableError`` names the worst row or column.
    """
    csv_path = Path(path)
    frame = pd.read_csv(csv_path, dtype="string", encoding="utf-8-sig", keep_default_na=False)
    if frame.shape[0] == 0 or frame.shape[1] < 2:
        raise IoTableError(f"ICIO CSV {csv_path.name} has no matrix")

    row_labels = [str(v).strip() for v in frame.iloc[:, 0].tolist()]
    col_labels = [str(c).strip() for c in frame.columns[1:]]
    values = _to_float64(frame.iloc[:, 1:])
    del frame

    row_index = _label_index(row_labels, "row")
    col_index = _label_index(col_labels, "column")
    ind_labels = list(layout.industry_labels())
    fd_labels = list(layout.final_demand_labels())
    row_pos = _positions(ind_labels, row_index, "rows")
    col_pos = _positions(ind_labels, col_index, "intermediate columns")
    fd_pos = _positions(fd_labels, col_index, "final-demand columns")
    try:
        out_col = col_index[layout.output_code]
        out_row = row_index[layout.output_code]
        va_row = row_index[layout.value_added_code]
        tls_row = row_index[layout.tax_code]
    except KeyError as exc:
        raise IoTableError(f"ICIO CSV is missing layout code {exc.args[0]!r}") from exc

    Z = np.ascontiguousarray(values[np.ix_(row_pos, col_pos)], dtype=np.float64)
    F = np.ascontiguousarray(values[np.ix_(row_pos, fd_pos)], dtype=np.float64)
    X = np.ascontiguousarray(values[row_pos, out_col], dtype=np.float64)
    VA = np.ascontiguousarray(values[va_row, col_pos], dtype=np.float64)
    tls = np.ascontiguousarray(values[tls_row, col_pos], dtype=np.float64)
    published_out = np.ascontiguousarray(values[out_row, col_pos], dtype=np.float64)
    del values

    check_row_identity(Z, F, X, ind_labels, rtol=rtol, atol=atol)
    check_column_identity(Z, X, VA, tls, ind_labels, rtol=rtol, atol=atol)
    _check_close(
        published_out - X,
        X,
        ind_labels,
        rtol,
        atol,
        "output row does not match the output column",
    )
    return IoTable(
        Z=Z,
        F=F,
        X=X,
        VA=VA,
        tls=tls,
        labels=ind_labels,
        fd_labels=fd_labels,
        year=_year_from_path(csv_path, layout),
        unit=layout.unit,
        vintage=layout.vintage,
    )


def check_row_identity(
    Z: NDArray[np.float64],
    F: NDArray[np.float64],
    X: NDArray[np.float64],
    labels: list[str],
    *,
    rtol: float = DEFAULT_RTOL,
    atol: float = DEFAULT_ATOL,
) -> None:
    """Raise ``IoTableError`` unless ``X = Z·1 + F·1`` row by row (spec §3.3 A04).

    The worst row is the one with the largest relative residual among those outside
    ``atol + rtol * |Z·1 + F·1|``. The message names that row's label.
    """
    expected = Z.sum(axis=1) + F.sum(axis=1)
    _check_close(X - expected, expected, labels, rtol, atol, "row identity X = Z·1 + F·1 failed")


def check_column_identity(
    Z: NDArray[np.float64],
    X: NDArray[np.float64],
    VA: NDArray[np.float64],
    tls: NDArray[np.float64],
    labels: list[str],
    *,
    rtol: float = DEFAULT_RTOL,
    atol: float = DEFAULT_ATOL,
) -> None:
    """Raise ``IoTableError`` unless ``X = 1'Z + TLS + VA`` column by column (spec §3.3 A04).

    Value added at basic prices and taxes less subsidies on products are separate
    rows in the OECD ReadMe, so both enter the column identity.
    """
    expected = Z.sum(axis=0) + VA + tls
    _check_close(X - expected, expected, labels, rtol, atol, "column identity X = 1'Z + TLS + VA failed")


def _check_close(
    residual: NDArray[np.float64],
    reference: NDArray[np.float64],
    labels: list[str],
    rtol: float,
    atol: float,
    what: str,
) -> None:
    tol = atol + rtol * np.abs(reference)
    bad = np.abs(residual) > tol
    if not np.any(bad):
        return
    relative = np.abs(residual) / np.maximum(np.abs(reference), 1e-12)
    score = np.where(bad, relative, -np.inf)
    i = int(np.argmax(score))
    raise IoTableError(
        f"{what} for {labels[i]} "
        f"(residual={residual[i]:.8g}, reference={reference[i]:.8g}, "
        f"relative={relative[i]:.8g}; rtol={rtol}, atol={atol}); spec §3.3 A04",
        label=str(labels[i]),
        residual=float(residual[i]),
    )


def _to_float64(block: pd.DataFrame) -> NDArray[np.float64]:
    stripped = block.apply(lambda col: col.str.strip())
    lowered = stripped.apply(lambda col: col.str.lower())
    numeric = stripped.mask(lowered.isin(_NA_TOKENS), "0")
    try:
        arr = numeric.astype("float64").to_numpy(dtype=np.float64, copy=True)
    except (ValueError, TypeError) as exc:
        raise IoTableError(f"ICIO value is not a number ({exc})") from exc
    if not np.isfinite(arr).all():
        raise IoTableError("ICIO table contains a non-finite value")
    return arr


def _label_index(labels: list[str], kind: str) -> dict[str, int]:
    index: dict[str, int] = {}
    for i, label in enumerate(labels):
        if label in index:
            raise IoTableError(f"duplicate ICIO {kind} label {label!r}")
        index[label] = i
    return index


def _positions(wanted: list[str], index: dict[str, int], what: str) -> NDArray[np.intp]:
    missing = [label for label in wanted if label not in index]
    if missing:
        preview = ", ".join(missing[:8])
        extra = "" if len(missing) <= 8 else f" (+{len(missing) - 8} more)"
        raise IoTableError(f"ICIO {what} missing {len(missing)} label(s): {preview}{extra}")
    return np.asarray([index[label] for label in wanted], dtype=np.intp)


def _year_from_path(path: Path, layout: IcioLayout) -> int:
    stem = path.stem
    if re.fullmatch(r"\d{4}", stem):
        year = int(stem)
    else:
        found = [int(y) for y in _YEAR_RE.findall(stem)]
        in_span = [y for y in found if layout.year_from <= y <= layout.year_to]
        if len(in_span) != 1:
            raise IoTableError(
                f"cannot infer a single ICIO year from filename {path.name!r}; name the file YYYY.csv"
            )
        year = in_span[0]
    if not layout.year_from <= year <= layout.year_to:
        raise IoTableError(
            f"year {year} is outside ICIO {layout.edition} coverage {layout.year_from}-{layout.year_to}"
        )
    return year
