from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from scripts.v2.core import build_panel, load_yaml, repo_root, write_panel_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the V2 research panel from canonical data.")
    parser.add_argument("--market", type=Path, required=True)
    parser.add_argument("--weather-windows", type=Path, required=True)
    parser.add_argument("--tier", choices=["provisional", "frozen"], required=True)
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    panel = build_panel(
        pd.read_parquet(args.market),
        pd.read_parquet(args.weather_windows),
        load_yaml(args.root / "config" / "v2" / "locations.yaml"),
        args.tier,
    )
    target = args.root / "data" / "panel" / "v2" / args.tier
    write_panel_bundle(panel, target, {"status": "external_build"})
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
