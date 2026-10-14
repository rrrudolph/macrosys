"""TF-IDF retrieval over the library, case summaries, and documents (spec §8.3, §10.4).

The corpus is mechanism text (id, name, assumptions, evidence, falsification), ``*.txt`` case
summaries, and ``Document`` rows. ``DocumentStore`` keeps metadata rather than the body, so the
caller supplies the retained excerpt keyed by ``doc_id``. ``retrieve`` ranks with cosine TF-IDF
and trims the returned text to a token budget using the four-characters-per-token heuristic.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from macrosys.mechanisms.library import Library
from macrosys.mechanisms.spec import MechanismSpec
from macrosys.store.documents import Document

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an the of and or to in for on with by is are was be as at from that this not".split()
)


@dataclass(frozen=True)
class _Doc:
    doc_id: str
    text: str
    tokens: list[str]


class Retriever:
    """In-memory TF-IDF index with a token budget (spec §8.3). No network and no extra dependencies."""

    def __init__(self, *, token_budget: int, chars_per_token: float = 4.0) -> None:
        if token_budget < 1:
            raise ValueError("token_budget must be >= 1")
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self._token_budget = token_budget
        self._chars_per_token = chars_per_token
        self._docs: list[_Doc] = []

    def add(self, doc_id: str, text: str) -> None:
        """Index ``text`` under ``doc_id``, replacing a previous row with the same id."""
        if not doc_id:
            raise ValueError("doc_id is empty")
        self._docs = [doc for doc in self._docs if doc.doc_id != doc_id]
        self._docs.append(_Doc(doc_id=doc_id, text=text, tokens=tokenize(text)))

    def add_library(self, library: Library) -> None:
        """Index each mechanism's id, name, assumptions, evidence, and falsification (spec §8.3)."""
        for mechanism_id in library.ids():
            spec = library.get(mechanism_id)
            self.add(f"mechanism:{spec.id}", mechanism_text(spec))

    def add_case_dir(self, directory: str | Path) -> None:
        """Index ``*.txt`` case summaries. The doc id is ``case:<stem>`` (spec §8.3, §10.1)."""
        root = Path(directory)
        if not root.is_dir():
            raise ValueError(f"case directory does not exist: {root}")
        for path in sorted(root.glob("*.txt")):
            self.add(f"case:{path.stem}", path.read_text(encoding="utf-8"))

    def add_documents(self, documents: Sequence[Document], texts: Mapping[str, str]) -> None:
        """Index ``Document`` rows (spec §4.7, §8.3).

        ``texts`` is the retained excerpt keyed by ``doc_id``. A row with no excerpt is indexed
        on its id, source, and url so the document is still findable.
        """
        for document in documents:
            body = texts.get(document.doc_id)
            if not body:
                body = " ".join(part for part in (document.doc_id, document.source, document.url) if part)
            self.add(f"document:{document.doc_id}", body)

    def retrieve(self, query: str, k: int) -> list[tuple[str, float, str]]:
        """Top ``k`` documents as ``(doc_id, score, text)``, trimmed to the token budget.

        Scores are cosine similarity of TF-IDF vectors. Text is cut from the end once the
        character budget (``token_budget * chars_per_token``) is spent, lowest ranks first to go.
        """
        if k < 1:
            raise ValueError("k must be >= 1")
        query_tokens = tokenize(query)
        if not query_tokens or not self._docs:
            return []
        scores = _scores(self._docs, query_tokens)
        order = sorted(range(len(self._docs)), key=lambda index: (-float(scores[index]), self._docs[index].doc_id))
        budget_chars = int(self._token_budget * self._chars_per_token)
        used = 0
        picked: list[tuple[str, float, str]] = []
        for index in order:
            if len(picked) >= k or used >= budget_chars:
                break
            score = float(scores[index])
            if score <= 0:
                continue
            room = budget_chars - used
            text = self._docs[index].text
            if len(text) > room:
                text = text[:room]
            used += len(text)
            picked.append((self._docs[index].doc_id, score, text))
        return picked


def mechanism_text(spec: MechanismSpec) -> str:
    """The fields a role is allowed to retrieve for one mechanism (spec §8.3 role 2)."""
    return "\n".join(
        [
            spec.id,
            spec.name,
            "\n".join(spec.assumptions),
            "\n".join(spec.evidence),
            spec.falsification,
        ]
    )


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens with a small stop list. Used by the TF-IDF index (spec §8.3)."""
    return [token for token in _TOKEN.findall(text.lower()) if token not in _STOP and len(token) > 1]


def _scores(docs: list[_Doc], query_tokens: list[str]) -> np.ndarray:
    df: dict[str, int] = {}
    counts: list[dict[str, int]] = []
    for doc in docs:
        local: dict[str, int] = {}
        for token in doc.tokens:
            local[token] = local.get(token, 0) + 1
        counts.append(local)
        for token in local:
            df[token] = df.get(token, 0) + 1
    n_docs = len(docs)
    idf = {token: math.log((n_docs + 1) / (freq + 1)) + 1.0 for token, freq in df.items()}
    query_counts: dict[str, int] = {}
    for token in query_tokens:
        query_counts[token] = query_counts.get(token, 0) + 1
    vocab = sorted(set(df) | set(query_counts))
    index = {token: position for position, token in enumerate(vocab)}
    matrix = np.zeros((n_docs, len(vocab)), dtype=float)
    for row, local in enumerate(counts):
        length = max(sum(local.values()), 1)
        for token, count in local.items():
            matrix[row, index[token]] = (count / length) * idf[token]
    query_vec = np.zeros(len(vocab), dtype=float)
    query_length = max(sum(query_counts.values()), 1)
    for token, count in query_counts.items():
        if token not in index:
            continue
        weight = idf.get(token, math.log(n_docs + 1) + 1.0)
        query_vec[index[token]] = (count / query_length) * weight
    query_norm = float(np.linalg.norm(query_vec))
    doc_norm = np.linalg.norm(matrix, axis=1)
    denom = doc_norm * query_norm
    scores = np.zeros(n_docs, dtype=float)
    np.divide(matrix @ query_vec, denom, out=scores, where=denom > 0)
    return scores
