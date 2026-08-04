from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from scripts.v2.core import (
    load_json,
    repo_root,
    sha256_file,
    validate_registry_files,
)


def validate_panel(root: Path, tier: str) -> list[str]:
    errors = []
    target = root / "data" / "panel" / "v2" / tier
    required = [
        target / "city_market_daily.csv",
        target / "city_market_daily.parquet",
        target / "data_dictionary.json",
        target / "panel_manifest.json",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.exists()]
    if missing:
        return [f"missing panel files: {missing}"]
    csv_frame = pd.read_csv(required[0])
    parquet_frame = pd.read_parquet(required[1])
    if len(csv_frame) != len(parquet_frame):
        errors.append("CSV and Parquet row counts differ")
    dictionary = load_json(required[2])
    if set(dictionary) != set(csv_frame.columns):
        errors.append("data dictionary does not close over panel columns")
    manifest = load_json(required[3])
    if manifest["row_count"] != len(csv_frame):
        errors.append("manifest row count differs")
    if manifest["csv_sha256"] != sha256_file(required[0]):
        errors.append("CSV hash differs from manifest")
    if manifest["parquet_sha256"] != sha256_file(required[1]):
        errors.append("Parquet hash differs from manifest")
    if set(csv_frame["panel_tier"]) != {tier}:
        errors.append("provisional/frozen tier contamination")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate V2 configuration and generated panel.")
    parser.add_argument("--tier", choices=["provisional", "frozen"])
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    errors = validate_registry_files(args.root)
    if args.tier:
        errors.extend(validate_panel(args.root, args.tier))
    print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
