"""Candidate production adapter for TWSE TAIEX; isolated from the weather pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from scripts.v2.core import RawArtifactStore, repo_root, write_json
from scripts.v2.probes.probe_taiex import _rows, with_previous_close

URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"


def months_between(start: date, end: date) -> list[str]:
    current = date(start.year, start.month, 1)
    result = []
    while current <= end:
        result.append(current.strftime("%Y%m%d"))
        current = date(current.year + (current.month == 12), 1 if current.month == 12 else current.month + 1, 1)
    return result


def request_plan(start: date, end: date) -> dict:
    return {"source_url": URL, "months": months_between(start, end), "params": {"response": "json"}, "raw_policy": "append_only_local"}


def main() -> int:
    parser = argparse.ArgumentParser(description="TAIEX candidate adapter; live use is always explicit and local-only.")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("start must not be after end")
    plan = request_plan(args.start, args.end)
    if not args.live:
        write_json(args.root / "data/audits/v2/taiex-adapter-acceptance/dry-run.json", {"mode": "dry_run", "plan": plan, "network_requests": 0, "production_status": "not_connected"})
        print(json.dumps({"mode": "dry_run", "network_requests": 0}))
        return 0
    if not args.local_only:
        parser.error("live mode requires --local-only")
    store = RawArtifactStore(args.root / ".local/source-raw/taiex")
    rows = []
    for token in plan["months"]:
        response = requests.get(URL, params={"date": token, "response": "json"}, timeout=(10, 30), headers={"Accept": "application/json"})
        response.raise_for_status()
        artifact = store.persist(source_id="twse_taiex_official", provider="TWSE", logical_name=token, payload=response.content, request={"url": URL, "params": {"date": token, "response": "json"}}, status="retrieved", licence="Open Government Data License v1.0", suffix=".json")
        rows.extend(_rows(response.json()))
    selected = [row for row in with_previous_close(sorted({row["trading_date"]: row for row in rows}.values(), key=lambda item: item["trading_date"])) if args.start.isoformat() <= row["trading_date"] <= args.end.isoformat()]
    audit = {"mode": "live_local_only", "record_count": len(selected), "normalized_sha256": hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest(), "retrieved_at": datetime.now(timezone.utc).isoformat(), "production_status": "not_connected", "calendar_status": "calendar_verification_partial"}
    write_json(args.root / "data/audits/v2/taiex-adapter-acceptance/live-local-only.json", audit)
    print(json.dumps({"mode": "live_local_only", "record_count": len(selected)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
