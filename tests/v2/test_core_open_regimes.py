"""Stage 5B-2: core-open regime registry + resolver tests."""
from __future__ import annotations

from datetime import date, time

import pytest

from scripts.v2.core import V2Error
from scripts.v2.core_open_regimes import (
    all_market_ids,
    load_registry,
    resolve_core_open,
    validate_registry,
)
from scripts.v2.session_time import resolve_open_window

RESEARCH_MARKETS = [
    "sse_composite",
    "szse_component",
    "topix",
    "nifty50",
    "ftse100",
    "dax",
    "sp500",
    "bse50",
]

EXPECTED = {
    "sse_composite": ("09:30", "Asia/Shanghai"),
    "szse_component": ("09:30", "Asia/Shanghai"),
    "topix": ("09:00", "Asia/Tokyo"),
    "nifty50": ("09:15", "Asia/Kolkata"),
    "ftse100": ("08:00", "Europe/London"),
    "dax": ("09:00", "Europe/Berlin"),
    "sp500": ("09:30", "America/New_York"),
    "bse50": ("09:30", "Asia/Shanghai"),
}


def _d(s: str) -> date:
    return date.fromisoformat(s)


# --- Registry -----------------------------------------------------------
def test_eight_research_markets_present():
    ids = set(all_market_ids())
    for m in RESEARCH_MARKETS:
        assert m in ids


def test_registry_validates_clean():
    assert validate_registry() == []


def test_reference_market_taiex_present():
    registry = load_registry()
    assert registry["reference_market"]["market_id"] == "taiex"


def test_all_qualified_single_regime():
    for m in RESEARCH_MARKETS:
        r = resolve_core_open(m, "2023-06-15")
        assert r["qualification_status"] == "qualified_single_regime"


def test_no_overlap_in_registry():
    registry = load_registry()
    for market in registry["markets"]:
        intervals = [
            (_d(r["effective_from"]), _d(r["effective_to"]))
            for r in market["regimes"]
        ]
        intervals.sort()
        for i in range(len(intervals) - 1):
            assert intervals[i][1] < intervals[i + 1][0]


def test_effective_boundary_inclusive():
    # bse50 effective_from is 2021-11-15 (inclusive), so 11-14 fails and 11-15 resolves
    with pytest.raises(V2Error):
        resolve_core_open("bse50", "2021-11-14")
    assert resolve_core_open("bse50", "2021-11-15")["core_open_local"] == "09:30"


def test_duplicate_market_regime_overlap_fails_validation():
    bad = {
        "schema_version": "2.0.0",
        "registry_id": "x",
        "markets": [
            {
                "market_id": "m1",
                "timezone": "Asia/Tokyo",
                "regimes": [
                    {"regime_id": "r1", "effective_from": "2020-01-01", "effective_to": "2022-12-31", "core_open_local": "09:00", "evidence_ids": [], "qualification_status": "qualified_single_regime"},
                    {"regime_id": "r2", "effective_from": "2022-06-01", "effective_to": "2025-12-31", "core_open_local": "09:00", "evidence_ids": [], "qualification_status": "qualified_single_regime"},
                ],
            }
        ],
        "evidence": {},
    }
    errors = validate_registry(registry=bad)
    assert any("overlapping" in e for e in errors)


def test_invalid_range_fails_validation():
    bad = {
        "schema_version": "2.0.0",
        "registry_id": "x",
        "markets": [
            {
                "market_id": "m1",
                "timezone": "Asia/Tokyo",
                "regimes": [
                    {"regime_id": "r1", "effective_from": "2022-01-01", "effective_to": "2020-01-01", "core_open_local": "09:00", "evidence_ids": [], "qualification_status": "qualified_single_regime"},
                ],
            }
        ],
        "evidence": {},
    }
    errors = validate_registry(registry=bad)
    assert any("effective_to before effective_from" in e for e in errors)


def test_invalid_clock_fails_validation():
    bad = {
        "schema_version": "2.0.0",
        "registry_id": "x",
        "markets": [
            {
                "market_id": "m1",
                "timezone": "Asia/Tokyo",
                "regimes": [
                    {"regime_id": "r1", "effective_from": "2020-01-01", "effective_to": "2025-12-31", "core_open_local": "9am", "evidence_ids": [], "qualification_status": "qualified_single_regime"},
                ],
            }
        ],
        "evidence": {},
    }
    errors = validate_registry(registry=bad)
    assert any("invalid core_open_local" in e for e in errors)


# --- Resolver -----------------------------------------------------------
def test_resolver_expected_clocks():
    for m, (clock, tz) in EXPECTED.items():
        r = resolve_core_open(m, "2023-06-15")
        assert r["core_open_local"] == clock
        assert r["timezone"] == tz


def test_resolver_unknown_market_fails():
    with pytest.raises(V2Error):
        resolve_core_open("does_not_exist", "2023-06-15")


def test_resolver_no_regime_date_fails():
    # bse50 before its exchange effective date -> fail-closed (no fallback)
    with pytest.raises(V2Error):
        resolve_core_open("bse50", "2020-03-04")


# --- Semantics (pre-open / early sessions must not win) -----------------
def test_shanghai_auction_not_selected():
    r = resolve_core_open("sse_composite", "2023-06-15")
    assert r["core_open_local"] == "09:30"
    assert r["core_open_local"] != "09:15"


def test_shenzhen_auction_not_selected():
    assert resolve_core_open("szse_component", "2023-06-15")["core_open_local"] == "09:30"


def test_mumbai_preopen_not_selected():
    assert resolve_core_open("nifty50", "2023-06-15")["core_open_local"] == "09:15"
    assert resolve_core_open("nifty50", "2023-06-15")["core_open_local"] != "09:00"


def test_xetra_extended_retail_not_selected():
    assert resolve_core_open("dax", "2023-06-15")["core_open_local"] == "09:00"
    assert resolve_core_open("dax", "2023-06-15")["core_open_local"] != "08:00"


def test_nyse_early_not_selected():
    assert resolve_core_open("sp500", "2023-06-15")["core_open_local"] == "09:30"


# --- Tokyo known close change -------------------------------------------
def test_tokyo_close_change_does_not_shift_open():
    assert resolve_core_open("topix", "2024-11-04")["core_open_local"] == "09:00"
    assert resolve_core_open("topix", "2024-11-05")["core_open_local"] == "09:00"


# --- DST integration (chain resolver -> session-time engine) ------------
def test_london_local_clock_unchanged_across_dst():
    for d in ("2024-03-25", "2024-10-28"):
        regime = resolve_core_open("ftse100", d)
        window = resolve_open_window(_d(d), regime["timezone"], time(8, 0), 120)
        assert window["core_open_local_clock"] == "08:00:00"


def test_new_york_local_clock_unchanged_across_dst():
    for d in ("2024-03-08", "2024-03-11"):
        regime = resolve_core_open("sp500", d)
        window = resolve_open_window(_d(d), regime["timezone"], time(9, 30), 120)
        assert window["core_open_local_clock"] == "09:30:00"


def test_london_utc_window_varies_with_dst():
    # London local 08:00 -> UTC 08:00 (GMT) in winter, 07:00 (BST) in summer
    winter = resolve_open_window(_d("2024-01-15"), "Europe/London", time(8, 0), 120)
    summer = resolve_open_window(_d("2024-07-15"), "Europe/London", time(8, 0), 120)
    assert winter["core_open_local_clock"] == summer["core_open_local_clock"] == "08:00:00"
    assert winter["core_open_utc"].startswith("2024-01-15T08:00")
    assert summer["core_open_utc"].startswith("2024-07-15T07:00")
