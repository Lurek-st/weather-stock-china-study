from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import backfill_estimate, repo_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan a bounded historical V2 backfill.")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run:
        print("full backfill requires an approved dry-run, credentials, licence review, and disk approval")
        return 4
    print(json.dumps(backfill_estimate(args.root, args.start, args.end), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
