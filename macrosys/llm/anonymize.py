"""Anonymise historical cases before a model sees them (spec §10.4, §8.3 anti-leakage).

Named entities become ``ENTITY_k``, absolute dates become offsets from the earliest date in the
text (``T0``, ``T0+3m``), and large round numbers become magnitude buckets. The mapping is
deterministic for a seed. Callers anonymise historical case text before it enters a role prompt
and deanonymise structural tokens in the output. Roles are still not asked for magnitudes,
outcomes, or what happened.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)"
)
_DATE = re.compile(
    rf"(?<!\w)(?P<iso>\d{{4}}-\d{{2}}-\d{{2}})(?!\w)"
    rf"|(?<!\w)(?P<mdy>{_MONTH}\s+\d{{1,2}},\s+\d{{4}})(?!\w)"
    rf"|(?<!\w)(?P<dmy>\d{{1,2}}\s+{_MONTH}\s+\d{{4}})(?!\w)"
    rf"|(?<!\w)(?P<my>{_MONTH}\s+\d{{4}})(?!\w)",
    re.IGNORECASE,
)
_ROUND = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d{4,})(?![\w])")


class AnonymizationMap(BaseModel):
    """Placeholder to original surface. ``deanonymize`` walks tokens longest-first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int
    entities: dict[str, str] = Field(default_factory=dict)
    dates: dict[str, str] = Field(default_factory=dict)
    numbers: dict[str, str] = Field(default_factory=dict)


def deanonymize(text: str, mapping: AnonymizationMap) -> str:
    """Replace placeholders with the original surfaces stored in ``mapping`` (spec §10.4).

    Longer tokens are replaced first so ``T0`` does not consume the start of ``T0+3m``.
    """
    pairs: list[tuple[str, str]] = []
    pairs.extend(mapping.entities.items())
    pairs.extend(mapping.dates.items())
    pairs.extend(mapping.numbers.items())
    for token, original in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(token, original)
    return text


class Anonymizer:
    """Seeded replacement of entities, dates, and large round numbers (spec §10.4)."""

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed

    def anonymize(
        self,
        text: str,
        entities: list[str],
        dates: bool = True,
        round_numbers: bool = True,
    ) -> tuple[str, AnonymizationMap]:
        """Return anonymised text and the mapping. The same seed and inputs yield the same pair.

        Entity indexes follow ``sha256(seed, name)`` order, then the longest surface is applied
        first so a shorter name does not cut a longer one. The earliest parsed date is ``T0``.
        A number of at least 1,000 that is divisible by 100 becomes ``BUCKET_1eN``.
        """
        rewritten, entity_map = _replace_entities(text, _labels(entities, self.seed))
        date_map: dict[str, str] = {}
        if dates:
            rewritten, date_map = _replace_dates(rewritten)
        number_map: dict[str, str] = {}
        if round_numbers:
            rewritten, number_map = _replace_round_numbers(rewritten)
        mapping = AnonymizationMap(
            seed=self.seed, entities=entity_map, dates=date_map, numbers=number_map
        )
        return rewritten, mapping


def _labels(entities: list[str], seed: int) -> dict[str, str]:
    unique = [entity for entity in dict.fromkeys(entities) if entity]
    ordered = sorted(unique, key=lambda entity: hashlib.sha256(f"{seed}\n{entity}".encode()).digest())
    return {entity: f"ENTITY_{index}" for index, entity in enumerate(ordered)}


def _replace_entities(text: str, labels: dict[str, str]) -> tuple[str, dict[str, str]]:
    spans: list[tuple[int, int, str]] = []
    for original, placeholder in labels.items():
        pattern = re.compile(rf"\b{re.escape(original)}\b")
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), placeholder))
    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    chosen: list[tuple[int, int, str]] = []
    cursor = 0
    used: dict[str, str] = {}
    originals = {placeholder: original for original, placeholder in labels.items()}
    for start, end, placeholder in spans:
        if start < cursor:
            continue
        chosen.append((start, end, placeholder))
        used[placeholder] = originals[placeholder]
        cursor = end
    return _splice(text, chosen), used


def _parse_date(match: re.Match[str]) -> tuple[date, bool]:
    if match.group("iso"):
        return date.fromisoformat(match.group("iso")), False
    if match.group("mdy"):
        month_name, day, year = re.match(
            rf"({_MONTH})\s+(\d{{1,2}}),\s+(\d{{4}})", match.group("mdy"), re.IGNORECASE
        ).groups()  # type: ignore[union-attr]
        return date(int(year), _MONTHS[month_name.lower()], int(day)), False
    if match.group("dmy"):
        day, month_name, year = re.match(
            rf"(\d{{1,2}})\s+({_MONTH})\s+(\d{{4}})", match.group("dmy"), re.IGNORECASE
        ).groups()  # type: ignore[union-attr]
        return date(int(year), _MONTHS[month_name.lower()], int(day)), False
    month_name, year = re.match(
        rf"({_MONTH})\s+(\d{{4}})", match.group("my"), re.IGNORECASE
    ).groups()  # type: ignore[union-attr]
    return date(int(year), _MONTHS[month_name.lower()], 1), True


def _relative(anchor: date, when: date, month_only: bool) -> str:
    if when == anchor and not month_only:
        return "T0"
    months = (when.year - anchor.year) * 12 + (when.month - anchor.month)
    if month_only or when.day == anchor.day:
        if months == 0:
            return "T0"
        return f"T0{months:+d}m"
    days = (when - anchor).days
    if days == 0:
        return "T0"
    return f"T0{days:+d}d"


def _unique_token(token: str, used: dict[str, str], surface: str) -> str:
    if token not in used or used[token] == surface:
        return token
    index = 2
    while f"{token}_{index}" in used:
        index += 1
    return f"{token}_{index}"


def _replace_dates(text: str) -> tuple[str, dict[str, str]]:
    parsed: list[tuple[int, int, str, date, bool]] = []
    for match in _DATE.finditer(text):
        when, month_only = _parse_date(match)
        parsed.append((match.start(), match.end(), match.group(0), when, month_only))
    if not parsed:
        return text, {}
    anchor = min(item[3] for item in parsed)
    used: dict[str, str] = {}
    spans: list[tuple[int, int, str]] = []
    for start, end, surface, when, month_only in parsed:
        token = _unique_token(_relative(anchor, when, month_only), used, surface)
        used[token] = surface
        spans.append((start, end, token))
    return _splice(text, spans), used


def _bucket(value: int) -> str:
    exponent = int(math.floor(math.log10(value)))
    return f"BUCKET_1e{exponent}"


def _replace_round_numbers(text: str) -> tuple[str, dict[str, str]]:
    used: dict[str, str] = {}
    spans: list[tuple[int, int, str]] = []
    for match in _ROUND.finditer(text):
        surface = match.group(1)
        value = int(surface.replace(",", ""))
        if value < 1000 or value % 100 != 0:
            continue
        token = _unique_token(_bucket(value), used, surface)
        used[token] = surface
        spans.append((match.start(), match.end(), token))
    return _splice(text, spans), used


def _splice(text: str, spans: list[tuple[int, int, str]]) -> str:
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for start, end, token in spans:
        pieces.append(text[cursor:start])
        pieces.append(token)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)
