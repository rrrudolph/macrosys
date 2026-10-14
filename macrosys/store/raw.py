"""Content-addressed raw payload store (spec §9.1, §9.4).

Bytes live at ``raw/<source_id>/<yyyy>/<mm>/<sha256>`` under the store root. A sidecar
``<sha256>.json`` holds the ``RawPayload`` record. The same bytes are stored once: a later
``put`` of identical content returns the existing payload and does not write a second copy.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from macrosys.core.contracts import RawPayload

# Case-insensitive substring match. Hyphen and underscore are equivalent so `api-key` and
# `api_key` both redact (spec §9.1: headers and request parameters are stored with secrets removed).
_SECRET_KEY = re.compile(
    r"api[_-]?key|apikey|token|authorization|subscription[_-]?key|password|signature|session",
    re.IGNORECASE,
)
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _require_aware(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return ts.astimezone(UTC)


def redact_secrets(value: Any) -> Any:
    """Return a copy of a request tree with secret-bearing keys replaced by ``"<redacted>"``.

    A key matches when it contains, case-insensitively, one of ``api_key``, ``apikey``,
    ``api-key``, ``token``, ``authorization``, ``subscription-key``, ``password``,
    ``signature``, or ``session`` (spec §9.1). Nested dicts and lists are walked. The input
    is not modified.
    """
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY.search(key):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    return value


class FileRawStore:
    """Filesystem ``RawStore`` (spec §9.1). ``root`` is the parent of the ``raw/`` directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

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
        """Store ``content`` immutably and return its payload record (spec §9.1, §9.4).

        Idempotent on identical bytes: if this sha256 is already in the store, the existing
        sidecar is returned and nothing is rewritten. Secrets in ``request`` are redacted
        before anything is written. ``fetched_at`` selects the ``yyyy/mm`` partition of a
        first write and must be timezone-aware.
        """
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise ValueError(f"source_id must be a short token without path separators, got {source_id!r}")
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("content must be bytes")
        if not isinstance(request, dict):
            raise TypeError("request must be a dict")
        body = bytes(content)
        fetched = _require_aware(fetched_at, "fetched_at")
        digest = hashlib.sha256(body).hexdigest()

        existing = self._locate(digest)
        if existing is not None:
            return self._load_or_write_sidecar(
                existing, source_id, request, status, content_type, fetched, digest, len(body)
            )

        relative = f"raw/{source_id}/{fetched:%Y}/{fetched:%m}/{digest}"
        dest = self._resolve(relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            self._write_bytes(dest, body)
        return self._load_or_write_sidecar(
            dest, source_id, request, status, content_type, fetched, digest, len(body)
        )

    def get(self, payload: RawPayload) -> bytes:
        """Read the bytes addressed by ``payload.path`` and check they hash to ``payload.sha256``."""
        path = self._resolve(payload.path)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != payload.sha256:
            raise ValueError(f"raw payload hash mismatch at {payload.path}: stored {digest}, record {payload.sha256}")
        return data

    def exists(self, sha256: str) -> bool:
        """True when a content file named ``sha256`` is already under this store (spec §9.4 dedup)."""
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            return False
        return self._locate(sha256) is not None

    def _load_or_write_sidecar(
        self,
        content_path: Path,
        source_id: str,
        request: dict[str, Any],
        status: int,
        content_type: str | None,
        fetched_at: datetime,
        digest: str,
        size_bytes: int,
    ) -> RawPayload:
        sidecar = content_path.with_name(digest + ".json")
        if sidecar.is_file():
            return RawPayload.model_validate_json(sidecar.read_text(encoding="utf-8"))
        relative = content_path.resolve().relative_to(self.root.resolve()).as_posix()
        payload = RawPayload(
            source_id=source_id,
            request=redact_secrets(request),
            fetched_at=fetched_at,
            status=status,
            content_type=content_type,
            sha256=digest,
            path=relative,
            size_bytes=size_bytes,
        )
        self._write_bytes(sidecar, payload.model_dump_json().encode("utf-8"))
        return payload

    def _locate(self, digest: str) -> Path | None:
        raw = self.root / "raw"
        if not raw.is_dir():
            return None
        for path in raw.rglob(digest):
            if path.is_file():
                return path
        return None

    def _resolve(self, relative: str) -> Path:
        root = self.root.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("payload path escapes the raw store root")
        return path

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
