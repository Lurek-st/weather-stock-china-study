"""Build tracked TWSE historical holiday-schedule query semantics audits.

Consumes the bounded live probe evidence captured under
`.local/source-raw/taiex-calendar/twse_official_holiday_schedule/<YEAR>/`
plus the discovery notes gathered from the official frontend page and JS.

Produces two tracked audits:

1. data/audits/v2/taiex-calendar/taiex-historical-query-semantics.json
   - frontend discovery (page, method, endpoint, year parameter, encoding)
   - negative control (legacy queryYear route)
   - OpenAPI holiday endpoint role
   - probe years 2020/2025/2026 with year-match verdicts
2. data/audits/v2/taiex-calendar/taiex-annual-schedules-2021-2026.json
   - per-year parse classification (closed / explicit open / unknown)
   - consistency checks (all dates in year, no duplicates, exclusivity)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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
SEMANTICS_AUDIT = "data/audits/v2/taiex-calendar/taiex-historical-query-semantics.json"
ANNUAL_AUDIT = "data/audits/v2/taiex-calendar/taiex-annual-schedules-2021-2026.json"
ANNUAL_AUDIT_2020_2026 = "data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026.json"
RECOVERY_AUDIT = "data/audits/v2/taiex-calendar/taiex-2020-annual-schedule-recovery.json"

FRONTEND_PAGE = "https://www.twse.com.tw/zh/trading/holiday.html"
HISTORICAL_ENDPOINT = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"
LEGACY_ROUTE = "https://www.twse.com.tw/holidaySchedule/holidaySchedule"
OPENAPI_ENDPOINT = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"

PROBE_YEARS = [2020, 2025, 2026]
FULL_YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


def _semantic_hash(payload: dict[str, Any]) -> str:
    """Stable semantic hash over the normalized schedule rows (date+name)."""
    rows = sorted(
        ({"date": str(row[0]), "name": str(row[1])} for row in payload.get("data", [])),
        key=lambda item: (item["date"], item["name"]),
    )
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _load_year(root: Path, year: int) -> tuple[dict[str, Any], dict[str, Any]]:
    base = root / RAW_BASE / str(year)
    artifacts = sorted(
        path for path in base.glob("r*-*.json") if not path.name.endswith(".manifest.json")
    )
    manifests = sorted(base.glob("r*-*.manifest.json"))
    if not artifacts or not manifests:
        raise V2Error(f"missing raw artifacts for {year}")
    payload = json.loads(artifacts[-1].read_text(encoding="utf-8"))
    manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
    if manifest["sha256"] != sha256_file(artifacts[-1]):
        raise V2Error(f"raw sha256 mismatch for {year}")
    return payload, manifest


def _year_match_verdict(payload: dict[str, Any], requested_year: int) -> dict[str, Any]:
    data = payload.get("data", [])
    title = payload.get("title", "")
    roc_year = requested_year - 1911
    title_year_match = str(roc_year) in title or f"{requested_year}" in title
    all_dates_in_year = all(str(row[0]).startswith(f"{requested_year}-") for row in data)
    if data and title_year_match and all_dates_in_year:
        status = "historical_year_query_match"
    elif not data and title_year_match and payload.get("queryYear") == requested_year:
        status = "requested_year_acknowledged_no_data"
    else:
        status = "fallback_to_current_year"
    return {
        "requested_gregorian_year": requested_year,
        "requested_roc_year": roc_year,
        "response_title": title,
        "response_queryYear": payload.get("queryYear"),
        "row_count": len(data),
        "first_date": data[0][0] if data else None,
        "last_date": data[-1][0] if data else None,
        "title_year_match": title_year_match,
        "all_dates_in_requested_year": all_dates_in_year,
        "year_match_status": status,
        "raw_sha256": None,
        "semantic_sha256": None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build TWSE historical holiday-schedule audits")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    # ---- probe years (2020 / 2025 / 2026) ----
    probe_records = {}
    for year in PROBE_YEARS:
        payload, manifest = _load_year(root, year)
        verdict = _year_match_verdict(payload, year)
        verdict["raw_sha256"] = manifest["sha256"]
        verdict["semantic_sha256"] = _semantic_hash(payload)
        probe_records[str(year)] = verdict

    semantics_audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_historical_holiday_query_semantics",
        "official_frontend_page": FRONTEND_PAGE,
        "discovery_method": "official_frontend_html_and_js_read",
        "historical_request_method": "GET",
        "historical_request_endpoint": HISTORICAL_ENDPOINT,
        "year_parameter_name": "date",
        "year_encoding": "gregorian_YYYY",
        "response_format": "json",
        "negative_control": {
            "route": LEGACY_ROUTE,
            "requested_queryYear": 109,
            "observed_title_year": "115 (2026)",
            "legacy_queryYear_route_status": "ignored_or_fallback_current_year",
        },
        "openapi_holiday_endpoint": OPENAPI_ENDPOINT,
        "openapi_holiday_endpoint_role": "current_year_snapshot_no_history_parameters",
        "probe_years": probe_records,
        "query_semantics_status": (
            "confirmed_2021_2026_available_2020_acknowledged_no_data"
        ),
        "data_boundary_note": (
            "frontend year dropdown starts at 2021 (data-date='b:2021,e:1,f:Y'); "
            "date=YYYY returns full schedules for 2021-2026 and returns the "
            "requested year title/queryYear but an empty data array for "
            "2019/2020; OpenAPI v1 endpoint has no parameters and returns only "
            "the current-year snapshot"
        ),
        "historical_backfill_run": False,
        "full_history_calendar_verified": False,
    }
    semantics_path = root / SEMANTICS_AUDIT
    write_json(semantics_path, semantics_audit)

    # ---- annual schedules 2021-2026 (unchanged live-year records) ----
    annual_records = {}
    all_parse_clean = True
    all_year_match = True
    for year in FULL_YEARS:
        payload, manifest = _load_year(root, year)
        parsed = parse_annual_schedule(payload, year)
        verdict = _year_match_verdict(payload, year)
        if verdict["year_match_status"] != "historical_year_query_match":
            all_year_match = False
        if parsed["classification_issues"] or not parsed["all_dates_in_requested_year"]:
            all_parse_clean = False
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

    annual_audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_annual_holiday_schedules_2021_2026",
        "years": list(FULL_YEARS),
        "all_years_match_requested_year": all_year_match,
        "all_years_parse_clean": all_parse_clean,
        "annual_schedule_status": "official_2021_2026_loaded",
        "year_records": annual_records,
        "full_history_calendar_verified": False,
        "historical_backfill_run": False,
        "extraordinary_closure_note": (
            "annual holiday schedules cover regular official closures/open "
            "days only; typhoon/earthquake/extraordinary closures require "
            "separate validation and are NOT claimed by this audit"
        ),
    }
    annual_path = root / ANNUAL_AUDIT
    write_json(annual_path, annual_audit)

    # ---- 2020 archival recovery + 2020-2026 merged audit ----
    recovery_audit_path = root / RECOVERY_AUDIT
    merged_records = {}
    all_merged_clean = True
    if recovery_audit_path.is_file():
        recovery = load_json(recovery_audit_path)
        if recovery.get("recovery_status") == "accepted":
            merged_records["2020"] = {
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
            if (
                not recovery["all_dates_in_year"]
                or not recovery["no_duplicate_dates"]
                or not recovery["open_closed_exclusive"]
                or recovery["classification_issues"]
            ):
                all_merged_clean = False
        else:
            all_merged_clean = False
    for year, record in annual_records.items():
        merged_records[year] = record
    merged_years = list(merged_records)
    merged_audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_annual_holiday_schedules_2020_2026",
        "years": merged_years,
        "all_years_match_requested_year": all_year_match and "2020" in merged_records,
        "all_years_parse_clean": all_parse_clean and all_merged_clean,
        "annual_schedule_status": (
            "official_2020_2026_loaded" if merged_years == ["2020", "2021", "2022", "2023", "2024", "2025", "2026"]
            else "official_2021_2026_loaded"
        ),
        "year_records": merged_records,
        "full_history_calendar_verified": False,
        "historical_backfill_run": False,
        "extraordinary_closure_note": (
            "annual holiday schedules cover regular official closures/open "
            "days only; typhoon/earthquake/extraordinary closures require "
            "separate validation and are NOT claimed by this audit"
        ),
    }
    merged_path = root / ANNUAL_AUDIT_2020_2026
    write_json(merged_path, merged_audit)

    print(
        json.dumps(
            {
                "status": "ok",
                "semantics_audit_path": SEMANTICS_AUDIT,
                "semantics_audit_sha256": sha256_file(semantics_path),
                "annual_audit_path": ANNUAL_AUDIT,
                "annual_audit_sha256": sha256_file(annual_path),
                "annual_audit_2020_2026_path": ANNUAL_AUDIT_2020_2026,
                "annual_audit_2020_2026_sha256": sha256_file(merged_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
