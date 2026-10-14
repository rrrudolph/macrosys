"""Parser: shapes, the row identity, and a corrupted copy that must fail."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from macrosys.core.units import assert_unit
from macrosys.io_tables import IoTable, IoTableError, parse_icio_csv
from tests.io_tables.conftest import FIXTURE, SMALL_LAYOUT


def test_fixture_shapes_labels_and_identity(icio_table: IoTable) -> None:
    table = icio_table
    assert table.Z.shape == (9, 9)
    assert table.F.shape == (9, 18)
    assert table.X.shape == (9,)
    assert table.VA.shape == (9,)
    assert table.tls.shape == (9,)
    assert table.labels[0] == "USA_A01"
    assert table.labels[2] == "USA_D"
    assert table.labels[-1] == "ROW_D"
    assert table.fd_labels[0] == "USA_HFCE"
    assert table.fd_labels[1] == "USA_NPISH"
    assert table.fd_labels[-1] == "ROW_DPABR"
    assert table.year == 2020
    assert table.unit == "USD_mn"
    assert assert_unit(table.unit).label == "USD_mn"
    assert table.vintage == "fixture"
    # The sentinel "na" in USA_A01 / USA_NPISH is an empty cell, stored as zero.
    assert table.F[0, 1] == 0.0
    assert table.X.tolist() == [29.0] * 9
    assert table.VA.tolist() == [20.0] * 9
    assert table.Z.sum() == 81.0


def test_row_identity_holds_on_fixture(icio_table: IoTable) -> None:
    expected = icio_table.Z.sum(axis=1) + icio_table.F.sum(axis=1)
    assert icio_table.X.tolist() == expected.tolist()


def test_corrupted_intermediate_flow_names_the_row(tmp_path: Path) -> None:
    frame = pd.read_csv(FIXTURE, index_col=0)
    frame.iloc[0, 0] = float(frame.iloc[0, 0]) + 50.0
    dest = tmp_path / "2020.csv"
    frame.to_csv(dest)
    with pytest.raises(IoTableError, match="USA_A01") as exc_info:
        parse_icio_csv(dest, SMALL_LAYOUT)
    assert exc_info.value.label == "USA_A01"
    assert exc_info.value.residual is not None
    assert abs(exc_info.value.residual) > 1.0


def test_year_outside_layout_is_rejected(tmp_path: Path) -> None:
    dest = tmp_path / "1990.csv"
    dest.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(IoTableError, match="1990"):
        parse_icio_csv(dest, SMALL_LAYOUT)
