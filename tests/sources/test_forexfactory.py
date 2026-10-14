"""Single-day Forex Factory parser. No request is made (spec §9.5, §9.8, P2.4)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from io import StringIO

import pytest

from macrosys.core.contracts import ParserChangedError
from macrosys.sources import SOURCES
from macrosys.sources.forexfactory import (
    _body_or_raise,
    day_url,
    fetch_one_day,
    main,
    parse_calendar_html,
)


def test_day_page_keeps_printed_values_and_clock(fixtures_dir):
    rows = parse_calendar_html((fixtures_dir / "sources" / "forexfactory" / "day.html").read_text(encoding="utf-8"))
    assert [row.event_id for row in rows] == ["1001", "1002", "1003", "1004"]

    payrolls, unemployment, statement, holiday = rows
    assert payrolls.currency == "USD"
    assert payrolls.event == "Nonfarm Payrolls"
    assert payrolls.impact == "high"
    assert payrolls.actual == "180K"
    assert payrolls.forecast == "170K"
    assert payrolls.previous == "165K"
    assert payrolls.previous_revised_from == "160K"
    assert payrolls.actual_marker == "better"
    assert payrolls.time_inferred is False
    assert payrolls.page_timezone == "America/Chicago"
    assert payrolls.scheduled_at == datetime(2026, 10, 14, 13, 30, tzinfo=UTC)

    assert unemployment.time_inferred is True
    assert unemployment.time_label == "8:30am"
    assert unemployment.scheduled_at == payrolls.scheduled_at
    assert unemployment.actual_marker == "worse"
    assert unemployment.impact == "medium"
    assert unemployment.previous_revised_from is None

    assert statement.scheduled_at is None
    assert statement.time_label == "Tentative"
    assert statement.actual is None
    assert statement.forecast is None
    assert statement.impact == "low"

    assert holiday.scheduled_at is None
    assert holiday.time_label == "Day 1"
    assert holiday.impact == "unknown"
    assert holiday.impact_label == "Holiday"


def test_a_second_day_on_the_page_is_rejected(fixtures_dir):
    html = (fixtures_dir / "sources" / "forexfactory" / "day.html").read_text(encoding="utf-8")
    html = html.replace("Day 1", "9:00am").replace("Bank Holiday", "Oct 15 CPI")
    html = html.replace(
        '<td class="calendar__cell calendar__time"><div><span>9:00am</span></div></td>',
        '<td class="calendar__cell calendar__date"><span>Thu Oct 15</span></td>'
        '<td class="calendar__cell calendar__time"><div><span>9:00am</span></div></td>',
        1,
    )
    with pytest.raises(ParserChangedError, match="does not match the day URL"):
        parse_calendar_html(html)


def test_missing_timezone_is_rejected(fixtures_dir):
    html = (fixtures_dir / "sources" / "forexfactory" / "day.html").read_text(encoding="utf-8")
    html = html.replace("timezone_name: 'America/Chicago'", "timezone: '-5'")
    with pytest.raises(ParserChangedError, match="timezone_name"):
        parse_calendar_html(html)


def test_month_url_is_rejected(fixtures_dir):
    html = (fixtures_dir / "sources" / "forexfactory" / "day.html").read_text(encoding="utf-8")
    html = html.replace("?day=oct14.2026", "?month=oct.2026")
    with pytest.raises(ParserChangedError, match="single-day"):
        parse_calendar_html(html)


def test_script_markup_does_not_become_an_event(fixtures_dir):
    html = (fixtures_dir / "sources" / "forexfactory" / "day.html").read_text(encoding="utf-8")
    html = html.replace(
        "<script>",
        '<script>var row = \'<tr data-event-id="999"><td class="calendar__event">Fake</td></tr>\';\n',
        1,
    )
    rows = parse_calendar_html(html)
    assert [row.event_id for row in rows] == ["1001", "1002", "1003", "1004"]


def test_scripted_get_403_points_at_a_saved_page():
    with pytest.raises(RuntimeError, match="HTTP 403") as caught:
        _body_or_raise(403, "blocked")
    assert "--file" in str(caught.value)
    assert _body_or_raise(200, "<html></html>") == "<html></html>"


def test_one_fetch_per_process():
    calls: list[str] = []

    def get(url: str) -> str:
        calls.append(url)
        return "<html></html>"

    assert day_url(date(2026, 10, 14)) == "https://www.forexfactory.com/calendar?day=oct14.2026"
    assert fetch_one_day(date(2026, 10, 14), get=get) == "<html></html>"
    with pytest.raises(RuntimeError, match="second Forex Factory request"):
        fetch_one_day(date(2026, 10, 15), get=get)
    assert calls == ["https://www.forexfactory.com/calendar?day=oct14.2026"]


def test_file_cli_does_not_fetch(fixtures_dir):
    out = StringIO()
    code = main(["--file", str(fixtures_dir / "sources" / "forexfactory" / "day.html")], out=out)
    assert code == 0
    text = out.getvalue()
    assert "Nonfarm Payrolls" in text
    assert "180K" in text
    assert "FOREXFACTORY" not in {getattr(cls, "source_id", "") for cls in SOURCES.values()}
