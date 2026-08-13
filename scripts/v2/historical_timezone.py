"""Historical IANA timezone qualification (Stage 5B-3).

Pure, zero-network.  Provides the tzdb provider audit (type + version), the
historical canary definitions with expected values, and a timezone fingerprint
that future climatology audits bind to, so a tzdata update that changed
historical rules would be detectable.

Frozen principle: historical civil time = IANA timezone evaluated at the
historical date.  No current-offset backfill, no hard-coded EST/EDT/BST/CET,
no manual DST transition tables.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
import zoneinfo
from importlib import metadata
from pathlib import Path
from typing import Any

BASELINE_PERIOD = "1991-2020"


def tzdb_provider_info() -> dict[str, Any]:
    """Audit the timezone data provider used by ``zoneinfo``.

    On Windows ``zoneinfo.TZPATH`` is typically empty and the actual provider is
    the first-party PyPI ``tzdata`` package (read via importlib fallback).
    """
    try:
        tzdata_version = metadata.version("tzdata")
        tzdata_installed = True
    except Exception:
        tzdata_version = None
        tzdata_installed = False

    provider: str
    provider_version: str | None
    if tzdata_installed:
        provider = "tzdata"
        provider_version = tzdata_version
    elif zoneinfo.TZPATH:
        provider = "system_tzdb"
        provider_version = None
    else:
        provider = "unresolved"
        provider_version = None

    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "zoneinfo_tzpath": list(zoneinfo.TZPATH),
        "tzdata_installed": tzdata_installed,
        "tzdata_version": tzdata_version,
        "timezone_data_provider": provider,
        "timezone_data_version": provider_version,
    }


def tzdata_zoneinfo_dir() -> Path | None:
    """Return the tzdata package's zoneinfo directory, if the package is present."""
    try:
        import tzdata
    except ImportError:
        return None
    candidate = Path(list(tzdata.__path__)[0]) / "zoneinfo"
    return candidate if candidate.exists() else None


# --- Historical canaries (label -> expected) ----------------------------
# Each canary maps (timezone, historical date, core-open clock) to the expected
# offset / UTC window / hourly support.  The expected values encode the real
# historical rule changes: US 2007, EU 1995->1996, UK 1995, China 1991 (last DST).
CANARIES: list[dict[str, Any]] = [
    # --- New York 2007 rule change ---
    {
        "label": "new_york_2006_03_20_spring",
        "timezone": "America/New_York",
        "date": "2006-03-20",
        "core_open_local": "09:30",
        "expected": {
            "offset_str": "-0500",
            "core_open_utc": "2006-03-20T14:30:00+00:00",
            "window_start_utc": "2006-03-20T12:30:00+00:00",
            "window_end_utc": "2006-03-20T14:30:00+00:00",
            "support": ["2006-03-20T12:00:00+00:00", "2006-03-20T13:00:00+00:00", "2006-03-20T14:00:00+00:00", "2006-03-20T15:00:00+00:00"],
        },
    },
    {
        "label": "new_york_2007_03_20_spring",
        "timezone": "America/New_York",
        "date": "2007-03-20",
        "core_open_local": "09:30",
        "expected": {
            "offset_str": "-0400",
            "core_open_utc": "2007-03-20T13:30:00+00:00",
            "window_start_utc": "2007-03-20T11:30:00+00:00",
            "window_end_utc": "2007-03-20T13:30:00+00:00",
            "support": ["2007-03-20T11:00:00+00:00", "2007-03-20T12:00:00+00:00", "2007-03-20T13:00:00+00:00", "2007-03-20T14:00:00+00:00"],
        },
    },
    {
        "label": "new_york_2006_10_30_fall",
        "timezone": "America/New_York",
        "date": "2006-10-30",
        "core_open_local": "09:30",
        "expected": {
            "offset_str": "-0500",
            "window_start_utc": "2006-10-30T12:30:00+00:00",
            "window_end_utc": "2006-10-30T14:30:00+00:00",
        },
    },
    {
        "label": "new_york_2007_10_30_fall",
        "timezone": "America/New_York",
        "date": "2007-10-30",
        "core_open_local": "09:30",
        "expected": {
            "offset_str": "-0400",
            "window_start_utc": "2007-10-30T11:30:00+00:00",
            "window_end_utc": "2007-10-30T13:30:00+00:00",
        },
    },
    # --- Berlin 1995 -> 1996 ---
    {
        "label": "berlin_1995_10_23",
        "timezone": "Europe/Berlin",
        "date": "1995-10-23",
        "core_open_local": "09:00",
        "expected": {
            "offset_str": "+0100",
            "core_open_utc": "1995-10-23T08:00:00+00:00",
            "window_start_utc": "1995-10-23T06:00:00+00:00",
            "window_end_utc": "1995-10-23T08:00:00+00:00",
            "support": ["1995-10-23T06:00:00+00:00", "1995-10-23T07:00:00+00:00", "1995-10-23T08:00:00+00:00"],
        },
    },
    {
        "label": "berlin_1996_10_23",
        "timezone": "Europe/Berlin",
        "date": "1996-10-23",
        "core_open_local": "09:00",
        "expected": {
            "offset_str": "+0200",
            "core_open_utc": "1996-10-23T07:00:00+00:00",
            "window_start_utc": "1996-10-23T05:00:00+00:00",
            "window_end_utc": "1996-10-23T07:00:00+00:00",
            "support": ["1996-10-23T05:00:00+00:00", "1996-10-23T06:00:00+00:00", "1996-10-23T07:00:00+00:00"],
        },
    },
    # --- London 1995 UK exception ---
    {
        "label": "london_1995_10_20",
        "timezone": "Europe/London",
        "date": "1995-10-20",
        "core_open_local": "08:00",
        "expected": {
            "offset_str": "+0100",
            "core_open_utc": "1995-10-20T07:00:00+00:00",
            "window_start_utc": "1995-10-20T05:00:00+00:00",
            "window_end_utc": "1995-10-20T07:00:00+00:00",
            "support": ["1995-10-20T05:00:00+00:00", "1995-10-20T06:00:00+00:00", "1995-10-20T07:00:00+00:00"],
        },
    },
    {
        "label": "london_1995_10_23",
        "timezone": "Europe/London",
        "date": "1995-10-23",
        "core_open_local": "08:00",
        "expected": {
            "offset_str": "+0000",
            "core_open_utc": "1995-10-23T08:00:00+00:00",
            "window_start_utc": "1995-10-23T06:00:00+00:00",
            "window_end_utc": "1995-10-23T08:00:00+00:00",
            "support": ["1995-10-23T06:00:00+00:00", "1995-10-23T07:00:00+00:00", "1995-10-23T08:00:00+00:00"],
        },
    },
    # --- Shanghai 1991 (last DST) / 1992 (no DST) ---
    {
        "label": "shanghai_1991_07_01_dst",
        "timezone": "Asia/Shanghai",
        "date": "1991-07-01",
        "core_open_local": "09:30",
        "expected": {
            "offset_str": "+0900",
            "core_open_utc": "1991-07-01T00:30:00+00:00",
            "window_start_utc": "1991-06-30T22:30:00+00:00",
            "window_end_utc": "1991-07-01T00:30:00+00:00",
            "support": ["1991-06-30T22:00:00+00:00", "1991-06-30T23:00:00+00:00", "1991-07-01T00:00:00+00:00", "1991-07-01T01:00:00+00:00"],
        },
    },
    {
        "label": "shanghai_1992_07_01_no_dst",
        "timezone": "Asia/Shanghai",
        "date": "1992-07-01",
        "core_open_local": "09:30",
        "expected": {
            "offset_str": "+0800",
            "core_open_utc": "1992-07-01T01:30:00+00:00",
            "window_start_utc": "1992-06-30T23:30:00+00:00",
            "window_end_utc": "1992-07-01T01:30:00+00:00",
            "support": ["1992-06-30T23:00:00+00:00", "1992-07-01T00:00:00+00:00", "1992-07-01T01:00:00+00:00", "1992-07-01T02:00:00+00:00"],
        },
    },
    # --- Static controls ---
    {
        "label": "tokyo_1991_07_01_static",
        "timezone": "Asia/Tokyo",
        "date": "1991-07-01",
        "core_open_local": "09:00",
        "expected": {"offset_str": "+0900"},
    },
    {
        "label": "tokyo_2020_07_01_static",
        "timezone": "Asia/Tokyo",
        "date": "2020-07-01",
        "core_open_local": "09:00",
        "expected": {"offset_str": "+0900"},
    },
    {
        "label": "kolkata_1991_07_01_static",
        "timezone": "Asia/Kolkata",
        "date": "1991-07-01",
        "core_open_local": "09:15",
        "expected": {"offset_str": "+0530"},
    },
    {
        "label": "kolkata_2020_07_01_static",
        "timezone": "Asia/Kolkata",
        "date": "2020-07-01",
        "core_open_local": "09:15",
        "expected": {"offset_str": "+0530"},
    },
]


def timezone_fingerprint() -> dict[str, Any]:
    """Provider fingerprint + canary values, hashed for future binding."""
    provider = tzdb_provider_info()
    canary_values = [
        {"label": c["label"], "offset_str": c["expected"]["offset_str"]}
        for c in CANARIES
    ]
    payload = json.dumps(
        {"provider": provider, "canary_values": canary_values},
        sort_keys=True,
        ensure_ascii=False,
    )
    return {
        "baseline_period": BASELINE_PERIOD,
        "provider": provider,
        "canary_values": canary_values,
        "timezone_canary_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
