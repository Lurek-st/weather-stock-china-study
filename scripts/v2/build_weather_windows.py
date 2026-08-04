from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from scripts.v2.core import build_weather_windows, load_yaml, repo_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Build local market-day weather windows.")
    parser.add_argument("--weather", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    hourly = pd.read_parquet(args.weather)
    calendar = pd.read_csv(args.calendar)
    markets = load_yaml(args.root / "config" / "v2" / "markets.yaml")["markets"]
    frames = []
    for market in markets:
        dates = calendar.loc[calendar["market_id"] == market["market_id"], "trading_date"]
        frames.append(build_weather_windows(hourly, market, dates))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pd.concat(frames, ignore_index=True).to_parquet(args.output, index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
