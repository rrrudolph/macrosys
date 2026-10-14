"""In-memory DuckDB with the data-spine schema applied."""

from __future__ import annotations

import duckdb
import pytest

from macrosys.store.schema import create_schema


@pytest.fixture
def conn():
    connection = duckdb.connect(":memory:")
    create_schema(connection)
    try:
        yield connection
    finally:
        connection.close()
