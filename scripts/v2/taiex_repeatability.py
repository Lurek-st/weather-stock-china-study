"""Run the identical TAIEX request plan twice and record independent attempt evidence.

Each run issues one GET per month (202602, 202603) through the same URL and
parameters as the candidate adapter. For every attempt the incoming response
bytes are hashed BEFORE the append-only store is consulted, so the run
evidence never copies a hash from an older manifest. Attempt records are
written, un-versioned, under ignored ``.local/source-probes/taiex-2026-repeatability/``
(run-1.json, run-2.json) and are not committed. The adapter's own audit file
``data/audits/v2/taiex-adapter-acceptance/live-local-only.json`` is refreshed
from the two runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import RawArtifactStore, repo_root, write_json
from scripts.v2.probes.common import semantic_hash
from scripts.v2.probes.probe_taiex import _rows
from scripts.v2.taiex_pilot_acceptance import plan_hashes

TWSE_MARKET_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
# Month tokens must match the candidate adapter (YYYYMMDD), so the append-only
# store resolves the same artifact and the second run is skipped as identical.
MONTHS = ["20260201", "20260301"]
RUN_COUNT = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_attempt(store: RawArtifactStore, month: str, request_number: int) -> dict[str, Any]:
    started_at = _now()
    params = {"date": month, "response": "json"}
    response = requests.get(
        TWSE_MARKET_URL,
        params=params,
        timeout=(10, 30),
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    raw = response.content
    incoming_raw_sha256 = hashlib.sha256(raw).hexdigest()
    rows = _rows(response.json())
    incoming_semantic_sha256 = semantic_hash(rows)
    result = store.persist(
        source_id="twse_taiex_official",
        provider="TWSE",
        logical_name=month,
        payload=raw,
        request={"url": TWSE_MARKET_URL, "params": params, "response": "json"},
        status="retrieved",
        licence="Open Government Data License v1.0",
        suffix=".json",
    )
    return {
        "request_month": month,
        "request_url": TWSE_MARKET_URL,
        "request_parameters": params,
        "request_number": request_number,
        "started_at": started_at,
        "completed_at": _now(),
        "http_status": response.status_code,
        "content_type": response.headers.get("Content-Type"),
        "content_length": len(raw),
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
        "incoming_raw_sha256": incoming_raw_sha256,
        "incoming_semantic_sha256": incoming_semantic_sha256,
        "incoming_row_count": len(rows),
        "artifact_action": "skipped_as_identical" if result.skipped_identical else "created",
        "artifact_revision": result.revision,
        "artifact_path": str(result.artifact_path.relative_to(repo_root())) if result.artifact_path.is_relative_to(repo_root()) else str(result.artifact_path),
        "skipped_as_identical": result.skipped_identical,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run TAIEX request plan twice with independent run evidence")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root
    store = RawArtifactStore(root / ".local/source-raw/taiex")
    evidence_dir = root / ".local/source-probes/taiex-2026-repeatability"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    for run_index in range(1, RUN_COUNT + 1):
        started_at = _now()
        attempts: list[dict[str, Any]] = []
        for request_number, month in enumerate(MONTHS, start=1):
            attempts.append(_run_attempt(store, month, request_number))
        run_hashes = plan_hashes(
            [
                {
                    "month": attempt["request_month"],
                    "raw_sha256": attempt["incoming_raw_sha256"],
                    "semantic_sha256": attempt["incoming_semantic_sha256"],
                }
                for attempt in attempts
            ]
        )
        run = {
            "run_id": f"run-{run_index}",
            "attempt_number": run_index,
            "started_at": started_at,
            "completed_at": _now(),
            "attempts": attempts,
            "plan_raw_sha256": run_hashes["plan_raw_sha256"],
            "plan_semantic_sha256": run_hashes["plan_semantic_sha256"],
        }
        write_json(evidence_dir / f"run-{run_index}.json", run)
        runs.append(run)
    # refresh the adapter audit file from the two independent runs
    run_1, run_2 = runs
    same_records = all(
        a["incoming_row_count"] == b["incoming_row_count"]
        for a, b in zip(run_1["attempts"], run_2["attempts"])
    )
    write_json(
        root / "data/audits/v2/taiex-adapter-acceptance/live-local-only.json",
        {
            "mode": "live_local_only",
            "record_count": run_1["attempts"][0]["incoming_row_count"] + run_1["attempts"][1]["incoming_row_count"],
            "same_record_count_across_runs": same_records,
            "runs": [
                {"run_id": "run-1", "plan_raw_sha256": run_1["plan_raw_sha256"], "plan_semantic_sha256": run_1["plan_semantic_sha256"], "attempts": run_1["attempts"]},
                {"run_id": "run-2", "plan_raw_sha256": run_2["plan_raw_sha256"], "plan_semantic_sha256": run_2["plan_semantic_sha256"], "attempts": run_2["attempts"]},
            ],
            "production_status": "not_connected",
            "calendar_status": "calendar_verification_partial",
        },
    )
    print(
        json.dumps(
            {
                "runs": len(runs),
                "plan_raw_sha256_run_1": run_1["plan_raw_sha256"],
                "plan_raw_sha256_run_2": run_2["plan_raw_sha256"],
                "plan_semantic_sha256_run_1": run_1["plan_semantic_sha256"],
                "plan_semantic_sha256_run_2": run_2["plan_semantic_sha256"],
                "artifact_actions": [
                    f"{a['request_month']}:{a['artifact_action']}"
                    for a in run_1["attempts"] + run_2["attempts"]
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
