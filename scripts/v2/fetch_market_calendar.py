from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import RawArtifactStore, repo_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Store official market-calendar evidence append-only.")
    parser.add_argument("--market", required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run or not args.input:
        print(json.dumps({"market": args.market, "source_id": args.source_id, "input_required": True}))
        return 0 if args.dry_run else 4
    result = RawArtifactStore(args.root / "data" / "source_raw" / "v2" / "calendars").persist(
        source_id=args.source_id,
        provider="registered exchange calendar",
        logical_name=args.market,
        payload=args.input.read_bytes(),
        request={"controlled_input": args.input.name},
        status="official_evidence",
        licence="provider terms apply",
        suffix=args.input.suffix.lower() or ".dat",
    )
    print(result.manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
