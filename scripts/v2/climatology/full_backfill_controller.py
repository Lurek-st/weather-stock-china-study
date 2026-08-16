"""Stage 5E-3C full backfill controller (ZERO-NETWORK control / authorization gate).

This module FREEZES the system-level execution contract for the future
952-unit full backfill.  It is a controller, not a downloader: Stage 5E-3C
performs NO network activity (no CDS retrieve, no service-health HTTP fetch,
no Nominatim, no ERA5 download).  Every network boundary is expressed as an
injectable callable so tests can prove the controller refuses to touch the
network unless fully authorized.

Frozen contracts owned here:

    canonical plan             data/audits/v2/climatology/full-era5-backfill-plan-1991-2020.json
    global_backfill_plan_hash  static scientific plan only (never progress)
    controller policy          config/v2/full-backfill-controller.yaml -> controller_policy_hash
    authorization candidate    config/v2/full-backfill-authorization.yaml -> authorization_candidate_hash
    progress journal           data/state/v2/climatology/full-backfill/progress.events.jsonl (append-only)
    progress snapshot          data/state/v2/climatology/full-backfill/progress.snapshot.json (derived)

Authority design (frozen):

    STATIC     canonical plan (tracked, immutable)
    IMMUTABLE  RawArtifactStore accepted artifacts (append-only, sha-bound)
    APPEND-ONLY progress event journal (gitignored)
    DERIVED    snapshot (convenience only; never higher authority than raw store)

The authorization file is the NETWORK KILL SWITCH: with
``live_backfill_authorized != true`` every ``--live`` invocation MUST fail
BEFORE any service-health client, CDS client, or network call is constructed.
Any hash mismatch (plan / policy / spatial / timezone) invalidates the
authorization and the controller stops with 0 network.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.cds_service_health import (
    DEFAULT_DATASET_ID,
    make_dataset_health_checker,
)
from scripts.v2.climatology.derived_store import (
    build_derived_artifact,
    classify_derived_units,
    derived_path_for,
    load_derived_artifact,
    persist_derived_atomic,
    validate_derived_artifact,
)
from scripts.v2.climatology.production_canary import (
    EXPECTED_TIMEZONE_CANARY_HASH,
    REQUEST_IDENTITY_CONTRACT_VERSION,
    load_global_registry_hash,
    lookup_accepted_request,
)
from scripts.v2.climatology.production_quarter import assert_stage5e3b_authorized
from scripts.v2.climatology.production_unit import (
    RAW_BASE,
    ClientConstructionError,
    PayloadValidationError,
    RawArtifactStore,
    execute_unit_once,
    extract_unit_exposures,
)
from scripts.v2.core import V2Error, load_json, load_yaml, repo_root

PLAN_PATH = "data/audits/v2/climatology/full-era5-backfill-plan-1991-2020.json"
CONTROLLER_POLICY_PATH = "config/v2/full-backfill-controller.yaml"
AUTHORIZATION_PATH = "config/v2/full-backfill-authorization.yaml"
STATE_DIR = "data/state/v2/climatology/full-backfill"
JOURNAL_PATH = "data/state/v2/climatology/full-backfill/progress.events.jsonl"
SNAPSHOT_PATH = "data/state/v2/climatology/full-backfill/progress.snapshot.json"
SCHEMA_VERSION = "1.0.0"
ERROR_RECORD_SCHEMA_VERSION = "1.0.0"
MAX_SANITIZED_MESSAGE_CHARS = 1024
OLD_CONTROLLER_POLICY_HASH = "a05924b0bf999eb37d5fa04d2e81520d5e799d1cf75715c3257663f32dc8c94a"

# Failure classification (frozen Stage 5E-3C contract).
FAILURE_CONTRACT = {
    "pre_network_contract": "non_retryable_until_control_review",
    "service_health_defer": "operational_deferred",
    "operational_retrieve": "retryable_operational",
    "transport_retry_exhausted": "transport_retry_exhausted",
    "service_job_terminal_failure": "service_job_terminal_failure",
    "request_contract_rejection": "contract_review_required",
    "payload_validation": "validation_failure_control_review_required",
    "derived_only": "raw_accepted_derived_retry",
}

EVENT_TYPES = {
    "unit_preflight",
    "unit_skip_accepted",
    "unit_retrieve_started",
    "unit_raw_accepted",
    "unit_derived_completed",
    "unit_derived_failure",
    "unit_operational_failure",
    "unit_validation_failure",
    "unit_contract_failure",
    "unit_service_deferred",
    "batch_started",
    "batch_completed",
    "batch_stopped",
}


# ---------------------------------------------------------------------------
# Static contract loaders (fail closed)
# ---------------------------------------------------------------------------


def load_plan(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    plan = load_json(root / PLAN_PATH)
    if plan.get("schema_version") != "1.0.0":
        raise V2Error("unexpected canonical plan schema version")
    if plan.get("formal_unit_count") != 960:
        raise V2Error(f"canonical plan formal_unit_count={plan.get('formal_unit_count')} != 960")
    return plan


def load_controller_policy(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    policy = load_yaml(root / CONTROLLER_POLICY_PATH)
    if policy.get("controller_schema_version") != "2.0.0":
        raise V2Error("unexpected controller policy schema version")
    _validate_controller_policy(policy)
    return policy


def _validate_controller_policy(policy: dict[str, Any]) -> None:
    controller_retry = policy.get("controller_retry") or {}
    logical = policy.get("logical_retrieve") or {}
    transport = policy.get("transport") or {}
    required = {
        "controller automatic annual retry": controller_retry.get("automatic_annual_retry") is False,
        "controller operational stop": controller_retry.get("controller_visible_operational_failure")
        == "stop_immediately",
        "one logical retrieve": logical.get("max_cds_retrieve_calls_per_unit_attempt") == 1,
        "accepted pre-network skip": logical.get("accepted_request_policy") == "pre_network_skip",
        "logical operational stop": logical.get("operational_failure_policy") == "stop_immediately",
        "three total transport tries": transport.get("maximum_total_tries_per_robust_http_operation") == 3,
        "transport delay": transport.get("retry_delay_seconds") == 120,
        "TLS verification": transport.get("tls_verification_enabled") is True,
        "retryable HTTP statuses": transport.get("retryable_http_statuses")
        == [408, 429, 500, 502, 503, 504],
        "retryable exception families": transport.get("retryable_exception_families")
        == ["ConnectionError", "ReadTimeout", "ChunkedEncodingError"],
        "SSL not retried": transport.get("ssl_error_automatic_retry") is False,
        "server Retry-After not used": transport.get("server_retry_after_used") is False,
        "job polling classification": transport.get("job_polling_classification")
        == "continuous_job_state_wait_not_controller_retry",
    }
    failed = [name for name, passed in required.items() if not passed]
    if failed:
        raise V2Error("invalid bounded controller policy: " + ", ".join(failed))


def runtime_transport_versions(
    version_getter: Callable[[str], str] | None = None,
) -> dict[str, str]:
    """Read installed distribution versions without constructing a client."""
    getter = version_getter or importlib.metadata.version
    names = ("cdsapi", "ecmwf-datastores-client", "multiurl", "requests", "urllib3")
    versions: dict[str, str] = {}
    for name in names:
        try:
            versions[name] = getter(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise V2Error(f"transport runtime dependency missing: {name}; 0 network") from exc
    return versions


def verify_transport_runtime(
    policy: dict[str, Any],
    version_getter: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Fail closed on version or pinned multiurl semantic drift, before network."""
    expected = (policy.get("transport") or {}).get("runtime_dependencies") or {}
    observed = runtime_transport_versions(version_getter)
    mismatches = {
        name: {"expected": expected.get(name), "observed": observed.get(name)}
        for name in observed
        if expected.get(name) != observed.get(name)
    }
    if mismatches:
        raise V2Error(f"transport runtime version mismatch: {mismatches}; 0 network")

    # This local/static check binds the policy's status semantics to the exact
    # installed multiurl implementation.  It performs no HTTP operation.
    from multiurl.retry import RETRIABLE

    expected_statuses = set((policy.get("transport") or {}).get("retryable_http_statuses") or [])
    if set(RETRIABLE) != expected_statuses:
        raise V2Error(
            f"pinned multiurl retry status semantics mismatch: {sorted(RETRIABLE)}; 0 network"
        )
    return {"valid": True, "versions": observed, "retryable_http_statuses": sorted(RETRIABLE)}


def make_policy_driven_cds_client_factory(
    policy: dict[str, Any],
    client_constructor: Callable[..., Any] | None = None,
    version_getter: Callable[[str], str] | None = None,
) -> Callable[[], Any]:
    """Return a lazy real-client factory whose retry settings come from policy."""
    _validate_controller_policy(policy)
    transport = policy["transport"]

    def factory() -> Any:
        verify_transport_runtime(policy, version_getter)
        constructor = client_constructor
        if constructor is None:
            import cdsapi

            constructor = cdsapi.Client
        return constructor(
            retry_max=transport["maximum_total_tries_per_robust_http_operation"],
            sleep_max=transport["retry_delay_seconds"],
            verify=transport["tls_verification_enabled"],
        )

    return factory


def load_authorization(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    auth = load_yaml(root / AUTHORIZATION_PATH)
    if auth.get("authorization_schema_version") != "1.0.0":
        raise V2Error("unexpected authorization schema version")
    return auth


def controller_policy_hash(root: Path | None = None) -> str:
    """SHA256 of the canonicalized controller policy (rules only)."""
    policy = load_controller_policy(root)
    canonical = json.dumps(policy, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def authorization_candidate_hash(root: Path | None = None) -> str:
    """Canonical SHA256 over {plan_hash, policy_hash, identity version,
    spatial registry hash, timezone canary hash}."""
    root = root or repo_root()
    plan = load_plan(root)
    payload = {
        "global_backfill_plan_hash": plan["global_backfill_plan_hash"],
        "controller_policy_hash": controller_policy_hash(root),
        "request_identity_contract_version": REQUEST_IDENTITY_CONTRACT_VERSION,
        "global_spatial_anchor_registry_hash": load_global_registry_hash(root),
        "timezone_canary_hash": EXPECTED_TIMEZONE_CANARY_HASH,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_authorization_binding(root: Path | None = None) -> dict[str, Any]:
    """Recompute all bound hashes and compare with the authorization file.

    ANY mismatch -> authorization invalid -> 0 network, STOP.
    """
    root = root or repo_root()
    auth = load_authorization(root)
    bound = auth.get("bound_hashes") or {}
    plan = load_plan(root)
    checks = {
        "global_backfill_plan_hash": plan["global_backfill_plan_hash"] == bound.get("global_backfill_plan_hash"),
        "controller_policy_hash": controller_policy_hash(root) == bound.get("controller_policy_hash"),
        "request_identity_contract_version": REQUEST_IDENTITY_CONTRACT_VERSION
        == bound.get("request_identity_contract_version"),
        "global_spatial_anchor_registry_hash": load_global_registry_hash(root)
        == bound.get("global_spatial_anchor_registry_hash"),
        "timezone_canary_hash": EXPECTED_TIMEZONE_CANARY_HASH == bound.get("timezone_canary_hash"),
    }
    candidate_hash = authorization_candidate_hash(root)
    checks["authorization_candidate_hash"] = candidate_hash == auth.get("authorization_candidate_hash")
    return {
        "valid": all(checks.values()),
        "checks": checks,
        "recomputed_candidate_hash": candidate_hash,
        "stored_candidate_hash": auth.get("authorization_candidate_hash"),
        "live_backfill_authorized": auth.get("live_backfill_authorized") is True,
    }


# ---------------------------------------------------------------------------
# Accepted-unit inventory (ZERO network: RawArtifactStore scan only)
# ---------------------------------------------------------------------------


def _raw_store(root: Path | None = None) -> RawArtifactStore:
    """RawArtifactStore rooted at RAW_BASE (absolute or relative-to-root)."""
    root = root or repo_root()
    base = Path(RAW_BASE)
    path = base if base.is_absolute() else root / base
    return RawArtifactStore(path)


def classify_units(plan: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Classify every formal unit against the RawArtifactStore.

    accepted predicate reuses the request-aware lookup (direct manifest OR
    append-only request-binding sidecar).  A broken/missing accepted artifact
    counts as ``invalid`` and FAILS the gate.
    """
    root = root or repo_root()
    store = _raw_store(root)
    accepted: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for unit in plan["units"]:
        rid = unit["final_request_id"]
        try:
            lookup = _lookup_accepted(store, rid)
        except V2Error as exc:
            invalid.append({"unit_key": unit["unit_key"], "final_request_id": rid, "reason": str(exc)})
            continue
        if lookup["found"]:
            accepted.append(
                {
                    "ordinal": unit["ordinal"],
                    "unit_key": unit["unit_key"],
                    "final_request_id": rid,
                    "binding_type": lookup["binding_type"],
                    "sha256": lookup["sha256"],
                    "artifact_id": lookup["artifact_id"],
                }
            )
        else:
            missing.append({"ordinal": unit["ordinal"], "unit_key": unit["unit_key"], "final_request_id": rid})
    return {"accepted": accepted, "missing": missing, "invalid": invalid}


def _lookup_accepted(store: RawArtifactStore, request_id: str) -> dict[str, Any]:
    from scripts.v2.climatology.production_canary import lookup_accepted_request

    return lookup_accepted_request(store, request_id)


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def sanitize_error_message(exc: BaseException) -> str:
    """Return a bounded durable message with common credential surfaces redacted."""
    message = str(exc)
    message = re.sub(r"(?i)(authorization\s*:\s*)[^\r\n]+", r"\1<redacted>", message)
    message = re.sub(
        r"(?i)\b(api[_-]?key|token|access[_-]?token|secret|key)\s*[=:]\s*[^\s,;&]+",
        r"\1=<redacted>",
        message,
    )
    message = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", message)
    return message[:MAX_SANITIZED_MESSAGE_CHARS]


def _http_status(exc: BaseException) -> int | None:
    for item in _exception_chain(exc):
        response = getattr(item, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _classify_failure(exc: BaseException) -> dict[str, str]:
    """Classify only stable exception types; otherwise preserve unknown."""
    import requests.exceptions
    from ecmwf.datastores.processing import ProcessingFailedError

    chain = _exception_chain(exc)
    if isinstance(exc, ClientConstructionError):
        return {
            "event_type": "unit_operational_failure",
            "failure_class": FAILURE_CONTRACT["operational_retrieve"],
            "error_category": "client_construction_failure",
            "phase": "client_construction",
        }
    if any(isinstance(item, PayloadValidationError) for item in chain):
        return {
            "event_type": "unit_validation_failure",
            "failure_class": FAILURE_CONTRACT["payload_validation"],
            "error_category": "payload_validation_failure",
            "phase": "payload_validation",
        }
    if any(isinstance(item, ProcessingFailedError) for item in chain):
        return {
            "event_type": "unit_operational_failure",
            "failure_class": FAILURE_CONTRACT["service_job_terminal_failure"],
            "error_category": "service_job_terminal_failure",
            "phase": "unknown",
        }
    if any(isinstance(item, requests.exceptions.SSLError) for item in chain):
        return {
            "event_type": "unit_operational_failure",
            "failure_class": FAILURE_CONTRACT["operational_retrieve"],
            "error_category": "non_retryable_ssl_failure",
            "phase": "unknown",
        }
    retryable_types = (
        requests.exceptions.ConnectionError,
        requests.exceptions.ReadTimeout,
        requests.exceptions.ChunkedEncodingError,
    )
    if any(isinstance(item, retryable_types) for item in chain):
        return {
            "event_type": "unit_operational_failure",
            "failure_class": FAILURE_CONTRACT["transport_retry_exhausted"],
            "error_category": "transport_retry_exhausted",
            "phase": "unknown",
        }
    if any(isinstance(item, V2Error) for item in chain):
        return {
            "event_type": "unit_contract_failure",
            "failure_class": FAILURE_CONTRACT["request_contract_rejection"],
            "error_category": "request_contract_rejection",
            "phase": "unknown",
        }
    return {
        "event_type": "unit_operational_failure",
        "failure_class": FAILURE_CONTRACT["operational_retrieve"],
        "error_category": "operational_failure_unknown",
        "phase": "unknown",
    }


def build_error_record(
    exc: BaseException,
    policy: dict[str, Any],
    transport_versions: dict[str, str],
    classification: dict[str, str] | None = None,
) -> dict[str, Any]:
    classification = classification or _classify_failure(exc)
    transport = policy["transport"]
    return {
        "error_record_schema_version": ERROR_RECORD_SCHEMA_VERSION,
        "error_category": classification["error_category"],
        "phase": classification["phase"],
        "exception_class": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "http_status": _http_status(exc),
        "sanitized_message": sanitize_error_message(exc),
        "transport_runtime_versions": dict(transport_versions),
        "transport_retry_policy": {
            "maximum_total_tries_per_robust_http_operation": transport[
                "maximum_total_tries_per_robust_http_operation"
            ],
            "retry_delay_seconds": transport["retry_delay_seconds"],
            "retryable_http_statuses": list(transport["retryable_http_statuses"]),
            "retryable_exception_families": list(transport["retryable_exception_families"]),
            "ssl_error_automatic_retry": transport["ssl_error_automatic_retry"],
        },
        "library_retry_count": "unknown",
    }


# ---------------------------------------------------------------------------
# Progress ledger (append-only journal + derived snapshot)
# ---------------------------------------------------------------------------


def journal_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / JOURNAL_PATH


def snapshot_path(root: Path | None = None) -> Path:
    return (root or repo_root()) / SNAPSHOT_PATH


def read_journal(root: Path | None = None) -> list[dict[str, Any]]:
    path = journal_path(root)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError as exc:
            raise V2Error(f"corrupt journal line: {exc}") from exc
    return events


def append_event(event: dict[str, Any], root: Path | None = None) -> None:
    root = root or repo_root()
    path = journal_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    events = read_journal(root)
    event = dict(event)
    event["schema_version"] = SCHEMA_VERSION
    event["event_seq"] = len(events) + 1
    event.setdefault("event_time_utc", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def write_snapshot(snapshot: dict[str, Any], root: Path | None = None) -> Path:
    """Atomic snapshot write (temp file + replace; never half-written)."""
    root = root or repo_root()
    path = snapshot_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def logical_attempt_number(
    events: list[dict[str, Any]], unit_key: str, final_request_id: str
) -> int:
    """Number logical starts durably observed for this exact unit/request plus one."""
    prior = sum(
        1
        for event in events
        if event.get("event_type") == "unit_retrieve_started"
        and event.get("unit_key") == unit_key
        and event.get("final_request_id") == final_request_id
    )
    return prior + 1


def reconcile_progress(plan: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Derive current progress from (plan + journal + raw store + derived
    store), NEVER from a stale snapshot ordinal alone.

    Authority: raw store accepted artifacts win over a stale pending journal
    entry; validated derived artifacts win over stale derived journal entries.
    If the journal claims accepted but the raw store is missing / SHA-mismatched,
    that unit is FAIL CLOSED (never pretend success).  A journal
    ``unit_derived_completed`` without a validated derived artifact is also
    FAIL CLOSED (derived completeness is never journal-alone).
    """
    root = root or repo_root()
    classification = classify_units(plan, root)
    derived_cls = classify_derived_units(plan, root, raw_classification=classification)
    derived_complete_keys = {u["unit_key"] for u in derived_cls["complete"]}
    events = read_journal(root)
    accepted_keys = {u["unit_key"] for u in classification["accepted"]}
    journal_events = {e["unit_key"]: e for e in events if e.get("unit_key")}
    blocked: list[str] = []
    for unit_key, ev in journal_events.items():
        if ev.get("event_type") == "unit_raw_accepted" and unit_key not in accepted_keys:
            blocked.append(unit_key)
        if ev.get("event_type") == "unit_derived_completed" and unit_key not in derived_complete_keys:
            blocked.append(unit_key)
        if ev.get("event_type") == "unit_validation_failure":
            blocked.append(unit_key)
        if ev.get("event_type") == "unit_contract_failure":
            blocked.append(unit_key)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "plan_hash": plan["global_backfill_plan_hash"],
        "authorization_candidate_hash": authorization_candidate_hash(root),
        "formal_units_total": plan["formal_unit_count"],
        "accepted_raw_units": len(classification["accepted"]),
        "derived_complete_units": len(derived_complete_keys),
        "derived_retryable_units": len(
            {
                e["unit_key"]
                for e in events
                if e.get("event_type") == "unit_derived_failure" and e.get("unit_key")
            }
            - derived_complete_keys
        ),
        "pending_units": len(classification["missing"]),
        "operational_retryable_units": len(
            [e for e in events if e.get("event_type") == "unit_operational_failure"]
        ),
        "blocked_units": sorted(set(blocked)),
        "current_year": _current_year_from_events(events),
        "last_event_seq": max((e.get("event_seq", 0) for e in events), default=0),
    }
    return {"snapshot": snapshot, "classification": classification, "derived": derived_cls, "events": events}


def _current_year_from_events(events: list[dict[str, Any]]) -> int | None:
    for ev in reversed(events):
        if ev.get("event_type") == "batch_started" and ev.get("batch_year"):
            return int(ev["batch_year"])
    return None


# ---------------------------------------------------------------------------
# Batch semantics (one baseline year per invocation, hard cap 32 slots)
# ---------------------------------------------------------------------------


def year_units(plan: dict[str, Any], year: int) -> list[dict[str, Any]]:
    units = [u for u in plan["units"] if u["year"] == year]
    if len(units) != 32:
        raise V2Error(f"year {year} has {len(units)} formal slots != 32")
    return units


def engine_unit(plan_unit: dict[str, Any]) -> dict[str, Any]:
    """Map a canonical plan row to the engine unit dict (start/end as date)."""
    return {
        "market_id": plan_unit["market_id"],
        "period": plan_unit["period"],
        "start": date.fromisoformat(plan_unit["local_start"]),
        "end": date.fromisoformat(plan_unit["local_end"]),
    }


def dry_run_year(plan: dict[str, Any], year: int, root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    units = year_units(plan, year)
    classification = classify_units(plan, root)
    accepted_keys = {u["unit_key"] for u in classification["accepted"]}
    existing_skips = [u for u in units if u["unit_key"] in accepted_keys]
    missing_units = [u for u in units if u["unit_key"] not in accepted_keys]
    return {
        "year": year,
        "formal_slots": len(units),
        "existing_accepted_skip": len(existing_skips),
        "new_missing": len(missing_units),
        "skipped_unit_keys": [u["unit_key"] for u in existing_skips],
        "missing_unit_keys": [u["unit_key"] for u in missing_units],
    }


def run_year_batch(
    plan: dict[str, Any],
    year: int,
    root: Path | None = None,
    health_checker: Callable[[], dict[str, Any]] | None = None,
    client_factory: Callable[[], Any] | None = None,
    retrieve_calls_tracker: list[int] | None = None,
    derived_extractor: Callable[..., dict[str, Any]] | None = None,
    runtime_version_getter: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Execute AT MOST ONE baseline year (32 formal slots max).

    Authorization kill switch is enforced by the CALLER (the CLI); this
    function additionally re-verifies the authorization binding before any
    retrieve.

    ``health_checker`` is the dataset-specific runtime health callable.  It is
    REQUIRED for live batch execution (Stage 5E-3C-R1 fail-closed contract):
    a ``None`` checker MUST NOT be interpreted as healthy.  Tests inject fake
    checkers; the real CLI wires ``make_dataset_health_checker`` AFTER the
    authorization kill switch / binding checks, so no health HTTP happens when
    not authorized.

    ``derived_extractor`` is injectable for hermetic tests (Stage 5E-3C-R2);
    when None the controller uses the FROZEN scientific engine
    ``production_unit.extract_unit_exposures``.  It never re-implements the
    science.
    """
    root = root or repo_root()
    binding = verify_authorization_binding(root)
    if not binding["valid"]:
        raise V2Error("authorization binding invalid (hash mismatch); 0 network")
    if not binding["live_backfill_authorized"]:
        raise V2Error("live_backfill_authorized != true; authorization kill switch engaged")
    policy = load_controller_policy(root)
    runtime_check = verify_transport_runtime(policy, runtime_version_getter)
    if health_checker is None:
        # Stage 5E-3C-R1: no implicit healthy fallback.  A live batch without a
        # dataset-specific health checker is a wiring defect -> FAIL CLOSED.
        raise V2Error("dataset-specific health checker required (None not allowed for live batch)")
    invocation_id = str(uuid.uuid4())

    def emit(event: dict[str, Any]) -> None:
        enriched = dict(event)
        enriched["invocation_id"] = invocation_id
        append_event(enriched, root)

    effective_client_factory = client_factory or make_policy_driven_cds_client_factory(
        policy, version_getter=runtime_version_getter
    )
    emit({"event_type": "batch_started", "batch_year": year})
    units = year_units(plan, year)
    classification = classify_units(plan, root)
    accepted_keys = {u["unit_key"] for u in classification["accepted"]}
    tracker = retrieve_calls_tracker if retrieve_calls_tracker is not None else []
    results: list[dict[str, Any]] = []
    new_retrieves = 0

    def _health_ok() -> bool:
        health = health_checker()
        return health.get("dataset_available") is True

    # Sub-batch circuit breaker: health check BEFORE the batch and then every
    # 8 NEW retrieve attempts (spec 13).  A non-available target dataset stops
    # the batch BEFORE the next retrieve; recorded as operational_deferred.
    if not _health_ok():
        emit(
            {
                "event_type": "unit_service_deferred",
                "unit_key": None,
                "final_request_id": None,
                "batch_year": year,
                "service_status": "pre_batch_non_available",
            },
        )
        write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
        return {"year": year, "outcome": "batch_stopped_service_deferred", "results": results}

    for unit in units:
        emit(
            {
                "event_type": "unit_preflight",
                "unit_key": unit["unit_key"],
                "final_request_id": unit["final_request_id"],
                "batch_year": year,
            },
        )
        if unit["unit_key"] in accepted_keys:
            emit(
                {
                    "event_type": "unit_skip_accepted",
                    "unit_key": unit["unit_key"],
                    "final_request_id": unit["final_request_id"],
                    "batch_year": year,
                },
            )
            results.append({"unit_key": unit["unit_key"], "outcome": "skip_accepted", "retrieve_calls": 0})
            # Stage 5E-3C-R2: accepted raw SKIPS RETRIEVE, not the whole unit.
            # The unit still needs derived processing (from the existing raw).
            derived = _ensure_derived(unit, plan, root, batch_year=year,
                                      derived_extractor=derived_extractor,
                                      client_factory=effective_client_factory,
                                      retrieve_calls_tracker=tracker,
                                      invocation_id=invocation_id,
                                      policy=policy,
                                      transport_versions=runtime_check["versions"])
            if derived["outcome"] != "derived_complete":
                return {"year": year, "outcome": derived["outcome"], "results": results, "error": derived.get("error")}
            results.append({"unit_key": unit["unit_key"], "outcome": "derived_complete", "retrieve_calls": 0})
            continue
        if new_retrieves > 0 and new_retrieves % 8 == 0 and not _health_ok():
            emit(
                {
                    "event_type": "unit_service_deferred",
                    "unit_key": unit["unit_key"],
                    "final_request_id": unit["final_request_id"],
                    "batch_year": year,
                    "service_status": "post_retrieve_non_available",
                },
            )
            write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
            return {"year": year, "outcome": "batch_stopped_service_deferred", "results": results}
        attempt_number = logical_attempt_number(
            read_journal(root), unit["unit_key"], unit["final_request_id"]
        )
        emit(
            {
                "event_type": "unit_retrieve_started",
                "unit_key": unit["unit_key"],
                "final_request_id": unit["final_request_id"],
                "batch_year": year,
                "attempt_number": attempt_number,
            },
        )
        try:
            outcome = execute_unit_once(
                engine_unit(unit), root=root, client_factory=effective_client_factory,
                retrieve_calls_tracker=tracker
            )
        except Exception as exc:  # noqa: BLE001 - operational failure classification
            classification = _classify_failure(exc)
            emit(
                {
                    "event_type": classification["event_type"],
                    "unit_key": unit["unit_key"],
                    "final_request_id": unit["final_request_id"],
                    "batch_year": year,
                    "failure_class": classification["failure_class"],
                    "attempt_number": attempt_number,
                    "error_record": build_error_record(
                        exc, policy, runtime_check["versions"], classification
                    ),
                },
            )
            write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
            suffix = classification["event_type"].replace("unit_", "")
            return {
                "year": year,
                "outcome": "batch_stopped_" + suffix,
                "results": results,
                "error": sanitize_error_message(exc),
            }
        new_retrieves += 1
        emit(
            {
                "event_type": "unit_raw_accepted",
                "unit_key": unit["unit_key"],
                "final_request_id": unit["final_request_id"],
                "batch_year": year,
                "artifact_id": outcome.get("artifact_id"),
                "raw_sha": outcome.get("sha256"),
            },
        )
        results.append({"unit_key": unit["unit_key"], "outcome": "raw_accepted", "retrieve_calls": outcome["retrieve_calls"]})
        # Stage 5E-3C-R2: raw accepted -> derived processing -> progress.
        derived = _ensure_derived(unit, plan, root, batch_year=year,
                                  derived_extractor=derived_extractor,
                                  client_factory=effective_client_factory,
                                  retrieve_calls_tracker=tracker,
                                  invocation_id=invocation_id,
                                  policy=policy,
                                  transport_versions=runtime_check["versions"])
        if derived["outcome"] != "derived_complete":
            return {"year": year, "outcome": derived["outcome"], "results": results, "error": derived.get("error")}
        results.append({"unit_key": unit["unit_key"], "outcome": "derived_complete", "retrieve_calls": 0})

    # Stage 5E-3C-R2 batch completion predicate (spec 15): batch_completed is
    # emitted ONLY after the full year is raw+derived complete with exact rows.
    completion = _year_completion_check(plan, year, root)
    if not completion["complete"]:
        write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
        return {"year": year, "outcome": "batch_stopped_incomplete", "results": results, "completion": completion}
    emit({"event_type": "batch_completed", "batch_year": year})
    write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
    return {
        "year": year,
        "outcome": "batch_completed",
        "results": results,
        "new_retrieves": new_retrieves,
        "completion": completion,
    }


def _ensure_derived(
    plan_unit: dict[str, Any],
    plan: dict[str, Any],
    root: Path | None = None,
    batch_year: int | None = None,
    derived_extractor: Callable[..., dict[str, Any]] | None = None,
    client_factory: Callable[[], Any] | None = None,
    retrieve_calls_tracker: list[int] | None = None,
    invocation_id: str | None = None,
    policy: dict[str, Any] | None = None,
    transport_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Ensure a unit's derived exposure artifact exists and is valid.

    Returns ``derived_complete`` on success.  On extraction / validation /
    persistence failure, records ``unit_derived_failure`` (raw stays accepted,
    no re-download) and stops the batch.
    """
    root = root or repo_root()
    engine = engine_unit(plan_unit)
    request_id = plan_unit["final_request_id"]

    def emit(event: dict[str, Any]) -> None:
        enriched = dict(event)
        if invocation_id is not None:
            enriched["invocation_id"] = invocation_id
        append_event(enriched, root)

    def error_record(exc: BaseException) -> dict[str, Any]:
        effective_policy = policy or load_controller_policy(root)
        effective_versions = transport_versions or runtime_transport_versions()
        classification = {
            "error_category": "derived_processing_failure",
            "phase": "payload_validation" if isinstance(exc, PayloadValidationError) else "unknown",
        }
        return build_error_record(exc, effective_policy, effective_versions, classification)

    # Reuse the raw acceptance lookup to get the accepted raw SHA.  In the
    # REAL path (derived_extractor is None -> frozen extract_unit_exposures)
    # an accepted raw MUST exist in the store.  In hermetic TEST mode a fake
    # extractor may supply raw_sha256 itself (fake execute did not persist).
    store = _raw_store(root)
    expected_raw_sha: str | None = None
    try:
        lookup = lookup_accepted_request(store, request_id)
        if lookup["found"]:
            expected_raw_sha = lookup["sha256"]
    except V2Error:
        expected_raw_sha = None

    # Valid derived artifact already exists -> idempotent skip (no re-extract).
    try:
        existing = load_derived_artifact(plan_unit, root)
    except V2Error:
        existing = None
    if existing is not None:
        if expected_raw_sha is None:
            raise V2Error(f"no accepted raw for derived processing: {plan_unit['unit_key']}")
        problems = validate_derived_artifact(plan_unit, existing, expected_raw_sha, root)
        if not problems:
            emit(
                {
                    "event_type": "unit_derived_completed",
                    "unit_key": plan_unit["unit_key"],
                    "final_request_id": request_id,
                    "batch_year": batch_year,
                    "raw_sha": expected_raw_sha,
                    "derived_already_valid": True,
                },
            )
            return {"outcome": "derived_complete", "derived_already_valid": True}

    # Stage 5E-3C-R2: run the FROZEN scientific extractor (never a second
    # implementation).  ``derived_extractor`` is injectable for hermetic tests;
    # the real controller uses production_unit.extract_unit_exposures.
    extractor = derived_extractor or extract_unit_exposures
    try:
        extract_out = extractor(engine, root=root)
    except Exception as exc:  # noqa: BLE001 - classified as derived failure
        emit(
            {
                "event_type": "unit_derived_failure",
                "unit_key": plan_unit["unit_key"],
                "final_request_id": request_id,
                "batch_year": batch_year,
                "raw_sha": expected_raw_sha,
                "failure_class": FAILURE_CONTRACT["derived_only"],
                "error_record": error_record(exc),
            },
        )
        write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
        return {"outcome": "batch_stopped_derived_failure", "error": str(exc)}

    if expected_raw_sha is None:
        # Hermetic test mode: fall back to the extractor-provided raw SHA.
        expected_raw_sha = extract_out.get("raw_sha256")
    if not expected_raw_sha:
        raise V2Error(f"no accepted raw for derived processing: {plan_unit['unit_key']}")

    artifact = build_derived_artifact(plan_unit, extract_out)
    problems = validate_derived_artifact(plan_unit, artifact, expected_raw_sha, root)
    if problems:
        validation_exc = PayloadValidationError("; ".join(problems))
        emit(
            {
                "event_type": "unit_derived_failure",
                "unit_key": plan_unit["unit_key"],
                "final_request_id": request_id,
                "batch_year": batch_year,
                "raw_sha": expected_raw_sha,
                "failure_class": FAILURE_CONTRACT["derived_only"],
                "error_record": error_record(validation_exc),
            },
        )
        write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
        return {"outcome": "batch_stopped_derived_failure", "error": "; ".join(problems)}

    try:
        persist_derived_atomic(plan_unit, artifact, expected_raw_sha, root)
    except V2Error as exc:
        emit(
            {
                "event_type": "unit_derived_failure",
                "unit_key": plan_unit["unit_key"],
                "final_request_id": request_id,
                "batch_year": batch_year,
                "raw_sha": expected_raw_sha,
                "failure_class": FAILURE_CONTRACT["derived_only"],
                "error_record": error_record(exc),
            },
        )
        write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
        return {"outcome": "batch_stopped_derived_failure", "error": str(exc)}

    emit(
        {
            "event_type": "unit_derived_completed",
            "unit_key": plan_unit["unit_key"],
            "final_request_id": request_id,
            "batch_year": batch_year,
            "raw_sha": expected_raw_sha,
        },
    )
    # The derived artifact and append-only event above are the durable
    # per-unit authorities.  Rebuilding the convenience snapshot here would
    # rescan and revalidate all 960 formal units after every successful unit,
    # making a one-year batch quadratic in completed artifacts.  The batch
    # completion path rebuilds the snapshot once; failure paths still rebuild
    # it immediately before stopping.
    return {"outcome": "derived_complete", "derived_already_valid": False}


def _year_completion_check(plan: dict[str, Any], year: int, root: Path | None = None) -> dict[str, Any]:
    """Batch completion predicate: 32 raw + 32 derived + exact scientific rows.

    Only when ALL hold may ``batch_completed`` be emitted (spec 15).
    """
    root = root or repo_root()
    year_plan_units = [u for u in plan["units"] if u["year"] == year]
    raw_cls = classify_units(plan, root)
    derived_cls = classify_derived_units(plan, root, raw_classification=raw_cls)
    raw_by_key = {u["unit_key"]: u for u in raw_cls["accepted"]}
    derived_by_key = {u["unit_key"]: u for u in derived_cls["complete"]}

    raw_ok = all(u["unit_key"] in raw_by_key for u in year_plan_units)
    derived_ok = all(u["unit_key"] in derived_by_key for u in year_plan_units)
    total_rows = 0
    missing_rows = 0
    consumed_extras = 0
    rows_ok = True
    for unit in year_plan_units:
        if unit["unit_key"] not in derived_by_key:
            rows_ok = False
            continue
        artifact = load_derived_artifact(unit, root)
        total_rows += artifact["row_count"]
        missing_rows += artifact["missing_count"]
        consumed_extras += artifact["firewall"]["consumed_extras"]
    expected_rows = 8 * 365  # 2920
    rows_ok = rows_ok and total_rows == expected_rows and missing_rows == 0 and consumed_extras == 0
    complete = raw_ok and derived_ok and rows_ok
    return {
        "complete": complete,
        "year": year,
        "formal_units": len(year_plan_units),
        "raw_accepted_year": len([u for u in year_plan_units if u["unit_key"] in raw_by_key]),
        "derived_complete_year": len([u for u in year_plan_units if u["unit_key"] in derived_by_key]),
        "expected_rows": expected_rows,
        "actual_rows": total_rows,
        "missing_rows": missing_rows,
        "consumed_extras": consumed_extras,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 5E-3C full backfill controller (ZERO network in 3C)")
    parser.add_argument("--year", type=int, help="single baseline year to inspect/run (one year max per invocation)")
    parser.add_argument("--status", action="store_true", help="print derived progress snapshot (offline)")
    parser.add_argument("--dry-run", action="store_true", help="print 32-slot batch plan for a year (offline)")
    parser.add_argument("--resume-year", type=int, help="resume a baseline year batch (same one-year bound)")
    parser.add_argument("--retry-unit", type=str, help="explicit retry of ONE retryable_operational unit")
    parser.add_argument("--live", action="store_true", help="run one baseline-year batch (kill-switch gated)")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root
    plan = load_plan(root)

    if args.status:
        state = reconcile_progress(plan, root)
        print(json.dumps(state["snapshot"], ensure_ascii=False, indent=2))
        return 0
    if args.dry_run:
        if args.year is None:
            parser.error("--dry-run requires --year YYYY")
        print(json.dumps(dry_run_year(plan, args.year, root), ensure_ascii=False, indent=2))
        return 0
    if args.retry_unit:
        auth = load_authorization(root)
        print(json.dumps(
            {
                "retry_unit": args.retry_unit,
                "allowed": auth.get("live_backfill_authorized") is True,
                "note": "explicit retry only for retryable_operational units; validation/contract blocked units "
                "require control-layer resolution",
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    if args.live:
        year = args.year or args.resume_year
        if year is None:
            parser.error("--live requires --year YYYY (one baseline year max)")
        if year < 1991 or year > 2020:
            parser.error("--year must be within 1991..2020")
        auth = load_authorization(root)
        if auth.get("live_backfill_authorized") is not True:
            print(json.dumps(
                {
                    "error": "live_backfill_authorized != true; authorization kill switch engaged",
                    "cds_retrieves": 0,
                    "service_health_fetches": 0,
                    "network": 0,
                },
                ensure_ascii=False,
                indent=2,
            ))
            return 1
        binding = verify_authorization_binding(root)
        if not binding["valid"]:
            print(json.dumps(
                {"error": "authorization binding invalid (hash mismatch); 0 network", "checks": binding["checks"]},
                ensure_ascii=False,
                indent=2,
            ))
            return 1
        policy = load_controller_policy(root)
        try:
            verify_transport_runtime(policy)
        except V2Error as exc:
            print(json.dumps({"error": str(exc), "network": 0}, ensure_ascii=False, indent=2))
            return 1
        # Only after kill switch + binding + runtime-version checks do we
        # construct network-capable callables.  The CDS factory itself remains
        # lazy, so accepted raw is skipped before cdsapi.Client construction.
        client_factory = make_policy_driven_cds_client_factory(policy)
        # Stage 5E-3C-R1: ONLY after kill switch + binding pass do we construct
        # the real dataset-specific health checker (construction is network-free;
        # the first fetch happens inside run_year_batch BEFORE the first
        # retrieve).  Kill switch false => health client never constructed.
        health_checker = make_dataset_health_checker(dataset_id=DEFAULT_DATASET_ID)
        print(json.dumps(
            run_year_batch(
                plan, year, root, health_checker=health_checker,
                client_factory=client_factory,
            ),
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    parser.error("require --status, --dry-run --year, --retry-unit, or --live --year")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
