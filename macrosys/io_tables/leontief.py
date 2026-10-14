"""Leontief algebra on a parsed input-output table (spec §3.3 A01, B01, B02; §4.2, §4.6).

Functions here take and return numpy float64 arrays. They do not use pandas. Currency
stays on ``IoTable.unit`` (``USD_mn`` for OECD ICIO); shares and multipliers are
dimensionless. Callers convert scale with ``macrosys.core.units.convert``.
"""

# Public names follow input-output notation (Z, A, X, F, L).
# ruff: noqa: N803, N806

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

import numpy as np
from numpy.typing import NDArray

from macrosys.io_tables.icio import (
    DEFAULT_ATOL,
    DEFAULT_RTOL,
    IoTable,
    check_column_identity,
    check_row_identity,
)


class LeontiefError(ValueError):
    """A technology or a shock for which the Leontief model is not defined."""


def technical_coefficients(Z: NDArray[np.float64], X: NDArray[np.float64]) -> NDArray[np.float64]:
    """Technical coefficient matrix A for spec §4.2 and §3.3 B01.

    Column j is intermediate inputs per unit of gross output of j, ``A[:, j] = Z[:, j] / X[j]``,
    in current basic prices. A column whose gross output is zero is set to zero so a missing
    industry does not produce infinite coefficients. ``Z`` and ``X`` are in the table unit;
    ``A`` is a dimensionless share. In a current-price table this ratio is also the B02 cost share.
    """
    flows = _square("Z", Z)
    output = _vector("X", X, flows.shape[0])
    return _divide_columns(flows, output)


def leontief_inverse(A: NDArray[np.float64]) -> NDArray[np.float64]:
    """Leontief inverse ``L = (I − A)⁻¹`` for spec §4.2 and §3.3 B01.

    Raises ``LeontiefError`` when the spectral radius of ``A`` is not strictly below 1,
    which is the condition for the Neumann series of total requirements to converge.
    The radius is the largest absolute eigenvalue from ``numpy.linalg.eigvals``. On the
    full ICIO industry dimension that factorization is cubic in n and is meant to be
    called deliberately, not inside a tight loop.
    """
    coeffs = _square("A", A)
    radius = _spectral_radius(coeffs)
    if radius >= 1.0:
        raise LeontiefError(
            f"spectral radius of A is {radius:.6g}, which is not < 1; "
            "the Leontief inverse is not defined (spec §3.3 B01)"
        )
    n = coeffs.shape[0]
    return np.linalg.inv(np.eye(n, dtype=np.float64) - coeffs)


def total_requirements(L: NDArray[np.float64], dF: NDArray[np.float64]) -> NDArray[np.float64]:
    """Gross-output response to a final-demand shock, ``dX = L dF`` (spec §3.3 B01).

    ``dF`` is a vector of length n, or an n×m matrix of shocks. The result has the same
    shape and the table's currency unit. This is the first-order, fixed-proportions effect;
    it does not apply substitution or a capacity constraint.
    """
    inverse = _square("L", L)
    shock = np.asarray(dF, dtype=np.float64)
    n = inverse.shape[0]
    if shock.ndim == 1 and shock.shape != (n,):
        raise LeontiefError(f"dF must have length {n}, got shape {shock.shape}")
    if shock.ndim == 2 and shock.shape[0] != n:
        raise LeontiefError(f"dF must have {n} rows, got shape {shock.shape}")
    if shock.ndim not in (1, 2):
        raise LeontiefError(f"dF must be 1-D or 2-D, got shape {shock.shape}")
    if not np.isfinite(shock).all():
        raise LeontiefError("dF contains non-finite values")
    return inverse @ shock


def domar_weights(X: NDArray[np.float64], gdp: float) -> NDArray[np.float64]:
    """Hulten / Baqaee–Farhi weights, sales over GDP (spec §3.3 B01).

    ``weights_i = X_i / gdp``, with ``X`` and ``gdp`` in the same currency unit. The weights
    are dimensionless. They sum to gross output over GDP, which is greater than 1 whenever
    intermediate sales are positive (Hulten 1978; Baqaee and Farhi 2019: the first-order
    aggregate effect of a sector shock equals its Domar weight).
    """
    output = np.asarray(X, dtype=np.float64)
    if output.ndim == 2 and 1 in output.shape:
        output = output.reshape(-1)
    if output.ndim != 1 or not np.isfinite(output).all():
        raise LeontiefError(f"X must be a finite vector, got shape {np.shape(X)}")
    scale = float(gdp)
    if not np.isfinite(scale) or scale == 0.0:
        raise LeontiefError(f"GDP must be a non-zero finite scalar in the same unit as X, got {gdp!r}")
    return output / scale


def import_content(
    L: NDArray[np.float64],
    dF: NDArray[np.float64],
    labels: list[str],
    economy: str,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Split a demand shock's gross-output response into domestic and foreign production (spec §3.3 A01).

    A producing row is domestic when the economy prefix of its ``<ECON>_<IND>`` label —
    the text before the first underscore — equals ``economy``. ``domestic_dX + foreign_dX``
    equals ``total_requirements(L, dF)``. This is a split of gross output by producing
    country, which is the import leakage of the shock in output terms, not a split of
    value added. Raises ``LeontiefError`` if ``economy`` is not one of the prefixes.
    """
    response = total_requirements(L, dF)
    if len(labels) != response.shape[0]:
        raise LeontiefError(f"labels ({len(labels)}) do not match the {response.shape[0]} producing rows")
    prefixes = [str(label).split("_", 1)[0] for label in labels]
    if economy not in prefixes:
        raise LeontiefError(f"economy {economy!r} is not a producing-row prefix")
    mask = np.asarray([prefix == economy for prefix in prefixes], dtype=bool)
    if response.ndim == 1:
        domestic = np.where(mask, response, 0.0)
        foreign = np.where(mask, 0.0, response)
    else:
        domestic = np.where(mask[:, None], response, 0.0)
        foreign = np.where(mask[:, None], 0.0, response)
    return domestic, foreign


def cost_shares(Z: NDArray[np.float64], X: NDArray[np.float64]) -> NDArray[np.float64]:
    """Materiality cost shares for spec §3.3 B02.

    ``S[i, j] = Z[i, j] / X[j]``, the current-price share of input i in the gross output
    of j. [J] The denominator is gross output ``X_j``, not the column's intermediate-cost
    total, so a column sums to ``1 − (VA_j + TLS_j) / X_j`` rather than to 1. In a
    current-price table this is the same ratio as ``technical_coefficients``. A column
    with zero gross output is all zeros. A 1% price change in i then moves j's unit cost
    by ``S[i, j]`` percent, which is the B02 materiality filter.
    """
    flows = _square("Z", Z)
    output = _vector("X", X, flows.shape[0])
    return _divide_columns(flows, output)


def value_added_multipliers(
    L: NDArray[np.float64],
    VA: NDArray[np.float64],
    X: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Value-added multipliers ``v' L`` for spec §3.3 B01.

    ``v_j = VA_j / X_j``, and 0 when ``X_j`` is 0. Entry j is economy-wide value added,
    in the table unit, generated per one unit of final demand for j (dimensionless).
    In a balanced table whose only primary input is ``VA`` (product taxes already inside
    VA, or zero), every multiplier equals 1.
    """
    inverse = _square("L", L)
    n = inverse.shape[0]
    output = _vector("X", X, n)
    value_added = _vector("VA", VA, n)
    share = np.zeros(n, dtype=np.float64)
    positive = output != 0.0
    share[positive] = value_added[positive] / output[positive]
    return share @ inverse


def upstreamness(
    A: NDArray[np.float64],
    X: NDArray[np.float64],
    F: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Antràs–Chor–Fally–Hillberry upstreamness, stages from production to final demand (spec §3.3 B01).

    With ``Z_ij = A_ij X_j`` and ``y`` the industry total of ``F`` (a vector, or a matrix
    summed across final-demand columns), the sales-allocation matrix is
    ``G_ij = Z_ij / (y_i + Σ_k Z_ik)`` and ``U = (I − G)⁻¹ 1``. When the table balances,
    the denominator equals ``X_i``, an industry that sells only to final demand has
    ``U_i = 1``, and an industry that sells only to that industry has ``U_i = 2``.
    Raises ``LeontiefError`` if the spectral radius of ``G`` is not strictly below 1
    (some output never reaches final demand). This is a network-position statistic
    beside B01, not a separate propagation (Antràs, Chor, Fally and Hillberry 2012).
    """
    coeffs = _square("A", A)
    n = coeffs.shape[0]
    output = _vector("X", X, n)
    final_use = _final_demand_vector(F, n)
    sales = coeffs * output  # Z_ij = A_ij * X_j
    denom = final_use + sales.sum(axis=1)
    # G_ij = Z_ij / denom_i  (share of i's sales), so the denominator indexes rows.
    allocation = np.zeros_like(sales)
    positive = denom != 0.0
    allocation[positive, :] = sales[positive, :] / denom[positive, None]
    radius = _spectral_radius(allocation)
    if radius >= 1.0:
        raise LeontiefError(
            f"spectral radius of the output-allocation matrix is {radius:.6g}, which is not < 1; "
            "upstreamness is undefined because some output never reaches final demand"
        )
    return np.linalg.solve(np.eye(n, dtype=np.float64) - allocation, np.ones(n, dtype=np.float64))


def aggregate(table: IoTable, mapping: Mapping[str, str]) -> IoTable:
    """Roll a table up to a coarser industry partition (spec §4.6).

    ``mapping`` sends every current ``<ECON>_<IND>`` label to its aggregate label.
    Rows and columns of ``Z``, and the rows of ``F``, ``X``, ``VA``, and ``tls``, are
    summed within each group. The grouping is a linear partition, so total gross output
    is unchanged and the row identity ``X = Z·1 + F·1`` and the column identity are
    preserved up to summation of each group's residuals. Final-demand columns are left
    as they are. Aggregate labels keep first-seen order. Refinement the other way is an
    overlay row with its own provenance, not a silent split (spec §4.6).
    """
    labels = list(table.labels)
    missing = [label for label in labels if label not in mapping]
    if missing:
        raise LeontiefError(
            f"aggregate mapping misses {missing[0]!r}"
            + (f" and {len(missing) - 1} other label(s)" if len(missing) > 1 else "")
        )
    groups: list[str] = []
    index: dict[str, int] = {}
    for label in labels:
        group = mapping[label]
        if not isinstance(group, str) or not group:
            raise LeontiefError(f"aggregate label for {label!r} must be a non-empty string")
        if group not in index:
            index[group] = len(groups)
            groups.append(group)
    assign = np.zeros((len(groups), len(labels)), dtype=np.float64)
    for i, label in enumerate(labels):
        assign[index[mapping[label]], i] = 1.0
    Z = assign @ table.Z @ assign.T
    F = assign @ table.F
    X = assign @ table.X
    VA = assign @ table.VA
    tls = assign @ table.tls
    widest = max(Counter(mapping[label] for label in labels).values())
    check_row_identity(Z, F, X, groups, rtol=DEFAULT_RTOL, atol=DEFAULT_ATOL * widest)
    check_column_identity(Z, X, VA, tls, groups, rtol=DEFAULT_RTOL, atol=DEFAULT_ATOL * widest)
    return IoTable(
        Z=Z,
        F=F,
        X=X,
        VA=VA,
        tls=tls,
        labels=groups,
        fd_labels=list(table.fd_labels),
        year=table.year,
        unit=table.unit,
        vintage=table.vintage,
    )


def _divide_columns(numerator: NDArray[np.float64], denominator: NDArray[np.float64]) -> NDArray[np.float64]:
    """Column-wise ratio. A zero denominator yields a zero column."""
    out = np.zeros_like(numerator, dtype=np.float64)
    positive = denominator != 0.0
    out[:, positive] = numerator[:, positive] / denominator[positive]
    return out


def _as_matrix(name: str, value: NDArray[np.float64]) -> NDArray[np.float64]:
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim != 2:
        raise LeontiefError(f"{name} must be 2-D, got shape {arr.shape}")
    if not np.isfinite(arr).all():
        raise LeontiefError(f"{name} contains non-finite values")
    return arr


def _square(name: str, value: NDArray[np.float64]) -> NDArray[np.float64]:
    arr = _as_matrix(name, value)
    if arr.shape[0] != arr.shape[1]:
        raise LeontiefError(f"{name} must be square, got shape {arr.shape}")
    return arr


def _vector(name: str, value: NDArray[np.float64], n: int) -> NDArray[np.float64]:
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim == 2 and 1 in arr.shape:
        arr = arr.reshape(-1)
    elif arr.ndim != 1:
        raise LeontiefError(f"{name} must be a vector of length {n}, got shape {arr.shape}")
    if arr.shape != (n,):
        raise LeontiefError(f"{name} must have length {n}, got {arr.shape[0]}")
    if not np.isfinite(arr).all():
        raise LeontiefError(f"{name} contains non-finite values")
    return arr


def _final_demand_vector(F: NDArray[np.float64], n: int) -> NDArray[np.float64]:
    arr = np.asarray(F, dtype=np.float64)
    if arr.ndim == 2:
        if arr.shape[0] != n:
            raise LeontiefError(f"F must have {n} rows, got shape {arr.shape}")
        if not np.isfinite(arr).all():
            raise LeontiefError("F contains non-finite values")
        return arr.sum(axis=1)
    return _vector("F", arr, n)


def _spectral_radius(A: NDArray[np.float64]) -> float:
    if A.shape[0] == 0:
        return 0.0
    values = np.linalg.eigvals(A)
    radius = float(np.max(np.abs(values)))
    if not np.isfinite(radius):
        raise LeontiefError("spectral radius is not finite")
    return radius
