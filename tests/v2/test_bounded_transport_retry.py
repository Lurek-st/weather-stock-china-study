"""Stage 5E-4C-R6 bounded transport and durable failure evidence tests.

All clients, stores, and transport calls are hermetic.  No CDS or other
external network operation is constructed by this module.
"""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests.exceptions
import yaml

import scripts.v2.climatology.derived_store as derived_store
import scripts.v2.climatology.full_backfill_controller as ctl
import scripts.v2.climatology.production_unit as production_unit
from scripts.v2.climatology.production_canary import (
    EXPECTED_TIMEZONE_CANARY_HASH,
    REQUEST_IDENTITY_CONTRACT_VERSION,
    load_global_registry_hash,
)
from scripts.v2.core import V2Error, repo_root
from tests.v2.fixtures.r2_hermetic_helpers import fake_execute_persisting

ROOT = repo_root()
PLAN = ctl.load_plan(ROOT)
OLD_POLICY_HASH = "a05924b0bf999eb37d5fa04d2e81520d5e799d1cf75715c3257663f32dc8c94a"
OLD_CANDIDATE_HASH = "37df1df9127346c962f371d9fcf6c3beb0cd968164b8c1ed3640eadb3cb5573c"


@pytest.fixture
def active_hermetic(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    derived = tmp_path / "derived"
    state = tmp_path / "state"
    payload = {
        "authorization_schema_version": "1.0.0",
        "live_backfill_authorized": True,
        "authorization_state": "test_only_active",
        "control_layer_approval_required": False,
        "bound_hashes": {
            "global_backfill_plan_hash": PLAN["global_backfill_plan_hash"],
            "controller_policy_hash": ctl.controller_policy_hash(ROOT),
            "request_identity_contract_version": REQUEST_IDENTITY_CONTRACT_VERSION,
            "global_spatial_anchor_registry_hash": load_global_registry_hash(ROOT),
            "timezone_canary_hash": EXPECTED_TIMEZONE_CANARY_HASH,
        },
    }
    payload["authorization_candidate_hash"] = ctl.authorization_candidate_hash(ROOT)
    auth_path = tmp_path / "authorization.yaml"
    auth_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    monkeypatch.setattr(ctl, "AUTHORIZATION_PATH", str(auth_path))
    monkeypatch.setattr(ctl, "RAW_BASE", str(raw))
    monkeypatch.setattr(production_unit, "RAW_BASE", str(raw))
    monkeypatch.setattr(derived_store, "DERIVED_BASE", str(derived))
    monkeypatch.setattr(ctl, "JOURNAL_PATH", str(state / "progress.events.jsonl"))
    monkeypatch.setattr(ctl, "SNAPSHOT_PATH", str(state / "progress.snapshot.json"))
    monkeypatch.setattr(ctl, "STATE_DIR", str(state))
    return {"raw": raw, "derived": derived, "state": state, "auth": auth_path}


def _healthy():
    return {"dataset_available": True, "status": "available"}


class ReadTimeoutClient:
    def retrieve(self, dataset, request, target):
        raise requests.exceptions.ReadTimeout(
            "https://example.invalid/result?token=secret Authorization: Bearer secret"
        )


def test_policy_parses_exact_layered_transport_contract():
    policy = ctl.load_controller_policy(ROOT)
    assert policy["controller_schema_version"] == "2.0.0"
    assert policy["controller_retry"] == {
        "automatic_annual_retry": False,
        "controller_visible_operational_failure": "stop_immediately",
    }
    assert policy["logical_retrieve"]["max_cds_retrieve_calls_per_unit_attempt"] == 1
    transport = policy["transport"]
    assert transport["maximum_total_tries_per_robust_http_operation"] == 3
    assert transport["retry_delay_seconds"] == 120
    assert transport["retryable_http_statuses"] == [408, 429, 500, 502, 503, 504]
    assert transport["retryable_exception_families"] == [
        "ConnectionError", "ReadTimeout", "ChunkedEncodingError"
    ]
    assert transport["ssl_error_automatic_retry"] is False
    assert transport["server_retry_after_used"] is False


def test_policy_hash_changed_and_old_authorization_cannot_bind(active_hermetic, monkeypatch):
    assert ctl.controller_policy_hash(ROOT) != OLD_POLICY_HASH
    old = yaml.safe_load(active_hermetic["auth"].read_text(encoding="utf-8"))
    old["bound_hashes"]["controller_policy_hash"] = OLD_POLICY_HASH
    old["authorization_candidate_hash"] = OLD_CANDIDATE_HASH
    active_hermetic["auth"].write_text(yaml.safe_dump(old), encoding="utf-8")
    calls = {"health": 0, "client": 0}

    def health():
        calls["health"] += 1
        return _healthy()

    def client():
        calls["client"] += 1
        return ReadTimeoutClient()

    with pytest.raises(V2Error, match="authorization binding invalid"):
        ctl.run_year_batch(PLAN, 1996, ROOT, health_checker=health, client_factory=client)
    assert calls == {"health": 0, "client": 0}


def test_runtime_dependency_mismatch_fails_before_health_or_client(active_hermetic):
    calls = {"health": 0, "client": 0}

    def health():
        calls["health"] += 1
        return _healthy()

    def client():
        calls["client"] += 1
        return ReadTimeoutClient()

    def mismatched(name):
        return "0.0.0" if name == "multiurl" else importlib.metadata.version(name)

    with pytest.raises(V2Error, match="transport runtime version mismatch"):
        ctl.run_year_batch(
            PLAN, 1996, ROOT, health_checker=health, client_factory=client,
            runtime_version_getter=mismatched,
        )
    assert calls == {"health": 0, "client": 0}


def test_policy_client_factory_passes_bounded_retry_and_tls_enabled():
    captured = []

    def constructor(**kwargs):
        captured.append(kwargs)
        return object()

    factory = ctl.make_policy_driven_cds_client_factory(
        ctl.load_controller_policy(ROOT), client_constructor=constructor
    )
    factory()
    assert captured == [{"retry_max": 3, "sleep_max": 120, "verify": True}]


def test_pinned_multiurl_robust_semantics_are_three_total_tries(monkeypatch):
    import multiurl.retry

    monkeypatch.setattr(multiurl.retry, "_logged_sleep", lambda seconds: None)
    for status in (408, 429, 500, 502, 503, 504):
        calls = []

        def response_call(url):
            calls.append(url)
            return SimpleNamespace(status_code=status, reason="test", headers={})

        wrapped = multiurl.retry.robust(response_call, maximum_tries=3, retry_after=120)
        assert wrapped("https://example.invalid").status_code == status
        assert len(calls) == 3

    for exc_type in (
        requests.exceptions.ConnectionError,
        requests.exceptions.ReadTimeout,
        requests.exceptions.ChunkedEncodingError,
    ):
        calls = []

        def exception_call(url, exc_type=exc_type):
            calls.append(url)
            raise exc_type("hermetic")

        wrapped = multiurl.retry.robust(exception_call, maximum_tries=3, retry_after=120)
        with pytest.raises(exc_type):
            wrapped("https://example.invalid")
        assert len(calls) == 3

    ssl_calls = []

    def ssl_call(url):
        ssl_calls.append(url)
        raise requests.exceptions.SSLError("hermetic")

    with pytest.raises(requests.exceptions.SSLError):
        multiurl.retry.robust(ssl_call, maximum_tries=3, retry_after=120)("https://example.invalid")
    assert len(ssl_calls) == 1


def test_accepted_raw_skips_before_client_factory(active_hermetic):
    first = next(unit for unit in PLAN["units"] if unit["year"] == 1996)
    fake_execute_persisting(
        ctl.engine_unit(first), root=ROOT, raw_base=str(active_hermetic["raw"])
    )
    client_calls = []

    def client():
        client_calls.append(1)
        return ReadTimeoutClient()

    def derived_fails(*args, **kwargs):
        raise RuntimeError("hermetic derived stop")

    out = ctl.run_year_batch(
        PLAN, 1996, ROOT, health_checker=_healthy, client_factory=client,
        derived_extractor=derived_fails,
    )
    assert out["outcome"] == "batch_stopped_derived_failure"
    assert client_calls == []


def test_failure_is_one_logical_call_and_persists_bounded_record(active_hermetic):
    tracker = []
    out = ctl.run_year_batch(
        PLAN, 1996, ROOT, health_checker=_healthy, client_factory=ReadTimeoutClient,
        retrieve_calls_tracker=tracker,
    )
    assert out["outcome"] == "batch_stopped_operational_failure"
    assert len(tracker) == 1
    events = ctl.read_journal(ROOT)
    starts = [event for event in events if event["event_type"] == "unit_retrieve_started"]
    failures = [event for event in events if event["event_type"] == "unit_operational_failure"]
    assert len(starts) == len(failures) == 1
    assert starts[0]["attempt_number"] == failures[0]["attempt_number"] == 1
    record = failures[0]["error_record"]
    assert record["error_category"] == "transport_retry_exhausted"
    assert record["phase"] == "unknown"
    assert record["library_retry_count"] == "unknown"
    assert len(record["sanitized_message"]) <= 1024
    assert "secret" not in record["sanitized_message"]
    invocation_ids = {event["invocation_id"] for event in events}
    assert len(invocation_ids) == 1
    assert all(event["event_time_utc"].endswith("Z") for event in events)


def test_logical_attempt_number_advances_across_explicit_invocations(active_hermetic):
    for _ in range(2):
        ctl.run_year_batch(
            PLAN, 1996, ROOT, health_checker=_healthy, client_factory=ReadTimeoutClient
        )
    starts = [
        event for event in ctl.read_journal(ROOT)
        if event["event_type"] == "unit_retrieve_started"
    ]
    assert [event["attempt_number"] for event in starts] == [1, 2]
    assert len({event["invocation_id"] for event in starts}) == 2


def test_old_journal_events_remain_readable(active_hermetic):
    path = ctl.journal_path(ROOT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": "1.0.0", "event_seq": 1,
                    "event_type": "unit_operational_failure", "attempt_number": 1}) + "\n",
        encoding="utf-8",
    )
    assert ctl.read_journal(ROOT)[0]["attempt_number"] == 1
    assert "invocation_id" not in ctl.read_journal(ROOT)[0]


def test_snapshot_tmp_is_non_authoritative_and_is_safely_overwritten(active_hermetic):
    snapshot = ctl.snapshot_path(ROOT)
    tmp = snapshot.with_suffix(".tmp")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text('{"accepted_raw_units": 999}\n', encoding="utf-8")
    tmp.write_text('{"accepted_raw_units": 888}\n', encoding="utf-8")
    state = ctl.reconcile_progress(PLAN, ROOT)
    assert state["snapshot"]["accepted_raw_units"] == 0
    ctl.write_snapshot(state["snapshot"], ROOT)
    assert json.loads(snapshot.read_text(encoding="utf-8"))["accepted_raw_units"] == 0
    assert not tmp.exists()


def test_terminal_job_failure_has_stable_classification():
    from ecmwf.datastores.processing import ProcessingFailedError

    classified = ctl._classify_failure(ProcessingFailedError("terminal"))
    assert classified["failure_class"] == "service_job_terminal_failure"
    assert classified["error_category"] == "service_job_terminal_failure"


def test_1996_pause_accounting_and_feb29_exposure_are_unchanged():
    dry = ctl.dry_run_year(PLAN, 1996, ROOT)
    assert dry["formal_slots"] == 32
    assert dry["existing_accepted_skip"] == 8
    assert dry["new_missing"] == 24
    for unit in ctl.year_units(PLAN, 1996):
        dates = production_unit.common_daily_dates(ctl.engine_unit(unit))
        assert not any(day.month == 2 and day.day == 29 for day in dates)


def test_representative_frozen_request_ids_unchanged():
    expected = {
        "sse_composite:1991Q1": "fc5a65e2f054cf7017eed2dea69b5585ff1385d3dfb24c63d40728cbb49b7817",
        "bse50:1996Q1": "8a3f9ca4dc5259a12ca06e035549c7efd51eacc2ddf5372d7ff27f04c8a637c8",
        "dax:1996Q4": "a021caacd99b71fdd8b2c4791f8f9503085dda0e672c587628072265318c1913",
    }
    actual = {unit["unit_key"]: unit["final_request_id"] for unit in PLAN["units"]}
    assert {key: actual[key] for key in expected} == expected
