from datetime import date

from scripts.v2.calendars.build_taiex_calendar import build_year, parse_twse_dates, previous_open_date


def test_weekends_close_and_official_holiday_closes():
    rows = build_year(2023, {date(2023, 6, 22)}, "https://example.test", "rev", "2026-01-01T00:00:00Z")
    by_date = {row["date"]: row for row in rows}
    assert by_date["2023-06-03"]["session_status"] == "closed_weekend"
    assert by_date["2023-06-22"]["session_status"] == "closed_official_holiday"


def test_roc_and_iso_dates_parse_without_guessing_invalid_dates():
    found = parse_twse_dates({"data": ["112/06/22", "2023/06/23", "bad"]})
    assert found == {date(2023, 6, 22), date(2023, 6, 23)}


def test_previous_open_crosses_weekend_and_holiday():
    rows = [
        {"date": "2023-06-22", "session_status": "closed_official_holiday"},
        {"date": "2023-06-23", "session_status": "open"},
        {"date": "2023-06-24", "session_status": "closed_weekend"},
        {"date": "2023-06-25", "session_status": "closed_weekend"},
        {"date": "2023-06-26", "session_status": "open_special"},
    ]
    assert previous_open_date(rows, "2023-06-26") == "2023-06-23"


def test_special_and_early_session_statuses_are_explicit_not_weekend_inferences():
    rows = [
        {"date": "2023-06-24", "session_status": "open_special"},
        {"date": "2023-06-25", "session_status": "early_close"},
    ]
    assert previous_open_date(rows, "2023-06-26") == "2023-06-25"
