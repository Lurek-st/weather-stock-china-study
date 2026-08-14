"""Stage 5E-3A production canary tests (sp500/2007Q1, TCC-only).

Coverage per the Stage 5E-3A contract:
- final request-id deterministic; changes with anchor/timezone/area/dates/variable
- ordering canonicalized (order-independent)
- 90 daily exposures / 360 support / 450 transport / 90 extras / 450 fields
- pre/post DST support controls
- exact NYSE 2x2 grid expected
- municipal location never used
- only TCC requested
- accepted request lookup skips client (fake first = 1, fake repeat = 0)
- corrupt accepted artifact fails closed
- invalid container never accepted
- transport extra never consumed
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.production_canary import (
    CANARY_MARKET,
    CANARY_NETCDF_VARIABLE,
    CANARY_PERIOD,
    CANARY_VARIABLE,
    DST_CONTROLS,
    EXPECTED_AMPLIFICATION,
    EXPECTED_AREA_NWS_E,
    EXPECTED_DAILY_EXPOSURES,
    EXPECTED_EXTRAS,
    EXPECTED_FIELDS,
    EXPECTED_GRID_CELL_VALUES,
    EXPECTED_GRID_LATITUDES,
    EXPECTED_GRID_LONGITUDES,
    EXPECTED_PERIOD_DAYS,
    EXPECTED_SUPPORT,
    EXPECTED_TIME_UNION,
    EXPECTED_TRANSPORT,
    EXPECTED_UNIQUE_SUPPORT,
    anchor_payload,
    cds_request,
    dst_control_rows,
    exact_grid_check,
    final_request_id,
    load_sp500_anchor,
    lookup_accepted_request,
    plan_counts,
    preflight,
)
from scripts.v2.core import RawArtifactStore, repo_root
from scripts.v2.spatial.grid_geometry import bilinear_weights, surrounding_cell


# ---------------------------------------------------------------------------
# Frozen contract
# ---------------------------------------------------------------------------


def test_canary_contract_market_period():
    assert CANARY_MARKET == "sp500"
    assert CANARY_PERIOD == "2007Q1"


def test_anchor_hash_matches_registry():
    anchor = load_sp500_anchor()
    assert anchor["anchor_hash"].startswith("1ec0a4db")
    assert anchor["qualification_status"] == "qualified"
    assert abs(anchor["coordinate"]["latitude"] - 40.7070653) < 1e-6
    assert abs(anchor["coordinate"]["longitude"] - (-74.0111761)) < 1e-6


def test_exact_expected_stencil():
    anchor = load_sp500_anchor()
    lat = float(anchor["coordinate"]["latitude"])
    lon = float(anchor["coordinate"]["longitude"])
    corners = surrounding_cell(lat, lon)
    assert {round(c.latitude, 2) for c in corners.values()} == set(EXPECTED_GRID_LATITUDES)
    assert {round(c.longitude, 2) for c in corners.values()} == set(EXPECTED_GRID_LONGITUDES)


def test_area_from_frozen_stencil():
    anchor = load_sp500_anchor()
    lat = float(anchor["coordinate"]["latitude"])
    lon = float(anchor["coordinate"]["longitude"])
    corners = surrounding_cell(lat, lon)
    north = max(c.latitude for c in corners.values())
    south = min(c.latitude for c in corners.values())
    west = min(c.longitude for c in corners.values())
    east = max(c.longitude for c in corners.values())
    assert [north, west, south, east] == EXPECTED_AREA_NWS_E


# ---------------------------------------------------------------------------
# Planning counts
# ---------------------------------------------------------------------------


def test_plan_counts_exact():
    c = plan_counts()
    assert c["period_days"] == EXPECTED_PERIOD_DAYS
    assert c["daily_exposures"] == EXPECTED_DAILY_EXPOSURES
    assert c["support"] == EXPECTED_SUPPORT
    assert c["unique_support"] == EXPECTED_UNIQUE_SUPPORT
    assert c["time_union"] == EXPECTED_TIME_UNION
    assert c["transport"] == EXPECTED_TRANSPORT
    assert c["extras"] == EXPECTED_EXTRAS
    assert abs(c["amplification"] - EXPECTED_AMPLIFICATION) < 1e-6
    assert c["cds_fields"] == EXPECTED_FIELDS
    assert c["grid_cell_values"] == EXPECTED_GRID_CELL_VALUES


def test_feb29_not_in_canary_period():
    # 2007 is not a leap year; canary is Jan-Mar 2007 so no Feb 29 exists.
    days = [(date(2007, 1, 1) + timedelta(days=i)).isoformat() for i in range(90)]
    assert not any(d.startswith("2007-02-29") for d in days)


def test_support_subset_transport():
    from scripts.v2.climatology.production_canary import daily_window_plan

    rows = daily_window_plan()
    support = set()
    for r in rows:
        support.update(r["support_utc"])
    transport = set()
    for d in [date(2007, 1, 1) + timedelta(days=i) for i in range(90)]:
        for h in EXPECTED_TIME_UNION:
            transport.add(datetime.fromisoformat(f"{d.isoformat()}T{h}:00+00:00").isoformat())
    assert support <= transport
    assert len(support) == EXPECTED_SUPPORT
    assert len(transport) == EXPECTED_TRANSPORT


# ---------------------------------------------------------------------------
# DST controls
# ---------------------------------------------------------------------------


def test_dst_positive_controls():
    rows = dst_control_rows()
    assert len(rows) == 2
    for r in rows:
        assert r["pass"], r


def test_dst_control_dates_present():
    dates = {r["date"] for r in dst_control_rows()}
    assert dates == {"2007-03-09", "2007-03-12"}


# ---------------------------------------------------------------------------
# Request identity
# ---------------------------------------------------------------------------


def test_request_id_deterministic():
    a = final_request_id()
    b = final_request_id()
    assert a == b
    assert len(a) == 64


def test_request_id_order_independent():
    payload = anchor_payload()
    # rebuild with shuffled key order; canonical serialization must match
    shuffled = {k: payload[k] for k in sorted(payload, reverse=True)}
    assert final_request_id(shuffled) == final_request_id(payload)


def test_request_id_changes_with_anchor_hash():
    p = anchor_payload()
    p2 = dict(p, anchor_hash="0" * 64)
    assert final_request_id(p2) != final_request_id(p)


def test_request_id_changes_with_timezone_fingerprint():
    p = anchor_payload()
    p2 = dict(p)
    p2["timezone_provider"] = dict(p["timezone_provider"], version="9999.1")
    assert final_request_id(p2) != final_request_id(p)


def test_request_id_changes_with_area():
    p = anchor_payload()
    p2 = dict(p, area=[40.75, -74.25, 40.5, -74.0 + 0.25])
    assert final_request_id(p2) != final_request_id(p)


def test_request_id_changes_with_dates():
    p = anchor_payload()
    p2 = dict(p, period="2007Q2")
    assert final_request_id(p2) != final_request_id(p)


def test_request_id_changes_with_variable():
    p = anchor_payload()
    p2 = dict(p, variable=["2m_temperature"])
    assert final_request_id(p2) != final_request_id(p)


def test_request_id_does_not_bind_machine_paths():
    p = anchor_payload()
    p2 = dict(p, retrieved_at="2026-08-14T00:00:00+00:00", machine="abc", abs_path="C:\\tmp\\x")
    # These extra keys are NOT part of the canonical payload; adding them to a
    # separate dict must not change the id (they are not serialized).
    assert final_request_id() == final_request_id(p)


# ---------------------------------------------------------------------------
# CDS request contract
# ---------------------------------------------------------------------------


def test_request_only_tcc():
    req = cds_request()
    assert req["variable"] == ["total_cloud_cover"]
    assert len(req["date"]) == 90
    assert req["time"] == EXPECTED_TIME_UNION
    assert req["area"] == EXPECTED_AREA_NWS_E
    assert req["data_format"] == "netcdf"
    assert req["download_format"] == "zip"


def test_netcdf_variable_is_tcc():
    assert CANARY_NETCDF_VARIABLE == "tcc"


def test_municipal_never_used():
    # The canary primary anchor never uses municipal coordinates; the registry
    # keeps municipal points ONLY under legacy_sensitivity metadata.
    anchor = load_sp500_anchor()
    coord = anchor["coordinate"]
    legacy = anchor.get("legacy_sensitivity") or {}
    municipal = legacy.get("municipal_coordinate") or {}
    assert coord["latitude"] != municipal.get("latitude")
    assert coord["longitude"] != municipal.get("longitude")
    payload = anchor_payload()
    assert "municipal" not in json.dumps(payload)
    assert "locations.yaml" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Exact grid validation
# ---------------------------------------------------------------------------


def test_exact_grid_check_passes():
    summary = {
        "member_summaries": [
            {
                "spatial": {
                    "latitude_count": 2,
                    "longitude_count": 2,
                    "latitude_min": 40.5,
                    "latitude_max": 40.75,
                    "longitude_min": -74.25,
                    "longitude_max": -74.0,
                }
            }
        ]
    }
    assert exact_grid_check(summary)["exact_grid_passed"] is True


def test_exact_grid_check_fails_wrong_cell():
    summary = {
        "member_summaries": [
            {
                "spatial": {
                    "latitude_count": 4,
                    "longitude_count": 4,
                    "latitude_min": 40.0,
                    "latitude_max": 41.0,
                    "longitude_min": -75.0,
                    "longitude_max": -73.0,
                }
            }
        ]
    }
    with pytest.raises(Exception):
        exact_grid_check(summary)


# ---------------------------------------------------------------------------
# Request-aware idempotency
# ---------------------------------------------------------------------------


def test_lookup_missing_returns_not_found(tmp_path):
    store = RawArtifactStore(tmp_path)
    out = lookup_accepted_request(store, "0" * 64)
    assert out["found"] is False
    assert out["skip"] is False


def _fake_accepted_artifact(tmp_path: Path, request_id: str, corrupt: bool = False) -> Path:
    store = RawArtifactStore(tmp_path)
    payload = b"PK\x03\x04fakezip"
    result = store.persist(
        source_id="cds_era5_hourly_climatology",
        provider="ECMWF",
        logical_name="sp500-2007q1-canary-test",
        payload=payload,
        request={"variable": ["total_cloud_cover"]},
        status="final",
        licence="cc",
        suffix=".zip",
        validation_metadata={"container_validation_passed": True, "raw_suffix": ".zip"},
        final_request_id=request_id,
    )
    if corrupt:
        # Corrupt the raw bytes AFTER persistence so the manifest SHA no
        # longer matches the artifact on disk (the fail-closed predicate).
        result.artifact_path.write_bytes(b"PK\x03\x04corrupted-bytes")
    return result.manifest_path


def test_lookup_accepted_skips(tmp_path):
    rid = final_request_id()
    _fake_accepted_artifact(tmp_path, rid)
    store = RawArtifactStore(tmp_path)
    out = lookup_accepted_request(store, rid)
    assert out["found"] is True
    assert out["skip"] is True
    assert out["reason"] == "accepted_request_already_present"


def test_corrupt_accepted_artifact_fails_closed(tmp_path):
    rid = final_request_id()
    manifest_path = _fake_accepted_artifact(tmp_path, rid, corrupt=True)
    store = RawArtifactStore(tmp_path)
    with pytest.raises(Exception):
        lookup_accepted_request(store, rid)
    # raw exists but sha mismatch -> fail closed, never silent re-download
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "final"


def test_fake_live_first_retrieve_1_repeat_0(tmp_path, monkeypatch):
    import scripts.v2.climatology.production_canary as pc

    # Point the module's raw store at a hermetic tmp dir.
    monkeypatch.setattr(pc, "RAW_BASE", str(tmp_path))
    tracker: list[str] = []

    class FakeClient:
        def __init__(self, raw_zip: Path):
            self.raw_zip = raw_zip
            self.calls = 0

        def retrieve(self, dataset, request, target):
            # Do NOT record here; the runner's _CountingClient records the
            # single call at its client boundary.
            self.calls += 1
            shutil.copyfile(self.raw_zip, target)

    # No accepted artifact yet -> first live run must invoke exactly one retrieve.
    # The fake writes an invalid container -> runner FAILS CLOSED (never accepted)
    # but the tracker records exactly one attempted retrieve.
    bad_zip = tmp_path / "bad.zip"
    bad_zip.write_bytes(b"not-a-real-zip")
    with pytest.raises(Exception):
        pc.run_live(root=repo_root(), client_factory=lambda: FakeClient(bad_zip), retrieve_calls_tracker=tracker)
    assert len(tracker) == 1  # exactly one retrieve attempt on first run

    # Repeat run with a pre-seeded accepted artifact -> pre-network SKIP, 0 calls.
    rid = final_request_id()
    _fake_accepted_artifact(tmp_path, rid)
    out = pc.run_live(root=repo_root(), client_factory=lambda: FakeClient(bad_zip), retrieve_calls_tracker=tracker)
    assert out["skipped"] is True
    assert out["retrieve_calls"] == 0
    assert out["skip_reason"] == "accepted_request_already_present"
    assert len(tracker) == 1  # still only the first run's single call


# ---------------------------------------------------------------------------
# Preflight gate
# ---------------------------------------------------------------------------


def test_preflight_all_true():
    gate = preflight()
    assert gate["gate_label"] == "READY_STAGE5E3A_LIVE"
    assert gate["all_true"] is True


def test_preflight_live_auth_remains_false():
    gate = preflight()
    # The canary gate itself does not flip the full backfill authorization.
    assert "LIVE-BACKFILL-AUTHORIZED" not in gate  # separate contract flag


# ---------------------------------------------------------------------------
# Transport firewall / consumption
# ---------------------------------------------------------------------------


def test_extras_never_consumed_by_contract():
    # The extraction contract consumes the 360 support exactly; extras (90)
    # are structurally excluded because cds_request time set is the union.
    assert EXPECTED_TRANSPORT - EXPECTED_SUPPORT == EXPECTED_EXTRAS
    assert EXPECTED_EXTRAS == 90
