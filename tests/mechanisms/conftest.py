"""Library fixture for mechanism tests. No network."""

from __future__ import annotations

from pathlib import Path

import pytest

from macrosys.mechanisms import Library, load_library

LIBRARY_DIR = Path(__file__).resolve().parents[2] / "mechanisms"


@pytest.fixture(scope="session")
def library() -> Library:
    return load_library(LIBRARY_DIR)
