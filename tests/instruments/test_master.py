import json
from pathlib import Path

import httpx
import pytest

from macrosys.instruments import (
    Instrument,
    MandateExclusionError,
    MandateExclusions,
    OpenFigiClient,
    load_universe,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "universe.example.yaml"


def test_example_universe_loads_with_defaults_and_hedged_pairs():
    book = load_universe(EXAMPLE)
    by_ticker = {row.ticker: row for row in book}
    assert len(book) >= 40
    assert by_ticker["ACWI"].role == "factor_global_equity"
    assert by_ticker["GLD"].role == "factor_gold"
    assert by_ticker["HEFA"].structure == "hedged"
    assert by_ticker["HEFA"].hedged_pair_of == "EFA"
    assert by_ticker["DXJ"].hedged_pair_of == "EWJ"
    assert by_ticker["USO"].structure == "futures"
    assert by_ticker["UUP"].structure == "futures"
    assert by_ticker["UNG"].structure == "futures"
    spy = by_ticker["SPY"]
    assert spy.instrument_id == "SPY"
    assert spy.type == "ETF"
    assert spy.base_ccy == "USD"
    assert spy.expense_ratio is None
    for row in book:
        MandateExclusions.check_instrument(row)


def test_mandate_rejects_leveraged_inverse_and_vix(fixtures_dir: Path):
    with pytest.raises(MandateExclusionError):
        load_universe(fixtures_dir / "instruments" / "excluded.yaml")
    rejected = [
        {"ticker": "UVXY", "structure": "futures"},
        {"ticker": "TQQQ", "structure": "physical"},
        {"ticker": "SQQQ", "structure": "inverse"},
        {"ticker": "SPY", "structure": "physical", "name": "VIX note"},
        {"ticker": "QQQ", "structure": "physical", "name": "ProShares Ultra QQQ"},
        {"ticker": "SPY", "structure": "physical", "leverage": 2},
        {"ticker": "SPY", "structure": "physical", "vix_linked": True},
    ]
    for row in rejected:
        with pytest.raises(MandateExclusionError):
            MandateExclusions.check(row)
    MandateExclusions.check({"ticker": "USO", "structure": "futures", "name": "United States Oil Fund"})
    MandateExclusions.check({"ticker": "BIL", "structure": "physical", "name": "short-term treasury"})
    MandateExclusions.check({"ticker": "UUP", "structure": "futures"})


def test_working_universe_keeps_verified_exposure_and_fees():
    book = {row.ticker: row for row in load_universe(EXAMPLE.parent / "universe.yaml")}
    assert book["SPY"].expense_ratio == 0.000945
    assert book["SPY"].exposure is not None
    assert book["SPY"].exposure.country == "USA"
    assert book["SPY"].exposure.asset_class == "equity"
    assert book["CPER"].expense_ratio == 0.0088
    assert book["CPER"].exposure is not None
    assert book["CPER"].exposure.commodity == "copper"
    assert book["CPER"].underlying == "copper"
    assert book["HEFA"].expense_ratio == 0.0035
    assert book["HEFA"].exposure is not None
    assert book["HEFA"].exposure.currency == "USD"
    # Fees read from the 2026-04-24 prospectuses; the USCF pages leave the cell blank.
    assert book["USO"].expense_ratio == 0.0086
    assert book["USO"].exposure is not None
    assert book["USO"].exposure.commodity == "wti"
    assert book["XLB"].exposure is not None and book["XLB"].exposure.sector is not None
    assert book["XLB"].expense_ratio == 0.0008
    assert book["UNG"].exposure is not None and book["UNG"].expense_ratio == 0.0117
    assert book["FXE"].expense_ratio == 0.004
    # Every universe record now carries both facts, so the approver never falls back to a blank.
    assert all(row.expense_ratio is not None and row.exposure is not None for row in book.values())


def test_hedged_pair_and_structure_rules(tmp_path: Path):
    missing = tmp_path / "missing.yaml"
    missing.write_text("instruments:\n  - {ticker: HEFA, structure: hedged}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hedged_pair_of"):
        load_universe(missing)
    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(
        "instruments:\n  - {ticker: HEFA, structure: hedged, hedged_pair_of: EFA}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not in the universe"):
        load_universe(unknown)
    ok = tmp_path / "ok.yaml"
    ok.write_text(
        "instruments:\n"
        "  - {ticker: EFA, structure: physical}\n"
        "  - {ticker: HEFA, structure: hedged, hedged_pair_of: EFA}\n"
        "  - {ticker: USO, structure: Futures, underlying: WTI}\n",
        encoding="utf-8",
    )
    book = {row.ticker: row for row in load_universe(ok)}
    assert book["HEFA"].hedged_pair_of == "EFA"
    assert book["USO"].structure == "futures"
    assert book["USO"].underlying == "WTI"
    with pytest.raises(ValueError, match="hedged_pair_of"):
        Instrument(instrument_id="SPY", ticker="SPY", structure="physical", hedged_pair_of="EFA")
    with pytest.raises(ValueError, match="expense_ratio"):
        Instrument(instrument_id="SPY", ticker="SPY", structure="physical", expense_ratio=1.5)


def test_openfigi_mapping_uses_mock_transport():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        jobs = json.loads(request.content)
        payload = []
        for job in jobs:
            ticker = job["idValue"]
            assert job["idType"] == "TICKER"
            assert job["exchCode"] == "US"
            if ticker == "NOPE":
                payload.append({"error": "No identifier found."})
            elif ticker == "MIX":
                payload.append(
                    {
                        "data": [
                            {"figi": "STOCK", "compositeFIGI": "STOCKC", "securityType": "Common Stock"},
                            {"figi": "ETF", "compositeFIGI": "ETFC", "securityType": "ETP"},
                        ]
                    }
                )
            else:
                payload.append({"data": [{"figi": f"FIGI{ticker}", "compositeFIGI": f"BBG{ticker}"}]})
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    anonymous = OpenFigiClient(api_key="", transport=transport)
    tickers = [f"T{i}" for i in range(11)]
    mapped = anonymous.map_tickers(tickers)
    assert len(calls) == 2  # 10 jobs per request without a key
    assert "X-OPENFIGI-APIKEY" not in calls[0].headers
    assert mapped["T0"] == "BBGT0"
    assert mapped["T10"] == "BBGT10"
    assert str(calls[0].url).rstrip("/") == "https://api.openfigi.com/v3/mapping"

    calls.clear()
    keyed = OpenFigiClient(api_key="test-key", transport=transport)
    keyed_map = keyed.map_tickers(["SPY", "SPY", "NOPE", "MIX"])
    assert len(calls) == 1
    assert calls[0].headers["X-OPENFIGI-APIKEY"] == "test-key"
    assert keyed_map == {"SPY": "BBGSPY", "NOPE": None, "MIX": "ETFC"}


def test_openfigi_http_error_and_no_live_socket():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    client = OpenFigiClient(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        client.map_tickers(["SPY"])

    live = OpenFigiClient(api_key="k")
    with pytest.raises(RuntimeError, match="network"):
        live.map_tickers(["SPY"])
