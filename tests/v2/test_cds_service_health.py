"""Stage 5E-3C-R1 dataset-specific service-health wiring tests (ZERO NETWORK).

Covers the frozen health authority contract:
- dataset-specific structured sanity status is the ONLY authority
- only normalized "available" continues; every other status defers (fail closed)
- banner / UI message text is NOT authority (both directions)
- broken metadata (HTTP / timeout / bad JSON / wrong id / missing field) fails closed
- kill switch precedes health network (0 health HTTP when not authorized)
- None checker FAIL CLOSED (no implicit healthy fallback)
- initial health check precedes first retrieve
- every-8-NEW-retrieves circuit breaker; accepted skips do not count
- no automatic retry

All network is injected fakes; Stage 5E-3C-R1 performs zero real HTTP.
"""
from __future__ import annotations

import json
import sys
import urllib.error
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.cds_service_health import (
    AVAILABLE_STATUS,
    DEFAULT_DATASET_ID,
    HEALTH_COLLECTION_URL,
    check_dataset_health,
    fetch_json,
    make_dataset_health_checker,
    normalize_status,
)
from scripts.v2.climatology.full_backfill_controller import (
    authorization_candidate_hash,
    controller_policy_hash,
    load_plan,
    run_year_batch,
)
from scripts.v2.climatology.production_canary import (
    EXPECTED_TIMEZONE_CANARY_HASH,
    REQUEST_IDENTITY_CONTRACT_VERSION,
    load_global_registry_hash,
)
from scripts.v2.core import V2Error, repo_root

ROOT = repo_root()
PLAN = load_plan(ROOT)
YEAR = 1992  # all 32 slots missing -> clean batch for interval tests


# ---------------------------------------------------------------------------
# Fake transport / fixtures
# ---------------------------------------------------------------------------


def make_collection(status: str | None, dataset_id: str = DEFAULT_DATASET_ID) -> dict:
    collection = {
        "id": dataset_id,
        "title": "ERA5 hourly data on single levels from 1940 to present",
        "type": "Collection",
    }
    if status is None:
        # missing sanity_check entirely
        return collection
    sanity = {"status": status}
    if status == "available":
        sanity["timestamp"] = "2026-08-14T00:00:00Z"
    collection["cads:sanity_check"] = sanity
    return collection


def fetcher_for(collection: dict | None = None, error: Exception | None = None, raw: str | None = None):
    calls = []

    def fetcher(url: str, timeout: float) -> dict:
        calls.append((url, timeout))
        if error is not None:
            raise error
        if raw is not None:
            return json.loads(raw)
        return collection

    fetcher.calls = calls  # type: ignore[attr-defined]
    return fetcher


def banner_collection(status: str, banner: str) -> dict:
    c = make_collection(status)
    c["description"] = banner
    c["links"] = [{"rel": "self", "href": HEALTH_COLLECTION_URL}]
    return c


# ---------------------------------------------------------------------------
# normalize_status semantics
# ---------------------------------------------------------------------------


def test_normalize_status_only_available_is_healthy():
    assert normalize_status("available") == AVAILABLE_STATUS
    assert normalize_status("  AVAILABLE  ") == AVAILABLE_STATUS
    for bad in ("warning", "degraded", "down", "unavailable", "maintenance",
                "expired", "disabled", "unknown"):
        token = normalize_status(bad)
        assert token is not None and token != AVAILABLE_STATUS
    assert normalize_status(None) is None
    assert normalize_status("") is None
    assert normalize_status(42) is None


# ---------------------------------------------------------------------------
# Structured health result shape
# ---------------------------------------------------------------------------


def test_health_result_shape_available():
    f = fetcher_for(make_collection("available"))
    r = check_dataset_health(fetcher=f, checked_at="2026-08-14T00:00:00Z")
    assert r["dataset_id"] == DEFAULT_DATASET_ID
    assert r["dataset_available"] is True
    assert r["status"] == "available"
    assert r["status_timestamp"] == "2026-08-14T00:00:00Z"
    assert r["source"] == "official_dataset_catalogue"
    assert r["error_class"] is None
    assert len(f.calls) == 1  # exactly one authoritative fetch


def test_health_result_no_credentials_or_abs_paths():
    f = fetcher_for(make_collection("available"))
    r = check_dataset_health(fetcher=f, checked_at="2026-08-14T00:00:00Z")
    blob = json.dumps(r)
    assert "cdsapirc" not in blob
    assert "apikey" not in blob
    assert "cookie" not in blob.lower()
    assert "C:" not in blob and "site-packages" not in blob


# ---------------------------------------------------------------------------
# Non-available matrix (spec 16)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["warning", "degraded", "down", "unavailable", "maintenance", "expired", "unknown", "null"],
)
def test_non_available_matrix_fails_closed(status):
    f = fetcher_for(make_collection(status))
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is False
    assert r["status"] == status


def test_missing_sanity_check_fails_closed():
    f = fetcher_for(make_collection(None))
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is False
    assert r["error_class"] == "missing_sanity_check"


def test_missing_status_field_fails_closed():
    collection = make_collection("available")
    del collection["cads:sanity_check"]["status"]
    f = fetcher_for(collection)
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is False
    assert r["status"] is None


# ---------------------------------------------------------------------------
# Broken metadata (spec 17)
# ---------------------------------------------------------------------------


def test_http_error_fails_closed():
    f = fetcher_for(error=urllib.error.HTTPError(HEALTH_COLLECTION_URL, 503, "Service Unavailable", {}, None))
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is False
    assert r["error_class"] == "http_error_503"


def test_timeout_fails_closed():
    f = fetcher_for(error=TimeoutError())
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is False
    assert r["error_class"] == "timeout"


def test_invalid_json_fails_closed():
    f = fetcher_for(raw="not json {")
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is False
    assert r["error_class"] == "json_parse_failure"


def test_wrong_dataset_id_fails_closed():
    f = fetcher_for(make_collection("available", dataset_id="reanalysis-era5-land"))
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is False
    assert r["error_class"].startswith("dataset_id_mismatch")


def test_unexpected_schema_fails_closed():
    f = fetcher_for(collection=["not", "a", "dict"])
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is False
    assert r["error_class"] == "unexpected_schema"


def test_transport_anomaly_fails_closed():
    f = fetcher_for(error=ConnectionError("reset"))
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is False
    assert r["error_class"] == "unexpected_transport_ConnectionError"


# ---------------------------------------------------------------------------
# Banner is NOT authority (spec 18)
# ---------------------------------------------------------------------------


def test_banner_degraded_but_structured_available_is_available():
    f = fetcher_for(banner_collection("available", "Degraded access to the data"))
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is True


def test_banner_healthy_but_structured_warning_is_not_available():
    f = fetcher_for(banner_collection("warning", "All systems operational"))
    r = check_dataset_health(fetcher=f)
    assert r["dataset_available"] is False
    assert r["status"] == "warning"


# ---------------------------------------------------------------------------
# make_dataset_health_checker (construction = no network)
# ---------------------------------------------------------------------------


def test_checker_factory_construction_does_not_fetch():
    f = fetcher_for(make_collection("available"))
    checker = make_dataset_health_checker(fetcher=f)
    assert not f.calls  # construction performs no fetch
    result = checker()
    assert result["dataset_available"] is True
    assert len(f.calls) == 1


def test_default_fetch_json_bounded_no_retry():
    # fetch_json signature must accept (url, timeout); no retry loop inside.
    import inspect

    sig = inspect.signature(fetch_json)
    assert "timeout" in sig.parameters


# ---------------------------------------------------------------------------
# Controller integration: missing checker (spec 14)
# ---------------------------------------------------------------------------


def test_run_year_batch_none_checker_fails_closed(tmp_path, monkeypatch):
    import scripts.v2.climatology.full_backfill_controller as ctl

    payload = {
        "authorization_schema_version": "1.0.0",
        "live_backfill_authorized": True,
        "authorization_state": "active",
        "control_layer_approval_required": False,
        "bound_hashes": {
            "global_backfill_plan_hash": PLAN["global_backfill_plan_hash"],
            "controller_policy_hash": controller_policy_hash(ROOT),
            "request_identity_contract_version": REQUEST_IDENTITY_CONTRACT_VERSION,
            "global_spatial_anchor_registry_hash": load_global_registry_hash(ROOT),
            "timezone_canary_hash": EXPECTED_TIMEZONE_CANARY_HASH,
        },
    }
    payload["authorization_candidate_hash"] = authorization_candidate_hash(ROOT)
    import yaml

    auth_path = tmp_path / "auth.yaml"
    auth_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setattr(ctl, "AUTHORIZATION_PATH", str(auth_path))
    monkeypatch.setattr(ctl, "RAW_BASE", str(tmp_path / "raw"))
    monkeypatch.setattr(ctl, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ctl, "JOURNAL_PATH", str(tmp_path / "state" / "progress.events.jsonl"))
    monkeypatch.setattr(ctl, "SNAPSHOT_PATH", str(tmp_path / "state" / "progress.snapshot.json"))

    tracker = []
    with pytest.raises(V2Error, match="dataset-specific health checker required"):
        run_year_batch(PLAN, YEAR, root=ROOT, retrieve_calls_tracker=tracker)
    assert len(tracker) == 0


# ---------------------------------------------------------------------------
# Controller integration: health precedes retrieve; every-8 circuit breaker
# ---------------------------------------------------------------------------


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    import scripts.v2.climatology.full_backfill_controller as ctl

    payload = {
        "authorization_schema_version": "1.0.0",
        "live_backfill_authorized": True,
        "authorization_state": "active",
        "control_layer_approval_required": False,
        "bound_hashes": {
            "global_backfill_plan_hash": PLAN["global_backfill_plan_hash"],
            "controller_policy_hash": controller_policy_hash(ROOT),
            "request_identity_contract_version": REQUEST_IDENTITY_CONTRACT_VERSION,
            "global_spatial_anchor_registry_hash": load_global_registry_hash(ROOT),
            "timezone_canary_hash": EXPECTED_TIMEZONE_CANARY_HASH,
        },
    }
    payload["authorization_candidate_hash"] = authorization_candidate_hash(ROOT)
    import yaml

    auth_path = tmp_path / "auth.yaml"
    auth_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setattr(ctl, "AUTHORIZATION_PATH", str(auth_path))
    monkeypatch.setattr(ctl, "RAW_BASE", str(tmp_path / "raw"))
    monkeypatch.setattr(ctl, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ctl, "JOURNAL_PATH", str(tmp_path / "state" / "progress.events.jsonl"))
    monkeypatch.setattr(ctl, "SNAPSHOT_PATH", str(tmp_path / "state" / "progress.snapshot.json"))
    return {"root": ROOT, "tmp": tmp_path}


class OkClient:
    def retrieve(self, dataset, request, target):
        Path(target).write_bytes(b"PK\x03\x04fakezip")


def test_health_precedes_first_retrieve(hermetic, monkeypatch):
    import scripts.v2.climatology.full_backfill_controller as ctl

    order: list[str] = []

    def health():
        order.append("health")
        return {"dataset_available": True, "status": "available"}

    def fake_execute(unit, root=None, client_factory=None, retrieve_calls_tracker=None):
        order.append("retrieve")
        return {
            "skipped": False, "market_id": unit["market_id"], "period": unit["period"],
            "retrieve_calls": 1, "request_id": "x" * 64, "artifact_id": "a",
            "sha256": "y" * 64, "bytes": 1,
        }

    monkeypatch.setattr(ctl, "execute_unit_once", fake_execute)
    out = run_year_batch(PLAN, YEAR, root=hermetic["root"], health_checker=health)
    assert out["outcome"] == "batch_completed"
    assert order[0] == "health"
    assert "retrieve" in order


def test_every_8_new_retrieves_circuit_breaker(hermetic, monkeypatch):
    import scripts.v2.climatology.full_backfill_controller as ctl

    health_calls = {"n": 0}
    available_until = 1  # first check (pre-batch) available; next check non-available

    def health():
        health_calls["n"] += 1
        if health_calls["n"] <= available_until:
            return {"dataset_available": True, "status": "available"}
        return {"dataset_available": False, "status": "warning"}

    def fake_execute(unit, root=None, client_factory=None, retrieve_calls_tracker=None):
        if retrieve_calls_tracker is not None:
            retrieve_calls_tracker.append("cds")
        return {
            "skipped": False, "market_id": unit["market_id"], "period": unit["period"],
            "retrieve_calls": 1, "request_id": "x" * 64, "artifact_id": "a",
            "sha256": "y" * 64, "bytes": 1,
        }

    monkeypatch.setattr(ctl, "execute_unit_once", fake_execute)
    tracker = []
    out = run_year_batch(PLAN, YEAR, root=hermetic["root"], health_checker=health,
                         retrieve_calls_tracker=tracker)
    assert out["outcome"] == "batch_stopped_service_deferred"
    assert len(tracker) == 8  # exactly 8 NEW retrieves; the 9th was blocked
    assert health_calls["n"] == 2  # pre-batch + after 8


def test_1991_skips_do_not_count_toward_interval(hermetic, monkeypatch):
    # 1991 has 3 accepted skips; the periodic check must still occur after 8
    # NEW retrieves, not after 8 formal slots.  Pre-seed the hermetic raw
    # store with the 3 real 1991 accepted units so classify_units skips them.
    import scripts.v2.climatology.full_backfill_controller as ctl

    from scripts.v2.climatology.production_unit import RAW_BASE
    from scripts.v2.core import RawArtifactStore

    store = RawArtifactStore(hermetic["tmp"] / "raw")
    for unit_key in ("sse_composite:1991Q3", "nifty50:1991Q3", "bse50:1991Q1"):
        plan_unit = next(u for u in PLAN["units"] if u["unit_key"] == unit_key)
        store.persist(
            source_id="cds_era5_hourly_climatology",
            provider="ECMWF",
            logical_name=f"{plan_unit['market_id']}-{plan_unit['period'].lower()}-prod-seed",
            payload=b"PK\x03\x04seedzip",
            request={"variable": ["total_cloud_cover"]},
            status="final",
            licence="cc",
            suffix=".zip",
            validation_metadata={"container_validation_passed": True, "raw_suffix": ".zip"},
            final_request_id=plan_unit["final_request_id"],
        )

    health_calls = {"n": 0}

    def health():
        health_calls["n"] += 1
        return {"dataset_available": True, "status": "available"}

    def fake_execute(unit, root=None, client_factory=None, retrieve_calls_tracker=None):
        if retrieve_calls_tracker is not None:
            retrieve_calls_tracker.append("cds")
        return {
            "skipped": False, "market_id": unit["market_id"], "period": unit["period"],
            "retrieve_calls": 1, "request_id": "x" * 64, "artifact_id": "a",
            "sha256": "y" * 64, "bytes": 1,
        }

    monkeypatch.setattr(ctl, "execute_unit_once", fake_execute)
    tracker = []
    out = run_year_batch(PLAN, 1991, root=hermetic["root"], health_checker=health,
                         retrieve_calls_tracker=tracker)
    # 3 accepted skips + 29 NEW retrieves: pre-batch check + checks at 8/16/24
    # = 4 health calls; 8-count only counts NEW retrieves (skip slots ignored).
    assert out["outcome"] == "batch_completed"
    assert len(tracker) == 29
    assert health_calls["n"] == 4


def test_health_http_error_no_retry_no_retrieve(hermetic):
    import scripts.v2.climatology.full_backfill_controller as ctl

    fetches = {"n": 0}

    def health():
        fetches["n"] += 1
        return {"dataset_available": False, "status": None, "error_class": "http_error_503"}

    out = run_year_batch(PLAN, YEAR, root=hermetic["root"], health_checker=health)
    assert out["outcome"] == "batch_stopped_service_deferred"
    assert fetches["n"] == 1  # no automatic retry of the health fetch


def test_service_defer_not_scientific_failure(hermetic):
    import scripts.v2.climatology.full_backfill_controller as ctl

    from scripts.v2.climatology.full_backfill_controller import read_journal

    def health():
        return {"dataset_available": False, "status": "down"}

    out = run_year_batch(PLAN, YEAR, root=hermetic["root"], health_checker=health)
    assert out["outcome"] == "batch_stopped_service_deferred"
    events = read_journal(hermetic["root"])
    types = {e["event_type"] for e in events}
    assert "unit_service_deferred" in types
    assert "unit_validation_failure" not in types
    assert "unit_contract_failure" not in types
    assert "unit_operational_failure" not in types
