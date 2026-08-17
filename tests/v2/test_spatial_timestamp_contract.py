"""Stage 5C-R1: layered timestamp-contract regression tests.

Covers transport (date x time Cartesian product), scientific-target selection,
extras identification, no-leakage, and deterministic window changes.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

import scripts.v2.spatial.build_taiex_spatial_pilot as pilot_mod
from scripts.v2.spatial.timestamp_contract import (
    assess_contract,
    canonical,
    extra_transport,
    scientific_target_set,
    transport_cartesian_set,
)

REAL_REQUEST = {
    "date": [
        "2026-03-01", "2026-03-02", "2026-03-03",
        "2026-03-04", "2026-03-05", "2026-03-06",
    ],
    "time": ["00:00", "23:00"],
}

REAL_EXTRAS = ["2026-03-01T00:00", "2026-03-06T23:00"]


# --- canonical normalization -------------------------------------------
def test_canonical_datetime():
    assert canonical(datetime(2026, 3, 1, 0, 0)) == "2026-03-01T00:00"


def test_canonical_subsecond_string():
    assert canonical("2026-03-01T00:00:00.000000000") == "2026-03-01T00:00"


def test_canonical_offset_string():
    assert canonical("2026-03-01T23:00:00+00:00") == "2026-03-01T23:00"


def test_canonical_z_string():
    assert canonical("2026-03-01T00:00:00Z") == "2026-03-01T00:00"


# --- transport set ------------------------------------------------------
def test_transport_cartesian_exact_match():
    req = {"date": ["2026-03-01", "2026-03-02"], "time": ["00:00", "23:00"]}
    assert transport_cartesian_set(req) == {
        "2026-03-01T00:00", "2026-03-01T23:00",
        "2026-03-02T00:00", "2026-03-02T23:00",
    }


def test_transport_cartesian_full_request_is_12():
    assert len(transport_cartesian_set(REAL_REQUEST)) == 12


def test_missing_transport_timestamp_fails():
    transport = {"A", "B", "C"}
    observed = {"A", "B"}  # C missing
    contract = assess_contract(transport, observed, {"A"}, {"A"})
    assert contract["transport_exact_match"] is False


def test_unexpected_extra_timestamp_fails():
    transport = {"A", "B"}
    observed = {"A", "B", "C"}  # unexpected 13th
    contract = assess_contract(transport, observed, {"A"}, {"A"})
    assert contract["transport_exact_match"] is False
    assert contract["raw_observed_count"] == 3


# --- scientific target --------------------------------------------------
def test_real_contract_10_of_12():
    scientific = scientific_target_set()
    transport = transport_cartesian_set(REAL_REQUEST)
    observed = set(transport)  # real raw matched transport
    analysis = set(scientific)
    contract = assess_contract(transport, observed, scientific, analysis)
    assert contract["scientific_target_count"] == 10
    assert contract["transport_requested_count"] == 12
    assert contract["raw_observed_count"] == 12
    assert contract["analysis_consumed_count"] == 10
    assert contract["transport_exact_match"] is True
    assert contract["scientific_subset_match"] is True
    assert contract["analysis_exact_target_match"] is True


def test_exact_two_extras_identified():
    scientific = scientific_target_set()
    transport = transport_cartesian_set(REAL_REQUEST)
    assert extra_transport(transport, scientific) == REAL_EXTRAS


def test_extras_never_enter_analysis():
    scientific = scientific_target_set()
    transport = transport_cartesian_set(REAL_REQUEST)
    analysis = set(scientific)
    contract = assess_contract(transport, set(transport), scientific, analysis)
    assert contract["no_extra_leakage_into_analysis"] is True


def test_extra_leakage_detected():
    scientific = scientific_target_set()
    transport = transport_cartesian_set(REAL_REQUEST)
    # leak one extra into the analysis set
    analysis = set(scientific) | {REAL_EXTRAS[0]}
    contract = assess_contract(transport, set(transport), scientific, analysis)
    assert contract["no_extra_leakage_into_analysis"] is False
    assert contract["analysis_exact_target_match"] is False


def test_changed_window_changes_target_deterministically(monkeypatch):
    from scripts.v2.spatial.build_taiex_spatial_acceptance import pre_open_timestamps

    before = pre_open_timestamps()
    assert len(before) == 10

    monkeypatch.setattr(pilot_mod, "PRE_OPEN_LOCAL_HOURS", [7])  # only 07:00 local
    after = pre_open_timestamps()
    assert len(after) == 5  # 5 days x 1 hour
    # deterministic: repeated call yields the same set
    assert after == pre_open_timestamps()
    # the narrowed window is a strict subset of the original targets
    assert set(after) < set(before)


def test_scientific_target_matches_known_dates():
    scientific = scientific_target_set()
    # every target is a pre-open boundary of the five pilot trading days
    assert "2026-03-01T23:00" in scientific  # Mon 07:00 Taipei
    assert "2026-03-06T00:00" in scientific  # Fri 08:00 Taipei
    # the two boundary extras are NOT scientific targets
    assert "2026-03-01T00:00" not in scientific
    assert "2026-03-06T23:00" not in scientific
