"""Documents and claims (spec §4.7). A claim is what a source says, not an observation.

``claims_for`` hides a claim whose ``claim_made_at`` is after ``as_of``, and hides it when the
linked document's ``published_at`` is after ``as_of``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

import duckdb
import pyarrow as pa
from pydantic import BaseModel, ConfigDict

from macrosys.core.contracts import EntityKey
from macrosys.store.schema import ensure_utc

RetainedText = Literal["full", "excerpt", "metadata_only"]


def _require_aware(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return ts


def _strings(values: list[str | None]) -> pa.Array:
    return pa.array(values, type=pa.string())


class Document(BaseModel):
    """One source text row (spec §4.7 ``document``)."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    content_hash: str
    source: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime | None = None
    retained_text_policy: RetainedText | None = None
    extraction_version: str | None = None


class Claim(BaseModel):
    """What a source says about an entity (spec §4.7 ``claim``). Not an observation."""

    model_config = ConfigDict(frozen=True)

    claim_id: str
    doc_id: str | None = None
    span: str | None = None
    claimant: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    variable: str | None = None
    value: float | None = None
    unit: str | None = None
    stage: str | None = None
    effective_date: date | None = None
    claim_made_at: datetime | None = None
    extraction_version: str | None = None
    confidence: str | None = None


class DocumentStore:
    """Append documents. A second row with the same ``doc_id`` is ignored (spec §4.7, §9.4)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    def append(self, rows: list[Document]) -> int:
        """Insert documents, ignoring duplicate ``doc_id`` values. Returns rows actually inserted.

        ``published_at`` and ``retrieved_at``, when set, must be timezone-aware (spec §9.2).
        """
        if not rows:
            return 0
        for row in rows:
            if row.published_at is not None:
                _require_aware(row.published_at, "published_at")
            if row.retrieved_at is not None:
                _require_aware(row.retrieved_at, "retrieved_at")
        table = pa.table(
            {
                "doc_id": _strings([row.doc_id for row in rows]),
                "source": _strings([row.source for row in rows]),
                "url": _strings([row.url for row in rows]),
                "published_at": pa.array(
                    [row.published_at for row in rows], type=pa.timestamp("us", tz="UTC")
                ),
                "retrieved_at": pa.array(
                    [row.retrieved_at for row in rows], type=pa.timestamp("us", tz="UTC")
                ),
                "content_hash": _strings([row.content_hash for row in rows]),
                "retained_text_policy": _strings([row.retained_text_policy for row in rows]),
                "extraction_version": _strings([row.extraction_version for row in rows]),
                "ord": pa.array(list(range(len(rows))), type=pa.int64()),
            }
        )
        self._conn.register("document_incoming", table)
        try:
            inserted = self._conn.execute(
                """
                INSERT INTO document (
                    doc_id, source, url, published_at, retrieved_at, content_hash,
                    retained_text_policy, extraction_version
                )
                SELECT
                    doc_id, source, url, published_at, retrieved_at, content_hash,
                    retained_text_policy, extraction_version
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY ord) AS rn
                    FROM document_incoming
                ) ranked
                WHERE rn = 1
                ON CONFLICT (doc_id) DO NOTHING
                """
            ).fetchone()
        finally:
            self._conn.unregister("document_incoming")
        if inserted is None:
            raise RuntimeError("document insert did not report a row count")
        return int(inserted[0])


class ClaimStore:
    """Append claims and read those knowable at ``as_of`` (spec §4.7, §9.2)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    def append(self, rows: list[Claim]) -> int:
        """Insert claims, ignoring duplicate ``claim_id`` values. Returns rows actually inserted.

        ``claim_made_at``, when set, must be timezone-aware. A ``doc_id`` must already exist on
        ``document`` because of the foreign key.
        """
        if not rows:
            return 0
        for row in rows:
            if row.claim_made_at is not None:
                _require_aware(row.claim_made_at, "claim_made_at")
        table = pa.table(
            {
                "claim_id": _strings([row.claim_id for row in rows]),
                "doc_id": _strings([row.doc_id for row in rows]),
                "span": _strings([row.span for row in rows]),
                "claimant": _strings([row.claimant for row in rows]),
                "entity_type": _strings([row.entity_type for row in rows]),
                "entity_id": _strings([row.entity_id for row in rows]),
                "variable": _strings([row.variable for row in rows]),
                "value": pa.array([row.value for row in rows], type=pa.float64()),
                "unit": _strings([row.unit for row in rows]),
                "stage": _strings([row.stage for row in rows]),
                "effective_date": pa.array([row.effective_date for row in rows], type=pa.date32()),
                "claim_made_at": pa.array(
                    [row.claim_made_at for row in rows], type=pa.timestamp("us", tz="UTC")
                ),
                "extraction_version": _strings([row.extraction_version for row in rows]),
                "confidence": _strings([row.confidence for row in rows]),
                "ord": pa.array(list(range(len(rows))), type=pa.int64()),
            }
        )
        self._conn.register("claim_incoming", table)
        try:
            inserted = self._conn.execute(
                """
                INSERT INTO claim (
                    claim_id, doc_id, span, claimant, entity_type, entity_id, variable,
                    value, unit, stage, effective_date, claim_made_at, extraction_version, confidence
                )
                SELECT
                    claim_id, doc_id, span, claimant, entity_type, entity_id, variable,
                    value, unit, stage, effective_date, claim_made_at, extraction_version, confidence
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (PARTITION BY claim_id ORDER BY ord) AS rn
                    FROM claim_incoming
                ) ranked
                WHERE rn = 1
                ON CONFLICT (claim_id) DO NOTHING
                """
            ).fetchone()
        finally:
            self._conn.unregister("claim_incoming")
        if inserted is None:
            raise RuntimeError("claim insert did not report a row count")
        return int(inserted[0])

    def claims_for(self, entity: EntityKey, as_of: datetime) -> list[Claim]:
        """Claims about ``entity`` that had been made by ``as_of`` (spec §9.2).

        ``claim_made_at`` must be at or before ``as_of``. When the claim cites a document whose
        ``published_at`` is set, that timestamp must also be at or before ``as_of``. A claim with
        no ``claim_made_at`` is not visible.
        """
        as_of = _require_aware(as_of, "as_of")
        ensure_utc(self._conn)
        rows = self._conn.execute(
            """
            SELECT
                c.claim_id, c.doc_id, c.span, c.claimant, c.entity_type, c.entity_id, c.variable,
                c.value, c.unit, c.stage, c.effective_date,
                CASE
                    WHEN c.claim_made_at IS NULL THEN NULL
                    ELSE strftime(timezone('UTC', c.claim_made_at), '%Y-%m-%dT%H:%M:%S.%fZ')
                END,
                c.extraction_version, c.confidence
            FROM claim c
            LEFT JOIN document d ON d.doc_id = c.doc_id
            WHERE c.entity_type = ?
              AND c.entity_id = ?
              AND c.claim_made_at IS NOT NULL
              AND c.claim_made_at <= ?
              AND (d.published_at IS NULL OR d.published_at <= ?)
            ORDER BY c.claim_made_at, c.claim_id
            """,
            [entity.entity_type, entity.entity_id, as_of, as_of],
        ).fetchall()
        claims: list[Claim] = []
        for row in rows:
            made = row[11]
            claims.append(
                Claim(
                    claim_id=row[0],
                    doc_id=row[1],
                    span=row[2],
                    claimant=row[3],
                    entity_type=row[4],
                    entity_id=row[5],
                    variable=row[6],
                    value=None if row[7] is None else float(row[7]),
                    unit=row[8],
                    stage=row[9],
                    effective_date=row[10],
                    claim_made_at=None if made is None else datetime.fromisoformat(made.replace("Z", "+00:00")),
                    extraction_version=row[12],
                    confidence=row[13],
                )
            )
        return claims
