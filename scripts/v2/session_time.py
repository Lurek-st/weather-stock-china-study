"""Generic session-time / DST window contract (market-agnostic, IANA-only).

Maps a fixed local core-open clock + IANA timezone + a trading date to the
exact pre-open exposure window in UTC and the ERA5 hourly support timestamps
needed for piecewise-linear integration.

The UTC offset is NEVER hard-coded; it is always derived by the IANA timezone
engine per date, so DST transitions are handled correctly by construction.
The helper is generic (Taipei 09:00, Mumbai 09:15, New York 09:30, London
08:00 all work) and stores no market names.

Separate concerns are kept distinct:
  - session_regime  = the fixed local open/close clocks (e.g. 09:30);
  - timezone_regime = the IANA zone name;
  - derived_offset  = the date-specific UTC offset produced by the engine.
DST is a timezone-regime property, never a new session regime.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.v2.core import V2Error


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise V2Error(f"unknown IANA timezone: {name}") from exc


def detect_local_time_issue(local_dt: datetime, tz: ZoneInfo) -> str | None:
    """Detect an ambiguous or nonexistent local wall-clock time.

    Returns "ambiguous" when the wall clock occurs twice (fall-back overlap),
    and "nonexistent" when it never occurs (spring-forward gap).  Returns None
    for an unambiguous time.

    Detection uses a UTC round-trip: a nonexistent time never round-trips to
    its own wall clock (both folds land on a different clock), while an
    ambiguous time round-trips on both folds but to two different UTC instants.
    """
    if local_dt.tzinfo is None:
        raise V2Error("local datetime must be timezone-aware")
    dt0 = local_dt.replace(fold=0)
    dt1 = local_dt.replace(fold=1)
    utc0 = dt0.astimezone(timezone.utc)
    utc1 = dt1.astimezone(timezone.utc)
    wall = local_dt.replace(tzinfo=None)
    back0 = utc0.astimezone(tz).replace(tzinfo=None)
    back1 = utc1.astimezone(tz).replace(tzinfo=None)
    if back0 != wall or back1 != wall:
        return "nonexistent"
    if utc0 != utc1:
        return "ambiguous"
    return None


def _floor_hour(utc: datetime) -> datetime:
    return utc.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(utc: datetime) -> datetime:
    floored = _floor_hour(utc)
    return floored if floored == utc else floored + timedelta(hours=1)


def hourly_support_timestamps(window_start_utc: datetime, window_end_utc: datetime) -> list[datetime]:
    """ERA5 hourly support timestamps bracketing ``[start, end)``.

    Returns every whole UTC hour from floor(start) through ceil(end), the set
    required to piecewise-linearly integrate the window.  A window on whole
    hours (e.g. 07:00-09:00) yields 3 timestamps; a :30 window (07:30-09:30)
    yields 4.
    """
    if window_end_utc <= window_start_utc:
        raise V2Error("window end must be after start")
    out: list[datetime] = []
    current = _floor_hour(window_start_utc)
    last = _ceil_hour(window_end_utc)
    while current <= last:
        out.append(current)
        current += timedelta(hours=1)
    # fail closed on duplicates (should never happen, but guard deterministically)
    if len(out) != len(set(out)):
        raise V2Error("duplicate hourly support timestamps")
    return out


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def resolve_open_window(
    trading_date: date,
    timezone_name: str,
    core_open_local_clock: time,
    window_minutes_before: int = 120,
    core_close_local_clock: time | None = None,
) -> dict:
    """Resolve the full pre-open exposure contract for one trading day.

    Returns a dict with local and UTC representations of the core open, the
    pre-open window, the hourly support timestamps, and the date-specific UTC
    offset.  Raises V2Error on an unknown timezone, an invalid session clock,
    or an ambiguous/nonexistent local open time.
    """
    if not 0 <= core_open_local_clock.hour < 24:
        raise V2Error("invalid core open clock hour")
    if (core_open_local_clock.minute, core_open_local_clock.second, core_open_local_clock.microsecond) not in {
        (0, 0, 0), (15, 0, 0), (30, 0, 0), (45, 0, 0)
    }:
        raise V2Error("core open clock must be a whole or :15/:30/:45 minute")
    tz = _tz(timezone_name)
    open_local = datetime.combine(trading_date, core_open_local_clock, tzinfo=tz)
    issue = detect_local_time_issue(open_local, tz)
    if issue is not None:
        raise V2Error(f"core open local time is {issue} on {trading_date} in {timezone_name}")

    window_start_local = open_local - timedelta(minutes=window_minutes_before)
    window_end_local = open_local

    open_utc = open_local.astimezone(timezone.utc)
    window_start_utc = window_start_local.astimezone(timezone.utc)
    window_end_utc = window_end_local.astimezone(timezone.utc)
    support = hourly_support_timestamps(window_start_utc, window_end_utc)

    return {
        "trading_date": trading_date.isoformat(),
        "timezone": timezone_name,
        "core_open_local_clock": core_open_local_clock.isoformat(),
        "window_minutes_before": window_minutes_before,
        "utc_offset_seconds": open_local.utcoffset().total_seconds(),
        "utc_offset_str": open_local.strftime("%z"),
        "core_open_local": _iso(open_local),
        "core_open_utc": _iso(open_utc),
        "window_start_local": _iso(window_start_local),
        "window_end_local": _iso(window_end_local),
        "window_start_utc": _iso(window_start_utc),
        "window_end_utc": _iso(window_end_utc),
        "hourly_support_timestamps": [_iso(t) for t in support],
        "hourly_support_count": len(support),
        "core_close_local": _iso(datetime.combine(trading_date, core_close_local_clock, tzinfo=tz)) if core_close_local_clock else None,
    }
