"""Fixed 365-day common calendar for circular climatology.

The climatology smoothing pool uses a common (non-leap) 365-day calendar so
that every date maps to a unique day-of-year 1..365.  Leap-year Feb 29 has no
common-calendar counterpart and is excluded from the pool (it never fills
another slot); leap-year dates on or after Mar 01 map one day earlier so the
calendar stays 365 days long.

Pure and zero-network.
"""
from __future__ import annotations

import calendar as _calendar
from datetime import date, timedelta


def is_leap(year: int) -> bool:
    return _calendar.isleap(year)


def common_day_of_year(d: date) -> int | None:
    """Map a Gregorian date to its common-calendar day-of-year (1..365).

    Returns None for leap-year Feb 29 (excluded from the common calendar).
    """
    if d.month == 2 and d.day == 29:
        return None
    doy = d.timetuple().tm_yday  # 1..365 or 1..366
    if is_leap(d.year) and d.month >= 3:
        doy -= 1  # skip the leap-day slot so Mar 01 stays day 60
    return doy


def common_day_to_actual_date(doy: int, year: int) -> date:
    """Map a common day-of-year (1..365) to an actual Gregorian date in ``year``.

    For leap years, common days >= 60 (Mar 01 onward) shift one day later
    because the leap Feb 29 occupies an actual calendar slot but not a common
    one.  ``doy`` must be in 1..365.
    """
    if not 1 <= doy <= 365:
        raise ValueError(f"common day-of-year out of range: {doy}")
    actual_doy = doy + 1 if (is_leap(year) and doy >= 60) else doy
    return date(year, 1, 1) + timedelta(days=actual_doy - 1)


def common_day_label(doy: int, reference_year: int = 2001) -> tuple[int, int]:
    """Return the (month, day) label for a common day-of-year, using a non-leap reference year."""
    if not 1 <= doy <= 365:
        raise ValueError(f"common day-of-year out of range: {doy}")
    d = date(reference_year, 1, 1) + timedelta(days=doy - 1)
    return (d.month, d.day)


def circular_window(center_month: int, center_day: int, radius: int, reference_year: int = 2001) -> list[int]:
    """Common day-of-year list for ``[center - radius, center + radius]`` (circular).

    Returns exactly ``2 * radius + 1`` common day-of-years in circular order,
    wrapping around 1..365 at the Dec/Jan boundary.  The center is resolved
    via a non-leap reference year.
    """
    center = common_day_of_year(date(reference_year, center_month, center_day))
    assert center is not None
    return [((center - 1 + offset) % 365) + 1 for offset in range(-radius, radius + 1)]
