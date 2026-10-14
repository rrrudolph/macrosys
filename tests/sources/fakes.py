"""In-memory raw store and a clock for connector tests (spec §9.1). No network."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from macrosys.core.contracts import RawPayload


class MemoryRawStore:
    """RawStore that keeps bytes in a dict. Paths follow `raw/<source>/<yyyy>/<mm>/<sha256>`."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.payloads: list[RawPayload] = []

    def put(
        self,
        source_id: str,
        request: dict[str, Any],
        content: bytes,
        *,
        status: int,
        content_type: str | None,
        fetched_at: datetime,
    ) -> RawPayload:
        digest = hashlib.sha256(content).hexdigest()
        payload = RawPayload(
            source_id=source_id,
            request=request,
            fetched_at=fetched_at,
            status=status,
            content_type=content_type,
            sha256=digest,
            path=f"raw/{source_id}/{fetched_at:%Y}/{fetched_at:%m}/{digest}",
            size_bytes=len(content),
        )
        self.blobs[digest] = content
        self.payloads.append(payload)
        return payload

    def get(self, payload: RawPayload) -> bytes:
        return self.blobs[payload.sha256]

    def exists(self, sha256: str) -> bool:
        return sha256 in self.blobs


class FakeClock:
    """Monotonic clock whose `sleep` advances the same counter the bucket reads."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)


def stored(
    source_id: str,
    content: bytes,
    request: dict[str, Any],
    when: datetime,
    *,
    content_type: str | None = "application/json",
) -> tuple[MemoryRawStore, RawPayload]:
    """Put one payload and return the store plus the record parsers receive."""
    store = MemoryRawStore()
    payload = store.put(
        source_id,
        request,
        content,
        status=200,
        content_type=content_type,
        fetched_at=when,
    )
    return store, payload
