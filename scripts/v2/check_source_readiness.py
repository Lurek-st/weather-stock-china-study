from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import load_yaml, repo_root, validate_registry_files


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate V2 registries and report source readiness.")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    errors = validate_registry_files(args.root)
    sources = load_yaml(args.root / "config" / "v2" / "source-registry.yaml")["sources"]
    cds_ready = (Path.home() / ".cdsapirc").exists() or bool(
        os.environ.get("CDSAPI_KEY")
    )
    result = {
        "valid": not errors,
        "errors": errors,
        "cds_credentials_present": cds_ready,
        "automated": [s["source_id"] for s in sources if s.get("automation") == "enabled"],
        "credential_gated": [
            s["source_id"] for s in sources if "credentials" in str(s.get("automation"))
        ],
        "manual_or_licence_blocked": [
            s["source_id"]
            for s in sources
            if str(s.get("status")).startswith("manual")
            or str(s.get("automation")).startswith("disabled")
        ],
    }
    print(json.dumps(result, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
