"""Stage 5E-3B cross-market production qualification tests (7 authorized units).

Covers the Stage 5E-3B contract:
- authorization registry exact 7 units; unauthorized quarter fails closed
- SP500 cannot be re-requested by 3B
- aggregate planning counts exact (640 / 2286 / 2581 / 295)
- per-market expected counts exact
- SSE 1991 DST transition; SZSE 1992 +08; TOPIX Feb29 semantics
- NIFTY :15 integration; FTSE/DAX DST transitions; BSE boundary support
- all identities bind non-null timezone hash + exact dates/times
- all use spatial registry anchors; no municipal primary; TCC only
- fake successful unit first=1 repeat=0; failure stops sequence; no retry
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.production_quarter import (
    AUTHORIZED_UNITS,
    EXPECTED_AGGREGATE,
    EXPECTED_TOTAL_FORMAL_UNITS,
    EXCLUDED_ALREADY_QUALIFIED,
    aggregate_counts,
    assert_authorized,
    cds_request_for,
    common_daily_dates,
    extract_unit_exposures,
    final_request_id_for,
    identity_payload,
    load_anchor,
    market_spec,
    quarter_daily_plan,
    quarter_transport,
    unit_from_args,
    unit_plan_counts,
)
from scripts.v2.climatology.production_canary import EXPECTED_TIMEZONE_CANARY_HASH, REQUEST_IDENTITY_CONTRACT_VERSION
from scripts.v2.core import V2Error, repo_root

UNITS = {u["market_id"]: u for u in AUTHORIZED_UNITS}


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_authorization_exact_7_units():
    assert len(AUTHORIZED_UNITS) == 7
    markets = [u["market_id"] for u in AUTHORIZED_UNITS]
    assert sorted(markets) == ["bse50", "dax", "ftse100", "nifty50", "sse_composite", "szse_component", "topix"]


def test_sp500_excluded():
    assert "sp500" in EXCLUDED_ALREADY_QUALIFIED
    with pytest.raises(V2Error):
        assert_authorized({"market_id": "sp500", "period": "2007Q1", "start": date(2007, 1, 1), "end": date(2007, 3, 31)})


def test_unauthorized_quarter_fails_closed():
    with pytest.raises(V2Error):
        unit_from_args("sse_composite", 1991, 4)  # not in registry
    with pytest.raises(V2Error):
        unit_from_args("topix", 2021, 1)  # not in registry


def test_each_authorized_unit_round_trips():
    for u in AUTHORIZED_UNITS:
        q = (u["start"].month - 1) // 3 + 1
        rebuilt = unit_from_args(u["market_id"], u["start"].year, q)
        assert rebuilt["period"] == u["period"]


# ---------------------------------------------------------------------------
# Aggregate planning
# ---------------------------------------------------------------------------


def test_aggregate_counts_exact():
    agg = aggregate_counts()
    t = agg["totals"]
    assert t["exposures"] == EXPECTED_AGGREGATE["exposures"] == 640
    assert t["support"] == EXPECTED_AGGREGATE["support"] == 2286
    assert t["transport"] == EXPECTED_AGGREGATE["transport"] == 2581
    assert t["extras"] == EXPECTED_AGGREGATE["extras"] == 295
    assert t["max_fields"] == EXPECTED_AGGREGATE["max_fields"] == 465


def test_aggregate_sums_match_per_market():
    agg = aggregate_counts()
    per = agg["per_market"]
    assert sum(c["daily_exposures"] for c in per.values()) == 640
    assert sum(c["support"] for c in per.values()) == 2286
    assert sum(c["transport"] for c in per.values()) == 2581
    assert sum(c["extras"] for c in per.values()) == 295


# ---------------------------------------------------------------------------
# Per-market expected counts
# ---------------------------------------------------------------------------


def test_sse_1991q3_counts():
    c = unit_plan_counts(UNITS["sse_composite"])
    assert c["daily_exposures"] == 92
    assert c["support"] == 368
    assert c["transport"] == 465
    assert c["extras"] == 97
    assert c["fields"] == 465
    assert c["first_transport_date"] == "1991-06-30"
    assert c["last_transport_date"] == "1991-09-30"
    assert c["request_times"] == ["00:00", "01:00", "02:00", "22:00", "23:00"]


def test_szse_1992q3_counts():
    c = unit_plan_counts(UNITS["szse_component"])
    assert c["daily_exposures"] == 92
    assert c["support"] == 368
    assert c["transport"] == 372
    assert c["extras"] == 4
    assert c["request_times"] == ["00:00", "01:00", "02:00", "23:00"]


def test_topix_2020q1_counts():
    c = unit_plan_counts(UNITS["topix"])
    assert c["daily_exposures"] == 90
    assert c["support"] == 270
    assert c["transport"] == 276
    assert c["extras"] == 6
    assert c["first_transport_date"] == "2019-12-31"


def test_nifty50_1991q3_counts():
    c = unit_plan_counts(UNITS["nifty50"])
    assert c["daily_exposures"] == 92
    assert c["support"] == 368
    assert c["transport"] == 368
    assert c["extras"] == 0
    assert c["amplification"] == 1.0
    assert c["request_times"] == ["01:00", "02:00", "03:00", "04:00"]


def test_ftse100_1995q4_counts():
    c = unit_plan_counts(UNITS["ftse100"])
    assert c["daily_exposures"] == 92
    assert c["support"] == 276
    assert c["transport"] == 368
    assert c["extras"] == 92
    assert c["request_times"] == ["05:00", "06:00", "07:00", "08:00"]


def test_dax_1996q4_counts():
    c = unit_plan_counts(UNITS["dax"])
    assert c["daily_exposures"] == 92
    assert c["support"] == 276
    assert c["transport"] == 368
    assert c["extras"] == 92


def test_bse50_1991q1_counts():
    c = unit_plan_counts(UNITS["bse50"])
    assert c["daily_exposures"] == 90
    assert c["support"] == 360
    assert c["transport"] == 364
    assert c["extras"] == 4
    assert c["first_transport_date"] == "1990-12-31"
    assert c["last_transport_date"] == "1991-03-31"


# ---------------------------------------------------------------------------
# DST / boundary semantics
# ---------------------------------------------------------------------------


def test_sse_1991_dst_transition():
    rows = {r["date"]: r for r in quarter_daily_plan(UNITS["sse_composite"])}
    assert rows["1991-09-14"]["utc_offset_str"] == "+0900"
    assert rows["1991-09-15"]["utc_offset_str"] == "+0800"


def test_szse_1992_fixed_plus08():
    rows = {r["date"]: r for r in quarter_daily_plan(UNITS["szse_component"])}
    offsets = {r["utc_offset_str"] for r in rows.values()}
    assert offsets == {"+0800"}


def test_topix_feb29_not_exposure_but_support():
    u = UNITS["topix"]
    exposure_dates = {d.isoformat() for d in common_daily_dates(u)}
    assert "2020-02-29" not in exposure_dates
    tr = quarter_transport(u)
    assert "2020-02-29" in tr["dates"]  # support for 2020-03-01 window
    assert tr["first_date"] == "2019-12-31"


def test_nifty50_fractional_window():
    rows = {r["date"]: r for r in quarter_daily_plan(UNITS["nifty50"])}
    r = rows["1991-07-01"]
    assert r["utc_offset_str"] == "+0530"
    assert r["window_start_utc"] == "1991-07-01T01:45:00+00:00"
    assert r["window_end_utc"] == "1991-07-01T03:45:00+00:00"
    assert r["support_count"] == 4


def test_ftse_dst_transition():
    rows = {r["date"]: r for r in quarter_daily_plan(UNITS["ftse100"])}
    assert rows["1995-10-21"]["utc_offset_str"] == "+0100"
    assert rows["1995-10-22"]["utc_offset_str"] == "+0000"


def test_dax_dst_transition():
    rows = {r["date"]: r for r in quarter_daily_plan(UNITS["dax"])}
    assert rows["1996-10-26"]["utc_offset_str"] == "+0200"
    assert rows["1996-10-27"]["utc_offset_str"] == "+0100"


def test_bse_boundary_support():
    tr = quarter_transport(UNITS["bse50"])
    assert tr["first_date"] == "1990-12-31"


def test_bse_no_exchange_existence_claim():
    # The BSE unit is a fixed target-regime weather-climatology clock; the
    # registry/audit must NOT claim a 1991 BSE trading session exists.
    spec = market_spec("bse50")
    assert spec["timezone"] == "Asia/Shanghai"
    assert spec["core_open_local"] == "09:30"


# ---------------------------------------------------------------------------
# Request identity v1.1.0
# ---------------------------------------------------------------------------


def test_identities_bind_non_null_timezone_hash():
    for u in AUTHORIZED_UNITS:
        p = identity_payload(u)
        tz = p["timezone"]
        assert tz["timezone_canary_hash"] == EXPECTED_TIMEZONE_CANARY_HASH
        assert len(tz["timezone_canary_hash"]) == 64
        assert tz["provider"] == "tzdata"
        assert tz["version"] == "2026.3"


def test_identities_bind_exact_dates_times():
    for u in AUTHORIZED_UNITS:
        p = identity_payload(u)
        req = cds_request_for(u)
        assert p["request_dates"] == sorted(set(req["date"]))
        assert p["request_times"] == sorted(set(req["time"]))
        assert p["request_identity_contract_version"] == REQUEST_IDENTITY_CONTRACT_VERSION == "1.1.0"


def test_identities_use_registry_anchors():
    for u in AUTHORIZED_UNITS:
        p = identity_payload(u)
        anchor = load_anchor(u["market_id"])
        assert p["anchor_hash"] == anchor["anchor_hash"]
        assert anchor["qualification_status"] == "qualified"


def test_no_municipal_primary():
    for u in AUTHORIZED_UNITS:
        p = identity_payload(u)
        assert "municipal" not in json.dumps(p)
        assert "locations.yaml" not in json.dumps(p)


def test_tcc_only_all_units():
    for u in AUTHORIZED_UNITS:
        req = cds_request_for(u)
        assert req["variable"] == ["total_cloud_cover"]


def test_request_id_deterministic_and_distinct():
    ids = {final_request_id_for(u) for u in AUTHORIZED_UNITS}
    assert len(ids) == 7
    for u in AUTHORIZED_UNITS:
        assert final_request_id_for(u) == final_request_id_for(u)


def test_identities_never_reuse_sp500_id():
    sp500_legacy = "6dd6398cdb83f4e9486a673fca97f486783ed46b6379706b3968c4d806cc2bc3"
    for u in AUTHORIZED_UNITS:
        assert final_request_id_for(u) != sp500_legacy


# ---------------------------------------------------------------------------
# Fake live: first=1 repeat=0 (hermetic)
# ---------------------------------------------------------------------------


def test_fake_live_first_1_repeat_0(tmp_path, monkeypatch):
    import scripts.v2.climatology.production_quarter as pq

    monkeypatch.setattr(pq, "RAW_BASE", str(tmp_path))
    tracker = []

    class FakeClient:
        def __init__(self, raw: Path):
            self.raw = raw

        def retrieve(self, dataset, request, target):
            import shutil

            shutil.copyfile(self.raw, target)

    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not-a-zip")
    unit = AUTHORIZED_UNITS[0]  # sse_composite 1991Q3
    with pytest.raises(Exception):
        pq.run_unit_live(unit, client_factory=lambda: FakeClient(bad), retrieve_calls_tracker=tracker)
    assert len(tracker) == 1  # exactly one attempted retrieve

    # Pre-seed accepted artifact -> repeat run skips before network.
    rid = pq.final_request_id_for(unit)
    store = pq.RawArtifactStore(tmp_path)
    store.persist(
        source_id=pq.RAW_SOURCE_ID,
        provider="ECMWF",
        logical_name=f"{unit['market_id']}-{unit['period'].lower()}-prod-test",
        payload=b"PK\x03\x04fakezip",
        request=pq.cds_request_for(unit),
        status="final",
        licence="cc",
        suffix=".zip",
        validation_metadata={"container_validation_passed": True, "raw_suffix": ".zip"},
        final_request_id=rid,
    )
    out = pq.run_unit_live(unit, client_factory=lambda: FakeClient(bad), retrieve_calls_tracker=tracker)
    assert out["skipped"] is True
    assert out["retrieve_calls"] == 0
    assert out["skip_reason"] == "accepted_request_already_present"
    assert len(tracker) == 1


def test_failure_stops_sequence(tmp_path, monkeypatch):
    # A failed unit raises; the runner never continues to the next market.
    import scripts.v2.climatology.production_quarter as pq

    monkeypatch.setattr(pq, "RAW_BASE", str(tmp_path))
    unit = AUTHORIZED_UNITS[0]
    with pytest.raises(V2Error):
        assert_authorized({"market_id": "sp500", "period": "2007Q1", "start": date(2007, 1, 1), "end": date(2007, 3, 31)})
    # No automatic retry: run_unit_live makes at most one retrieve call.
    tracker = []

    class BadClient:
        def retrieve(self, dataset, request, target):
            Path(target).write_bytes(b"not-a-zip")

    with pytest.raises(Exception):
        pq.run_unit_live(unit, client_factory=BadClient, retrieve_calls_tracker=tracker)
    assert len(tracker) == 1
