from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import load_yaml, repo_root, write_json
from scripts.v2.probes.common import FINAL_STATUSES, classify
from scripts.v2.probes.probe_taiex import run as run_taiex
from scripts.v2.probes.probe_dnb import run as run_dnb
from scripts.v2.probes.probe_kospi import build_result as build_kospi_result


WINDOWS = [
    {"window_id": "recent_30", "request_rule": "previous 60 calendar days; select latest 30 valid records"},
    {"window_id": "2023_06", "start": "2023-06-01", "end": "2023-06-30"},
    {"window_id": "2020_10", "start": "2020-10-01", "end": "2020-10-31"},
]


def result_for(candidate: dict, live: bool) -> dict:
    licence = copy.deepcopy(candidate["licence_assessment"])
    status = classify(licence["values"], licence["third_party_rights_status"],
                      candidate["technical_access"]["live_access_confirmed"],
                      candidate["data_quality"]["quality_gate_confirmed"],
                      candidate["historical_coverage_confirmed"])
    assert status in FINAL_STATUSES
    return {
        "probe_version": "1.0.0", "market_id": candidate["market_id"], "city_id": candidate["city_id"],
        "source_id": candidate["source_id"], "executed_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live" if live else "dry_run", "windows": WINDOWS,
        "licence_assessment": licence, "third_party_rights": copy.deepcopy(candidate["third_party_rights"]),
        "technical_access": copy.deepcopy(candidate["technical_access"]), "data_quality": copy.deepcopy(candidate["data_quality"]),
        "calendar_assessment": copy.deepcopy(candidate["calendar_assessment"]), "repeatability": copy.deepcopy(candidate["repeatability"]),
        "historical_coverage_confirmed": candidate["historical_coverage_confirmed"],
        "blocking_issues": list(candidate["blocking_issues"]), "final_probe_status": status,
        "raw_response_policy": "local_only:.local/source-probes; no raw response is versioned",
    }


def taiex_quality_gate(live: dict) -> bool:
    """Require all fixed-window quality, return, and repeatability checks."""
    windows = {row["window_id"]: row for row in live["windows"]}
    if set(windows) != {"recent_30", "2023_06", "2020_10"}:
        return False
    for row in windows.values():
        quality = row["quality"]
        returns = row["return_calculation"]
        if (
            row["record_count"] <= 0
            or quality["valid_trading_record_count"] != row["record_count"]
            or quality["missing_counts"]["close"] != 0
            or quality["date_parse_failures"] != 0
            or quality["duplicate_dates"] != 0
            or quality["weekend_records"] != 0
            or quality["ohlc_anomalies"]
            or returns["calculated_return_count"] != row["record_count"]
            or returns["boundary_missing_count"] != 0
        ):
            return False
    repeats = [row for row in live["repeatability"].values() if isinstance(row, dict) and "byte_status" in row]
    return bool(repeats) and all(
        row["byte_status"] == "byte_identical" and row["semantic_status"] == "semantic_identical"
        for row in repeats
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated, non-production open-market probe plans.")
    parser.add_argument("--dry-run", action="store_true", help="write only reproducible evidence plans and summaries")
    parser.add_argument("--live", action="store_true", help="not yet enabled: live adapters remain deliberately isolated")
    parser.add_argument("--market", help="limit execution to one configured market_id")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    config = load_yaml(args.root / "config" / "v2" / "open-market-candidates.yaml")
    output = args.root / "data" / "audits" / "v2" / "source-probes"
    results = []
    for candidate in config["candidates"]:
        if args.market and candidate["market_id"] != args.market:
            continue
        result = build_kospi_result(args.root, live=args.live) if candidate["market_id"] == "kospi" else result_for(candidate, live=False)
        if args.live and candidate["market_id"] == "taiex":
            live = run_taiex(args.root)
            result["mode"] = "live"
            result["windows"] = live["windows"]
            result["repeatability"] = copy.deepcopy(live["repeatability"])
            result["repeatability"]["status"] = "completed"
            result["technical_access"]["live_access_confirmed"] = True
            result["data_quality"]["quality_gate_confirmed"] = taiex_quality_gate(live)
            result["final_probe_status"] = classify(
                result["licence_assessment"]["values"],
                result["licence_assessment"]["third_party_rights_status"],
                result["technical_access"]["live_access_confirmed"],
                result["data_quality"]["quality_gate_confirmed"],
                candidate["historical_coverage_confirmed"],
            )
        if args.live and candidate["market_id"] == "aex_dnb":
            live = run_dnb(args.root)
            result["mode"] = "live"
            result["technical_access"]["live_access_confirmed"] = live["access_confirmed"]
            result["technical_access"]["direct_resource_probe"] = live["resources"]
            result["technical_access"]["json_structures"] = live["json_structures"]
            result["repeatability"] = {key: value["status"] for key, value in live["resources"].items()}
            result["final_probe_status"] = classify(
                result["licence_assessment"]["values"],
                result["licence_assessment"]["third_party_rights_status"],
                result["technical_access"]["live_access_confirmed"],
                result["data_quality"]["quality_gate_confirmed"],
                candidate["historical_coverage_confirmed"],
            )
        destination = output / candidate["market_id"] / "probe-result.json"
        write_json(destination, result)
        results.append({"market_id": candidate["market_id"], "status": result["final_probe_status"]})
    if args.market and not results:
        parser.error(f"unknown market_id: {args.market}")
    print(json.dumps({"probe_version": "1.0.0", "mode": "live" if args.live else "dry_run", "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
