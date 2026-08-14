"""Stage 5E-3C-R1 runtime health wiring audit builder (ZERO network).

Records the implementation-compliance repair: the real CLI --live path now
wires the dataset-specific service-health checker; a None checker FAILS
CLOSED instead of being treated as healthy.  No scientific / policy change:
the three frozen hashes and the authorization candidate are untouched.

Output: data/audits/v2/climatology/full-backfill-runtime-health-wiring.json
Byte-identical on rebuild; no absolute paths; no timestamps.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.cds_service_health import (
    DEFAULT_DATASET_ID,
    HEALTH_COLLECTION_URL,
    make_dataset_health_checker,
)
from scripts.v2.climatology.full_backfill_controller import (
    AUTHORIZATION_PATH,
    CONTROLLER_POLICY_PATH,
    PLAN_PATH,
    authorization_candidate_hash,
    controller_policy_hash,
    load_plan,
)
from scripts.v2.core import repo_root, write_json

AUDIT_OUTPUT = "data/audits/v2/climatology/full-backfill-runtime-health-wiring.json"


def _source_paths(root: Path) -> dict[str, str]:
    """Relative source paths (no absolute paths in the audit)."""
    return {
        "plan": PLAN_PATH,
        "controller_policy": CONTROLLER_POLICY_PATH,
        "authorization": AUTHORIZATION_PATH,
        "health_checker_module": "scripts/v2/climatology/cds_service_health.py",
        "controller_module": "scripts/v2/climatology/full_backfill_controller.py",
    }


def build_audit(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    plan = load_plan(root)
    policy_hash = controller_policy_hash(root)
    candidate_hash = authorization_candidate_hash(root)

    # Static wiring facts (no network executed at audit time).
    checker = make_dataset_health_checker  # factory reference; never invoked here

    audit = {
        "schema_version": "2.0.0",
        "audit_type": "full_backfill_runtime_health_wiring",
        "scope": "Stage 5E-3C-R1: wire dataset-specific service-health circuit breaker into the real CLI "
        "--live path; remove None=healthy fallback; implementation compliance repair (ZERO network)",
        "root_cause": "live_cli_health_checker_not_wired",
        "policy_changed": False,
        "plan_changed": False,
        "authorization_candidate_changed": False,
        "health_checker_runtime_wired": True,
        "none_checker_fail_closed": True,
        "dataset_specific_authority": "reanalysis-era5-single-levels structured sanity status",
        "banner_is_not_authority": True,
        "kill_switch_precedes_health_network": True,
        "health_precedes_cds_retrieve": True,
        "health_interval_new_retrieves": 8,
        "automatic_retry": False,
        "health_source": HEALTH_COLLECTION_URL,
        "target_dataset": DEFAULT_DATASET_ID,
        "source_paths": _source_paths(root),
        "hashes": {
            "global_backfill_plan_hash": plan["global_backfill_plan_hash"],
            "controller_policy_hash": policy_hash,
            "authorization_candidate_hash": candidate_hash,
        },
        "authorization": {
            "live_backfill_authorized": False,
            "authorization_state": "candidate_for_control_layer",
            "control_layer_approval_required": True,
        },
        "actual_network": {
            "cds_retrieve": 0,
            "service_health_fetch": 0,
            "other": 0,
        },
        "execution_started": False,
        "full_backfill_started": False,
    }
    return audit


def main(argv: list[str] | None = None) -> int:
    root = repo_root()
    audit = build_audit(root)
    target = root / AUDIT_OUTPUT
    write_json(target, audit)
    print(json.dumps(
        {
            "audit_written": AUDIT_OUTPUT,
            "root_cause": audit["root_cause"],
            "policy_changed": audit["policy_changed"],
            "health_checker_runtime_wired": audit["health_checker_runtime_wired"],
            "none_checker_fail_closed": audit["none_checker_fail_closed"],
            "live_backfill_authorized": audit["authorization"]["live_backfill_authorized"],
            "actual_network": audit["actual_network"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
