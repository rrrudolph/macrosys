"""Small array-valued quantity for vector mechanisms such as B01 (spec §3.3 B01, §4.2).

Numpy stays in this module. Scalar mechanisms keep using ``Quantity``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from macrosys.core.units import assert_unit


@dataclass(frozen=True, eq=False)
class ArrayQuantity:
    """A numpy array with one unit and row/column labels.

    ``cols`` is ``None`` for a vector. Labels are the entity keys the array is aligned on
    (industry, country, or a dated quarter). Spec §3.3 B01.
    """

    values: np.ndarray
    unit: str
    rows: tuple[str, ...]
    cols: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        assert_unit(self.unit)
        arr = np.array(self.values, dtype=float, copy=True)
        rows = tuple(self.rows)
        cols = None if self.cols is None else tuple(self.cols)
        if arr.ndim == 1:
            if cols is not None:
                raise ValueError("a vector ArrayQuantity cannot have cols")
            if arr.shape[0] != len(rows):
                raise ValueError(f"row labels {len(rows)} do not match length {arr.shape[0]}")
        elif arr.ndim == 2:
            if cols is None:
                raise ValueError("a matrix ArrayQuantity needs cols")
            if arr.shape != (len(rows), len(cols)):
                raise ValueError(f"shape {arr.shape} does not match labels {(len(rows), len(cols))}")
        else:
            raise ValueError(f"ArrayQuantity supports 1-D or 2-D arrays, got ndim {arr.ndim}")
        if not np.isfinite(arr).all():
            raise ValueError("ArrayQuantity values must be finite")
        object.__setattr__(self, "values", arr)
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "cols", cols)

    def scale(self, k: float) -> ArrayQuantity:
        """Multiply every element by a dimensionless factor. Spec §3.4 rule 5 (gate scaling)."""
        return ArrayQuantity(values=self.values * k, unit=self.unit, rows=self.rows, cols=self.cols)

    def bump(self, frac: float) -> ArrayQuantity:
        """Increase each element by ``frac`` of its absolute value (or by ``frac`` if it is zero).

        Used by the counterfactual sign test (spec §10.1 item 3).
        """
        delta = np.where(self.values == 0.0, frac, np.abs(self.values) * frac)
        return ArrayQuantity(values=self.values + delta, unit=self.unit, rows=self.rows, cols=self.cols)

    def total(self) -> float:
        """Sum of elements, for sign tests on a vector output (spec §10.1 item 3)."""
        return float(self.values.sum())


def matvec(matrix: ArrayQuantity, vector: ArrayQuantity) -> ArrayQuantity:
    """Matrix-vector product. The vector's unit is kept; the matrix is treated as dimensionless.

    Implements the Leontief map dX = L dF (spec §3.3 B01). Column labels of ``matrix`` must equal
    the row labels of ``vector``.
    """
    if matrix.cols is None:
        raise ValueError("matvec needs a matrix")
    if vector.cols is not None:
        raise ValueError("matvec needs a vector")
    if matrix.cols != vector.rows:
        raise ValueError(f"column labels {matrix.cols} do not match vector rows {vector.rows}")
    prod = matrix.values @ vector.values
    return ArrayQuantity(values=prod, unit=vector.unit, rows=matrix.rows)


def hadamard(vector: ArrayQuantity, weights: ArrayQuantity) -> ArrayQuantity:
    """Elementwise product of a vector by dimensionless weights with the same row labels.

    Used for the domestic/imported split of gross output (spec §3.3 B01). The result keeps
    ``vector.unit``.
    """
    if vector.cols is not None or weights.cols is not None:
        raise ValueError("hadamard operates on vectors")
    if vector.rows != weights.rows:
        raise ValueError(f"row labels differ: {vector.rows} vs {weights.rows}")
    return ArrayQuantity(values=vector.values * weights.values, unit=vector.unit, rows=vector.rows)


def complement_hadamard(vector: ArrayQuantity, weights: ArrayQuantity) -> ArrayQuantity:
    """Elementwise product by ``(1 - weights)`` with the same row labels (spec §3.3 B01)."""
    if vector.cols is not None or weights.cols is not None:
        raise ValueError("complement_hadamard operates on vectors")
    if vector.rows != weights.rows:
        raise ValueError(f"row labels differ: {vector.rows} vs {weights.rows}")
    return ArrayQuantity(values=vector.values * (1.0 - weights.values), unit=vector.unit, rows=vector.rows)
