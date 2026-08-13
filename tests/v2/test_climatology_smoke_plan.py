"""Stage 5E-1: climatology smoke plan-level tests (zero network)."""
from __future__ import annotations

from datetime import date

import pytest

from scripts.v2.climatology.build_taiex_climatology_smoke import (
    BASELINE_YEARS,
    cds_request_for_year,
    temporal_support_timestamps,
)
from scripts.v2.fetch_era5 import expected_timestamps


def test_three_years_93_daily_exposures():
    plans = [cds_request_for_year(y, "Asia/Taipei") for y in BASELINE_YEARS]
    total = sum(p["daily_count"] for p in plans)
    assert total == 93


def test_each_year_31_slots():
    for y in BASELINE_YEARS:
        plan = cds_request_for_year(y, "Asia/Taipei")
        assert plan["daily_count"] == 31


def test_feb29_never_a_daily_exposure():
    for y in BASELINE_YEARS:
        plan = cds_request_for_year(y, "Asia/Taipei")
        for d in plan["actual_dates"]:
            dd = date.fromisoformat(d)
            assert not (dd.month == 2 and dd.day == 29)


def test_temporal_support_subset_of_transport():
    plans = [cds_request_for_year(y, "Asia/Taipei") for y in BASELINE_YEARS]
    support = set(temporal_support_timestamps(plans))
    transport: set[str] = set()
    for plan in plans:
        transport |= {t.isoformat() for t in expected_timestamps(plan["request"])}
    assert support <= transport


def test_transport_has_boundary_extras():
    # transport (date x time cartesian) strictly covers support (93 x 3 = 279)
    plans = [cds_request_for_year(y, "Asia/Taipei") for y in BASELINE_YEARS]
    support = set(temporal_support_timestamps(plans))
    transport: set[str] = set()
    for plan in plans:
        transport |= {t.isoformat() for t in expected_timestamps(plan["request"])}
    assert len(support) == 279
    assert len(transport) == 291
    assert len(transport - support) == 12


def test_leap_year_requests_feb29_as_bracket():
    # 2020 needs Feb 29 23:00 UTC as the 07:00 Taipei bracket for Mar 01
    plan = cds_request_for_year(2020, "Asia/Taipei")
    assert "2020-02-29" in plan["request"]["date"]
    # the Mar 01 daily entry's first breakpoint is Feb 29 23:00 UTC
    mar01 = next(row for row in plan["daily"] if row["actual_date"] == "2020-03-01")
    assert mar01["breakpoints_utc"][0].startswith("2020-02-29T23:00")
