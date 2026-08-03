#!/usr/bin/env python3
"""Generate engineering acceptance metrics for a date range."""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    from .api_dependency_scan import detect_active_openai_dependencies
    from .validate_records import validate_file
except ImportError:
    from api_dependency_scan import detect_active_openai_dependencies
    from validate_records import validate_file

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "regional-daily-record-v1.schema.json"
REGIONS = ("asia", "europe", "us")
MANDATORY_MARKET = ("previous_close", "open", "high", "low", "close", "close_to_close_return_pct", "intraday_range_pct")


def dates_between(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def known_source_coverage(record: dict[str, Any]) -> tuple[int, int]:
    known = {s["source_id"] for s in record.get("sources", [])}
    non_null = covered = 0
    for city in record.get("cities", []):
        for window in city.get("weather_windows", {}).values():
            for metric in window.get("metrics", {}).values():
                if metric.get("value") is not None:
                    non_null += 1
                    refs = set(metric.get("source_ids", []))
                    if refs and refs <= known:
                        covered += 1
    for market in record.get("markets", []):
        for metric in market.get("metrics", {}).values():
            if metric.get("value") is not None:
                non_null += 1
                refs = set(metric.get("source_ids", []))
                if refs and refs <= known:
                    covered += 1
    return non_null, covered


def load_expected(path: str | None, start: date, end: date) -> tuple[dict[str, set[str]], bool]:
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        unknown_regions = set(raw) - set(REGIONS)
        if unknown_regions:
            raise ValueError(f"unknown regions in expected calendar: {sorted(unknown_regions)}")
        expected = {r: set(raw.get(r, [])) for r in REGIONS}
        for region, values in expected.items():
            for value in values:
                parsed = date.fromisoformat(value)
                if not start <= parsed <= end:
                    raise ValueError(
                        f"{region}/{value} is outside acceptance range {start}..{end}"
                    )
        return expected, False
    weekdays = {d.isoformat() for d in dates_between(start, end) if d.weekday() < 5}
    return {r: set(weekdays) for r in REGIONS}, True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--repo-root", default=str(ROOT))
    parser.add_argument("--expected-json", help="Official-calendar-reviewed expected region/date lists")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    root = Path(args.repo_root)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if start > end:
        raise ValueError("--start must be on or before --end")
    expected, calendar_assumption = load_expected(args.expected_json, start, end)
    schema_path = root / "schemas" / "regional-daily-record-v1.schema.json"
    if not schema_path.exists():
        schema_path = SCHEMA
    files: dict[tuple[str, str], Path] = {}
    for region in REGIONS:
        for path in (root / "data" / "raw" / region).glob("*.json") if (root / "data" / "raw" / region).exists() else []:
            if args.start <= path.stem <= args.end:
                files[(region, path.stem)] = path

    validation_failures: dict[str, list[str]] = {}
    source_total = source_covered = 0
    open_markets = open_market_complete = 0
    weather_windows = weather_scored = 0
    for (region, day), path in sorted(files.items()):
        errors = validate_file(path, schema_path)
        if errors:
            validation_failures[f"{region}/{day}"] = errors
        record = json.loads(path.read_text(encoding="utf-8"))
        total, covered = known_source_coverage(record)
        source_total += total; source_covered += covered
        for city in record.get("cities", []):
            for window_name in ("pre_open", "trading_session"):
                weather_windows += 1
                if city["weather_windows"][window_name].get("score_status") in {"complete_score", "partial_score"}:
                    weather_scored += 1
        for market in record.get("markets", []):
            if market.get("session", {}).get("trading_status") in {"open", "early_close", "special_session"}:
                open_markets += 1
                metrics = market.get("metrics", {})
                if all(metrics.get(k, {}).get("value") is not None for k in MANDATORY_MARKET):
                    open_market_complete += 1

    expected_pairs = {(r, d) for r, days in expected.items() for d in days}
    actual_pairs = set(files)
    generation_rate = len(actual_pairs & expected_pairs) / len(expected_pairs) if expected_pairs else 1.0
    validation_rate = (len(files) - len(validation_failures)) / len(files) if files else 0.0
    source_rate = source_covered / source_total if source_total else 0.0
    market_rate = open_market_complete / open_markets if open_markets else 1.0
    weather_rate = weather_scored / weather_windows if weather_windows else 0.0

    active_api_references = detect_active_openai_dependencies(root)

    metrics = {
        "expected_region_records": len(expected_pairs), "actual_region_records": len(actual_pairs & expected_pairs),
        "generation_rate": round(generation_rate, 4), "validation_rate": round(validation_rate, 4),
        "source_coverage_rate": round(source_rate, 4), "mandatory_market_completeness": round(market_rate, 4),
        "weather_scored_rate": round(weather_rate, 4), "validation_failures": validation_failures,
        "missing_expected": sorted(f"{r}/{d}" for r, d in expected_pairs - actual_pairs),
        "unexpected_present": sorted(f"{r}/{d}" for r, d in actual_pairs - expected_pairs),
        "calendar_assumption_weekdays_only": calendar_assumption,
        "active_openai_api_references": active_api_references,
    }
    thresholds = {
        "generation_rate_gte_0_95": generation_rate >= 0.95,
        "validation_rate_eq_1": validation_rate == 1.0,
        "source_coverage_eq_1": source_rate == 1.0,
        "market_completeness_gte_0_95": market_rate >= 0.95,
        "weather_scored_gte_0_90": weather_rate >= 0.90,
        "no_unexpected_records": not (actual_pairs - expected_pairs),
        "no_active_openai_api": not active_api_references,
    }
    result = {
        "version": "1.0.2", "start": args.start, "end": args.end,
        "metrics": metrics, "thresholds": thresholds,
        "v1_engineering_acceptance_pass": all(thresholds.values()) and not calendar_assumption,
        "note": "A pass requires --expected-json reviewed against official exchange calendars; weekday-only fallback cannot produce a final pass."
    }
    output = Path(args.output) if args.output else root / "analysis" / "results" / "acceptance.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(thresholds.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
