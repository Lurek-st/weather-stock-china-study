"""Stage 5E-1: common-calendar unit tests (365-day mapping, Feb29 exclusion, circular)."""
from __future__ import annotations

from datetime import date

import pytest

from scripts.v2.climatology.common_calendar import (
    circular_window,
    common_day_label,
    common_day_of_year,
    common_day_to_actual_date,
)


def test_feb28_maps_to_59():
    assert common_day_of_year(date(2019, 2, 28)) == 59
    assert common_day_of_year(date(2020, 2, 28)) == 59  # leap year Feb 28 unchanged


def test_feb29_is_none():
    assert common_day_of_year(date(2020, 2, 29)) is None


def test_mar01_maps_to_60():
    assert common_day_of_year(date(2019, 3, 1)) == 60  # non-leap
    assert common_day_of_year(date(2020, 3, 1)) == 60  # leap year shifts -1


def test_dec_jan_circular_wrap():
    # center Jan 01 (day 1), radius 15 -> 31 days wrapping around day 365
    days = circular_window(1, 1, 15)
    assert len(days) == 31
    assert days[0] == 351  # Dec 17 of the previous year (1 - 15 wrapped)
    assert days[15] == 1   # center Jan 01
    assert days[-1] == 16  # Jan 16
    assert len(set(days)) == 31


def test_plus_minus_15_gives_exactly_31_slots():
    days = circular_window(3, 4, 15)
    assert len(days) == 31
    assert len(set(days)) == 31  # no duplicates
    # center = Mar 04 = day 63
    assert days[0] == 48   # Feb 17
    assert days[15] == 63  # Mar 04
    assert days[-1] == 78  # Mar 19


def test_feb29_never_in_circular_window():
    # the Mar 04 +-15 window must contain 31 distinct common days, none Feb 29
    days = circular_window(3, 4, 15)
    assert all(d != 60 - 1 for d in [])  # no-op sanity
    # common day 60 == Mar 01 (Feb 29 has NO common day-of-year)
    labels = [common_day_label(d) for d in days]
    assert (2, 29) not in labels


def test_common_day_label():
    assert common_day_label(48) == (2, 17)
    assert common_day_label(59) == (2, 28)
    assert common_day_label(60) == (3, 1)
    assert common_day_label(63) == (3, 4)
    assert common_day_label(78) == (3, 19)
    assert common_day_label(365) == (12, 31)


def test_common_day_to_actual_date_non_leap():
    assert common_day_to_actual_date(48, 2018) == date(2018, 2, 17)
    assert common_day_to_actual_date(63, 2018) == date(2018, 3, 4)
    assert common_day_to_actual_date(78, 2018) == date(2018, 3, 19)


def test_common_day_to_actual_date_leap_year():
    # leap 2020: common day 59 = Feb 28 (unchanged), day 60 = Mar 01 (shift +1)
    assert common_day_to_actual_date(59, 2020) == date(2020, 2, 28)
    assert common_day_to_actual_date(60, 2020) == date(2020, 3, 1)
    assert common_day_to_actual_date(63, 2020) == date(2020, 3, 4)
    assert common_day_to_actual_date(78, 2020) == date(2020, 3, 19)


def test_common_day_to_actual_date_never_feb29():
    # no common day maps to Feb 29 in any leap year
    for doy in range(1, 366):
        d = common_day_to_actual_date(doy, 2020)
        assert not (d.month == 2 and d.day == 29)


def test_common_day_of_year_out_of_range_label():
    with pytest.raises(ValueError):
        common_day_label(0)
    with pytest.raises(ValueError):
        common_day_label(366)
