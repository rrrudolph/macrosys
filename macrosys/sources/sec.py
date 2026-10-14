"""SEC EDGAR companyfacts and Form N-PORT holdings (spec §9.1, §9.2, §9.6).

`companyfacts` observations are one row per `(CIK, taxonomy, concept, unit, period)`. `published_at`
is the `filed` date at 23:59:59 America/New_York, converted to UTC. `vintage_id` is the accession
number. Series ids are `SEC:<CIK>:<taxonomy>:<concept>:<unit>` using the 10-digit CIK and the SEC unit
token. The observation's `unit` is the controlled-vocabulary label (`USD`, `shares` → `count`,
`pure` → `ratio`).

N-PORT tables and columns are those in the SEC readme PDF
(https://www.sec.gov/files/nport_readme.pdf, read 2026-10-14): `SUBMISSION` (`FILING_DATE`,
`REPORT_DATE`), `FUND_REPORTED_INFO` (`SERIES_ID`), `FUND_REPORTED_HOLDING` (including `ISSUER_CUSIP`,
`ISSUER_TITLE`, `CURRENCY_CODE`, `CURRENCY_VALUE`, `PERCENTAGE`, `ASSET_CAT`, `INVESTMENT_COUNTRY`),
and `IDENTIFIERS` (`IDENTIFIER_ISIN`). `PERCENTAGE` is a percent of net assets (Item C.2.d), stored as
a share of NAV. `CURRENCY_VALUE` is the Item C.2.c value, which the form requires in US dollars, so
`ccy_mv` is `USD`. The holding table has no sector column, so `sector` is None. TSV dates are taken
as ISO-8601; a quarterly ZIP was not downloaded (each is hundreds of MB). Asset-category codes follow
Form N-PORT Item C.4.a. An unknown code raises ParserChangedError.

User-Agent is env `SEC_USER_AGENT` (spec §9.5). There is no API key. The rate limit is the registry's
10 requests per second.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from macrosys.core.contracts import (
    Observation,
    ParserChangedError,
    RawPayload,
    RawStore,
    ReleaseEvent,
    SeriesMeta,
    SourceTier,
)
from macrosys.core.units import UnitError, assert_unit
from macrosys.sources.common import (
    ET,
    end_of_day,
    fetched_at,
    load_json,
    require_keys,
    require_mapping,
    source_url,
)
from macrosys.sources.iso3166 import to_iso3
from macrosys.sources.records import AssetClass, HoldingSnapshotRow
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"

_SEC_UNITS = {
    "shares": "count",
    "pure": "ratio",
}

# Item C.4.a codes from the N-PORT form. Unknown non-blank codes quarantine the file (spec §9.4).
_ASSET: dict[str, AssetClass] = {
    "EC": "equity",
    "EP": "equity",
    "DBT": "fixed_income",
    "LON": "fixed_income",
    "SN": "fixed_income",
    "ABS-MBS": "fixed_income",
    "ABS-APCP": "fixed_income",
    "ABS-CBDO": "fixed_income",
    "ABS-O": "fixed_income",
    "STIV": "cash",
    "COMM": "commodity",
    "DE": "derivative",
    "DF": "derivative",
    "DFE": "derivative",
    "DO": "derivative",
    "DCO": "derivative",
    "DS": "derivative",
    "RE": "other",
    "FUND": "fund",
}

_HOLDING_COLUMNS = (
    "ACCESSION_NUMBER",
    "HOLDING_ID",
    "CURRENCY_CODE",
    "PERCENTAGE",
    "ASSET_CAT",
)


def map_sec_unit(token: str) -> str:
    """Map a companyfacts unit token onto the controlled vocabulary (spec §9.1)."""
    label = _SEC_UNITS.get(token, token)
    try:
        assert_unit(label)
    except UnitError as exc:
        raise ValueError(f"SEC unit {token!r} has no mapping in parser {PARSER_VERSION}") from exc
    return label


def _cik(value: Any) -> str:
    return str(value).strip().zfill(10)


def _iso_date(value: str, *, what: str) -> date:
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise ParserChangedError(f"SEC {what} {value!r} is not an ISO date") from exc


def parse_companyfacts(payload: RawPayload, content: bytes) -> list[Observation]:
    """Parse a `companyfacts` JSON document into vintage observations (spec §9.1, §9.2)."""
    doc = require_mapping(load_json(content, what="SEC companyfacts"), what="SEC companyfacts")
    require_keys(doc, ("cik", "facts"), what="SEC companyfacts")
    facts = doc["facts"]
    if not isinstance(facts, dict):
        raise ParserChangedError("SEC companyfacts `facts` is not an object")
    cik = _cik(doc["cik"])
    ingested = fetched_at(payload)
    url = source_url(payload)
    rows: list[Observation] = []
    for taxonomy, concepts in facts.items():
        if not isinstance(concepts, dict):
            raise ParserChangedError(f"SEC taxonomy {taxonomy!r} is not an object")
        for concept, body in concepts.items():
            if not isinstance(body, dict) or not isinstance(body.get("units"), dict):
                raise ParserChangedError(f"SEC concept {concept} is missing units")
            for unit_name, entries in body["units"].items():
                if not isinstance(entries, list):
                    raise ParserChangedError(f"SEC {concept} {unit_name} facts are not a list")
                unit = map_sec_unit(str(unit_name))
                series_id = f"SEC:{cik}:{taxonomy}:{concept}:{unit_name}"
                for fact in entries:
                    if not isinstance(fact, dict):
                        raise ParserChangedError(f"SEC {concept} fact is not an object")
                    require_keys(fact, ("end", "val", "accn", "filed"), what=f"SEC {concept}")
                    end = _iso_date(str(fact["end"]), what="fact end")
                    start = _iso_date(str(fact["start"]), what="fact start") if fact.get("start") else end
                    filed = _iso_date(str(fact["filed"]), what="filed")
                    rows.append(
                        Observation(
                            series_id=series_id,
                            ref_period_start=start,
                            ref_period_end=end,
                            value=float(fact["val"]) if fact["val"] is not None else None,
                            unit=unit,
                            published_at=end_of_day(filed, ET),
                            ingested_at=ingested,
                            vintage_id=str(fact["accn"]),
                            source_url=url,
                            source_hash=payload.sha256,
                            quality_flag="ok",
                        )
                    )
    return rows


def _asset_class(code: str) -> AssetClass:
    text = code.strip().upper()
    if not text:
        return "other"
    try:
        return _ASSET[text]
    except KeyError as exc:
        raise ParserChangedError(f"N-PORT ASSET_CAT {code!r} has no mapping in parser {PARSER_VERSION}") from exc


def _tables(blob: bytes) -> dict[str, str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ParserChangedError("N-PORT payload is not a ZIP") from exc
    found: dict[str, str] = {}
    for name in archive.namelist():
        base = Path(name).name
        if base.endswith(".tsv"):
            found[base] = archive.read(name).decode("utf-8-sig")
    archive.close()
    return found


def _read_tsv(text: str, *, what: str, required: tuple[str, ...]) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    fieldnames = list(reader.fieldnames or [])
    missing = [column for column in required if column not in fieldnames]
    if missing:
        raise ParserChangedError(f"N-PORT {what} is missing {missing}")
    return [dict(row) for row in reader]


def _blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def parse_nport_zip(content: bytes) -> list[HoldingSnapshotRow]:
    """Read one quarterly N-PORT ZIP into holding rows (spec §9.1, §9.6).

    `weight` is `PERCENTAGE / 100`. A holding with a blank percentage is omitted rather than stored
    as zero. `instrument_id` is `SERIES_ID`. `holding_key` prefers ISIN, then CUSIP, then another
    identifier, then the holding id.
    """
    tables = _tables(content)
    for name in ("SUBMISSION.tsv", "FUND_REPORTED_INFO.tsv", "FUND_REPORTED_HOLDING.tsv"):
        if name not in tables:
            raise ParserChangedError(f"N-PORT ZIP is missing {name}")
    submissions = {
        row["ACCESSION_NUMBER"]: row
        for row in _read_tsv(tables["SUBMISSION.tsv"], what="SUBMISSION", required=("ACCESSION_NUMBER", "FILING_DATE", "REPORT_DATE"))
    }
    funds = {
        row["ACCESSION_NUMBER"]: row
        for row in _read_tsv(
            tables["FUND_REPORTED_INFO.tsv"],
            what="FUND_REPORTED_INFO",
            required=("ACCESSION_NUMBER", "SERIES_ID"),
        )
    }
    identifiers: dict[str, list[dict[str, str]]] = {}
    if "IDENTIFIERS.tsv" in tables:
        for row in _read_tsv(tables["IDENTIFIERS.tsv"], what="IDENTIFIERS", required=("HOLDING_ID",)):
            identifiers.setdefault(row["HOLDING_ID"], []).append(row)
    holdings = _read_tsv(
        tables["FUND_REPORTED_HOLDING.tsv"],
        what="FUND_REPORTED_HOLDING",
        required=_HOLDING_COLUMNS,
    )
    rows: list[HoldingSnapshotRow] = []
    for holding in holdings:
        percent = holding.get("PERCENTAGE") or ""
        if _blank(percent):
            continue
        accession = holding["ACCESSION_NUMBER"]
        submission = submissions.get(accession)
        fund = funds.get(accession)
        if submission is None or fund is None:
            raise ParserChangedError(f"N-PORT holding accession {accession} has no submission or series row")
        series_id = (fund.get("SERIES_ID") or "").strip()
        if not series_id:
            raise ParserChangedError(f"N-PORT accession {accession} has no SERIES_ID")
        filed = _iso_date(submission["FILING_DATE"], what="FILING_DATE")
        as_of = _iso_date(submission["REPORT_DATE"], what="REPORT_DATE")
        rows.append(
            HoldingSnapshotRow(
                instrument_id=series_id,
                as_of=as_of,
                published_at=end_of_day(filed, ET),
                source="SEC",
                holding_key=_holding_key(holding, identifiers.get(holding["HOLDING_ID"], [])),
                name=(holding.get("ISSUER_TITLE") or holding.get("ISSUER_NAME") or "").strip(),
                weight=float(percent) / 100.0,
                country=to_iso3(holding.get("INVESTMENT_COUNTRY")),
                sector=None,
                ccy=(holding.get("CURRENCY_CODE") or "").strip(),
                asset_class=_asset_class(holding.get("ASSET_CAT") or ""),
                market_value=_optional_float(holding.get("CURRENCY_VALUE")),
                ccy_mv="USD" if not _blank(holding.get("CURRENCY_VALUE")) else None,
            )
        )
    return rows


def _optional_float(value: str | None) -> float | None:
    if _blank(value):
        return None
    return float(value or "")


def _holding_key(holding: dict[str, str], identifiers: list[dict[str, str]]) -> str:
    for row in identifiers:
        isin = (row.get("IDENTIFIER_ISIN") or "").strip()
        if isin:
            return isin
    cusip = (holding.get("ISSUER_CUSIP") or "").strip()
    if cusip:
        return cusip
    for row in identifiers:
        other = (row.get("OTHER_IDENTIFIER") or "").strip()
        if other:
            return other
    return f"HOLDING:{holding['HOLDING_ID']}"


class SecSource:
    """SEC EDGAR `DataSource` for companyfacts (spec §9.6). Env: `SEC_USER_AGENT`."""

    source_id = "SEC"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore, *, transport: Any = None, sleeper: Any = None) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)

    def discover(self) -> list[SeriesMeta]:
        """Companyfacts concepts are discovered by parsing a filing (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """Download `companyfacts` for a CIK. `series_id` is `SEC:<CIK>` or a bare CIK (spec §9.6)."""
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        cik = _cik(series_id.removeprefix("SEC:").split(":", 1)[0])
        base = self.cfg.access.base_url or ""
        return client.get(urljoin(base, f"api/xbrl/companyfacts/CIK{cik}.json"))

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Parse companyfacts JSON. See `parse_companyfacts` (spec §9.1)."""
        return parse_companyfacts(payload, content)

    def schedule(self) -> list[ReleaseEvent]:
        """EDGAR has no single release calendar in this connector (spec §9.1). Returns []."""
        return []
