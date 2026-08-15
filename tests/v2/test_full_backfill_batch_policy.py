"""Stage 5E-3C annual batch execution policy tests (hermetic, ZERO network).

Covers spec 13 (sub-batch circuit breaker), spec 14 (failure classification),
spec 39 (failure policy) and spec 40 (annual batch bound) with a fully
injected client / health checker -- the controller never touches the network.

Because run_year_batch requires a live-authorized binding, these tests build a
HERMETIC authorization file in a tmp root pointing at the REAL plan / policy
hashes, with live_backfill_authorized=true, and a hermetic RawArtifactStore
(tmp RAW_BASE).  The kill-switch tests (auth=false -> 0 network) live in
test_full_backfill_controller.py and never flip the tracked file.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.full_backfill_controller import (
    authorization_candidate_hash,
    controller_policy_hash,
    load_plan,
    reconcile_progress,
    run_year_batch,
)
from scripts.v2.climatology.production_canary import (
    EXPECTED_TIMEZONE_CANARY_HASH,
    REQUEST_IDENTITY_CONTRACT_VERSION,
    load_global_registry_hash,
)
from scripts.v2.core import repo_root

ROOT = repo_root()
PLAN = load_plan(ROOT)
YEAR = 1992  # all 32 slots missing in this year -> clean batch

from tests.v2.fixtures.r2_hermetic_helpers import (
    fake_derived_extractor,
    fake_execute_persisting,
)


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Hermetic controller root: isolated raw, derived, journal and snapshot."""
    import scripts.v2.climatology.full_backfill_controller as ctl
    import scripts.v2.climatology.derived_store as ds

    from tests.v2.fixtures.r2_hermetic_helpers import assert_isolation

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
    monkeypatch.setattr(ds, "DERIVED_BASE", str(tmp_path / "derived"))
    assert_isolation(ctl.RAW_BASE, ds.DERIVED_BASE, ROOT)
    return {"root": ROOT, "tmp": tmp_path, "derived": tmp_path / "derived"}


class OkClient:
    """Writes a fake (invalid) zip -> container validation fails."""

    def __init__(self, payload: bytes = b"PK\x03\x04fakezip"):
        self.payload = payload

    def retrieve(self, dataset, request, target):
        Path(target).write_bytes(self.payload)


class FailClient:
    """Raises at the client boundary (timeout/connection style)."""

    def retrieve(self, dataset, request, target):
        raise ConnectionError("simulated network timeout")


def test_service_deferred_before_any_retrieve(hermetic):
    """Non-available target dataset -> STOP BEFORE first retrieve (0 calls)."""
    tracker = []

    def health():
        return {"dataset_available": False, "status": "down"}

    out = run_year_batch(
        PLAN, YEAR, root=hermetic["root"],
        health_checker=health, client_factory=OkClient, retrieve_calls_tracker=tracker,
    )
    assert out["outcome"] == "batch_stopped_service_deferred"
    assert len(tracker) == 0


def test_health_checked_after_every_8_new_retrieves(hermetic, monkeypatch):
    """Circuit breaker: health re-check at 8 NEW retrieve attempts.

    execute_unit_once is stubbed to succeed (we test controller orchestration,
    not the engine; engine behavior is covered in test_full_backfill_controller
    and the canary/quarter suites).  Fake zip would fail validation on the
    first unit, which is a different scenario.
    """
    import scripts.v2.climatology.full_backfill_controller as ctl

    calls = {"health": 0, "retrieve": 0}

    def fake_execute(unit, root=None, client_factory=None, retrieve_calls_tracker=None):
        calls["retrieve"] += 1
        return fake_execute_persisting(unit, root=root, retrieve_calls_tracker=retrieve_calls_tracker,
                                       raw_base=ctl.RAW_BASE)

    monkeypatch.setattr(ctl, "execute_unit_once", fake_execute)

    def health():
        calls["health"] += 1
        return {"dataset_available": True, "status": "available"}

    out = run_year_batch(
        PLAN, YEAR, root=hermetic["root"],
        health_checker=health, client_factory=OkClient,
        derived_extractor=fake_derived_extractor,
    )
    # 32 slots; initial + checks at 8/16/24 = 4 health calls.
    assert out["outcome"] == "batch_completed"
    assert out["new_retrieves"] == 32
    assert calls["health"] == 4
    assert calls["retrieve"] == 32


def test_operational_failure_stops_after_exactly_one_retrieve(hermetic):
    """Operational failure -> exactly ONE retrieve attempt, batch stops."""
    tracker = []

    def health():
        return {"dataset_available": True, "status": "available"}

    out = run_year_batch(
        PLAN, YEAR, root=hermetic["root"],
        health_checker=health, client_factory=FailClient, retrieve_calls_tracker=tracker,
    )
    assert out["outcome"] == "batch_stopped_operational_failure"
    assert len(tracker) == 1  # exactly one attempt, never a second


def test_validation_failure_blocked_no_retry(hermetic):
    """Payload validation failure -> blocked (validation_failure), no retry."""
    tracker = []

    def health():
        return {"dataset_available": True, "status": "available"}

    out = run_year_batch(
        PLAN, YEAR, root=hermetic["root"],
        health_checker=health, client_factory=OkClient, retrieve_calls_tracker=tracker,
    )
    assert out["outcome"] == "batch_stopped_validation_failure"
    assert len(tracker) == 1
    # The unit is blocked in the snapshot.
    state = reconcile_progress(PLAN, hermetic["root"])
    assert state["snapshot"]["blocked_units"]


def test_year_boundary_forced_stop(hermetic, monkeypatch):
    """A batch never proceeds into another year."""
    import scripts.v2.climatology.full_backfill_controller as ctl

    from scripts.v2.climatology.full_backfill_controller import read_journal

    def fake_execute(unit, root=None, client_factory=None, retrieve_calls_tracker=None):
        return fake_execute_persisting(unit, root=root, retrieve_calls_tracker=retrieve_calls_tracker,
                                       raw_base=ctl.RAW_BASE)

    monkeypatch.setattr(ctl, "execute_unit_once", fake_execute)
    tracker = []

    def health():
        return {"dataset_available": True, "status": "available"}

    out = run_year_batch(
        PLAN, YEAR, root=hermetic["root"],
        health_checker=health, client_factory=OkClient, retrieve_calls_tracker=tracker,
        derived_extractor=fake_derived_extractor,
    )
    events = read_journal(hermetic["root"])
    years = {e.get("batch_year") for e in events if e.get("batch_year")}
    assert years == {YEAR}
    assert len(tracker) <= 32
