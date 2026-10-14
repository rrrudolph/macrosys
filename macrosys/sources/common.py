"""Shared parsing helpers for source connectors (spec §9.1–9.2).

Publication times are timezone-aware. A clock time this package invents is flagged
`reconstructed_pubtime` by the caller; these helpers only build the timestamp.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from macrosys.core.contracts import ParserChangedError, RawPayload

ET = ZoneInfo("America/New_York")

_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")
_YEAR = re.compile(r"^(\d{4})$")


def load_json(content: bytes, *, what: str) -> Any:
    """Decode a JSON payload or raise ParserChangedError when the bytes are not JSON (spec §9.4)."""
    try:
        return json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParserChangedError(f"{what} payload is not JSON") from exc


def require_mapping(doc: Any, *, what: str) -> dict[str, Any]:
    """Return a JSON object, or raise ParserChangedError when the top-level shape is not an object."""
    if not isinstance(doc, dict):
        raise ParserChangedError(f"{what} payload is not a JSON object")
    return doc


def require_keys(doc: dict[str, Any], keys: tuple[str, ...], *, what: str) -> None:
    """Raise ParserChangedError when an expected key is absent (spec §9.4 quarantine rule)."""
    missing = [key for key in keys if key not in doc]
    if missing:
        raise ParserChangedError(f"{what} is missing {missing}")


def aware(ts: datetime, *, what: str) -> datetime:
    """Reject a naive timestamp. Callers must not invent an offset silently (spec §9.2)."""
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{what} must be timezone-aware")
    return ts


def fetched_at(payload: RawPayload) -> datetime:
    """Return the payload's ingestion time, which parsers use as `ingested_at` (spec §9.2)."""
    return aware(payload.fetched_at, what="fetched_at")


def end_of_day(day: date, zone: ZoneInfo) -> datetime:
    """Last whole second of `day` in `zone`, converted to UTC (spec §9.2)."""
    local = datetime.combine(day, time(23, 59, 59), tzinfo=zone)
    return local.astimezone(UTC)


def parse_instant(text: str) -> datetime | None:
    """Parse an ISO timestamp that includes a clock time. A date alone returns None."""
    raw = text.strip()
    if not raw or "T" not in raw and " " not in raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def source_url(payload: RawPayload) -> str:
    """Rebuild a source URL from the stored request, leaving redacted secrets out (spec §9.1)."""
    request = payload.request
    url = str(request.get("url") or "")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return url
    kept = []
    for key, value in params.items():
        if value in (None, "", "***"):
            continue
        kept.append(f"{key}={value}")
    if not kept:
        return url
    joiner = "&" if "?" in url else "?"
    return url + joiner + "&".join(kept)


def fred_period(obs: date, frequency: str) -> tuple[date, date]:
    """Map a FRED observation date to a reference period (spec §9.1).

    Daily dates are the day. Weekly dates are treated as the week ending that day ([J], FRED's usual
    weekly convention). Monthly, quarterly, and annual dates fall in the calendar period that contains them.
    """
    if frequency == "D":
        return obs, obs
    if frequency == "W":
        return obs - timedelta(days=6), obs
    if frequency == "M":
        start = obs.replace(day=1)
        return start, obs.replace(day=calendar.monthrange(obs.year, obs.month)[1])
    if frequency == "Q":
        start_month = ((obs.month - 1) // 3) * 3 + 1
        end_month = start_month + 2
        start = date(obs.year, start_month, 1)
        end = date(obs.year, end_month, calendar.monthrange(obs.year, end_month)[1])
        return start, end
    if frequency == "A":
        return date(obs.year, 1, 1), date(obs.year, 12, 31)
    raise ParserChangedError(f"FRED frequency {frequency!r} is not one of D, W, M, Q, A")


def sdmx_period(token: str) -> tuple[date, date]:
    """Map an SDMX TIME_PERIOD token to inclusive calendar bounds (spec §9.1, ECB csvdata).

    Accepts daily `YYYY-MM-DD`, monthly `YYYY-MM`, quarterly `YYYY-Qn`, and annual `YYYY`.
    """
    text = token.strip()
    day = _DAY.fullmatch(text)
    if day:
        parsed = date(int(day.group(1)), int(day.group(2)), int(day.group(3)))
        return parsed, parsed
    month = _MONTH.fullmatch(text)
    if month:
        year, mon = int(month.group(1)), int(month.group(2))
        start = date(year, mon, 1)
        return start, date(year, mon, calendar.monthrange(year, mon)[1])
    quarter = _QUARTER.fullmatch(text)
    if quarter:
        year, q = int(quarter.group(1)), int(quarter.group(2))
        start_month = 3 * (q - 1) + 1
        end_month = start_month + 2
        start = date(year, start_month, 1)
        end = date(year, end_month, calendar.monthrange(year, end_month)[1])
        return start, end
    year = _YEAR.fullmatch(text)
    if year:
        y = int(year.group(1))
        return date(y, 1, 1), date(y, 12, 31)
    raise ParserChangedError(f"SDMX TIME_PERIOD {token!r} is not daily, monthly, quarterly, or annual")
