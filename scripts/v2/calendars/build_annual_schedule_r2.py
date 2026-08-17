"""Build the corrected (r2) TAIEX annual-schedule canonical audit.

Re-runs the 2021-2026 annual-schedule parsing with the date-aware
settlement-only fix and merges the unchanged 2020 archival recovery.  The old
accepted audit is preserved as historical evidence; this new audit supersedes
it and records the defect id, changed records, and the parser code hash.

Produces:
  data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026-r2.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import (
    SCHEMA_VERSION,
    V2Error,
    load_json,
    repo_root,
    sha256_file,
    write_json,
)
from scripts.v2.parse_twse_holiday_schedule import parse_annual_schedule

RAW_BASE = ".local/source-raw/taiex-calendar/twse_official_holiday_schedule"
RECOVERY_AUDIT = "data/audits/v2/taiex-calendar/taiex-2020-annual-schedule-recovery.json"
OLD_ANNUAL_AUDIT = "data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026.json"
NEW_ANNUAL_AUDIT = "data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026-r2.json"
PARSER_CODE = "scripts/v2/parse_twse_holiday_schedule.py"

FULL_YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


def _load_year(root: Path, year: int) -> tuple[dict[str, Any], dict[str, Any]]:
    base = root / RAW_BASE / str(year)
    artifacts = sorted(p for p in base.glob("r*-*.json") if not p.name.endswith(".manifest.json"))
    manifests = sorted(base.glob("r*-*.manifest.json"))
    if not artifacts or not manifests:
        raise V2Error(f"missing raw artifacts for {year}")
    payload = json.loads(artifacts[-1].read_text(encoding="utf-8"))
    manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
    if manifest["sha256"] != sha256_file(artifacts[-1]):
        raise V2Error(f"raw sha256 mismatch for {year}")
    return payload, manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build corrected r2 annual-schedule audit")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    annual_records: dict[str, Any] = {}
    for year in FULL_YEARS:
        payload, manifest = _load_year(root, year)
        parsed = parse_annual_schedule(payload, year)
        annual_records[str(year)] = {
            "raw_artifact_id": manifest["artifact_id"],
            "revision": manifest["revision"],
            "sha256": manifest["sha256"],
            "response_title": payload.get("title"),
            "row_count": len(payload.get("data", [])),
            "closed_official_dates": parsed["closed_official_dates"],
            "explicit_open_dates": parsed["open_special_or_explicit_open_dates"],
            "informational_unknown_dates": parsed["informational_unknown_dates"],
            "all_dates_in_requested_year": parsed["all_dates_in_requested_year"],
            "no_duplicate_dates": parsed["no_duplicate_dates"],
            "open_closed_exclusive": parsed["open_closed_exclusive"],
            "classification_issues": parsed["classification_issues"],
        }

    # merge unchanged 2020 archival recovery
    recovery = load_json(root / RECOVERY_AUDIT)
    merged_records: dict[str, Any] = {
        "2020": {
            "raw_artifact_id": recovery["recovery_source"]["raw_artifact_id"],
            "revision": None,
            "sha256": recovery["recovery_source"]["raw_sha256"],
            "response_title": "109 年市場開休市日期",
            "row_count": recovery["row_count"],
            "closed_official_dates": recovery["closed_official_dates"],
            "explicit_open_dates": recovery["explicit_open_dates"],
            "informational_unknown_dates": recovery["informational_unknown_dates"],
            "all_dates_in_requested_year": recovery["all_dates_in_year"],
            "no_duplicate_dates": recovery["no_duplicate_dates"],
            "open_closed_exclusive": recovery["open_closed_exclusive"],
            "classification_issues": recovery["classification_issues"],
            "recovery_source": recovery["recovery_source"],
            "recovery_audit_path": RECOVERY_AUDIT,
        }
    }
    for year, record in annual_records.items():
        merged_records[year] = record

    # compute changed records vs the old accepted audit
    old = load_json(root / OLD_ANNUAL_AUDIT)
    changed: list[dict[str, Any]] = []
    for year, rec in merged_records.items():
        old_rec = old["year_records"].get(year, {})
        for key in ("closed_official_dates", "explicit_open_dates"):
            old_set = set(old_rec.get(key, []))
            new_set = set(rec.get(key, []))
            for d in sorted(old_set - new_set):
                changed.append({"date": d, "year": year, "field": key, "old": "present", "new": "absent"})
            for d in sorted(new_set - old_set):
                changed.append({"date": d, "year": year, "field": key, "old": "absent", "new": "present"})

    audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_annual_holiday_schedules_2020_2026_r2",
        "years": list(merged_records),
        "annual_schedule_status": "official_2020_2026_loaded",
        "year_records": merged_records,
        "full_history_calendar_verified": False,
        "historical_backfill_run": False,
        "supersedes": OLD_ANNUAL_AUDIT,
        "supersedes_reason": "settlement-only parse fix (I-006): trading-day-named rows with a note stating their own date has no trading are now classified closed",
        "defect_id": "I-006",
        "parser_code_path": PARSER_CODE,
        "parser_code_sha256": sha256_file(root / PARSER_CODE),
        "changed_record_count": len(changed),
        "changed_records": changed,
        "raw_source_bindings_unchanged": True,
        "extraordinary_closure_note": (
            "annual holiday schedules cover regular official closures/open "
            "days only; typhoon/earthquake/extraordinary closures require "
            "separate validation and are NOT claimed by this audit"
        ),
    }
    write_json(root / NEW_ANNUAL_AUDIT, audit)
    print(json.dumps(
        {
            "status": "ok",
            "new_audit_path": NEW_ANNUAL_AUDIT,
            "new_audit_sha256": sha256_file(root / NEW_ANNUAL_AUDIT),
            "changed_record_count": len(changed),
            "changed_records": changed,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
