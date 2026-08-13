"""Stage 5B-3: historical IANA timezone canary tests (pure, no network)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, time

import pytest

from scripts.v2.historical_timezone import CANARIES, tzdata_zoneinfo_dir, tzdb_provider_info
from scripts.v2.session_time import resolve_open_window


def _observed(canary: dict) -> dict:
    return resolve_open_window(
        date.fromisoformat(canary["date"]),
        canary["timezone"],
        time.fromisoformat(canary["core_open_local"]),
        120,
    )


# --- Provider -----------------------------------------------------------
def test_provider_and_version_resolvable():
    info = tzdb_provider_info()
    assert info["timezone_data_provider"] in {"tzdata", "system_tzdb"}
    assert info["timezone_data_version"] is not None


def test_provider_is_tzdata_on_windows():
    # On Windows there is no system tzdb, so tzdata must be the provider.
    info = tzdb_provider_info()
    if sys.platform == "win32":
        assert info["timezone_data_provider"] == "tzdata"
        assert info["tzdata_installed"] is True


def test_ambient_matches_explicit_tzdata_provider():
    # Recompute every canary offset with PYTHONTZPATH pointed ONLY at the tzdata
    # package zoneinfo dir, and compare to the ambient runtime.
    zdir = tzdata_zoneinfo_dir()
    if zdir is None:
        pytest.skip("tzdata package not present; cannot run explicit-provider check")
    code = (
        "import json, zoneinfo\n"
        "from datetime import datetime\n"
        "for tz, d in json.loads(input()):\n"
        "    z = zoneinfo.ZoneInfo(tz)\n"
        "    print(z.utcoffset(datetime.fromisoformat(d + 'T12:00:00')))\n"
    )
    pairs = [(c["timezone"], c["date"]) for c in CANARIES]
    payload = json.dumps(pairs)
    env = dict(os.environ)
    env["PYTHONTZPATH"] = str(zdir)
    explicit = subprocess.run(
        [sys.executable, "-c", code], input=payload, capture_output=True, text=True, env=env
    ).stdout.splitlines()
    for canary, exp in zip(CANARIES, explicit):
        observed_offset = _observed(canary)["utc_offset_str"]
        # compare the signed HH:MM portion
        assert observed_offset == _offset_from_timedelta_str(exp), canary["label"]


def _offset_from_timedelta_str(td_repr: str) -> str:
    # "1:00:00" -> "+0100", "9:00:00" -> "+0900", "-1 day, 19:00:00" -> "-0500"
    if "day" in td_repr:
        # e.g. "-1 day, 19:00:00" -> -(24h - 19h) = -05:00
        days_part, time_part = td_repr.split(",")
        days = int(days_part.split()[0])
        h, m, _ = time_part.strip().split(":")
        total_minutes = days * 1440 + int(h) * 60 + int(m)
    else:
        h, m, _ = td_repr.split(":")
        sign = "-" if td_repr.startswith("-") else "+"
        total_minutes = int(h) * 60 + int(m)
        if sign == "-":
            total_minutes = -total_minutes
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}{total_minutes % 60:02d}"


# --- Individual canaries ------------------------------------------------
def _assert_canary(label: str):
    canary = next(c for c in CANARIES if c["label"] == label)
    obs = _observed(canary)
    exp = canary["expected"]
    assert obs["utc_offset_str"] == exp["offset_str"], label
    if "core_open_utc" in exp:
        assert obs["core_open_utc"] == exp["core_open_utc"], label
    if "window_start_utc" in exp:
        assert obs["window_start_utc"] == exp["window_start_utc"], label
        assert obs["window_end_utc"] == exp["window_end_utc"], label
    if "support" in exp:
        assert obs["hourly_support_timestamps"] == exp["support"], label


def test_new_york_2006_03_20_minus5():
    _assert_canary("new_york_2006_03_20_spring")


def test_new_york_2007_03_20_minus4():
    _assert_canary("new_york_2007_03_20_spring")


def test_new_york_2006_10_30_minus5():
    _assert_canary("new_york_2006_10_30_fall")


def test_new_york_2007_10_30_minus4():
    _assert_canary("new_york_2007_10_30_fall")


def test_berlin_1995_10_23_plus1():
    _assert_canary("berlin_1995_10_23")


def test_berlin_1996_10_23_plus2():
    _assert_canary("berlin_1996_10_23")


def test_london_1995_10_20_plus1():
    _assert_canary("london_1995_10_20")


def test_london_1995_10_23_plus0():
    _assert_canary("london_1995_10_23")


def test_shanghai_1991_07_01_plus9_cross_date():
    canary = next(c for c in CANARIES if c["label"] == "shanghai_1991_07_01_dst")
    obs = _observed(canary)
    assert obs["utc_offset_str"] == "+0900"
    # cross-date window: 1991-06-30 22:30 -> 1991-07-01 00:30
    assert obs["window_start_utc"] == "1991-06-30T22:30:00+00:00"
    assert obs["window_end_utc"] == "1991-07-01T00:30:00+00:00"
    assert obs["hourly_support_timestamps"] == [
        "1991-06-30T22:00:00+00:00",
        "1991-06-30T23:00:00+00:00",
        "1991-07-01T00:00:00+00:00",
        "1991-07-01T01:00:00+00:00",
    ]


def test_shanghai_1992_07_01_plus8_no_dst():
    canary = next(c for c in CANARIES if c["label"] == "shanghai_1992_07_01_no_dst")
    obs = _observed(canary)
    assert obs["utc_offset_str"] == "+0800"
    assert obs["core_open_utc"] == "1992-07-01T01:30:00+00:00"


def test_tokyo_static_plus9():
    assert _observed(next(c for c in CANARIES if c["label"] == "tokyo_1991_07_01_static"))["utc_offset_str"] == "+0900"
    assert _observed(next(c for c in CANARIES if c["label"] == "tokyo_2020_07_01_static"))["utc_offset_str"] == "+0900"


def test_kolkata_static_plus530():
    assert _observed(next(c for c in CANARIES if c["label"] == "kolkata_1991_07_01_static"))["utc_offset_str"] == "+0530"
    assert _observed(next(c for c in CANARIES if c["label"] == "kolkata_2020_07_01_static"))["utc_offset_str"] == "+0530"


# --- Rule-difference assertions ----------------------------------------
def test_china_1991_dst_1992_no_dst():
    c91 = next(c for c in CANARIES if c["label"] == "shanghai_1991_07_01_dst")
    c92 = next(c for c in CANARIES if c["label"] == "shanghai_1992_07_01_no_dst")
    assert _observed(c91)["utc_offset_str"] == "+0900"
    assert _observed(c92)["utc_offset_str"] == "+0800"


def test_berlin_historical_rule_difference():
    c95 = next(c for c in CANARIES if c["label"] == "berlin_1995_10_23")
    c96 = next(c for c in CANARIES if c["label"] == "berlin_1996_10_23")
    assert _observed(c95)["utc_offset_str"] == "+0100"
    assert _observed(c96)["utc_offset_str"] == "+0200"
