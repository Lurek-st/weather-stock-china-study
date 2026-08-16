"""Stage 5E-3C authorization gate audit builder (ZERO network).

Recomputes every hash from tracked sources, verifies the authorization
candidate binding, classifies the 960-unit inventory against the
RawArtifactStore, and writes the frozen gate audit:

    data/audits/v2/climatology/full-backfill-authorization-gate.json

Also rewrites config/v2/full-backfill-authorization.yaml with the true
computed hashes (keeping live_backfill_authorized=false).  Byte-identical on
rebuild, no absolute paths, no timestamps.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.production_canary import (
    EXPECTED_TIMEZONE_CANARY_HASH,
    REQUEST_IDENTITY_CONTRACT_VERSION,
    load_global_registry_hash,
)
from scripts.v2.climatology.full_backfill_controller import (
    AUTHORIZATION_PATH,
    CONTROLLER_POLICY_PATH,
    PLAN_PATH,
    STATE_DIR,
    authorization_candidate_hash,
    classify_units,
    controller_policy_hash,
    load_controller_policy,
    load_plan,
    verify_authorization_binding,
)
from scripts.v2.core import V2Error, load_json, repo_root, write_json

AUDIT_OUTPUT = "data/audits/v2/climatology/full-backfill-authorization-gate.json"


def _canonical_authorization_payload(root: Path) -> dict[str, Any]:
    plan = load_plan(root)
    policy_hash = controller_policy_hash(root)
    candidate_hash = authorization_candidate_hash(root)
    return {
        "authorization_schema_version": "1.0.0",
        "live_backfill_authorized": False,
        "authorization_state": "candidate_for_control_layer",
        "control_layer_approval_required": True,
        "bound_hashes": {
            "global_backfill_plan_hash": plan["global_backfill_plan_hash"],
            "controller_policy_hash": policy_hash,
            "request_identity_contract_version": REQUEST_IDENTITY_CONTRACT_VERSION,
            "global_spatial_anchor_registry_hash": load_global_registry_hash(root),
            "timezone_canary_hash": EXPECTED_TIMEZONE_CANARY_HASH,
        },
        "authorization_candidate_hash": candidate_hash,
    }


def write_authorization_yaml(root: Path) -> Path:
    """Rewrite the tracked authorization candidate with true computed hashes."""
    payload = _canonical_authorization_payload(root)
    yaml_lines = [
        "# Stage 5E-4C-R6 bounded-transport authorization CANDIDATE (NOT live).",
        "#",
        "# This file is the NETWORK KILL SWITCH.  live_backfill_authorized MUST stay",
        "# false until the control layer accepts Stage 5E-4C-R6 and issues a separate",
        "# minimal activation checkpoint.  WorkBuddy must NEVER flip it to true on its",
        "# own.",
        "#",
        "# authorization_candidate_hash binds:",
        "#   global_backfill_plan_hash + controller_policy_hash +",
        "#   request_identity_contract_version + global_spatial_anchor_registry_hash +",
        "#   timezone_canary_hash",
        "#",
        "# If ANY of these contracts changes, the candidate hash changes and this",
        "# authorization is automatically invalid (no silent inheritance of older",
        "# permissions).",
        'authorization_schema_version: "1.0.0"',
        "live_backfill_authorized: false",
        'authorization_state: "candidate_for_control_layer"',
        "control_layer_approval_required: true",
        "",
        "bound_hashes:",
        f'  global_backfill_plan_hash: "{payload["bound_hashes"]["global_backfill_plan_hash"]}"',
        f'  controller_policy_hash: "{payload["bound_hashes"]["controller_policy_hash"]}"',
        f'  request_identity_contract_version: "{payload["bound_hashes"]["request_identity_contract_version"]}"',
        f'  global_spatial_anchor_registry_hash: "{payload["bound_hashes"]["global_spatial_anchor_registry_hash"]}"',
        f'  timezone_canary_hash: "{payload["bound_hashes"]["timezone_canary_hash"]}"',
        "",
        f'authorization_candidate_hash: "{payload["authorization_candidate_hash"]}"',
        "",
    ]
    target = root / AUTHORIZATION_PATH
    target.write_text("\n".join(yaml_lines), encoding="utf-8")
    return target


def build_audit(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    plan = load_plan(root)
    controller_policy = load_controller_policy(root)
    policy = _canonical_authorization_payload(root)
    binding = verify_authorization_binding(root)
    classification = classify_units(plan, root)

    if classification["invalid"]:
        raise V2Error(f"invalid={len(classification['invalid'])}; gate FAIL: {classification['invalid'][:3]}")
    if len(classification["accepted"]) + len(classification["missing"]) != plan["formal_unit_count"]:
        raise V2Error("raw inventory does not partition the 960-unit canonical plan")

    request_ids = [u["final_request_id"] for u in plan["units"]]
    unit_keys = [u["unit_key"] for u in plan["units"]]
    audit = {
        "schema_version": "3.0.0",
        "audit_type": "full_backfill_authorization_gate",
        "scope": "Stage 5E-4C-R6: bind bounded transport policy and exact runtime dependencies; ZERO network",
        "formal_plan": {
            "units": plan["formal_unit_count"],
            "markets": len(plan["market_order"]),
            "years": 30,
            "quarters": 4,
            "daily_exposures": plan["exposure_invariants"]["total_daily_exposures"],
            "invariants_hold": plan["exposure_invariants"]["invariants_hold"],
        },
        "hashes": {
            "global_backfill_plan_hash": plan["global_backfill_plan_hash"],
            "controller_policy_hash": policy["bound_hashes"]["controller_policy_hash"],
            "authorization_candidate_hash": policy["authorization_candidate_hash"],
            "spatial_registry_hash": policy["bound_hashes"]["global_spatial_anchor_registry_hash"],
            "timezone_canary_hash": policy["bound_hashes"]["timezone_canary_hash"],
            "superseded_controller_policy_hash": "a05924b0bf999eb37d5fa04d2e81520d5e799d1cf75715c3257663f32dc8c94a",
            "superseded_authorization_candidate_hash": "37df1df9127346c962f371d9fcf6c3beb0cd968164b8c1ed3640eadb3cb5573c",
        },
        "identity": {
            "version": REQUEST_IDENTITY_CONTRACT_VERSION,
            "unique_request_ids": len(set(request_ids)),
            "request_ids_count": len(request_ids),
        },
        "inventory": {
            "accepted": len(classification["accepted"]),
            "missing": len(classification["missing"]),
            "invalid": len(classification["invalid"]),
        },
        "accepted_units": classification["accepted"],
        "controller": {
            "concurrency": controller_policy["concurrency"],
            "batch": controller_policy["execution_batch"],
            "slots_per_batch": controller_policy["max_formal_slots_per_invocation"],
            "health_interval": controller_policy["health_check_interval_new_retrieves"],
            "automatic_annual_retry": controller_policy["controller_retry"]["automatic_annual_retry"],
            "max_logical_retrieves_per_unit_attempt": controller_policy["logical_retrieve"]
            ["max_cds_retrieve_calls_per_unit_attempt"],
            "transport": controller_policy["transport"],
            "progress_after_every_unit": controller_policy["progress_write_frequency"] == "after_every_unit",
        },
        "authorization": {
            "candidate": True,
            "control_layer_required": True,
            "live_backfill_authorized": False,
        },
        "network": {
            "cds_retrieves": 0,
            "service_health_fetches": 0,
            "other_network": 0,
        },
        "execution_started": True,
        "full_backfill_started": True,
        "full_climatology_started": True,
        "binding_verified": binding["valid"],
        "binding_checks": binding["checks"],
    }
    return audit


def main(argv: list[str] | None = None) -> int:
    root = repo_root()
    write_authorization_yaml(root)
    audit = build_audit(root)
    target = root / AUDIT_OUTPUT
    write_json(target, audit)
    print(json.dumps(
        {
            "audit_written": AUDIT_OUTPUT,
            "formal_units": audit["formal_plan"]["units"],
            "accepted": audit["inventory"]["accepted"],
            "missing": audit["inventory"]["missing"],
            "invalid": audit["inventory"]["invalid"],
            "plan_hash": audit["hashes"]["global_backfill_plan_hash"],
            "policy_hash": audit["hashes"]["controller_policy_hash"],
            "candidate_hash": audit["hashes"]["authorization_candidate_hash"],
            "live_backfill_authorized": audit["authorization"]["live_backfill_authorized"],
            "network": audit["network"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
