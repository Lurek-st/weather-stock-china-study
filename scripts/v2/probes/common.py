from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Iterable


LICENCE_VALUES = {"yes", "no", "unclear", "not_applicable"}
FINAL_STATUSES = {
    "open_core_candidate_pass", "open_core_candidate_conditional", "rights_unresolved",
    "technical_access_blocked", "data_quality_fail", "historical_coverage_fail", "not_suitable",
}


def semantic_hash(records: Iterable[dict[str, Any]]) -> str:
    """Hash data semantics, independent of transport whitespace and field order."""
    normalized = sorted(
        (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in records)
    )
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def classify(licence: dict[str, str], third_party: str, technical_ok: bool,
             quality_ok: bool, history_ok: bool) -> str:
    """Conservative governance gate; no affirmative result can be inferred."""
    if not technical_ok:
        return "technical_access_blocked"
    required = ("download_allowed", "automated_access_allowed", "academic_research_allowed",
                "transformation_allowed", "public_raw_redistribution_allowed",
                "public_derived_panel_allowed")
    if any(licence.get(key) != "yes" for key in required) or third_party == "unclear":
        return "rights_unresolved"
    if not history_ok:
        return "historical_coverage_fail"
    if not quality_ok:
        return "data_quality_fail"
    return "open_core_candidate_conditional"


def quality_check(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Non-destructive generic checks for normalized daily index records."""
    parsed: list[tuple[date, dict[str, Any]]] = []
    invalid_dates = 0
    for row in rows:
        try:
            parsed.append((date.fromisoformat(str(row.get("trading_date", ""))), row))
        except ValueError:
            invalid_dates += 1
    dates = [item[0] for item in parsed]
    duplicate_dates = len(dates) - len(set(dates))
    weekend = sum(item.weekday() >= 5 for item in dates)
    anomalies: list[dict[str, Any]] = []
    missing: dict[str, int] = {}
    for name in ("open", "high", "low", "close", "previous_close", "volume"):
        missing[name] = sum(row.get(name) in (None, "") for _, row in parsed)
    for current_date, row in parsed:
        values = {key: row.get(key) for key in ("open", "high", "low", "close")}
        try:
            numeric = {key: float(value) for key, value in values.items() if value not in (None, "")}
        except (TypeError, ValueError):
            anomalies.append({"date": current_date.isoformat(), "kind": "non_numeric_ohlc"})
            continue
        if {"open", "high", "low", "close"}.issubset(numeric) and (
            numeric["high"] < max(numeric["open"], numeric["close"])
            or numeric["low"] > min(numeric["open"], numeric["close"])
            or numeric["high"] < numeric["low"]
            or any(value <= 0 for value in numeric.values())
        ):
            anomalies.append({"date": current_date.isoformat(), "kind": "ohlc_logic", "raw": values})
    return {
        "raw_record_count": len(rows), "valid_trading_record_count": len(parsed),
        "date_min": min(dates).isoformat() if dates else None, "date_max": max(dates).isoformat() if dates else None,
        "date_parse_failures": invalid_dates, "duplicate_dates": duplicate_dates,
        "weekend_records": weekend, "sorted_ascending": dates == sorted(dates),
        "missing_counts": missing, "ohlc_anomalies": anomalies,
    }
