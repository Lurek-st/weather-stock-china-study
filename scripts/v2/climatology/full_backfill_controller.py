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
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.production_canary import (
    EXPECTED_TIMEZONE_CANARY_HASH,
    REQUEST_IDENTITY_CONTRACT_VERSION,
    load_global_registry_hash,
)
from scripts.v2.climatology.production_quarter import assert_stage5e3b_authorized
from scripts.v2.climatology.production_unit import RAW_BASE, RawArtifactStore, execute_unit_once, extract_unit_exposures
from scripts.v2.core import V2Error, load_json, load_yaml, repo_root

PLAN_PATH = "data/audits/v2/climatology/full-era5-backfill-plan-1991-2020.json"
CONTROLLER_POLICY_PATH = "config/v2/full-backfill-controller.yaml"
AUTHORIZATION_PATH = "config/v2/full-backfill-authorization.yaml"
STATE_DIR = "data/state/v2/climatology/full-backfill"
JOURNAL_PATH = "data/state/v2/climatology/full-backfill/progress.events.jsonl"
SNAPSHOT_PATH = "data/state/v2/climatology/full-backfill/progress.snapshot.json"
SCHEMA_VERSION = "1.0.0"

# Failure classification (frozen Stage 5E-3C contract).
FAILURE_CONTRACT = {
    "pre_network_contract": "non_retryable_until_control_review",
    "service_health_defer": "operational_deferred",
    "operational_retrieve": "retryable_operational",
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
    if policy.get("controller_schema_version") != "1.0.0":
        raise V2Error("unexpected controller policy schema version")
    return policy


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


def classify_units(plan: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Classify every formal unit against the RawArtifactStore.

    accepted predicate reuses the request-aware lookup (direct manifest OR
    append-only request-binding sidecar).  A broken/missing accepted artifact
    counts as ``invalid`` and FAILS the gate.
    """
    root = root or repo_root()
    store = RawArtifactStore(root / RAW_BASE)
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


def reconcile_progress(plan: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Derive current progress from (plan + journal + raw store), NEVER from a
    stale snapshot ordinal alone.

    Authority: raw store accepted artifacts win over a stale pending journal
    entry.  If the journal claims accepted but the raw store is missing /
    SHA-mismatched, that unit is FAIL CLOSED (never pretend success).
    """
    root = root or repo_root()
    classification = classify_units(plan, root)
    events = read_journal(root)
    accepted_keys = {u["unit_key"] for u in classification["accepted"]}
    journal_events = {e["unit_key"]: e for e in events if e.get("unit_key")}
    blocked: list[str] = []
    for unit_key, ev in journal_events.items():
        if ev.get("event_type") == "unit_raw_accepted" and unit_key not in accepted_keys:
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
        "derived_complete_units": len(
            [e for e in events if e.get("event_type") == "unit_derived_completed" and e.get("unit_key") in accepted_keys]
        ),
        "pending_units": len(classification["missing"]),
        "operational_retryable_units": len(
            [e for e in events if e.get("event_type") == "unit_operational_failure"]
        ),
        "blocked_units": sorted(set(blocked)),
        "current_year": _current_year_from_events(events),
        "last_event_seq": max((e.get("event_seq", 0) for e in events), default=0),
    }
    return {"snapshot": snapshot, "classification": classification, "events": events}


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
) -> dict[str, Any]:
    """Execute AT MOST ONE baseline year (32 formal slots max).

    Authorization kill switch is enforced by the CALLER (the CLI); this
    function additionally re-verifies the authorization binding before any
    retrieve.  ``health_checker`` is injected for tests (Stage 5E-3C itself
    performs no HTTP).
    """
    root = root or repo_root()
    binding = verify_authorization_binding(root)
    if not binding["valid"]:
        raise V2Error("authorization binding invalid (hash mismatch); 0 network")
    if not binding["live_backfill_authorized"]:
        raise V2Error("live_backfill_authorized != true; authorization kill switch engaged")
    append_event({"event_type": "batch_started", "batch_year": year}, root)
    units = year_units(plan, year)
    classification = classify_units(plan, root)
    accepted_keys = {u["unit_key"] for u in classification["accepted"]}
    tracker = retrieve_calls_tracker if retrieve_calls_tracker is not None else []
    results: list[dict[str, Any]] = []
    new_retrieves = 0

    def _health_ok() -> bool:
        if health_checker is None:
            return True
        health = health_checker()
        return health.get("dataset_available") is True

    # Sub-batch circuit breaker: health check BEFORE the batch and then every
    # 8 NEW retrieve attempts (spec 13).  A non-available target dataset stops
    # the batch BEFORE the next retrieve; recorded as operational_deferred.
    if not _health_ok():
        append_event(
            {
                "event_type": "unit_service_deferred",
                "unit_key": None,
                "final_request_id": None,
                "batch_year": year,
                "service_status": "pre_batch_non_available",
            },
            root,
        )
        write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
        return {"year": year, "outcome": "batch_stopped_service_deferred", "results": results}

    for unit in units:
        append_event(
            {
                "event_type": "unit_preflight",
                "unit_key": unit["unit_key"],
                "final_request_id": unit["final_request_id"],
                "batch_year": year,
            },
            root,
        )
        if unit["unit_key"] in accepted_keys:
            append_event(
                {
                    "event_type": "unit_skip_accepted",
                    "unit_key": unit["unit_key"],
                    "final_request_id": unit["final_request_id"],
                    "batch_year": year,
                },
                root,
            )
            results.append({"unit_key": unit["unit_key"], "outcome": "skip_accepted", "retrieve_calls": 0})
            continue
        if new_retrieves > 0 and new_retrieves % 8 == 0 and not _health_ok():
            append_event(
                {
                    "event_type": "unit_service_deferred",
                    "unit_key": unit["unit_key"],
                    "final_request_id": unit["final_request_id"],
                    "batch_year": year,
                    "service_status": "post_retrieve_non_available",
                },
                root,
            )
            write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
            return {"year": year, "outcome": "batch_stopped_service_deferred", "results": results}
        append_event(
            {
                "event_type": "unit_retrieve_started",
                "unit_key": unit["unit_key"],
                "final_request_id": unit["final_request_id"],
                "batch_year": year,
            },
            root,
        )
        try:
            outcome = execute_unit_once(
                engine_unit(unit), root=root, client_factory=client_factory, retrieve_calls_tracker=tracker
            )
        except V2Error as exc:
            # Classification: V2Error from payload/container validation is a
            # payload validation failure; other V2Error is a request/contract
            # rejection.  Neither is automatically retried.
            msg = str(exc)
            if any(token in msg for token in ("container", "corrupt", "timestamp", "grid", "variable", "validation")):
                event_type, failure_class = "unit_validation_failure", FAILURE_CONTRACT["payload_validation"]
            else:
                event_type, failure_class = "unit_contract_failure", FAILURE_CONTRACT["request_contract_rejection"]
            append_event(
                {
                    "event_type": event_type,
                    "unit_key": unit["unit_key"],
                    "final_request_id": unit["final_request_id"],
                    "batch_year": year,
                    "failure_class": failure_class,
                    "attempt_number": 1,
                },
                root,
            )
            write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
            return {"year": year, "outcome": "batch_stopped_" + event_type.replace("unit_", ""), "results": results}
        except Exception as exc:  # noqa: BLE001 - operational failure classification
            append_event(
                {
                    "event_type": "unit_operational_failure",
                    "unit_key": unit["unit_key"],
                    "final_request_id": unit["final_request_id"],
                    "batch_year": year,
                    "failure_class": FAILURE_CONTRACT["operational_retrieve"],
                    "attempt_number": 1,
                },
                root,
            )
            write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
            return {"year": year, "outcome": "batch_stopped_operational_failure", "results": results, "error": str(exc)}
        new_retrieves += 1
        append_event(
            {
                "event_type": "unit_raw_accepted",
                "unit_key": unit["unit_key"],
                "final_request_id": unit["final_request_id"],
                "batch_year": year,
                "artifact_id": outcome.get("artifact_id"),
                "raw_sha": outcome.get("sha256"),
            },
            root,
        )
        results.append({"unit_key": unit["unit_key"], "outcome": "raw_accepted", "retrieve_calls": outcome["retrieve_calls"]})
    append_event({"event_type": "batch_completed", "batch_year": year}, root)
    write_snapshot(reconcile_progress(plan, root)["snapshot"], root)
    return {"year": year, "outcome": "batch_completed", "results": results, "new_retrieves": new_retrieves}


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
        print(json.dumps(run_year_batch(plan, year, root), ensure_ascii=False, indent=2))
        return 0
    parser.error("require --status, --dry-run --year, --retry-unit, or --live --year")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
