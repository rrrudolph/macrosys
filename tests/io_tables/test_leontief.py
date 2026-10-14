"""Textbook Leontief inverse, leakage split, cost shares, Domar weights, aggregation."""

from __future__ import annotations

import numpy as np
import pytest

from macrosys.io_tables import (
    IoTable,
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

# Miller & Blair, Input-Output Analysis (2009), the standard 2-sector table:
# Z = [[150, 500], [200, 100]], X = [1000, 1000].
# A = [[0.15, 0.50], [0.20, 0.10]], det(I−A) = 0.665,
# L = (1/0.665) * [[0.90, 0.50], [0.20, 0.85]].
_MB_Z = np.array([[150.0, 500.0], [200.0, 100.0]], dtype=np.float64)
_MB_X = np.array([1000.0, 1000.0], dtype=np.float64)
_MB_L = np.array([[1.3534, 0.7519], [0.3008, 1.2782]], dtype=np.float64)


def test_miller_blair_inverse_to_four_decimals() -> None:
    got = leontief_inverse(technical_coefficients(_MB_Z, _MB_X))
    np.testing.assert_allclose(got, _MB_L, atol=5e-5)


def test_zero_output_column_is_zero() -> None:
    flows = np.array([[5.0, 4.0], [5.0, 6.0]])
    output = np.array([0.0, 10.0])
    coeffs = technical_coefficients(flows, output)
    assert coeffs[:, 0].tolist() == [0.0, 0.0]
    np.testing.assert_allclose(coeffs[:, 1], [0.4, 0.6])


def test_value_added_multipliers_are_one_in_a_closed_table() -> None:
    coeffs = technical_coefficients(_MB_Z, _MB_X)
    inverse = leontief_inverse(coeffs)
    value_added = _MB_X - _MB_Z.sum(axis=0)
    multipliers = value_added_multipliers(inverse, value_added, _MB_X)
    np.testing.assert_allclose(multipliers, [1.0, 1.0])


def test_import_content_partitions_total_requirements(icio_table: IoTable) -> None:
    coeffs = technical_coefficients(icio_table.Z, icio_table.X)
    inverse = leontief_inverse(coeffs)
    shock = np.zeros(9, dtype=np.float64)
    shock[0] = 1.0
    domestic, foreign = import_content(inverse, shock, icio_table.labels, "USA")
    response = total_requirements(inverse, shock)
    np.testing.assert_allclose(domestic + foreign, response)
    assert np.all(domestic[3:] == 0.0)
    assert np.all(foreign[:3] == 0.0)
    assert domestic.sum() > 0.0
    assert foreign.sum() > 0.0
    with pytest.raises(LeontiefError):
        import_content(inverse, shock, icio_table.labels, "FRA")


def test_cost_share_columns_sum_to_one_minus_value_added_share(icio_table: IoTable) -> None:
    shares = cost_shares(icio_table.Z, icio_table.X)
    value_added_share = icio_table.VA / icio_table.X
    np.testing.assert_allclose(shares.sum(axis=0), 1.0 - value_added_share, atol=1e-12)
    # Intermediate use is positive, so a column is not a pure value-added share.
    assert np.all(shares.sum(axis=0) > 0.0)
    assert np.all(shares.sum(axis=0) < 1.0)


def test_domar_weights_sum_above_one_when_there_is_intermediate_use(icio_table: IoTable) -> None:
    assert float(icio_table.Z.sum()) > 0.0
    weights = domar_weights(icio_table.X, float(icio_table.VA.sum()))
    assert float(weights.sum()) > 1.0
    np.testing.assert_allclose(weights.sum(), icio_table.X.sum() / icio_table.VA.sum())


def test_aggregate_preserves_output_and_row_identity(icio_table: IoTable) -> None:
    mapping: dict[str, str] = {}
    for economy in ("USA", "DEU", "ROW"):
        mapping[f"{economy}_A01"] = f"{economy}_A01"
        mapping[f"{economy}_C26"] = f"{economy}_MFG"
        mapping[f"{economy}_D"] = f"{economy}_MFG"
    rolled = aggregate(icio_table, mapping)
    assert rolled.labels == ["USA_A01", "USA_MFG", "DEU_A01", "DEU_MFG", "ROW_A01", "ROW_MFG"]
    assert rolled.Z.shape == (6, 6)
    assert rolled.F.shape == (6, 18)
    assert rolled.fd_labels == icio_table.fd_labels
    np.testing.assert_allclose(rolled.X.sum(), icio_table.X.sum())
    np.testing.assert_allclose(rolled.X, rolled.Z.sum(axis=1) + rolled.F.sum(axis=1))
    np.testing.assert_allclose(rolled.VA.sum(), icio_table.VA.sum())
    with pytest.raises(LeontiefError, match="USA_C26"):
        aggregate(icio_table, {"USA_A01": "USA_A01"})


def test_spectral_radius_guard() -> None:
    infeasible = np.array([[0.0, 2.0], [2.0, 0.0]], dtype=np.float64)
    with pytest.raises(LeontiefError, match="spectral radius"):
        leontief_inverse(infeasible)
    with pytest.raises(LeontiefError, match="spectral radius"):
        leontief_inverse(np.array([[1.0]]))
    feasible = leontief_inverse(np.array([[0.5]]))
    np.testing.assert_allclose(feasible, [[2.0]])


def test_upstreamness_of_a_two_stage_chain() -> None:
    # Industry 0 sells 10 to industry 1; industry 1 sells 100 to final demand.
    # Distances are 2 and 1. Outputs differ so the row-wise allocation is not
    # the same matrix as a column-wise ratio.
    coeffs = np.array([[0.0, 0.1], [0.0, 0.0]], dtype=np.float64)
    output = np.array([10.0, 100.0], dtype=np.float64)
    final = np.array([0.0, 100.0], dtype=np.float64)
    got = upstreamness(coeffs, output, final)
    np.testing.assert_allclose(got, [2.0, 1.0])
