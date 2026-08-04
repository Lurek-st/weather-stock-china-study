from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from scripts.v2.core import normalize_market_frame, repo_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize controlled market CSV.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    output = args.output or args.root / "data" / "canonical" / "v2" / "market" / "market.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    normalize_market_frame(pd.read_csv(args.input)).to_parquet(output, index=False)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
