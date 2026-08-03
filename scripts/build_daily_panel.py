#!/usr/bin/env python3
"""Flatten validated regional JSON records into a research panel CSV."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def value(metrics: dict[str, Any], key: str):
    item = metrics.get(key)
    return item.get("value") if isinstance(item, dict) else None


def city_rows(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for city in record["cities"]:
        row: dict[str, Any] = {
            "date": record["target_date"],
            "region": record["region"],
            "city_id": city["city_id"],
            "city_name": city["city_name"],
            "country": city["country"],
            "timezone": city["timezone"],
            "collection_mode": record["collection_mode"],
            "lag_days": record["lag_days"],
            "data_quality_status": record["data_quality"]["status"],
        }
        for window_name, window in city["weather_windows"].items():
            prefix = f"weather_{window_name}"
            row[f"{prefix}_index"] = window["weather_index"]
            row[f"{prefix}_score"] = window["weather_score"]
            row[f"{prefix}_score_status"] = window["score_status"]
            for metric_name in [
                "temperature_mean_c", "apparent_temperature_mean_c", "precipitation_total_mm",
                "cloud_cover_mean_pct", "sunshine_duration_hours", "relative_humidity_mean_pct",
                "wind_speed_mean_kmh", "wind_gust_max_kmh", "aqi", "pm2_5_ug_m3"
            ]:
                row[f"{prefix}_{metric_name}"] = value(window["metrics"], metric_name)
        result[city["city_id"]] = row
    return result


def load_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        cities = city_rows(record)
        for market in record["markets"]:
            row = dict(cities[market["city_id"]])
            row.update({
                "market_id": market["market_id"],
                "index_name": market["index_name"],
                "exchange": market["exchange"],
                "currency": market["currency"],
                "primary_index": market.get("primary", True),
                "trading_status": market["session"]["trading_status"],
                "market_index": market["market_index"],
                "market_score": market["market_score"],
                "market_score_status": market["score_status"],
            })
            for metric_name in [
                "previous_close", "open", "high", "low", "close", "close_to_close_return_pct",
                "open_to_close_return_pct", "intraday_range_pct", "volume", "turnover",
                "advance_count", "decline_count", "unchanged_count", "market_breadth"
            ]:
                row[f"market_{metric_name}"] = value(market["metrics"], metric_name)
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "data" / "raw"))
    parser.add_argument("--output", default=str(ROOT / "data" / "derived" / "daily_panel.csv"))
    args = parser.parse_args()
    rows = load_rows(Path(args.input))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).sort_values(["date", "market_id"] if rows else []).to_csv(output, index=False)
    print(f"rows={len(rows)} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
