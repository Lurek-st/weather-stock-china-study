"""Stage 5B-1: generic session-time / DST window contract tests."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from scripts.v2.climatology.temporal_integration import piecewise_linear_mean
from scripts.v2.core import V2Error
from scripts.v2.session_time import (
    detect_local_time_issue,
    hourly_support_timestamps,
    resolve_open_window,
)

NY = "America/New_York"
OPEN_0930 = time(9, 30)
CLOSE_1600 = time(16, 0)


def _w(d: str):
    return resolve_open_window(date.fromisoformat(d), NY, OPEN_0930, 120, CLOSE_1600)


# --- Session ------------------------------------------------------------
def test_core_open_0930_retained():
    r = _w("2024-03-08")
    assert r["core_open_local_clock"] == "09:30:00"
    assert r["core_open_local"].endswith("T09:30:00-05:00")


def test_120_minute_window():
    r = _w("2024-03-08")
    start = datetime.fromisoformat(r["window_start_local"])
    end = datetime.fromisoformat(r["window_end_local"])
    assert end - start == timedelta(minutes=120)


def test_no_early_session_substitution():
    # core open stays 09:30; it never becomes 07:00 early / 06:30 pre-open
    for d in ("2024-03-08", "2024-03-11"):
        r = _w(d)
        assert r["core_open_local_clock"] == "09:30:00"
        assert "T07:00:00" not in r["core_open_local"]
        assert "T06:30:00" not in r["core_open_local"]


# --- DST offsets + shift ------------------------------------------------
def test_mar08_offset_minus5():
    assert _w("2024-03-08")["utc_offset_str"] == "-0500"


def test_mar11_offset_minus4():
    assert _w("2024-03-11")["utc_offset_str"] == "-0400"


def test_exact_one_hour_utc_shift_across_spring_dst():
    before = _w("2024-03-08")
    after = _w("2024-03-11")
    # local WALL CLOCK identical (07:30 local in both), offset differs
    assert before["window_start_local"][11:19] == after["window_start_local"][11:19]
    assert before["window_start_utc"] == "2024-03-08T12:30:00+00:00"
    assert after["window_start_utc"] == "2024-03-11T11:30:00+00:00"
    assert before["core_open_utc"] == "2024-03-08T14:30:00+00:00"
    assert after["core_open_utc"] == "2024-03-11T13:30:00+00:00"


def test_nov01_offset_minus4():
    assert _w("2024-11-01")["utc_offset_str"] == "-0400"


def test_nov04_offset_minus5():
    assert _w("2024-11-04")["utc_offset_str"] == "-0500"


def test_fall_back_shifts_utc_later_one_hour():
    before = _w("2024-11-01")
    after = _w("2024-11-04")
    assert before["window_start_utc"] == "2024-11-01T11:30:00+00:00"
    assert after["window_start_utc"] == "2024-11-04T12:30:00+00:00"


# --- Hourly support -----------------------------------------------------
def test_30min_window_four_support():
    r = _w("2024-03-08")
    assert r["hourly_support_count"] == 4
    assert [t[11:16] for t in r["hourly_support_timestamps"]] == ["12:00", "13:00", "14:00", "15:00"]


def test_30min_window_support_after_dst():
    r = _w("2024-03-11")
    assert [t[11:16] for t in r["hourly_support_timestamps"]] == ["11:00", "12:00", "13:00", "14:00"]


def test_exact_hour_window_three_support():
    # Taipei-style whole-hour window [07:00, 09:00) yields 3 support hours
    r = resolve_open_window(date(2024, 3, 4), "Asia/Taipei", time(9, 0), 120)
    assert r["hourly_support_count"] == 3
    assert [t[11:16] for t in r["hourly_support_timestamps"]] == ["23:00", "00:00", "01:00"]


def test_quarter_hour_window_support():
    # Mumbai-style 09:15 open -> [07:15, 09:15) -> 4 support hours
    r = resolve_open_window(date(2024, 3, 4), "Asia/Kolkata", time(9, 15), 120)
    assert r["hourly_support_count"] == 4


def test_support_sorted_and_no_duplicate():
    for d in ("2024-03-08", "2024-03-11", "2024-11-01", "2024-11-04"):
        ts = _w(d)["hourly_support_timestamps"]
        assert ts == sorted(ts)
        assert len(ts) == len(set(ts))


# --- DST safety ---------------------------------------------------------
def test_nonexistent_local_time_detected():
    tz = ZoneInfo(NY)
    assert detect_local_time_issue(datetime(2024, 3, 10, 2, 30, tzinfo=tz), tz) == "nonexistent"


def test_ambiguous_local_time_detected():
    tz = ZoneInfo(NY)
    assert detect_local_time_issue(datetime(2024, 11, 3, 1, 30, tzinfo=tz), tz) == "ambiguous"


def test_normal_local_time_none():
    tz = ZoneInfo(NY)
    assert detect_local_time_issue(datetime(2024, 3, 8, 9, 30, tzinfo=tz), tz) is None


def test_nonexistent_open_fails_closed():
    # a hypothetical market open during the spring-forward gap must fail
    with pytest.raises(V2Error):
        resolve_open_window(date(2024, 3, 10), NY, time(2, 30), 120)


def test_invalid_timezone_fails():
    with pytest.raises(V2Error):
        resolve_open_window(date(2024, 3, 8), "Not/AZone", OPEN_0930, 120)


def test_invalid_session_clock_fails():
    with pytest.raises(V2Error):
        resolve_open_window(date(2024, 3, 8), NY, time(9, 12), 120)  # minute not :00/:15/:30/:45


# --- Temporal integration oracle ----------------------------------------
def _support_utc(d: str) -> list[datetime]:
    r = _w(d)
    return [datetime.fromisoformat(t) for t in r["hourly_support_timestamps"]]


def _window_bounds_utc(d: str) -> tuple[datetime, datetime]:
    r = _w(d)
    return datetime.fromisoformat(r["window_start_utc"]), datetime.fromisoformat(r["window_end_utc"])


def test_constant_field_oracle():
    # after-DST window [11:30, 13:30), all support = 0.4 -> mean 0.4 -> 40 pct
    ts = _support_utc("2024-03-11")
    start, end = _window_bounds_utc("2024-03-11")
    values = [0.4, 0.4, 0.4, 0.4]
    assert piecewise_linear_mean(ts, values, start, end) == pytest.approx(0.4)


def test_linear_field_oracle():
    # linear ramp 0.0, 0.1, 0.2, 0.3 over support [11,12,13,14]; window [11:30,13:30)
    ts = _support_utc("2024-03-11")
    start, end = _window_bounds_utc("2024-03-11")
    values = [0.0, 0.1, 0.2, 0.3]
    # independent hand computation:
    # v(11:30)=0.05, v(12:00)=0.10, v(13:00)=0.20, v(13:30)=0.25
    # integral = 0.5*(0.05+0.10)*0.5 + 0.5*(0.10+0.20)*1.0 + 0.5*(0.20+0.25)*0.5 = 0.30
    # mean = 0.30 / 2.0 = 0.15
    oracle = 0.15
    assert piecewise_linear_mean(ts, values, start, end) == pytest.approx(oracle, abs=1e-12)


def test_linear_field_before_dst_matches_after_dst():
    # the same local ramp integrates to the same local mean regardless of DST
    def integrate(d):
        ts = _support_utc(d)
        start, end = _window_bounds_utc(d)
        values = [0.0, 0.1, 0.2, 0.3]
        return piecewise_linear_mean(ts, values, start, end)

    assert integrate("2024-03-08") == pytest.approx(integrate("2024-03-11"), abs=1e-12)
