"""Zero-network tests for the date-aware settlement-only parser fix (I-006).

Covers the four known settlement-only misclassifications, the anti-false-
positive last-trading-day case, and the note date-reference semantics.
"""
from __future__ import annotations

from datetime import date

from scripts.v2.parse_twse_holiday_schedule import (
    _note_no_trade_dates,
    classify_row,
    parse_annual_schedule,
)


# ---------------------------------------------------------------------------
# note date-reference extraction
# ---------------------------------------------------------------------------


def test_note_no_trade_dates_single():
    assert _note_no_trade_dates("2月8日市場無交易，僅辦理結算交割作業。", 2021) == {date(2021, 2, 8)}


def test_note_no_trade_dates_pair():
    assert _note_no_trade_dates("1月18日及1月19日市場無交易，僅辦理結算交割作業。", 2023) == {
        date(2023, 1, 18),
        date(2023, 1, 19),
    }


def test_note_no_trade_dates_none():
    assert _note_no_trade_dates("農曆春節前最後交易。", 2021) == set()


# ---------------------------------------------------------------------------
# positive settlement-only (row's own date has no trading)
# ---------------------------------------------------------------------------


def test_settlement_only_2021_02_08_closed():
    assert (
        classify_row("農曆春節前最後交易日", "2月8日市場無交易，僅辦理結算交割作業。", date(2021, 2, 8))
        == "closed_official"
    )


def test_settlement_only_2021_02_09_closed():
    assert (
        classify_row("農曆春節前最後交易日", "2月9日市場無交易，僅辦理結算交割作業。", date(2021, 2, 9))
        == "closed_official"
    )


def test_settlement_only_2022_01_27_closed():
    assert (
        classify_row("農曆春節前最後交易日", "1月27日市場無交易，僅辦理結算交割作業。", date(2022, 1, 27))
        == "closed_official"
    )


def test_settlement_only_2022_01_28_closed():
    assert (
        classify_row("農曆春節前最後交易日", "1月28日市場無交易，僅辦理結算交割作業。", date(2022, 1, 28))
        == "closed_official"
    )


# ---------------------------------------------------------------------------
# anti-false-positive (last trading day whose note references OTHER dates)
# ---------------------------------------------------------------------------


def test_last_trading_day_2023_01_17_open():
    note = "農曆春節前最後交易。\r\n1月18日及1月19日市場無交易，僅辦理結算交割作業。"
    assert classify_row("農曆春節前最後交易日", note, date(2023, 1, 17)) == "open_special_or_explicit_open"


def test_last_trading_day_simple_open():
    assert (
        classify_row("農曆春節前最後交易日", "農曆春節前最後交易。", date(2021, 2, 5))
        == "open_special_or_explicit_open"
    )


# ---------------------------------------------------------------------------
# backward compatibility (no row_date -> prior behavior)
# ---------------------------------------------------------------------------


def test_classify_row_without_row_date_prior_behavior():
    # Without a row date we cannot bind "X月X日" to the row, so the prior
    # (trading-day-name => open) behavior is preserved.
    assert (
        classify_row("農曆春節前最後交易日", "2月8日市場無交易，僅辦理結算交割作業。")
        == "open_special_or_explicit_open"
    )


def test_explicit_trading_day_open():
    assert classify_row("國曆新年開始交易日", "國曆新年開始交易。", date(2026, 1, 2)) == "open_special_or_explicit_open"


def test_makeup_no_trade_closed():
    assert classify_row("農曆除夕前一日", "2月10日調整放假，於2月20日補行上班，但不交易亦不交割。", date(2021, 2, 10)) == "closed_official"


def test_settlement_only_named_row_closed():
    assert classify_row("市場無交易，僅辦理結算交割作業", "", date(2023, 1, 18)) == "closed_official"


# ---------------------------------------------------------------------------
# end-to-end parse_annual_schedule
# ---------------------------------------------------------------------------


def _payload_2021():
    return {
        "stat": "ok",
        "title": "110 年市場開休市日期",
        "fields": ["日期", "名稱", "說明"],
        "data": [
            ["2021-02-05", "農曆春節前最後交易日", "農曆春節前最後交易。"],
            ["2021-02-08", "農曆春節前最後交易日", "2月8日市場無交易，僅辦理結算交割作業。"],
            ["2021-02-09", "農曆春節前最後交易日", "2月9日市場無交易，僅辦理結算交割作業。"],
            ["2021-02-10", "農曆除夕前一日", "2月10日（星期三）調整放假，於2月20日（星期六）補行上班，但不交易亦不交割。"],
            ["2021-02-11", "農曆除夕", "依規定放假1日。"],
            ["2021-02-17", "農曆春節後開始交易日", "農曆春節後開始交易。"],
        ],
        "queryYear": 2021,
        "total": 6,
    }


def test_parse_annual_schedule_2021_settlement_only_closed():
    parsed = parse_annual_schedule(_payload_2021(), 2021)
    assert "2021-02-08" in parsed["closed_official_dates"]
    assert "2021-02-09" in parsed["closed_official_dates"]
    assert "2021-02-08" not in parsed["open_special_or_explicit_open_dates"]
    assert "2021-02-09" not in parsed["open_special_or_explicit_open_dates"]
    # real last trading day remains open
    assert "2021-02-05" in parsed["open_special_or_explicit_open_dates"]
    assert "2021-02-17" in parsed["open_special_or_explicit_open_dates"]
    assert parsed["open_closed_exclusive"] is True
    assert parsed["classification_issues"] == []
