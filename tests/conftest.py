"""Shared test configuration.

Tests never touch the network (AGENTS.md). Any attempt to open a socket fails the test unless the test is
marked `network`, in which case it is skipped by default (run with `-m network` to opt in).
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("-m"):
        return
    skip = pytest.mark.skip(reason="network tests are opt-in: run with -m network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if "network" in request.keywords:
        yield
        return

    def guard(*_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("network access is disabled in tests; use a fixture under tests/fixtures/")

    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)
    yield


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
