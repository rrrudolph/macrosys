"""TF-IDF retrieval over the mechanism library, cases, and documents (spec §8.3)."""

from __future__ import annotations

import hashlib

import duckdb

from macrosys.llm.retrieval import Retriever
from macrosys.mechanisms.library import load_library
from macrosys.store.documents import Document, DocumentStore
from macrosys.store.schema import create_schema
from tests.llm.fakes import FIXTURES, ROOT


def test_inventory_query_returns_the_inventory_mechanism() -> None:
    retriever = Retriever(token_budget=4000, chars_per_token=4)
    retriever.add_library(load_library(ROOT / "mechanisms"))
    hits = retriever.retrieve("inventory days of cover", k=3)
    assert hits
    assert hits[0][0] == "mechanism:B04_inventory_cycle"
    assert hits[0][1] > hits[1][1] if len(hits) > 1 else True
    assert "days of cover" in hits[0][2].lower()


def test_token_budget_trims_the_returned_text() -> None:
    retriever = Retriever(token_budget=1, chars_per_token=4)
    retriever.add_library(load_library(ROOT / "mechanisms"))
    hits = retriever.retrieve("inventory days of cover", k=5)
    assert len(hits) == 1
    assert len(hits[0][2]) <= 4
    assert hits[0][0] == "mechanism:B04_inventory_cycle"


def test_case_summaries_and_document_rows() -> None:
    cases = Retriever(token_budget=500)
    cases.add_case_dir(FIXTURES / "cases")
    case_hits = cases.retrieve("canal blockage diverted ships", k=1)
    assert case_hits[0][0] == "case:suez"

    text = "zzqwidget inventory bulletin"
    document = Document(doc_id="doc-zz", content_hash=hashlib.sha256(text.encode()).hexdigest(), source="fixture")
    connection = duckdb.connect(":memory:")
    create_schema(connection)
    inserted = DocumentStore(connection).append([document])
    assert inserted == 1
    documents = Retriever(token_budget=200)
    documents.add_documents([document], {document.doc_id: text})
    doc_hits = documents.retrieve("zzqwidget bulletin", k=1)
    assert doc_hits[0][0] == "document:doc-zz"
    assert "zzqwidget" in doc_hits[0][2]
