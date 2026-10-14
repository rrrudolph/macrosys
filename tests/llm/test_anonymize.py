"""Historical-case anonymisation (spec §10.4)."""

from __future__ import annotations

import re

from macrosys.llm.anonymize import Anonymizer, deanonymize
from tests.llm.fakes import FIXTURES

_ENTITIES = ["Tohoku", "Bank of Japan", "Toyota"]
_MONTHS = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b",
    re.IGNORECASE,
)


def test_historical_paragraph_loses_entities_and_absolute_dates() -> None:
    paragraph = (FIXTURES / "historical.txt").read_text(encoding="utf-8")
    anonymizer = Anonymizer(seed=7)
    text, mapping = anonymizer.anonymize(paragraph, _ENTITIES)
    again, mapping_again = anonymizer.anonymize(paragraph, _ENTITIES)
    assert text == again
    assert mapping == mapping_again
    for entity in _ENTITIES:
        assert entity not in text
    assert _MONTHS.search(text) is None
    assert re.search(r"\b20\d{2}\b", text) is None
    assert "10,000,000" not in text
    assert "10000000" not in text
    assert "T0" in text
    assert "T0+3m" in text
    assert "BUCKET_1e7" in text
    assert any(token.startswith("ENTITY_") for token in mapping.entities)
    restored = deanonymize(text, mapping)
    assert "Tohoku" in restored
    assert "March 11, 2011" in restored
    assert "10,000,000" in restored
