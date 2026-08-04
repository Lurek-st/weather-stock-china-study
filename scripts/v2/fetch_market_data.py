from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import RawArtifactStore, load_yaml, repo_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Import registered structured market source artifacts.")
    parser.add_argument("--market", required=True)
    parser.add_argument("--input", type=Path, help="Controlled official/vendor CSV export.")
    parser.add_argument("--source-id")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    registry = load_yaml(args.root / "config" / "v2" / "source-registry.yaml")["sources"]
    expected = [row for row in registry if args.market in row["source_id"]]
    if not expected:
        print(json.dumps({"market": args.market, "status": "unavailable", "reason": "no registered source"}))
        return 4
    source = next((row for row in expected if row["source_id"] == args.source_id), expected[0])
    report = {
        "market": args.market,
        "source_id": source["source_id"],
        "source_status": source["status"],
        "automation": source["automation"],
        "input_required": True,
    }
    if args.dry_run or not args.input:
        print(json.dumps(report, indent=2))
        return 0 if args.dry_run else 4
    payload = args.input.read_bytes()
    result = RawArtifactStore(args.root / "data" / "source_raw" / "v2" / "market").persist(
        source_id=source["source_id"],
        provider=source["provider"],
        logical_name=args.market,
        payload=payload,
        request={"controlled_input": args.input.name},
        status="provisional",
        licence=source["licence"],
        suffix=args.input.suffix.lower() or ".dat",
    )
    print(result.manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
