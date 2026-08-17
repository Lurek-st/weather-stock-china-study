from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import load_json, repo_root, validate_registry_files, write_json
from scripts.v2.validate_v2_data import validate_panel


def build_audit(root: Path, week: str, tier: str, maturity: dict) -> dict:
    errors = validate_registry_files(root) + validate_panel(root, tier)
    panel_manifest = load_json(root / "data" / "panel" / "v2" / tier / "panel_manifest.json")
    return {
        "schema_version": "2.0.0",
        "week": week,
        "tier": tier,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "maturity": maturity,
        "panel": panel_manifest,
        "validation": {"passed": not errors, "errors": errors},
        "causal_claims_permitted": False,
        "known_limitations": [
            "station observations are validation-only",
            "fixture execution does not validate live market licences or endpoints",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a machine-readable weekly V2 audit.")
    parser.add_argument("--week", required=True)
    parser.add_argument("--tier", choices=["provisional", "frozen"], required=True)
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    audit = build_audit(args.root, args.week, args.tier, {"status": "external_build"})
    target = args.root / "data" / "audits" / "v2" / f"{args.week}-{args.tier}.json"
    write_json(target, audit)
    print(json.dumps(audit, indent=2))
    return 0 if audit["validation"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
