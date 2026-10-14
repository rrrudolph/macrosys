"""MetaTrader 5 adapter with a fake MetaTrader5 module (spec §9.6)."""

import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from macrosys.sources.mt5 import MT5Source, mt5_module_loaded
from tests.sources.fakes import MemoryRawStore


class _FakeMT5:
    TIMEFRAME_D1 = 16408

    def __init__(self) -> None:
        self.login = None
        self.password = None
        self.server = None

    def initialize(self, login: int, password: str, server: str) -> bool:
        self.login = login
        self.password = password
        self.server = server
        return True

    def last_error(self) -> tuple[int, str]:
        return (1, "ok")

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return True

    def symbol_info(self, symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            digits=5,
            trade_contract_size=100_000,
            currency_profit="USD",
            sessions=[{"day": "Monday", "open": "00:00", "close": "23:59"}],
        )

    def copy_rates_range(self, symbol: str, timeframe: int, start: datetime, end: datetime) -> list[dict]:
        assert timeframe == 16408
        return [
            {
                "time": 1_704_067_200,
                "open": 1.10,
                "high": 1.20,
                "low": 1.00,
                "close": 1.15,
                "tick_volume": 10,
                "spread": 1,
                "real_volume": 0,
            }
        ]


def test_import_does_not_load_metatrader():
    sys.modules.pop("MetaTrader5", None)
    assert mt5_module_loaded() is False


def test_rates_are_cross_check_and_record_close_time(monkeypatch):
    fake = _FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    when = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    source = MT5Source(MemoryRawStore(), login=100, password="pw-secret", server="Demo-Server", now=lambda: when)
    assert source.tier == "cross_check"
    spec, bars = source.rates(
        "EURUSD",
        16408,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
    )
    assert fake.password == "pw-secret"
    assert spec.digits == 5
    assert spec.contract_size == 100_000
    assert spec.currency == "USD"
    assert spec.trade_sessions[0]["close"] == "23:59"
    bar = bars[0]
    assert bar.source_id == "MT5"
    assert bar.tier == "cross_check"
    assert bar.close == 1.15
    assert bar.volume == 10
    assert bar.ccy == "USD"
    assert bar.ingested_at == when
    assert "close_time=2024-01-01T23:59:00+00:00" in bar.session_note
    assert "close_basis=trade_session" in bar.session_note
    dumped = bar.model_dump_json() + spec.model_dump_json()
    assert "pw-secret" not in dumped
