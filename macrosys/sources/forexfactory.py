"""One-day Forex Factory calendar parser (spec §9.5, §9.8, backlog P2.4).

Fair Economy's notices, read 2026-10-14, say the calendar database (FEED) may not be copied,
republished, or redistributed, and that the site may not be accessed by a method other than the
interface they provide. This module is kept for posterity. It is not in the source registry, not
called by the ingest jobs, and `fetch_one_day` allows one day page per process. A historical
backfill is out of the question until Fair Economy grants written consent, or until a terms-clean
consensus source replaces it.

The page worth parsing is a single rendered day (`?day=oct14.2026`). A scripted GET of that URL
returned HTTP 403 on 2026-10-14, so `--file` is the working path: save the rendered page from a
browser. This module does not impersonate a browser to get past that refusal. The markup checked
that day, on the full rendered document, has `table.calendar__table`, event rows with
`data-event-id`, and cells `calendar__time`, `calendar__currency`, `calendar__impact`,
`calendar__event`, `calendar__actual`, `calendar__forecast`, and `calendar__previous`. The clock
is whatever `timezone_name` the page embeds (a visitor's zone, not a fixed New York clock), and
`scheduled_at` is that clock in UTC. A blank time cell repeats the previous event's time; that
inheritance is flagged rather than stored as an observed clock. "Tentative" and "Day N" are not
clock times and do not become timestamps.

Printed actual, forecast, and previous stay strings (`180K`, `-0.4%`). Stripping the suffix and
casting to float, as the older scrapers did, drops the unit. Their green/red "better"/"worse"
class is recorded and is not a score.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal, TextIO
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from macrosys.core.contracts import ParserChangedError

USER_AGENT = "macrosys-forexfactory-posterity/0.1 (personal; one calendar day; not a pipeline)"
_BASE = "https://www.forexfactory.com/calendar"
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_NUMBER = {name: number for number, name in enumerate(_MONTHS, start=1)}
_CELL_NAMES = ("date", "time", "currency", "impact", "event", "actual", "forecast", "previous")
_DAY_URL = re.compile(r"[?&]day=([a-z]{3})(\d{1,2})\.(\d{4})", re.IGNORECASE)
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})\s*([ap]m)", re.IGNORECASE)
_CELL_DAY = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})",
    re.IGNORECASE,
)
_TIMEZONE = re.compile(r"""timezone_name:\s*['"]([^'"]+)['"]""")
_REVISED_FROM = re.compile(r"^Revised from\s+(.+)$", re.IGNORECASE)

Impact = Literal["high", "medium", "low", "unknown"]


class CalendarPrint(BaseModel):
    """One calendar row from a single-day page (spec §9.8). Values are the printed text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    currency: str
    event: str
    impact: Impact
    impact_label: str | None
    scheduled_at: datetime | None
    time_label: str
    time_inferred: bool
    actual: str | None
    forecast: str | None
    previous: str | None
    previous_revised_from: str | None
    actual_marker: str | None
    page_timezone: str
    source_url: str


class _Cell:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.span_class = ""
        self.span_title: str | None = None

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())


class _Row:
    def __init__(self, event_id: str | None) -> None:
        self.event_id = event_id
        self.cells: dict[str, _Cell] = {}


class _PageParser(HTMLParser):
    """Collect the day URL, the embedded timezone, and event rows (spec §9.8)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_url: str | None = None
        self.timezone_name: str | None = None
        self.rows: list[_Row] = []
        self._script: list[str] = []
        self._in_raw = False
        self._row: _Row | None = None
        self._cell_name: str | None = None
        self._cell: _Cell | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._in_raw:
            return
        attr = {key: value for key, value in attrs}
        if tag == "meta" and attr.get("property") == "og:url" and self.og_url is None:
            self.og_url = attr.get("content")
        if tag in {"script", "style"}:
            self._in_raw = True
            self._script = []
            return
        if tag == "tr":
            self._row = _Row(attr.get("data-event-id"))
        if tag == "td" and self._row is not None:
            self._cell_name = _cell_name(attr.get("class") or "")
            self._cell = _Cell()
        if tag == "span" and self._cell is not None:
            span_class = attr.get("class") or ""
            title = attr.get("title")
            if not self._cell.span_class and span_class:
                self._cell.span_class = span_class
            if title and (self._cell.span_title is None or "impact" in (self._cell_name or "")):
                self._cell.span_title = title
                if span_class:
                    self._cell.span_class = span_class

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._in_raw:
            if tag == "script" and self.timezone_name is None:
                found = _TIMEZONE.search("".join(self._script))
                if found:
                    self.timezone_name = found.group(1)
            self._in_raw = False
            self._script = []
            return
        if self._in_raw:
            return
        if tag == "td" and self._row is not None and self._cell_name and self._cell is not None:
            self._row.cells[self._cell_name] = self._cell
            self._cell_name = None
            self._cell = None
        if tag == "tr" and self._row is not None:
            if self._row.event_id:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._in_raw:
            self._script.append(data)
        elif self._cell is not None:
            self._cell.parts.append(data)


class _FetchGate:
    """One outbound calendar request per process (spec §9.5)."""

    def __init__(self) -> None:
        self.used = False

    def allow(self) -> None:
        if self.used:
            raise RuntimeError(
                "Refusing a second Forex Factory request. "
                "This module allows one day page per process (spec §9.5, §9.8)."
            )
        self.used = True


_GATE = _FetchGate()


def day_url(day: date) -> str:
    """URL of one calendar day page (spec §9.8). Month and week URLs are not built."""
    month = _MONTHS[day.month - 1]
    return f"{_BASE}?day={month}{day.day}.{day.year}"


def parse_calendar_html(html: str) -> list[CalendarPrint]:
    """Parse one saved day page into calendar rows (spec §9.8, P2.4).

    Requires the page's `og:url` to be a `?day=` URL and `timezone_name` in the embedded
    settings. A second distinct date cell is rejected, so a week or month page cannot be
    parsed by accident. Clock times are stored in UTC; `page_timezone` is the zone the page
    used, so an evening print can fall on the next UTC date. Tentative rows and multi-day
    labels ("Day 1") keep `scheduled_at` empty. Markup inside `<script>` or `<style>` is
    ignored, so a parser that treats those blocks as HTML cannot invent rows from them.
    """
    parser = _PageParser()
    parser.feed(html)
    if not parser.og_url:
        raise ParserChangedError("Forex Factory page is missing og:url")
    page_day = _page_day(parser.og_url)
    if not parser.timezone_name:
        raise ParserChangedError("Forex Factory page is missing timezone_name")
    zone = _zone(parser.timezone_name)
    if not parser.rows:
        raise ParserChangedError("Forex Factory page has no data-event-id rows")

    prints: list[CalendarPrint] = []
    current_day: date | None = None
    previous_label = ""
    previous_at: datetime | None = None
    for row in parser.rows:
        cells = row.cells
        if "date" in cells and cells["date"].text:
            found = _cell_day(cells["date"].text, page_day.year)
            if found != page_day:
                raise ParserChangedError(
                    f"date cell {found.isoformat()} does not match the day URL {page_day.isoformat()}"
                )
            current_day = found
        if current_day is None:
            raise ParserChangedError("event row appears before the date cell")
        if row.event_id is None:
            continue
        time_cell = _required(cells, "time", row.event_id)
        currency = _required(cells, "currency", row.event_id).text
        event = _required(cells, "event", row.event_id).text
        if not currency or not event:
            raise ParserChangedError(f"event {row.event_id} is missing currency or title")
        label = time_cell.text
        inferred = False
        scheduled: datetime | None
        if not label:
            if not previous_label:
                raise ParserChangedError(f"event {row.event_id} has a blank time and nothing to inherit")
            label = previous_label
            scheduled = previous_at
            inferred = True
        else:
            scheduled = _clock(label, current_day, zone)
            previous_label = label
            previous_at = scheduled
        impact_cell = cells.get("impact")
        impact_label = impact_cell.span_title if impact_cell else None
        actual_cell = cells.get("actual")
        previous_cell = cells.get("previous")
        prints.append(
            CalendarPrint(
                event_id=row.event_id,
                currency=currency,
                event=event,
                impact=_impact(impact_label),
                impact_label=impact_label,
                scheduled_at=scheduled,
                time_label=label,
                time_inferred=inferred,
                actual=_blank(actual_cell.text if actual_cell else ""),
                forecast=_blank(cells["forecast"].text if "forecast" in cells else ""),
                previous=_blank(previous_cell.text if previous_cell else ""),
                previous_revised_from=_revised_from(previous_cell),
                actual_marker=_marker(actual_cell.span_class if actual_cell else ""),
                page_timezone=parser.timezone_name,
                source_url=parser.og_url,
            )
        )
    return prints


def fetch_one_day(day: date, *, get: Callable[[str], str] | None = None) -> str:
    """Download one day page and return its HTML (spec §9.5, §9.8).

    The process latch trips before the request, so a failure still consumes the one
    allowed call. Pass `get` only from tests. The default client does not follow
    redirects, and it sends `USER_AGENT`.
    """
    _GATE.allow()
    url = day_url(day)
    if get is None:
        return _http_get(url)
    return get(url)


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    """Parse one local day page, or fetch exactly one day, and write CSV to `out` (spec §9.8)."""
    args = _parser().parse_args(argv)
    sink = sys.stdout if out is None else out
    if args.file is not None:
        html = args.file.read_text(encoding="utf-8")
    else:
        print(
            "One request only. Fair Economy prohibits copying the calendar database "
            "and access other than their interface (spec §9.5, §9.8).",
            file=sys.stderr,
        )
        html = fetch_one_day(date.fromisoformat(args.once))
    rows = parse_calendar_html(html)
    _write_csv(rows, sink)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse one Forex Factory calendar day. Not a pipeline source (spec §9.8)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="Parse a saved day page. Makes no request.")
    group.add_argument(
        "--once",
        metavar="YYYY-MM-DD",
        help="One scripted GET of that day. Refused with HTTP 403 on 2026-10-14; use --file.",
    )
    return parser


def _write_csv(rows: list[CalendarPrint], out: TextIO) -> None:
    writer = csv.DictWriter(out, fieldnames=list(CalendarPrint.model_fields))
    writer.writeheader()
    for row in rows:
        dumped = row.model_dump()
        if dumped["scheduled_at"] is not None:
            dumped["scheduled_at"] = dumped["scheduled_at"].isoformat()
        writer.writerow(dumped)


def _http_get(url: str) -> str:
    import httpx

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        response = client.get(url, headers=headers)
    return _body_or_raise(response.status_code, response.text)


def _body_or_raise(status: int, body: str) -> str:
    """Return a 200 body. A 403 names the browser-save path and does not retry (spec §9.5)."""
    if status == 403:
        raise RuntimeError(
            "Forex Factory returned HTTP 403 to a scripted GET of one day page "
            "(checked 2026-10-14). Save the rendered day page from a browser and pass --file. "
            "This module does not impersonate a browser."
        )
    if status != 200:
        raise RuntimeError(f"Forex Factory returned HTTP {status} for one day page")
    return body


def _cell_name(class_name: str) -> str | None:
    classes = set(class_name.split())
    for name in _CELL_NAMES:
        if f"calendar__{name}" in classes:
            return name
    return None


def _page_day(url: str) -> date:
    found = _DAY_URL.search(url)
    if found is None:
        raise ParserChangedError("og:url is not a single-day calendar URL")
    month = _MONTH_NUMBER.get(found.group(1).lower())
    if month is None:
        raise ParserChangedError(f"og:url has an unknown month {found.group(1)!r}")
    return date(int(found.group(3)), month, int(found.group(2)))


def _cell_day(text: str, year: int) -> date:
    found = _CELL_DAY.search(text)
    if found is None:
        raise ParserChangedError(f"date cell is not a month and day: {text!r}")
    month = _MONTH_NUMBER[found.group(1).lower()]
    return date(year, month, int(found.group(2)))


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except KeyError as exc:
        raise ParserChangedError(f"timezone_name {name!r} is not a known zone") from exc


def _clock(label: str, day: date, zone: ZoneInfo) -> datetime | None:
    found = _CLOCK.fullmatch(label.strip())
    if found is None:
        return None
    hour = int(found.group(1))
    minute = int(found.group(2))
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise ParserChangedError(f"clock time is out of range: {label!r}")
    if hour == 12:
        hour = 0
    if found.group(3).lower() == "pm":
        hour += 12
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    return local.astimezone(UTC)


def _impact(label: str | None) -> Impact:
    if not label:
        return "unknown"
    head = label.lower().split(" ", 1)[0]
    if head == "high":
        return "high"
    if head == "medium":
        return "medium"
    if head == "low":
        return "low"
    return "unknown"


def _required(cells: dict[str, _Cell], name: str, event_id: str) -> _Cell:
    cell = cells.get(name)
    if cell is None:
        raise ParserChangedError(f"event {event_id} is missing the {name} cell")
    return cell


def _blank(text: str) -> str | None:
    stripped = text.strip()
    return stripped or None


def _revised_from(cell: _Cell | None) -> str | None:
    if cell is None or "revised" not in cell.span_class.split():
        return None
    if not cell.span_title:
        return None
    found = _REVISED_FROM.match(cell.span_title.strip())
    if found is None:
        return None
    return found.group(1).strip()


def _marker(span_class: str) -> str | None:
    tokens = set(span_class.split())
    if "better" in tokens:
        return "better"
    if "worse" in tokens:
        return "worse"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
