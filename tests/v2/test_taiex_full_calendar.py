"""Zero-network tests for TAIEX full-history calendar qualification.

Covers DGPA parsing (ROC dates, list entries, Taipei cell extraction, today/
tomorrow clause splitting, work status classification, closure-date
derivation, frozen TWSE rule), FMTQIK trading-date parsing, and calendar
reconciliation.  No test performs network I/O.
"""
from __future__ import annotations

from scripts.v2.calendars.dgpa_disaster import (
    classify_clause,
    closure_dates_for_event,
    derive_market_closure,
    derive_taipei_statuses,
    extract_city_rows,
    extract_taipei_cell,
    parse_event_date,
    parse_list_entries,
    split_today_tomorrow,
)
from scripts.v2.calendars.fmtqik_market import parse_trading_dates
from scripts.v2.calendars.build_taiex_full_calendar import (
    build_expected_sets,
    reconcile,
)

# ---------------------------------------------------------------------------
# DGPA ROC date parsing
# ---------------------------------------------------------------------------


def test_roc_event_date_parsed():
    assert parse_event_date("113年7月24日天然災害停止辦公及上課情形") == "2024-07-24"
    assert parse_event_date("109年1月2日天然災害停止辦公及上課情形") == "2020-01-02"


def test_roc_event_date_missing():
    assert parse_event_date("無日期") is None


def test_roc_crosses_century():
    assert parse_event_date("99年12月31日天然災害停止辦公及上課情形") == "2010-12-31"


# ---------------------------------------------------------------------------
# DGPA list entry parsing
# ---------------------------------------------------------------------------

LIST_HTML = """
<a href="information?uid=374&amp;pid=11988">113.07.26 113年7月24日天然災害停止辦公及上課情形</a>
<a href="information?uid=374&amp;pid=11989">113.07.26 113年7月25日天然災害停止辦公及上課情形</a>
"""


def test_list_entries_parse_pid_and_dates():
    entries = parse_list_entries(LIST_HTML)
    assert len(entries) == 2
    assert entries[0]["pid"] == "11988"
    assert entries[0]["event_date"] == "2024-07-24"
    assert entries[0]["announce_date"] == "2024-07-26"
    assert entries[1]["event_date"] == "2024-07-25"


# ---------------------------------------------------------------------------
# DGPA Taipei cell extraction
# ---------------------------------------------------------------------------

ATTACHMENT_HTML = """
<table>
<tr><th>縣市名稱</th><th>是否停止上班上課情形</th></tr>
<tr><td>基隆市</td><td>今天停止上班、停止上課。</td></tr>
<tr><td>臺北市</td><td>今天停止上班、停止上課。 明天停止上班、停止上課。</td></tr>
<tr><td>新北市</td><td>今天停止上班、停止上課。</td></tr>
</table>
"""


def test_extract_taipei_cell():
    cell = extract_taipei_cell(ATTACHMENT_HTML)
    assert cell is not None
    assert "停止上班" in cell
    assert "臺北市" not in cell  # returns the status cell, not the city name


def test_extract_taipei_missing():
    html = "<table><tr><td>新北市</td><td>今天停止上班。</td></tr></table>"
    assert extract_taipei_cell(html) is None


def test_extract_city_rows_count():
    rows = extract_city_rows(ATTACHMENT_HTML)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# today/tomorrow clause splitting
# ---------------------------------------------------------------------------


def test_split_today_and_tomorrow():
    today, tomorrow = split_today_tomorrow("今天停止上班、停止上課。 明天照常上班、照常上課。")
    assert "停止上班" in today
    assert "照常" in tomorrow


def test_split_tomorrow_only():
    today, tomorrow = split_today_tomorrow("明天停止上班、停止上課。")
    assert today is None
    assert "停止上班" in tomorrow


def test_split_today_only():
    today, tomorrow = split_today_tomorrow("今天停止上班、停止上課。")
    assert "停止上班" in today
    assert tomorrow is None


# ---------------------------------------------------------------------------
# clause classification
# ---------------------------------------------------------------------------


def test_classify_full_day_stop():
    assert classify_clause("今天停止上班、停止上課。") == "full_day_stop"


def test_classify_normal():
    assert classify_clause("今天照常上班、照常上課。") == "normal"
    assert classify_clause("尚未列入警戒區。") == "normal"
    assert classify_clause("尚未宣布消息") == "normal"


def test_classify_afternoon_stop():
    assert classify_clause("今天下午停止上班、停止上課。") == "afternoon_stop"


def test_classify_morning_stop():
    assert classify_clause("今天上午停止上班、停止上課。") == "morning_stop"


def test_classify_local_school_partial():
    # School-level suspension only; the whole city is not suspended.
    assert classify_clause("臺北市士林區陽明山國民小學:今天照常上班、停止上課。") == "partial_or_local"


def test_classify_city_normal_with_school_note():
    # Whole city normal tomorrow, but one school suspends tomorrow.
    assert (
        classify_clause("明天照常上班、照常上課。 臺北市私立立人國際國民中小學 (小學部):明天停止上課。")
        == "normal"
    )


def test_classify_city_normal_with_village_note():
    # Whole city not in alert area, but mountain villages suspend today.
    assert (
        classify_clause("尚未列入警戒區。 士林區永福里、新安里:今天停止上班、停止上課。")
        == "normal"
    )


def test_classify_village_only_partial():
    assert (
        classify_clause("士林區永福里、新安里、陽明里:今天停止上班、停止上課。")
        == "partial_or_local"
    )


def test_derive_village_only_partial():
    s = derive_taipei_statuses("士林區永福里、新安里:明天停止上班、停止上課。 北投區湖田里:明天停止上班。")
    assert s["today_status"] == "partial_or_local"
    assert s["tomorrow_status"] is None


def test_classify_none_is_normal():
    # A missing clause (not announced) is normal, not a suspension.
    assert classify_clause(None) == "normal"


def test_classify_ambiguous():
    assert classify_clause("今天停止上班。 今天照常上課。") == "ambiguous"


# ---------------------------------------------------------------------------
# Taipei statuses + closure-date derivation
# ---------------------------------------------------------------------------


def test_derive_statuses_full_day_today_and_tomorrow():
    s = derive_taipei_statuses("今天停止上班、停止上課。 明天停止上班、停止上課。")
    assert s["today_status"] == "full_day_stop"
    assert s["tomorrow_status"] == "full_day_stop"


def test_derive_statuses_tomorrow_only():
    s = derive_taipei_statuses("明天停止上班、停止上課。")
    assert s["today_status"] == "normal"
    assert s["tomorrow_status"] == "full_day_stop"


def test_derive_statuses_absent_is_normal():
    s = derive_taipei_statuses(None)
    assert s["today_status"] == "normal"
    assert s["tomorrow_status"] is None


def test_closure_today_and_tomorrow():
    s = derive_taipei_statuses("今天停止上班、停止上課。 明天停止上班、停止上課。")
    closes = closure_dates_for_event("2024-07-24", s)
    assert closes == ["2024-07-24", "2024-07-25"]


def test_closure_tomorrow_only_next_day():
    s = derive_taipei_statuses("明天停止上班、停止上課。")
    closes = closure_dates_for_event("2024-07-23", s)
    assert closes == ["2024-07-24"]


def test_closure_normal_none():
    s = derive_taipei_statuses("今天照常上班、照常上課。")
    assert closure_dates_for_event("2024-07-23", s) == []


# ---------------------------------------------------------------------------
# Frozen TWSE natural-disaster rule
# ---------------------------------------------------------------------------


def test_derive_market_closure_rule():
    assert derive_market_closure("full_day_stop") == "market_closed_full_day"
    assert derive_market_closure("morning_stop") == "market_closed_full_day"
    assert derive_market_closure("afternoon_stop") == "regular_cash_market_open"
    assert derive_market_closure("normal") == "no_natural_disaster_closure"
    assert derive_market_closure("partial_or_local") == "no_automatic_full_market_closure"
    assert derive_market_closure("ambiguous") == "unresolved"
    assert derive_market_closure("missing") == "unresolved"


# ---------------------------------------------------------------------------
# FMTQIK trading-date parsing
# ---------------------------------------------------------------------------


def _fmtqik(rows):
    return {
        "stat": "OK",
        "title": "109年01月市場成交資訊",
        "fields": ["日期", "成交股數", "成交金額", "成交筆數", "發行量加權股價指數", "漲跌點數"],
        "data": rows,
    }


def test_fmtqik_parse_trading_dates():
    payload = _fmtqik(
        [
            ["109/01/02", "1", "2", "3", "4", "5"],
            ["109/01/03", "1", "2", "3", "4", "5"],
            ["109/01/31", "1", "2", "3", "4", "5"],
        ]
    )
    parsed = parse_trading_dates(payload, 2020, 1)
    assert parsed["dates"] == ["2020-01-02", "2020-01-03", "2020-01-31"]
    assert parsed["issues"] == []


def test_fmtqik_duplicate_date_fails():
    payload = _fmtqik([["109/01/02", "1", "2", "3", "4", "5"], ["109/01/02", "1", "2", "3", "4", "5"]])
    parsed = parse_trading_dates(payload, 2020, 1)
    assert "duplicate_date:2020-01-02" in parsed["issues"]


def test_fmtqik_out_of_month_fails():
    payload = _fmtqik([["109/02/01", "1", "2", "3", "4", "5"]])
    parsed = parse_trading_dates(payload, 2020, 1)
    assert any("out_of_month" in i for i in parsed["issues"])


def test_fmtqik_malformed_date_fails():
    payload = _fmtqik([["not-a-date", "1", "2", "3", "4", "5"]])
    parsed = parse_trading_dates(payload, 2020, 1)
    assert any("unparseable" in i for i in parsed["issues"])


# ---------------------------------------------------------------------------
# Calendar reconciliation
# ---------------------------------------------------------------------------


def _annual_fixture():
    return {
        "year_records": {
            "2020": {
                "closed_official_dates": ["2020-01-01"],
                "explicit_open_dates": ["2020-01-02"],
            }
        }
    }


def test_build_expected_sets_weekday_weekend_holiday():
    annual = _annual_fixture()
    expected_open, expected_closed = build_expected_sets(annual, set(), "2020-01-01", "2020-01-05")
    assert expected_open == {"2020-01-02", "2020-01-03"}
    assert expected_closed == {"2020-01-01", "2020-01-04", "2020-01-05"}


def test_build_expected_sets_disaster_closure_overrides():
    annual = _annual_fixture()
    expected_open, expected_closed = build_expected_sets(
        annual, {"2020-01-03"}, "2020-01-01", "2020-01-05"
    )
    assert "2020-01-03" in expected_closed
    assert "2020-01-03" not in expected_open


def test_reconcile_perfect_match():
    expected_open = {"2020-01-02", "2020-01-03"}
    expected_closed = {"2020-01-01", "2020-01-04", "2020-01-05"}
    observed_open = {"2020-01-02", "2020-01-03"}
    sets = reconcile(expected_open, expected_closed, observed_open)
    assert sets["expected_open_but_no_market_record"] == []
    assert sets["expected_closed_but_market_record"] == []


def test_reconcile_expected_open_missing_record():
    expected_open = {"2020-01-02", "2020-01-03"}
    expected_closed = {"2020-01-01", "2020-01-04", "2020-01-05"}
    observed_open = {"2020-01-02"}
    sets = reconcile(expected_open, expected_closed, observed_open)
    assert sets["expected_open_but_no_market_record"] == ["2020-01-03"]


def test_reconcile_expected_closed_has_record():
    expected_open = {"2020-01-02", "2020-01-03"}
    expected_closed = {"2020-01-01", "2020-01-04", "2020-01-05"}
    observed_open = {"2020-01-02", "2020-01-03", "2020-01-04"}
    sets = reconcile(expected_open, expected_closed, observed_open)
    assert sets["expected_closed_but_market_record"] == ["2020-01-04"]
