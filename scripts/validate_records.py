#!/usr/bin/env python3
"""Validate regional records against JSON Schema and deterministic scoring rules."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

try:
    from .scoring import market_score, weather_score
except ImportError:  # direct script execution
    from scoring import market_score, weather_score

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "schemas" / "regional-daily-record-v1.schema.json"


def close_enough(a: float | None, b: float | None, tolerance: float = 0.11) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(float(a), float(b), abs_tol=tolerance)


def semantic_errors(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    source_ids = {s["source_id"] for s in record.get("sources", [])}
    city_ids = {c["city_id"] for c in record.get("cities", [])}

    for city in record.get("cities", []):
        for window_name, window in city.get("weather_windows", {}).items():
            for metric_name, metric in window.get("metrics", {}).items():
                refs = set(metric.get("source_ids", []))
                unknown = refs - source_ids
                if unknown:
                    errors.append(f"{city['city_id']}.{window_name}.{metric_name}: unknown source ids {sorted(unknown)}")
                if metric.get("value") is not None and not refs:
                    errors.append(f"{city['city_id']}.{window_name}.{metric_name}: non-null value without source")
                if metric.get("data_type") == "forecast":
                    errors.append(f"{city['city_id']}.{window_name}.{metric_name}: historical record cannot use forecast")
            metrics = window.get("metrics", {})
            start, end = window.get("start_local"), window.get("end_local")
            window_hours = None
            try:
                sh, sm = map(int, start.split(":"))
                eh, em = map(int, end.split(":"))
                window_hours = ((eh * 60 + em) - (sh * 60 + sm)) / 60
                if window_hours <= 0:
                    window_hours += 24
            except Exception:
                errors.append(f"{city['city_id']}.{window_name}: invalid HH:MM window")
            expected = weather_score(metrics, window_hours, window.get("alert_severity"))
            if not close_enough(expected["index"], window.get("weather_index")):
                errors.append(f"{city['city_id']}.{window_name}: weather_index mismatch, expected {expected['index']}")
            if expected["score"] != window.get("weather_score"):
                errors.append(f"{city['city_id']}.{window_name}: weather_score mismatch, expected {expected['score']}")
            if expected["status"] != window.get("score_status"):
                errors.append(f"{city['city_id']}.{window_name}: weather score_status mismatch, expected {expected['status']}")

    for market in record.get("markets", []):
        if market.get("city_id") not in city_ids:
            errors.append(f"{market.get('market_id')}: city_id not present in cities")
        session = market.get("session", {})
        calendar_refs = set(session.get("calendar_source_ids", []))
        unknown_calendar = calendar_refs - source_ids
        if unknown_calendar:
            errors.append(f"{market.get('market_id')}: unknown calendar source ids {sorted(unknown_calendar)}")
        if session.get("official_calendar_checked") and not calendar_refs:
            errors.append(f"{market.get('market_id')}: checked calendar requires calendar_source_ids")
        for metric_name, metric in market.get("metrics", {}).items():
            refs = set(metric.get("source_ids", []))
            unknown = refs - source_ids
            if unknown:
                errors.append(f"{market['market_id']}.{metric_name}: unknown source ids {sorted(unknown)}")
            if metric.get("value") is not None and not refs:
                errors.append(f"{market['market_id']}.{metric_name}: non-null value without source")
        status = session.get("trading_status")
        if status in {"closed_holiday", "closed_weekend", "unexpected_closure"}:
            non_null = sorted(
                name
                for name, metric in market.get("metrics", {}).items()
                if metric.get("value") is not None
            )
            if non_null:
                errors.append(f"{market['market_id']}: closed session has non-null metrics {non_null}")
            if market.get("market_score") is not None or market.get("market_index") is not None:
                errors.append(f"{market['market_id']}: closed session must not have a market score")
            continue
        if status in {"open", "early_close", "special_session"}:
            mandatory = {
                "previous_close", "open", "high", "low", "close",
                "close_to_close_return_pct", "intraday_range_pct",
            }
            missing = sorted(
                name
                for name in mandatory
                if market.get("metrics", {}).get(name, {}).get("value") is None
            )
            if missing:
                errors.append(f"{market['market_id']}: open session missing mandatory metrics {missing}")
        expected = market_score(market.get("metrics", {}))
        if not close_enough(expected["index"], market.get("market_index")):
            errors.append(f"{market['market_id']}: market_index mismatch, expected {expected['index']}")
        if expected["score"] != market.get("market_score"):
            errors.append(f"{market['market_id']}: market_score mismatch, expected {expected['score']}")
        if expected["status"] != market.get("score_status"):
            errors.append(f"{market['market_id']}: market score_status mismatch, expected {expected['status']}")

    if record.get("collection_mode") == "backfill" and not record.get("backfill_reason"):
        errors.append("backfill_reason is required when collection_mode=backfill")
    return errors


def validate_file(path: Path, schema_path: Path) -> list[str]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    record = json.loads(path.read_text(encoding="utf-8"))
    return validate_record(record, schema_path, schema=schema)


def validate_record(
    record: dict[str, Any],
    schema_path: Path = DEFAULT_SCHEMA,
    *,
    schema: dict[str, Any] | None = None,
) -> list[str]:
    schema = schema or json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [f"schema: {e.json_path}: {e.message}" for e in sorted(validator.iter_errors(record), key=lambda e: list(e.path))]
    errors.extend(f"semantic: {e}" for e in semantic_errors(record))
    return errors


def find_json_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.json") if "schemas" not in p.parts and "config" not in p.parts))
        elif path.suffix == ".json":
            files.append(path)
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", help="JSON files or directories")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    args = parser.parse_args()

    files = find_json_files(args.paths)
    if not files:
        print("No JSON records found", file=sys.stderr)
        return 2
    failed = 0
    for path in files:
        try:
            errors = validate_file(path, Path(args.schema))
        except Exception as exc:
            errors = [f"fatal: {exc}"]
        if errors:
            failed += 1
            print(f"FAIL {path}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"PASS {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
