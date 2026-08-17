"""Stage 5E-4E-R5 hermetic snapshot-finalization hardening tests.

All filesystem work is isolated under ``tmp_path``.  Network-capable
boundaries are never constructed.
"""
from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path

import pytest

import scripts.v2.climatology.full_backfill_controller as ctl
from scripts.v2.core import V2Error, repo_root


ROOT = repo_root()
POLICY = ctl.load_controller_policy(ROOT)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def sharing_violation(winerror: int) -> PermissionError:
    exc = PermissionError(f"hermetic WinError {winerror}")
    exc.winerror = winerror
    return exc


def configure_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "progress.snapshot.json"
    monkeypatch.setattr(ctl, "SNAPSHOT_PATH", str(target))
    return target


def prepare_completed_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    monkeypatch.setattr(ctl, "JOURNAL_PATH", str(tmp_path / "progress.events.jsonl"))
    monkeypatch.setattr(ctl, "SNAPSHOT_PATH", str(tmp_path / "progress.snapshot.json"))
    monkeypatch.setattr(
        ctl,
        "verify_authorization_binding",
        lambda root=None: {"valid": True, "live_backfill_authorized": True},
    )
    monkeypatch.setattr(ctl, "load_controller_policy", lambda root=None: copy.deepcopy(POLICY))
    monkeypatch.setattr(
        ctl,
        "verify_transport_runtime",
        lambda policy, version_getter=None: {"valid": True, "versions": {}},
    )
    monkeypatch.setattr(ctl, "year_units", lambda plan, year: [])
    monkeypatch.setattr(
        ctl,
        "classify_units",
        lambda plan, root=None: {"accepted": [], "missing": [], "invalid": []},
    )
    monkeypatch.setattr(
        ctl,
        "_year_completion_check",
        lambda plan, year, root=None: {"complete": True, "formal_rows": 2920},
    )
    calls = {"reconcile": 0}

    def reconcile(plan, root=None):
        calls["reconcile"] += 1
        return {"snapshot": {"schema_version": "1.0.0", "current_year": 2004}}

    monkeypatch.setattr(ctl, "reconcile_progress", reconcile)
    return calls


def test_normal_snapshot_replacement_succeeds_first_attempt(tmp_path, monkeypatch):
    target = configure_snapshot(tmp_path, monkeypatch)
    sources = []
    real_replace = os.replace

    def replace(source, destination):
        sources.append(Path(source))
        real_replace(source, destination)

    written = ctl.write_snapshot(
        {"schema_version": "1.0.0", "accepted_raw_units": 450},
        ROOT,
        policy=POLICY,
        replace_func=replace,
        uuid_factory=lambda: uuid.UUID(int=1),
        pid_getter=lambda: 123,
    )
    assert written == target
    assert len(sources) == 1
    assert sources[0].parent == target.parent
    assert sources[0].name == f"{target.name}.123.{uuid.UUID(int=1).hex}.tmp"
    assert json.loads(target.read_text(encoding="utf-8"))["accepted_raw_units"] == 450


def test_two_logical_writers_never_share_source_temp(tmp_path, monkeypatch):
    target = configure_snapshot(tmp_path, monkeypatch)
    sources = []

    def fail(source, destination):
        sources.append(Path(source))
        raise OSError("non-retryable hermetic stop")

    for value in (1, 2):
        with pytest.raises(ctl.SnapshotFinalizationError):
            ctl.write_snapshot(
                {"schema_version": "1.0.0"},
                ROOT,
                policy=POLICY,
                replace_func=fail,
                uuid_factory=lambda value=value: uuid.UUID(int=value),
                pid_getter=lambda: 456,
            )
    assert len(sources) == 2
    assert sources[0] != sources[1]
    assert all(path.parent == target.parent for path in sources)


@pytest.mark.parametrize("winerror", [5, 32])
def test_retryable_windows_contention_then_success(tmp_path, monkeypatch, winerror):
    target = configure_snapshot(tmp_path, monkeypatch)
    real_replace = os.replace
    clock = FakeClock()
    attempts = []

    def replace(source, destination):
        attempts.append(Path(source))
        if len(attempts) == 1:
            raise sharing_violation(winerror)
        real_replace(source, destination)

    ctl.write_snapshot(
        {"schema_version": "1.0.0"},
        ROOT,
        policy=POLICY,
        replace_func=replace,
        monotonic_func=clock.monotonic,
        sleep_func=clock.sleep,
    )
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert clock.sleeps == [0.025]
    assert target.exists()


def test_retry_exhaustion_is_deterministic_and_bounded(tmp_path, monkeypatch):
    configure_snapshot(tmp_path, monkeypatch)
    clock = FakeClock()
    attempts = []

    def replace(source, destination):
        attempts.append(Path(source))
        raise sharing_violation(5)

    with pytest.raises(ctl.SnapshotFinalizationError) as caught:
        ctl.write_snapshot(
            {"schema_version": "1.0.0"},
            ROOT,
            policy=POLICY,
            replace_func=replace,
            monotonic_func=clock.monotonic,
            sleep_func=clock.sleep,
        )
    failure = caught.value
    assert failure.retry_exhausted is True
    assert failure.attempt_count == len(attempts) == 14
    assert clock.now == pytest.approx(5.0)
    assert sum(clock.sleeps) == pytest.approx(5.0)
    assert max(clock.sleeps) <= 0.5
    assert len(set(attempts)) == 1


def test_non_retryable_replacement_exception_is_not_retried(tmp_path, monkeypatch):
    configure_snapshot(tmp_path, monkeypatch)
    clock = FakeClock()
    attempts = []

    def replace(source, destination):
        attempts.append(Path(source))
        raise OSError("disk contract failure")

    with pytest.raises(ctl.SnapshotFinalizationError) as caught:
        ctl.write_snapshot(
            {"schema_version": "1.0.0"},
            ROOT,
            policy=POLICY,
            replace_func=replace,
            monotonic_func=clock.monotonic,
            sleep_func=clock.sleep,
        )
    assert caught.value.retry_exhausted is False
    assert caught.value.attempt_count == 1
    assert len(attempts) == 1
    assert clock.sleeps == []


def test_serialization_occurs_once_across_replace_retries(tmp_path, monkeypatch):
    configure_snapshot(tmp_path, monkeypatch)
    real_replace = os.replace
    clock = FakeClock()
    counts = {"serialize": 0, "replace": 0}

    def serializer(snapshot):
        counts["serialize"] += 1
        return json.dumps(snapshot) + "\n"

    def replace(source, destination):
        counts["replace"] += 1
        if counts["replace"] < 4:
            raise sharing_violation(5)
        real_replace(source, destination)

    ctl.write_snapshot(
        {"schema_version": "1.0.0"},
        ROOT,
        policy=POLICY,
        replace_func=replace,
        monotonic_func=clock.monotonic,
        sleep_func=clock.sleep,
        serializer=serializer,
    )
    assert counts == {"serialize": 1, "replace": 4}


def test_post_batch_exhaustion_keeps_batch_completed_and_emits_one_diagnostic(
    tmp_path, monkeypatch
):
    calls = prepare_completed_batch(tmp_path, monkeypatch)
    clock = FakeClock()
    real_write_snapshot = ctl.write_snapshot

    def write_snapshot(snapshot, root=None, *, policy=None):
        return real_write_snapshot(
            snapshot,
            root,
            policy=policy,
            replace_func=lambda source, destination: (_ for _ in ()).throw(sharing_violation(5)),
            monotonic_func=clock.monotonic,
            sleep_func=clock.sleep,
        )

    monkeypatch.setattr(ctl, "write_snapshot", write_snapshot)

    result = ctl.run_year_batch({}, 2004, tmp_path, health_checker=lambda: {"dataset_available": True})
    events = ctl.read_journal(tmp_path)
    diagnostics = [e for e in events if e["event_type"] == "snapshot_finalization_failed"]
    assert result["outcome"] == "batch_completed"
    assert result["snapshot_status"] == "pending"
    assert calls["reconcile"] == 1
    assert [e["event_type"] for e in events].count("batch_completed") == 1
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic["snapshot_status"] == "pending"
    assert diagnostic["attempt_count"] == 14
    assert diagnostic["exception_class"] == "PermissionError"
    assert diagnostic["winerror"] == 5
    assert diagnostic["retry_exhausted"] is True
    assert diagnostic["target_basename"] == "progress.snapshot.json"
    assert diagnostic["temp_basename"].startswith("progress.snapshot.json.")
    assert "unit_key" not in diagnostic
    assert "error" not in diagnostic
    assert events.index(next(e for e in events if e["event_type"] == "batch_completed")) < events.index(diagnostic)


def test_post_batch_non_retryable_failure_is_immediate_and_nonfatal(tmp_path, monkeypatch):
    prepare_completed_batch(tmp_path, monkeypatch)
    attempts = []
    real_write_snapshot = ctl.write_snapshot

    def replace(source, destination):
        attempts.append(1)
        raise OSError("non-retryable")

    monkeypatch.setattr(
        ctl,
        "write_snapshot",
        lambda snapshot, root=None, *, policy=None: real_write_snapshot(
            snapshot, root, policy=policy, replace_func=replace
        ),
    )
    result = ctl.run_year_batch({}, 2004, tmp_path, health_checker=lambda: {"dataset_available": True})
    diagnostic = [e for e in ctl.read_journal(tmp_path) if e["event_type"] == "snapshot_finalization_failed"]
    assert result["outcome"] == "batch_completed"
    assert result["snapshot_status"] == "pending"
    assert attempts == [1]
    assert len(diagnostic) == 1
    assert diagnostic[0]["retry_exhausted"] is False


def test_post_batch_success_reports_complete_and_reconciles_once(tmp_path, monkeypatch):
    calls = prepare_completed_batch(tmp_path, monkeypatch)
    result = ctl.run_year_batch({}, 2004, tmp_path, health_checker=lambda: {"dataset_available": True})
    assert result["outcome"] == "batch_completed"
    assert result["snapshot_status"] == "complete"
    assert calls["reconcile"] == 1
    assert ctl.snapshot_path(tmp_path).exists()
    assert not [e for e in ctl.read_journal(tmp_path) if e["event_type"] == "snapshot_finalization_failed"]


def test_pre_batch_snapshot_failure_remains_fail_closed(tmp_path, monkeypatch):
    prepare_completed_batch(tmp_path, monkeypatch)
    failure = ctl.SnapshotFinalizationError(
        OSError("pre-batch snapshot failure"),
        attempt_count=1,
        elapsed_seconds=0.0,
        target_path=tmp_path / "progress.snapshot.json",
        temp_path=tmp_path / "progress.snapshot.json.1.deadbeef.tmp",
        retry_exhausted=False,
    )
    monkeypatch.setattr(ctl, "write_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    with pytest.raises(ctl.SnapshotFinalizationError):
        ctl.run_year_batch({}, 2004, tmp_path, health_checker=lambda: {"dataset_available": False})
    event_types = [event["event_type"] for event in ctl.read_journal(tmp_path)]
    assert event_types == ["batch_started", "unit_service_deferred"]


def test_diagnostic_event_does_not_block_or_count_as_operational_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ctl, "JOURNAL_PATH", str(tmp_path / "progress.events.jsonl"))
    monkeypatch.setattr(
        ctl,
        "classify_units",
        lambda plan, root=None: {"accepted": [], "missing": [], "invalid": []},
    )
    monkeypatch.setattr(
        ctl,
        "classify_derived_units",
        lambda plan, root=None, raw_classification=None: {"complete": [], "missing": [], "invalid": []},
    )
    monkeypatch.setattr(ctl, "authorization_candidate_hash", lambda root=None: "candidate")
    ctl.append_event(
        {
            "event_type": "snapshot_finalization_failed",
            "batch_year": 2004,
            "snapshot_status": "pending",
            "attempt_count": 14,
        },
        tmp_path,
    )
    state = ctl.reconcile_progress(
        {"global_backfill_plan_hash": "plan", "formal_unit_count": 960}, tmp_path
    )["snapshot"]
    assert state["blocked_units"] == []
    assert state["operational_retryable_units"] == 0


def test_status_is_strictly_read_only(tmp_path, monkeypatch, capsys):
    target = configure_snapshot(tmp_path, monkeypatch)
    monkeypatch.setattr(ctl, "load_plan", lambda root=None: {"formal_unit_count": 960})
    monkeypatch.setattr(
        ctl,
        "reconcile_progress",
        lambda plan, root=None: {"snapshot": {"schema_version": "1.0.0", "current_year": 2004}},
    )
    monkeypatch.setattr(
        ctl,
        "write_snapshot",
        lambda *args, **kwargs: pytest.fail("--status must not write a snapshot"),
    )
    assert ctl.main(["--status", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["current_year"] == 2004
    assert not target.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_legacy_fixed_temp_is_ignored_and_snapshot_schema_is_compatible(tmp_path, monkeypatch):
    target = configure_snapshot(tmp_path, monkeypatch)
    legacy = target.with_suffix(".tmp")
    legacy.write_text('{"accepted_raw_units": 999}\n', encoding="utf-8")
    snapshot = {
        "schema_version": "1.0.0",
        "plan_hash": "frozen-plan",
        "accepted_raw_units": 450,
        "pending_units": 510,
        "blocked_units": [],
    }
    ctl.write_snapshot(snapshot, ROOT, policy=POLICY)
    assert json.loads(target.read_text(encoding="utf-8")) == snapshot
    assert json.loads(legacy.read_text(encoding="utf-8"))["accepted_raw_units"] == 999


SNAPSHOT_FIELDS = {
    "temp_file_strategy": "wrong",
    "atomic_replace_primitive": "Path.replace",
    "retry_scope": "whole_writer",
    "retryable_exception_classes": ["OSError"],
    "retryable_winerrors": [5],
    "retry_deadline_seconds": 6.0,
    "backoff_initial_seconds": 0.05,
    "backoff_multiplier": 3.0,
    "backoff_max_seconds": 1.0,
    "post_batch_exhaustion_semantics": "batch_failed",
    "diagnostic_event_type": "unit_operational_failure",
    "status_mode": "write_repair",
}


@pytest.mark.parametrize("field,drifted", SNAPSHOT_FIELDS.items())
def test_policy_validation_fails_closed_for_every_snapshot_semantic(field, drifted):
    missing = copy.deepcopy(POLICY)
    del missing["snapshot_finalization"][field]
    with pytest.raises(V2Error, match="invalid bounded controller policy"):
        ctl._validate_controller_policy(missing)

    changed = copy.deepcopy(POLICY)
    changed["snapshot_finalization"][field] = drifted
    with pytest.raises(V2Error, match="invalid bounded controller policy"):
        ctl._validate_controller_policy(changed)
