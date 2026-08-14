"""Stage 5B-3R1: timezone provider contract audit builder (no network).

Verifies the frozen tzdata provider contract against the runtime, the
dependency manifest, and the historical-timezone qualification audit's
fingerprint, then writes a small reproducibility audit.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import SCHEMA_VERSION, load_json, repo_root, sha256_file, write_json
from scripts.v2.timezone_provider import check_provider_runtime, load_provider_contract

AUDIT_PATH = "data/audits/v2/time/timezone-provider-contract.json"
HISTORICAL_AUDIT = "data/audits/v2/time/historical-timezone-canary-1991-2020.json"


def _requirements_binding(root: Path, provider: str, version: str) -> bool:
    req = (root / "requirements.txt").read_text(encoding="utf-8")
    line = f"{provider}=={version}"
    return line in req


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the timezone provider contract audit")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    contract = load_provider_contract(root)
    check = check_provider_runtime(root)
    historical = load_json(root / HISTORICAL_AUDIT)
    canary_hash = historical["fingerprint"]["timezone_canary_hash"]

    binding = _requirements_binding(root, contract["provider"], contract["version"])

    runtime_match = check["exact_match"]
    provider_type_match = check["provider"] == "tzdata"
    version_exact_match = check["actual_version"] == contract["version"]
    fingerprint_present = bool(canary_hash)

    research_usable = (
        provider_type_match
        and version_exact_match
        and runtime_match
        and fingerprint_present
        and binding
    )

    audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "timezone_provider_contract",
        "provider": contract["provider"],
        "exact_version": contract["version"],
        "qualification_audit": contract["qualification_audit"],
        "timezone_canary_hash": canary_hash,
        "runtime": {
            "actual_version": check["actual_version"],
            "runtime_match": runtime_match,
            "provider_type_match": provider_type_match,
            "version_exact_match": version_exact_match,
        },
        "dependency_manifest_binding": {
            "requirements_has_exact_pin": binding,
            "pinned_line": f"{contract['provider']}=={contract['version']}",
        },
        "fingerprint_binding_present": fingerprint_present,
        "research_usable": research_usable,
        "no_network_attestation": {"cds_api_calls": 0, "era5_downloads": 0},
        "code_hashes": {
            "timezone_provider.py": sha256_file(root / "scripts/v2/timezone_provider.py"),
            "timezone-provider.yaml": sha256_file(root / "config/v2/timezone-provider.yaml"),
            "build_timezone_provider_contract_audit.py": sha256_file(root / "scripts/v2/time/build_timezone_provider_contract_audit.py"),
        },
        "reproducibility": "deterministic (byte-identical across repeated builds; pure functions, no network)",
        "historical_backfill_run": False,
    }
    write_json(root / AUDIT_PATH, audit)
    gate = research_usable
    print(json.dumps(
        {
            "gate": "PASS_STAGE5B3R1_TIMEZONE_PROVIDER_LOCK" if gate else "REVISE_STAGE5B3R1_TIMEZONE_PROVIDER_LOCK",
            "provider": contract["provider"],
            "exact_version": contract["version"],
            "runtime_match": runtime_match,
            "requirements_binding": binding,
            "canary_hash": canary_hash,
            "research_usable": research_usable,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
