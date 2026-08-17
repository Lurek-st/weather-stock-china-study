from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import load_json, repo_root, sha256_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify every V2 raw artifact against its manifest.")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    base = args.root / "data" / "source_raw" / "v2"
    failures = []
    checked = 0
    for manifest_path in base.rglob("*.manifest.json"):
        manifest = load_json(manifest_path)
        prefix = manifest_path.name.replace(".manifest.json", "")
        candidates = [p for p in manifest_path.parent.glob(f"{prefix}.*") if p != manifest_path]
        checked += 1
        if len(candidates) != 1 or sha256_file(candidates[0]) != manifest["sha256"]:
            failures.append(str(manifest_path.relative_to(args.root)))
    print(json.dumps({"checked": checked, "failures": failures}, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
