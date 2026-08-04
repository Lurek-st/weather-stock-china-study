from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from scripts.v2.core import repo_root, write_panel_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze a final-only panel revision.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    panel = pd.read_parquet(args.input)
    if not panel["is_final"].fillna(False).all():
        raise SystemExit("refusing to freeze: non-final market values are present")
    panel["panel_tier"] = "frozen"
    panel["source_revision"] = args.revision
    target = args.root / "data" / "panel" / "v2" / "frozen"
    write_panel_bundle(
        panel,
        target,
        {
            "status": "frozen",
            "revision": args.revision,
            "frozen_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
