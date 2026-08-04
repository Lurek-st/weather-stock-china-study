from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import load_yaml, repo_root, write_json
from scripts.v2.probes.common import FINAL_STATUSES, classify
from scripts.v2.probes.probe_taiex import run as run_taiex


WINDOWS = [
    {"window_id": "recent_30", "request_rule": "previous 60 calendar days; select latest 30 valid records"},
    {"window_id": "2023_06", "start": "2023-06-01", "end": "2023-06-30"},
    {"window_id": "2020_10", "start": "2020-10-01", "end": "2020-10-31"},
]


def result_for(candidate: dict, live: bool) -> dict:
    licence = candidate["licence_assessment"]
    status = classify(licence["values"], licence["third_party_rights_status"],
                      candidate["technical_access"]["live_access_confirmed"],
                      candidate["data_quality"]["quality_gate_confirmed"],
                      candidate["historical_coverage_confirmed"])
    assert status in FINAL_STATUSES
    return {
        "probe_version": "1.0.0", "market_id": candidate["market_id"], "city_id": candidate["city_id"],
        "source_id": candidate["source_id"], "executed_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live" if live else "dry_run", "windows": WINDOWS,
        "licence_assessment": licence, "third_party_rights": candidate["third_party_rights"],
        "technical_access": candidate["technical_access"], "data_quality": candidate["data_quality"],
        "calendar_assessment": candidate["calendar_assessment"], "repeatability": candidate["repeatability"],
        "blocking_issues": candidate["blocking_issues"], "final_probe_status": status,
        "raw_response_policy": "local_only:.local/source-probes; no raw response is versioned",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated, non-production open-market probe plans.")
    parser.add_argument("--dry-run", action="store_true", help="write only reproducible evidence plans and summaries")
    parser.add_argument("--live", action="store_true", help="not yet enabled: live adapters remain deliberately isolated")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    config = load_yaml(args.root / "config" / "v2" / "open-market-candidates.yaml")
    output = args.root / "data" / "audits" / "v2" / "source-probes"
    results = []
    for candidate in config["candidates"]:
        result = result_for(candidate, live=False)
        if args.live and candidate["market_id"] == "taiex":
            live = run_taiex(args.root)
            result["mode"] = "live"
            result["windows"] = live["windows"]
            result["repeatability"] = live["repeatability"]
            result["technical_access"]["live_access_confirmed"] = True
            result["data_quality"]["quality_gate_confirmed"] = False
            result["blocking_issues"].append("Live data quality was sampled, but rights are unresolved and no production acceptance is permitted.")
        destination = output / candidate["market_id"] / "probe-result.json"
        write_json(destination, result)
        results.append({"market_id": candidate["market_id"], "status": result["final_probe_status"]})
    print(json.dumps({"probe_version": "1.0.0", "mode": "live" if args.live else "dry_run", "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
