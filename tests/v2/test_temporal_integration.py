"""Stage 5E-1: temporal integration unit tests (piecewise-linear, generic)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from scripts.v2.climatology.temporal_integration import piecewise_linear_mean
from scripts.v2.core import V2Error


def _h(hour: int, minute: int = 0) -> datetime:
    return datetime(2020, 1, 1, hour, minute)


def test_constant_field_mean_is_constant():
    times = [_h(7), _h(8), _h(9)]
    values = [0.5, 0.5, 0.5]
    assert piecewise_linear_mean(times, values, _h(7), _h(9)) == pytest.approx(0.5)


def test_linear_field_mean_is_midpoint():
    # linear ramp 0.0 -> 0.2 over 07:00..09:00 (slope 0.1 / hour)
    times = [_h(7), _h(8), _h(9)]
    values = [0.0, 0.1, 0.2]
    assert piecewise_linear_mean(times, values, _h(7), _h(9)) == pytest.approx(0.1)


def test_exact_hour_boundary_simpson_shape():
    # (TCC07 + 2*TCC08 + TCC09) / 4
    times = [_h(7), _h(8), _h(9)]
    values = [0.2, 0.8, 0.4]
    expected = (0.2 + 2 * 0.8 + 0.4) / 4
    assert piecewise_linear_mean(times, values, _h(7), _h(9)) == pytest.approx(expected)


def test_quarter_hour_boundary():
    # window [07:00, 08:15) over a linear ramp -> value at midpoint 07:37:30
    times = [_h(7), _h(8), _h(9)]
    values = [0.0, 0.1, 0.2]
    result = piecewise_linear_mean(times, values, _h(7), _h(8, 15))
    assert result == pytest.approx(0.0625, abs=1e-12)


def test_half_hour_boundary():
    times = [_h(7), _h(8), _h(9)]
    values = [0.0, 0.1, 0.2]
    result = piecewise_linear_mean(times, values, _h(7), _h(8, 30))
    assert result == pytest.approx(0.075, abs=1e-12)


def test_missing_bracket_fails():
    times = [_h(7), _h(8)]
    values = [0.1, 0.2]
    with pytest.raises(V2Error):
        piecewise_linear_mean(times, values, _h(7), _h(9))  # 09:00 not covered


def test_duplicate_timestamp_fails():
    times = [_h(7), _h(8), _h(8)]
    values = [0.1, 0.2, 0.3]
    with pytest.raises(V2Error):
        piecewise_linear_mean(times, values, _h(7), _h(9))


def test_empty_fails():
    with pytest.raises(V2Error):
        piecewise_linear_mean([], [], _h(7), _h(9))


def test_length_mismatch_fails():
    with pytest.raises(V2Error):
        piecewise_linear_mean([_h(7), _h(8)], [0.1], _h(7), _h(8))


def test_timezone_aware_input():
    tz = ZoneInfo("Asia/Taipei")
    # 07:00-09:00 Taipei, constant field
    times = [
        datetime(2020, 3, 4, 7, 0, tzinfo=tz),
        datetime(2020, 3, 4, 8, 0, tzinfo=tz),
        datetime(2020, 3, 4, 9, 0, tzinfo=tz),
    ]
    values = [0.6, 0.6, 0.6]
    result = piecewise_linear_mean(
        times, values,
        datetime(2020, 3, 4, 7, 0, tzinfo=tz),
        datetime(2020, 3, 4, 9, 0, tzinfo=tz),
    )
    assert result == pytest.approx(0.6)


def test_timezone_aware_equals_utc_equivalent():
    tz = ZoneInfo("Asia/Taipei")
    values = [0.2, 0.8, 0.4]
    local_times = [
        datetime(2020, 3, 4, h, 0, tzinfo=tz) for h in (7, 8, 9)
    ]
    utc_times = [t.astimezone(timezone.utc) for t in local_times]
    local_start = local_times[0]
    local_end = local_times[-1]
    utc_start = utc_times[0]
    utc_end = utc_times[-1]
    assert piecewise_linear_mean(local_times, values, local_start, local_end) == pytest.approx(
        piecewise_linear_mean(utc_times, values, utc_start, utc_end)
    )


def test_non_hourly_breakpoint_linear():
    # a :30 breakpoint (e.g. future 09:30 open markets) must integrate exactly
    times = [_h(7), _h(8), _h(9)]
    values = [0.0, 0.1, 0.2]
    result = piecewise_linear_mean(times, values, _h(7), _h(8, 45))
    assert result == pytest.approx(0.0875, abs=1e-12)
