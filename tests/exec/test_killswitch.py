"""Kill switch cancels working orders and blocks submits until acknowledged (spec §11.7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from macrosys.decide.portfolio import OrderState
from macrosys.exec.killswitch import KillSwitch
from macrosys.exec.oms import KillSwitchEngaged, OrderManager
from tests.exec.support import FakeBroker, ManualClock, ticket


def test_kill_switch_cancels_and_blocks_until_acknowledged(tmp_path) -> None:
    broker = FakeBroker()
    clock = ManualClock(datetime(2026, 3, 2, 15, 0, tzinfo=UTC))
    flag = tmp_path / "KILL"
    switch = KillSwitch(flag)
    working = ticket()
    later = ticket(client_order_id="b" * 24, instrument_id="IEF")
    with OrderManager(tmp_path / "oms.sqlite", broker, clock, kill_switch=switch) as oms:
        oms.submit(working)
        switch.engage("monthly paper test", oms)
        assert switch.is_engaged() is True
        assert oms.state(working.client_order_id) is OrderState.CANCELLED
        assert broker.cancelled == ["B-" + working.client_order_id]
        with pytest.raises(KillSwitchEngaged):
            oms.submit(later)
        assert broker.place_calls == 1
        with pytest.raises(ValueError):
            switch.disengage("  ", oms)
        assert switch.is_engaged() is True
        switch.disengage("monthly test, book unchanged", oms)
        assert switch.is_engaged() is False
        assert any(item.detail == "monthly test, book unchanged" for item in oms.incidents())
        assert oms.submit(later) is OrderState.SUBMITTED
        assert broker.place_calls == 2
