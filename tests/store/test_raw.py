import hashlib
import json
from datetime import UTC, datetime

import pytest

from macrosys.core.contracts import RawStore
from macrosys.store.raw import FileRawStore

FETCHED = datetime(2026, 10, 14, 15, tzinfo=UTC)
BODY = b'{"observations":[]}'


def _request() -> dict:
    return {
        "url": "https://api.stlouisfed.org/fred/series/observations",
        "params": {"api_key": "SECRET", "apikey": "SECRET", "api-key": "SECRET", "series_id": "GDPC1"},
        "headers": {
            "Authorization": "Bearer SECRET",
            "Ocp-Apim-Subscription-Key": "SECRET",
            "X-App-Token": "SECRET",
            "Accept": "application/json",
        },
        "password": "SECRET",
        "signature": "SECRET",
        "session": "SECRET",
    }


def test_raw_store_redacts_secrets_and_is_content_addressed(tmp_path):
    store = FileRawStore(tmp_path)
    assert isinstance(store, RawStore)
    request = _request()
    payload = store.put(
        "FRED",
        request,
        BODY,
        status=200,
        content_type="application/json",
        fetched_at=FETCHED,
    )

    assert request["params"]["api_key"] == "SECRET"  # caller's dict is not mutated
    assert payload.sha256 == hashlib.sha256(BODY).hexdigest()
    assert payload.path == f"raw/FRED/2026/10/{payload.sha256}"
    assert payload.size_bytes == len(BODY)
    assert payload.request["params"]["series_id"] == "GDPC1"
    assert payload.request["headers"]["Accept"] == "application/json"
    for key in ("api_key", "apikey", "api-key"):
        assert payload.request["params"][key] == "<redacted>"
    assert payload.request["headers"]["Authorization"] == "<redacted>"
    assert payload.request["headers"]["Ocp-Apim-Subscription-Key"] == "<redacted>"
    assert payload.request["headers"]["X-App-Token"] == "<redacted>"
    assert payload.request["password"] == "<redacted>"
    assert payload.request["signature"] == "<redacted>"
    assert payload.request["session"] == "<redacted>"

    again = store.put(
        "BLS",
        {"api_key": "OTHER-SECRET"},
        BODY,
        status=200,
        content_type="application/json",
        fetched_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert again == payload
    assert store.exists(payload.sha256)
    assert not store.exists("b" * 64)
    assert store.get(payload) == BODY

    files = list(tmp_path.rglob(payload.sha256))
    assert len(files) == 1
    sidecar = files[0].with_name(payload.sha256 + ".json")
    text = sidecar.read_text(encoding="utf-8")
    assert "SECRET" not in text
    assert "OTHER-SECRET" not in text
    assert json.loads(text)["sha256"] == payload.sha256


def test_raw_store_rejects_path_like_source_ids(tmp_path):
    store = FileRawStore(tmp_path)
    with pytest.raises(ValueError):
        store.put("FRED/../x", {}, b"abc", status=200, content_type=None, fetched_at=FETCHED)
