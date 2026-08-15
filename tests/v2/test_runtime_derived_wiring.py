"""Stage 5E-3C-R2 runtime derived-processing wiring tests (ZERO network).

Covers the frozen raw->derived contract:
- derived artifact store: schema, payload hash, atomic persist, idempotent skip
- acceptance predicate: request_id-bound, raw_sha-bound, exact rows, 0 missing
- existing raw + missing derived -> 0 retrieve + derive once
- new raw -> retrieve once + derive
- derived failure preserves raw; resume never re-downloads
- corrupt derived artifact FAIL CLOSED (no silent overwrite)
- derived completeness never relies on journal alone
- 1991 hermetic annual simulation: 32 raw / 32 derived / 2920 rows / 29 NEW / 4 health
- health interval counter unaffected by derived processing

All network is fakes; the REAL repository raw store is never touched (tests
use hermetic tmp stores).
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.derived_store import (
    DERIVED_SCHEMA_VERSION,
    build_derived_artifact,
    classify_derived_units,
    derived_path_for,
    derived_payload_sha256,
    load_derived_artifact,
    persist_derived_atomic,
    validate_derived_artifact,
)
from scripts.v2.climatology.full_backfill_controller import (
    authorization_candidate_hash,
    controller_policy_hash,
    load_plan,
    read_journal,
    reconcile_progress,
    run_year_batch,
)
from scripts.v2.climatology.production_canary import (
    EXPECTED_TIMEZONE_CANARY_HASH,
    REQUEST_IDENTITY_CONTRACT_VERSION,
    load_global_registry_hash,
)
from scripts.v2.core import V2Error, repo_root
from tests.v2.fixtures.r2_hermetic_helpers import (
    FAKE_RAW_SHA,
    fake_derived_extractor,
    fake_execute_persisting,
)

ROOT = repo_root()
PLAN = load_plan(ROOT)
YEAR = 1992  # all 32 slots missing -> clean batch


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Hermetic controller root: live-authorized binding + empty stores.

    ALL test artifacts (raw / derived / journal / fixture) land explicitly
    under pytest ``tmp_path`` (never the production default).  Isolation is
    enforced with fail-fast assertions so a regression that resolves onto a
    production store breaks the test immediately instead of polluting data.
    """
    import scripts.v2.climatology.full_backfill_controller as ctl
    import scripts.v2.climatology.derived_store as ds

    from tests.v2.fixtures.r2_hermetic_helpers import assert_isolation

    tmp_path = tmp_path / "hermetic"
    tmp_path.mkdir(parents=True, exist_ok=True)

    payload = {
        "authorization_schema_version": "1.0.0",
        "live_backfill_authorized": True,
        "authorization_state": "approved",
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
    monkeypatch.setattr(ctl, "execute_unit_once", fake_execute_persisting)

    # Hard ZERO-NETWORK boundary for every R2 lifecycle test.  The fake health
    # and retrieve boundaries should make these unreachable; if future wiring
    # escapes either boundary, fail before DNS or socket I/O.
    network_attempts = []

    def block_network(*args, **kwargs):
        network_attempts.append({"args": repr(args), "kwargs": repr(kwargs)})
        raise AssertionError("R2 hermetic test attempted real network access")

    monkeypatch.setattr(socket, "getaddrinfo", block_network)
    monkeypatch.setattr(socket, "create_connection", block_network)
    monkeypatch.setattr(socket.socket, "connect", block_network)

    # Isolation contract (Stage 5E-3C-R2): fail fast if a base resolves onto a
    # production store.
    assert_isolation(ctl.RAW_BASE, ds.DERIVED_BASE, ROOT)
    assert Path(ctl.RAW_BASE).resolve() != (ROOT / "data/source_raw/v2/climatology").resolve()
    assert Path(ds.DERIVED_BASE).resolve() != (ROOT / "data/canonical/v2/climatology/full-backfill").resolve()
    return {"root": ROOT, "tmp": tmp_path, "network_attempts": network_attempts}


def _seed_raw(plan, store_base: Path, unit_key: str):
    """Persist a fake accepted raw for one unit into the hermetic store."""
    from scripts.v2.core import RawArtifactStore
    from tests.v2.fixtures.r2_hermetic_helpers import FAKE_RAW_PAYLOAD

    unit = next(u for u in plan["units"] if u["unit_key"] == unit_key)
    store = RawArtifactStore(store_base)
    store.persist(
        source_id="cds_era5_hourly_climatology",
        provider="ECMWF",
        logical_name=f"{unit['market_id']}-{unit['period'].lower()}-seed",
        payload=FAKE_RAW_PAYLOAD,
        request={"variable": ["total_cloud_cover"]},
        status="final",
        licence="cc",
        suffix=".zip",
        validation_metadata={"container_validation_passed": True, "raw_suffix": ".zip"},
        final_request_id=unit["final_request_id"],
    )
    return unit


# ---------------------------------------------------------------------------
# Derived store unit tests (spec 4-8)
# ---------------------------------------------------------------------------


def test_isolation_rejects_production_stores_and_their_parents(tmp_path):
    from tests.v2.fixtures.r2_hermetic_helpers import assert_isolation

    safe_raw = tmp_path / "safe-raw"
    safe_derived = tmp_path / "safe-derived"
    with pytest.raises(AssertionError, match="production store"):
        assert_isolation(str(ROOT / "data/source_raw/v2/climatology"), str(safe_derived), ROOT)
    with pytest.raises(AssertionError, match="production store"):
        assert_isolation(str(ROOT / "data/source_raw/v2"), str(safe_derived), ROOT)
    with pytest.raises(AssertionError, match="production store"):
        assert_isolation(str(safe_raw), str(ROOT / "data/canonical/v2/climatology/full-backfill"), ROOT)
    with pytest.raises(AssertionError, match="production store"):
        assert_isolation(str(safe_raw), str(ROOT / "data/canonical/v2/climatology"), ROOT)


def test_derived_artifact_schema_and_payload_hash():
    plan_unit = next(u for u in PLAN["units"] if u["unit_key"] == "sp500:2007Q1")
    engine = {
        "market_id": plan_unit["market_id"],
        "period": plan_unit["period"],
        "start": __import__("datetime").date.fromisoformat(plan_unit["local_start"]),
        "end": __import__("datetime").date.fromisoformat(plan_unit["local_end"]),
    }
    extract = fake_derived_extractor(engine, ROOT)
    artifact = build_derived_artifact(plan_unit, extract)
    assert artifact["schema_version"] == DERIVED_SCHEMA_VERSION
    assert artifact["unit_key"] == plan_unit["unit_key"]
    assert artifact["final_request_id"] == plan_unit["final_request_id"]
    assert artifact["raw_sha256"] == extract["raw_sha256"]
    assert artifact["row_count"] == plan_unit["daily_exposure_count"]
    assert artifact["missing_count"] == 0
    # payload hash excludes itself
    assert artifact["derived_payload_sha256"] == derived_payload_sha256(artifact)
    # no abs paths / credentials
    blob = json.dumps(artifact)
    assert "C:" not in blob and "cdsapirc" not in blob


def test_derived_acceptance_predicate_valid():
    plan_unit = next(u for u in PLAN["units"] if u["unit_key"] == "sp500:2007Q1")
    engine = {
        "market_id": plan_unit["market_id"],
        "period": plan_unit["period"],
        "start": __import__("datetime").date.fromisoformat(plan_unit["local_start"]),
        "end": __import__("datetime").date.fromisoformat(plan_unit["local_end"]),
    }
    artifact = build_derived_artifact(plan_unit, fake_derived_extractor(engine, ROOT))
    problems = validate_derived_artifact(plan_unit, artifact, FAKE_RAW_SHA, ROOT)
    assert problems == []


def test_derived_acceptance_predicate_fail_closed(tmp_path):
    plan_unit = next(u for u in PLAN["units"] if u["unit_key"] == "sp500:2007Q1")
    engine = {
        "market_id": plan_unit["market_id"],
        "period": plan_unit["period"],
        "start": __import__("datetime").date.fromisoformat(plan_unit["local_start"]),
        "end": __import__("datetime").date.fromisoformat(plan_unit["local_end"]),
    }
    artifact = build_derived_artifact(plan_unit, fake_derived_extractor(engine, ROOT))
    # corrupt: wrong raw sha
    bad = json.loads(json.dumps(artifact))
    bad["raw_sha256"] = "0" * 64
    assert validate_derived_artifact(plan_unit, bad, FAKE_RAW_SHA, ROOT)
    # corrupt: wrong request id
    bad2 = json.loads(json.dumps(artifact))
    bad2["final_request_id"] = "1" * 64
    assert validate_derived_artifact(plan_unit, bad2, FAKE_RAW_SHA, ROOT)
    # corrupt: wrong row count
    bad3 = json.loads(json.dumps(artifact))
    bad3["row_count"] = bad3["row_count"] - 1
    assert validate_derived_artifact(plan_unit, bad3, FAKE_RAW_SHA, ROOT)
    # corrupt: missing date
    bad4 = json.loads(json.dumps(artifact))
    bad4["rows"].pop(0)
    assert validate_derived_artifact(plan_unit, bad4, FAKE_RAW_SHA, ROOT)
    # corrupt: missing_count > 0
    bad5 = json.loads(json.dumps(artifact))
    bad5["missing_count"] = 1
    assert validate_derived_artifact(plan_unit, bad5, FAKE_RAW_SHA, ROOT)
    # corrupt: consumed_extras > 0
    bad6 = json.loads(json.dumps(artifact))
    bad6["firewall"]["consumed_extras"] = 3
    assert validate_derived_artifact(plan_unit, bad6, FAKE_RAW_SHA, ROOT)
    # corrupt: bad payload hash
    bad7 = json.loads(json.dumps(artifact))
    bad7["derived_payload_sha256"] = "0" * 64
    assert validate_derived_artifact(plan_unit, bad7, FAKE_RAW_SHA, ROOT)


def test_persist_atomic_idempotent_and_corrupt_fail_closed(tmp_path):
    plan_unit = next(u for u in PLAN["units"] if u["unit_key"] == "sp500:2007Q1")
    engine = {
        "market_id": plan_unit["market_id"],
        "period": plan_unit["period"],
        "start": __import__("datetime").date.fromisoformat(plan_unit["local_start"]),
        "end": __import__("datetime").date.fromisoformat(plan_unit["local_end"]),
    }
    artifact = build_derived_artifact(plan_unit, fake_derived_extractor(engine, ROOT))
    # atomic write (no .tmp leftover)
    r1 = persist_derived_atomic(plan_unit, artifact, FAKE_RAW_SHA, tmp_path)
    assert r1["written"] is True
    assert not list(tmp_path.glob("*.tmp"))
    # idempotent skip on valid existing
    r2 = persist_derived_atomic(plan_unit, artifact, FAKE_RAW_SHA, tmp_path)
    assert r2["already_valid"] is True and r2["written"] is False
    # corrupt existing -> FAIL CLOSED (no silent overwrite)
    path = derived_path_for(plan_unit, tmp_path)
    corrupt = json.loads(path.read_text(encoding="utf-8"))
    corrupt["row_count"] = corrupt["row_count"] + 99
    path.write_text(json.dumps(corrupt), encoding="utf-8")
    with pytest.raises(V2Error, match="control-layer review required"):
        persist_derived_atomic(plan_unit, artifact, FAKE_RAW_SHA, tmp_path)
    # artifact NOT overwritten
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["row_count"] == corrupt["row_count"]


# ---------------------------------------------------------------------------
# classify_derived_units (spec 17)
# ---------------------------------------------------------------------------


def test_classify_derived_units_all_missing_for_missing_raw(hermetic):
    cls = classify_derived_units(PLAN, hermetic["root"])
    assert len(cls["complete"]) == 0
    assert len(cls["invalid"]) == 0
    # 952 missing raw -> derived missing too (no raw, no derived)
    assert len(cls["missing"]) == 960


def test_classify_derived_units_complete_after_persist(hermetic):
    # seed one accepted raw + derive it
    unit = _seed_raw(PLAN, hermetic["tmp"] / "raw", "sp500:2007Q1")
    engine = {
        "market_id": unit["market_id"],
        "period": unit["period"],
        "start": __import__("datetime").date.fromisoformat(unit["local_start"]),
        "end": __import__("datetime").date.fromisoformat(unit["local_end"]),
    }
    artifact = build_derived_artifact(unit, fake_derived_extractor(engine, ROOT))
    # root=hermetic root so the DERIVED_BASE monkeypatch determines the path
    persist_derived_atomic(unit, artifact, FAKE_RAW_SHA, hermetic["root"])
    cls = classify_derived_units(PLAN, hermetic["root"])
    assert "sp500:2007Q1" in {u["unit_key"] for u in cls["complete"]}


# ---------------------------------------------------------------------------
# Controller lifecycle (spec 18-21)
# ---------------------------------------------------------------------------


def test_existing_raw_missing_derived_resume(hermetic):
    import scripts.v2.climatology.full_backfill_controller as ctl

    # seed 3 accepted 1991 raw (derived missing)
    for uk in ("sse_composite:1991Q3", "nifty50:1991Q3", "bse50:1991Q1"):
        _seed_raw(PLAN, hermetic["tmp"] / "raw", uk)
    ctl.execute_unit_once = fake_execute_persisting  # type: ignore[attr-defined]
    tracker = []

    def health():
        return {"dataset_available": True, "status": "available"}

    # First run: accepted skips retrieve=0 but derive (extract called); the
    # other 29 get retrieved+derived.  Full batch should complete.
    out = run_year_batch(PLAN, 1991, root=hermetic["root"], health_checker=health,
                         retrieve_calls_tracker=tracker, derived_extractor=fake_derived_extractor)
    import sys; print("DEBUG_OUTCOME:", out["outcome"], file=sys.stderr)
    print("DEBUG_COMPLETION:", out.get("completion"), file=sys.stderr)
    assert out["outcome"] == "batch_completed", out.get("error")
    # 3 skips + 29 new = 29 retrieves
    assert len(tracker) == 29
    # 32 derived complete
    cls = classify_derived_units(PLAN, hermetic["root"])
    complete_1991 = [u for u in cls["complete"] if u["unit_key"].endswith(":1991") or u["unit_key"].split(":")[1].startswith("1991")]
    assert len(complete_1991) == 32


def test_new_raw_retrieve_once_then_derive(hermetic):
    import scripts.v2.climatology.full_backfill_controller as ctl

    ctl.execute_unit_once = fake_execute_persisting  # type: ignore[attr-defined]
    tracker = []
    events_before = len(read_journal(hermetic["root"]))

    def health():
        return {"dataset_available": True, "status": "available"}

    out = run_year_batch(PLAN, YEAR, root=hermetic["root"], health_checker=health,
                         retrieve_calls_tracker=tracker, derived_extractor=fake_derived_extractor)
    assert out["outcome"] == "batch_completed", out.get("error")
    assert len(tracker) == 32  # one retrieve per new unit
    events = read_journal(hermetic["root"])
    raw_accepted = [e for e in events if e.get("event_type") == "unit_raw_accepted"]
    derived_completed = [e for e in events if e.get("event_type") == "unit_derived_completed"]
    assert len(raw_accepted) == 32
    assert len(derived_completed) == 32
    # raw accepted precedes derived completed for the same unit
    by_key = {}
    for e in events:
        if e.get("event_type") in ("unit_raw_accepted", "unit_derived_completed") and e.get("unit_key"):
            by_key.setdefault(e["unit_key"], []).append(e["event_type"])
    for key, seq in by_key.items():
        assert seq[0] == "unit_raw_accepted"
        assert "unit_derived_completed" in seq


def test_derived_failure_preserves_raw_and_no_redownload(hermetic):
    import scripts.v2.climatology.full_backfill_controller as ctl

    ctl.execute_unit_once = fake_execute_persisting  # type: ignore[attr-defined]
    tracker = []
    calls = {"extract": 0}

    def bad_extractor(unit, root=None):
        calls["extract"] += 1
        raise RuntimeError("simulated extraction failure")

    def health():
        return {"dataset_available": True, "status": "available"}

    out = run_year_batch(PLAN, YEAR, root=hermetic["root"], health_checker=health,
                         retrieve_calls_tracker=tracker, derived_extractor=bad_extractor)
    assert out["outcome"] == "batch_stopped_derived_failure"
    assert len(tracker) == 1  # raw retrieved once for the first unit
    # raw is still accepted (preserved)
    events = read_journal(hermetic["root"])
    raw_accepted = [e for e in events if e.get("event_type") == "unit_raw_accepted"]
    derived_fail = [e for e in events if e.get("event_type") == "unit_derived_failure"]
    assert len(raw_accepted) == 1
    assert len(derived_fail) == 1
    assert derived_fail[0]["failure_class"] == "raw_accepted_derived_retry"
    # snapshot marks derived retryable
    state = reconcile_progress(PLAN, hermetic["root"])
    assert state["snapshot"]["derived_retryable_units"] == 1

    # Resume with fixed extractor: the unit whose raw was accepted gets 0
    # retrieve + re-derive; the remaining 31 units get retrieved + derived.
    tracker2 = []
    out2 = run_year_batch(PLAN, YEAR, root=hermetic["root"], health_checker=health,
                          retrieve_calls_tracker=tracker2, derived_extractor=fake_derived_extractor)
    assert out2["outcome"] == "batch_completed", out2.get("error")
    assert len(tracker2) == 31  # 31 remaining; the accepted one is NOT re-downloaded
    # the first unit's raw was NOT re-downloaded: it was retrieved exactly once
    # across both runs (1 in run1 + 0 for that unit in run2).
    events2 = read_journal(hermetic["root"])
    first_unit = [e for e in events if e.get("event_type") == "unit_retrieve_started"]
    assert len(first_unit) == 1  # exactly one retrieve_started for the first unit
    assert len([e for e in events2 if e.get("event_type") == "unit_retrieve_started"]) == 32
    assert reconcile_progress(PLAN, hermetic["root"])["snapshot"]["derived_retryable_units"] == 0


def test_mid_success_interrupt_resumes_from_durable_raw_and_derived(hermetic):
    """A missing per-unit snapshot must not weaken crash/resume authority.

    Five units complete raw+derived, then the sixth is interrupted after raw
    acceptance but before derived persistence.  Resume must trust validated
    durable artifacts rather than the absent snapshot or a stale journal-only
    completion event.
    """
    import scripts.v2.climatology.full_backfill_controller as ctl

    seeded_keys = {"sse_composite:1991Q3", "nifty50:1991Q3", "bse50:1991Q1"}
    for unit_key in seeded_keys:
        _seed_raw(PLAN, hermetic["tmp"] / "raw", unit_key)

    first_tracker = []
    first_extract_keys = []
    interrupted = {"unit_key": None}

    def interrupting_extractor(unit, root=None):
        unit_key = f"{unit['market_id']}:{unit['period']}"
        first_extract_keys.append(unit_key)
        if len(first_extract_keys) == 6:
            interrupted["unit_key"] = unit_key
            raise KeyboardInterrupt("controlled mid-success interruption")
        return fake_derived_extractor(unit, root)

    def health():
        return {"dataset_available": True, "status": "available"}

    with pytest.raises(KeyboardInterrupt, match="controlled mid-success interruption"):
        run_year_batch(
            PLAN,
            1991,
            root=hermetic["root"],
            health_checker=health,
            retrieve_calls_tracker=first_tracker,
            derived_extractor=interrupting_extractor,
        )

    completed_before_resume = set(first_extract_keys[:5])
    interrupted_key = interrupted["unit_key"]
    assert interrupted_key is not None
    assert len(first_tracker) == 6
    assert not (hermetic["tmp"] / "state" / "progress.snapshot.json").exists()

    before_cls = classify_derived_units(PLAN, hermetic["root"])
    before_complete = {u["unit_key"] for u in before_cls["complete"]}
    assert completed_before_resume <= before_complete
    assert interrupted_key not in before_complete
    durable_payloads = {
        unit_key: derived_path_for(
            next(u for u in PLAN["units"] if u["unit_key"] == unit_key),
            hermetic["root"],
        ).read_bytes()
        for unit_key in completed_before_resume
    }

    # A journal-only completion is deliberately stale and must never substitute
    # for the missing validated derived artifact.
    interrupted_unit = next(u for u in PLAN["units"] if u["unit_key"] == interrupted_key)
    ctl.append_event(
        {
            "event_type": "unit_derived_completed",
            "unit_key": interrupted_key,
            "final_request_id": interrupted_unit["final_request_id"],
            "batch_year": 1991,
            "raw_sha": FAKE_RAW_SHA,
            "test_stale_journal_only": True,
        },
        hermetic["root"],
    )

    resume_tracker = []
    resume_extract_keys = []

    def resume_extractor(unit, root=None):
        resume_extract_keys.append(f"{unit['market_id']}:{unit['period']}")
        return fake_derived_extractor(unit, root)

    out = run_year_batch(
        PLAN,
        1991,
        root=hermetic["root"],
        health_checker=health,
        retrieve_calls_tracker=resume_tracker,
        derived_extractor=resume_extractor,
    )

    assert out["outcome"] == "batch_completed"
    assert len(first_tracker) + len(resume_tracker) == 29
    assert len(resume_tracker) == 23
    assert completed_before_resume.isdisjoint(resume_extract_keys)
    assert interrupted_key in resume_extract_keys
    assert completed_before_resume.isdisjoint(seeded_keys)
    for unit_key, payload in durable_payloads.items():
        unit = next(u for u in PLAN["units"] if u["unit_key"] == unit_key)
        assert derived_path_for(unit, hermetic["root"]).read_bytes() == payload

    events = read_journal(hermetic["root"])
    retrieved_keys = [
        e["unit_key"]
        for e in events
        if e.get("event_type") == "unit_retrieve_started" and e.get("unit_key")
    ]
    assert retrieved_keys.count(interrupted_key) == 1
    assert seeded_keys.isdisjoint(retrieved_keys)

    completion = out["completion"]
    assert completion == {
        "complete": True,
        "year": 1991,
        "formal_units": 32,
        "raw_accepted_year": 32,
        "derived_complete_year": 32,
        "expected_rows": 2920,
        "actual_rows": 2920,
        "missing_rows": 0,
        "consumed_extras": 0,
    }
    final_cls = classify_derived_units(PLAN, hermetic["root"])
    final_1991 = {u["unit_key"] for u in final_cls["complete"] if ":1991Q" in u["unit_key"]}
    assert len(final_1991) == 32
    assert reconcile_progress(PLAN, hermetic["root"])["snapshot"]["blocked_units"] == []
    assert hermetic["network_attempts"] == []


def test_existing_raw_derived_failure_preserves_raw(hermetic):
    import scripts.v2.climatology.full_backfill_controller as ctl

    # seed ONLY the 3 accepted 1991 raw (no derived), then fail extraction.
    # Batch order is year->quarter->market, so sse_composite:1991Q1 (unseeded)
    # is the first unit and WILL be retrieved once; the 3 SEEDED units must
    # never be retrieved.
    for uk in ("sse_composite:1991Q3", "nifty50:1991Q3", "bse50:1991Q1"):
        _seed_raw(PLAN, hermetic["tmp"] / "raw", uk)
    ctl.execute_unit_once = fake_execute_persisting  # type: ignore[attr-defined]
    tracker = []

    def bad_extractor(unit, root=None):
        raise RuntimeError("simulated extraction failure")

    def health():
        return {"dataset_available": True, "status": "available"}

    out = run_year_batch(PLAN, 1991, root=hermetic["root"], health_checker=health,
                         retrieve_calls_tracker=tracker, derived_extractor=bad_extractor)
    assert out["outcome"] == "batch_stopped_derived_failure"
    # exactly one retrieve (first unseeded unit); the 3 seeded units NEVER
    # retrieve (accepted raw skip != derived skip).
    assert len(tracker) == 1
    events = read_journal(hermetic["root"])
    retrieved_keys = {
        e["unit_key"] for e in events
        if e.get("event_type") == "unit_retrieve_started" and e.get("unit_key")
    }
    assert "sse_composite:1991Q3" not in retrieved_keys
    assert "nifty50:1991Q3" not in retrieved_keys
    assert "bse50:1991Q1" not in retrieved_keys
    assert sum(1 for e in events if e.get("event_type") == "unit_derived_failure") >= 1
    # raw unchanged: the 3 seeded raw remain accepted (no re-download)
    from scripts.v2.climatology.full_backfill_controller import classify_units

    raw_cls = classify_units(PLAN, hermetic["root"])
    accepted_keys = {u["unit_key"] for u in raw_cls["accepted"]}
    assert {"sse_composite:1991Q3", "nifty50:1991Q3", "bse50:1991Q1"} <= accepted_keys


# ---------------------------------------------------------------------------
# 1991 full hermetic simulation (spec 23-24)
# ---------------------------------------------------------------------------


def test_1991_full_hermetic_simulation(hermetic, monkeypatch):
    import scripts.v2.climatology.full_backfill_controller as ctl

    real_reconcile = ctl.reconcile_progress
    reconcile_calls = {"n": 0}

    def counting_reconcile(*args, **kwargs):
        reconcile_calls["n"] += 1
        return real_reconcile(*args, **kwargs)

    monkeypatch.setattr(ctl, "reconcile_progress", counting_reconcile)

    # 3 existing accepted raw (1991)
    for uk in ("sse_composite:1991Q3", "nifty50:1991Q3", "bse50:1991Q1"):
        _seed_raw(PLAN, hermetic["tmp"] / "raw", uk)
    ctl.execute_unit_once = fake_execute_persisting  # type: ignore[attr-defined]
    tracker = []
    health_calls = {"n": 0}

    def health():
        health_calls["n"] += 1
        return {"dataset_available": True, "status": "available"}

    out = run_year_batch(PLAN, 1991, root=hermetic["root"], health_checker=health,
                         retrieve_calls_tracker=tracker, derived_extractor=fake_derived_extractor)
    assert out["outcome"] == "batch_completed", out.get("error")
    assert out["new_retrieves"] == 29
    assert len(tracker) == 29
    assert health_calls["n"] == 4  # pre + after 8/16/24
    completion = out["completion"]
    assert completion["raw_accepted_year"] == 32
    assert completion["derived_complete_year"] == 32
    assert completion["actual_rows"] == 2920
    assert completion["missing_rows"] == 0
    assert completion["consumed_extras"] == 0
    # the 3 seeded units were never retrieved
    events = read_journal(hermetic["root"])
    retrieved_keys = {
        e["unit_key"] for e in events
        if e.get("event_type") == "unit_retrieve_started" and e.get("unit_key")
    }
    assert "sse_composite:1991Q3" not in retrieved_keys
    assert "nifty50:1991Q3" not in retrieved_keys
    assert "bse50:1991Q1" not in retrieved_keys
    # but all 3 are derived complete
    cls = classify_derived_units(PLAN, hermetic["root"])
    complete_keys = {u["unit_key"] for u in cls["complete"]}
    assert {"sse_composite:1991Q3", "nifty50:1991Q3", "bse50:1991Q1"} <= complete_keys
    assert reconcile_calls["n"] == 1  # final snapshot only; never once per successful unit
    assert hermetic["network_attempts"] == []


# ---------------------------------------------------------------------------
# Health counter regression (spec 14): derived must not count
# ---------------------------------------------------------------------------


def test_health_interval_unaffected_by_derived(hermetic):
    import scripts.v2.climatology.full_backfill_controller as ctl

    ctl.execute_unit_once = fake_execute_persisting  # type: ignore[attr-defined]
    calls = {"health": 0, "extract": 0}

    def health():
        calls["health"] += 1
        return {"dataset_available": True, "status": "available"}

    def counting_extractor(unit, root=None):
        calls["extract"] += 1
        return fake_derived_extractor(unit, root)

    out = run_year_batch(PLAN, YEAR, root=hermetic["root"], health_checker=health,
                         derived_extractor=counting_extractor)
    assert out["outcome"] == "batch_completed", out.get("error")
    # 32 retrieves: health checks at pre + 8/16/24 = 4; extraction (32) did NOT
    # advance the health counter.
    assert calls["health"] == 4
    assert calls["extract"] == 32
