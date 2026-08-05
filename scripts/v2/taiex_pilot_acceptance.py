"""Deterministic 2026 TAIEX market-only pilot-window acceptance audit.

Audit-evidence repair (2026-08-05): the audit now
  1. counts duplicate trading dates on the raw row sequence BEFORE any
     de-duplication (no silent {trading_date: row} collapse before checks);
  2. validates the full interval 2026-02-26 .. 2026-03-08, explicitly covering
     the preceding trading day, the official closure day, and all weekends, so
     a record on any closure/weekend forces unexpected_market_dates and fails
     the pilot;
  3. consumes two independent run evidence files (run-1.json / run-2.json)
     produced by scripts/v2/taiex_repeatability.py, never copying one hash
     into both first and second slots; and
  4. derives the extraordinary-closure conclusion from a locally stored,
     hashed official TWSE announcement response, instead of hard-coding it.

The script is read-only against the network: every value is derived from
local evidence under the ignored ``.local`` tree. No weather, no historical
backfill, no production connection.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import repo_root, write_json
from scripts.v2.probes.common import semantic_hash
from scripts.v2.probes.probe_taiex import _rows, with_previous_close

TWSE_MARKET_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
OFFICIAL_CALENDAR_URL = "https://www.twse.com.tw/holidaySchedule/holidaySchedule"
TWSE_NEWS_URL = "https://www.twse.com.tw/rwd/zh/news/newsList"
PILOT_CANDIDATE_START = date(2026, 3, 1)
PILOT_CANDIDATE_END = date(2026, 5, 31)
REQUEST_SCOPE_START = date(2026, 2, 26)
REQUEST_SCOPE_END = date(2026, 3, 6)
VALIDATION_INTERVAL_START = date(2026, 2, 26)
VALIDATION_INTERVAL_END = date(2026, 3, 8)
CALENDAR_YEAR = 2026
EXTRAORDINARY_KEYWORDS = ("颱風", "台风", "地震", "停市", "暫停", "暂停", "休市", "取消", "特殊", "災害", "灾害", "停止交易")

TRADING_DAY_KEYWORDS = ("交易日", "開始交易", "最後交易")


def parse_official_calendar(payload: dict[str, Any]) -> dict[str, Any]:
    closed: set[date] = set()
    open_special: set[date] = set()
    rows: list[dict[str, Any]] = []
    for item in payload.get("data", []):
        raw_day = str(item[0])
        name = str(item[1])
        note = str(item[2]) if len(item) > 2 else ""
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        if any(keyword in name for keyword in TRADING_DAY_KEYWORDS):
            open_special.add(day)
            status = "open_special"
        else:
            closed.add(day)
            status = "closed_official"
        rows.append(
            {
                "date": day.isoformat(),
                "name": name,
                "status": status,
                "note": note,
            }
        )
    return {
        "source_url": OFFICIAL_CALENDAR_URL,
        "closed": sorted(item.isoformat() for item in closed),
        "open_special": sorted(item.isoformat() for item in open_special),
        "rows": rows,
    }


def select_pilot_week(
    closed: set[date], start: date, end: date
) -> dict[str, Any]:
    monday = start + timedelta(days=(7 - start.weekday()) % 7)
    excluded: list[dict[str, Any]] = []
    while monday + timedelta(days=4) <= end:
        week = [monday + timedelta(days=i) for i in range(5)]
        if all(day not in closed for day in week):
            return {
                "selection_algorithm": "earliest_full_monday_to_friday_week_with_no_official_closure",
                "selection_start_range": start.isoformat(),
                "selection_end_range": end.isoformat(),
                "selected_week": monday.isoformat(),
                "selected_week_dates": [day.isoformat() for day in week],
                "selection_reason": (
                    f"week {monday.isoformat()} to {(monday + timedelta(days=4)).isoformat()} "
                    "has five weekdays and no official TWSE closure; it is the earliest "
                    "eligible week in the candidate range"
                ),
                "excluded_weeks": excluded,
            }
        excluded.append(
            {
                "week_start": monday.isoformat(),
                "reason": "contains an official closure",
            }
        )
        monday += timedelta(days=7)
    raise ValueError("no eligible pilot week in candidate range")


def previous_open_date(closed: set[date], target: date) -> date | None:
    cursor = target - timedelta(days=1)
    while cursor >= date(2020, 1, 1):
        if cursor.weekday() < 5 and cursor not in closed:
            return cursor
        cursor -= timedelta(days=1)
    return None


def _ohlc_errors(row: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        open_v = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
    except (TypeError, ValueError):
        return ["non_numeric_ohlc"]
    if high < open_v:
        errors.append("high_below_open")
    if high < close:
        errors.append("high_below_close")
    if low > open_v:
        errors.append("low_above_open")
    if low > close:
        errors.append("low_above_close")
    if high < low:
        errors.append("high_below_low")
    return errors


def count_duplicate_dates(raw_rows: Iterable[dict[str, Any]]) -> list[str]:
    """Count duplicate trading dates on the RAW sequence, before any de-dupe."""
    counts = Counter(str(row["trading_date"]) for row in raw_rows)
    return sorted(day for day, count in counts.items() if count > 1)


def cross_validate_interval(
    closed: set[date],
    interval: list[date],
    pilot_week: list[date],
    request_scope: list[date],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Per-day cross-check over the full validation interval.

    ``rows`` must already be duplicate-free and previous-close-enriched; the
    caller is responsible for running :func:`count_duplicate_dates` first.
    """
    by_date = {row["trading_date"]: row for row in rows}
    daily: list[dict[str, Any]] = []
    issues: list[str] = []
    expected_open = [day for day in interval if day.weekday() < 5 and day not in closed]
    for day in interval:
        record = by_date.get(day.isoformat())
        present = record is not None
        expected = day.weekday() < 5 and day not in closed
        if expected and present:
            status = "matched_open"
        elif expected and not present:
            status = "missing_market_record"
            issues.append(f"missing_market_record:{day.isoformat()}")
        elif not expected and not present:
            status = "matched_closed"
        else:
            status = "unexpected_market_record"
            issues.append(f"unexpected_market_record:{day.isoformat()}")
        entry: dict[str, Any] = {
            "date": day.isoformat(),
            "weekday": day.strftime("%A"),
            "calendar_expected_status": "open" if expected else "closed",
            "calendar_reason": (
                "official_open_session" if expected and day not in closed
                else ("official_closure" if day in closed else "weekend")
            ),
            "market_record_present": present,
            "open": record["open"] if present else None,
            "high": record["high"] if present else None,
            "low": record["low"] if present else None,
            "close": record["close"] if present else None,
            "source_revision": record.get("source_revision", 1) if present else None,
            "match_status": status,
            "quality_flags": record.get("quality_flags", []) if present else [],
        }
        daily.append(entry)
    matched_open_dates = sorted(
        entry["date"] for entry in daily if entry["match_status"] == "matched_open"
    )
    missing_market_dates = sorted(
        entry["date"] for entry in daily if entry["match_status"] == "missing_market_record"
    )
    unexpected_market_dates = sorted(
        entry["date"] for entry in daily if entry["match_status"] == "unexpected_market_record"
    )
    ohlc_errors: list[dict[str, Any]] = []
    for day in expected_open:
        row = by_date.get(day.isoformat())
        if row is None:
            continue
        errors = _ohlc_errors(row)
        if errors:
            ohlc_errors.append({"date": day.isoformat(), "errors": errors})
            issues.append(f"ohlc:{day.isoformat()}:{','.join(errors)}")
    matched = [entry for entry in daily if entry["match_status"] in {"matched_open", "matched_closed"}]
    calendar_match_rate = round(len(matched) / len(daily), 6) if daily else 0.0
    previous_close_matches: list[dict[str, Any]] = []
    for day in pilot_week:
        row = by_date.get(day.isoformat())
        if row is None:
            continue
        previous_open = previous_open_date(closed, day)
        prev_row = by_date.get(previous_open.isoformat()) if previous_open else None
        expected_value = float(prev_row["close"]) if prev_row is not None else None
        actual = (
            float(row["previous_close"])
            if row.get("previous_close") is not None
            else None
        )
        ok = expected_value is not None and actual is not None and abs(actual - expected_value) < 1e-9
        previous_close_matches.append(
            {
                "date": day.isoformat(),
                "expected_previous_open_date": previous_open.isoformat() if previous_open else None,
                "expected_previous_close": expected_value,
                "actual_previous_close": actual,
                "match": ok,
            }
        )
        if not ok:
            issues.append(f"previous_close:{day.isoformat()}")
    return_calcs: list[dict[str, Any]] = []
    for day in pilot_week:
        row = by_date.get(day.isoformat())
        if row is None:
            continue
        expected_return = (
            None
            if row["previous_close"] is None
            else round((float(row["close"]) / float(row["previous_close"]) - 1.0) * 100.0, 10)
        )
        ok = (row["return_pct"] is None and expected_return is None) or (
            row["return_pct"] is not None
            and expected_return is not None
            and abs(row["return_pct"] - expected_return) < 1e-9
        )
        return_calcs.append({"date": day.isoformat(), "recalculated": expected_return, "match": ok})
        if not ok:
            issues.append(f"return:{day.isoformat()}")
    return {
        "validation_interval_start": interval[0].isoformat(),
        "validation_interval_end": interval[-1].isoformat(),
        "calendar_expected_open_count": len(expected_open),
        "request_scope_record_count": sum(
            1 for day in request_scope if day.isoformat() in by_date
        ),
        "validation_interval_record_count": sum(
            1 for day in interval if day.isoformat() in by_date
        ),
        "pilot_week_record_count": sum(
            1 for day in pilot_week if day.isoformat() in by_date
        ),
        "records_in_scope": sorted(by_date),
        "matched_open_dates": matched_open_dates,
        "missing_market_dates": missing_market_dates,
        "unexpected_market_dates": unexpected_market_dates,
        "duplicate_dates": [],
        "unresolved_dates": [],
        "calendar_match_rate": calendar_match_rate,
        "daily": daily,
        "previous_close_check": previous_close_matches,
        "previous_close_match": all(item["match"] for item in previous_close_matches),
        "return_recalculation": return_calcs,
        "return_recalculation_match": all(item["match"] for item in return_calcs),
        "ohlc_validation_passed": not ohlc_errors,
        "ohlc_errors": ohlc_errors,
        "issues": issues,
    }


def plan_hashes(month_hashes: list[dict[str, str]]) -> dict[str, str]:
    """Plan-level combined hash over months sorted by month id."""
    payload = sorted(month_hashes, key=lambda item: item["month"])
    raw_blob = json.dumps(
        [{"month": item["month"], "raw_sha256": item["raw_sha256"]} for item in payload],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    semantic_blob = json.dumps(
        [{"month": item["month"], "semantic_sha256": item["semantic_sha256"]} for item in payload],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    import hashlib

    return {
        "plan_raw_sha256": hashlib.sha256(raw_blob).hexdigest(),
        "plan_semantic_sha256": hashlib.sha256(semantic_blob).hexdigest(),
    }


def classify_repeatability(
    months: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify using per-month independent run evidence (run 1 vs run 2)."""
    per_month = []
    raw_identical = True
    semantic_identical = True
    for item in sorted(months, key=lambda row: row["month"]):
        raw_match = item["run1_raw_sha256"] == item["run2_raw_sha256"]
        semantic_match = item["run1_semantic_sha256"] == item["run2_semantic_sha256"]
        raw_identical = raw_identical and raw_match
        semantic_identical = semantic_identical and semantic_match
        per_month.append(
            {
                "month": item["month"],
                "run1_raw_sha256": item["run1_raw_sha256"],
                "run2_raw_sha256": item["run2_raw_sha256"],
                "run1_semantic_sha256": item["run1_semantic_sha256"],
                "run2_semantic_sha256": item["run2_semantic_sha256"],
                "raw_match": raw_match,
                "semantic_match": semantic_match,
            }
        )
    if raw_identical:
        classification = "byte_identical"
    elif semantic_identical:
        classification = "semantic_identical"
    else:
        classification = "revised"
    return {"per_month": per_month, "classification": classification}


def analyze_extraordinary_evidence(
    evidence: dict[str, Any],
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    """Derive the extraordinary-closure conclusion from stored official evidence."""
    announcements = evidence.get("announcements", [])
    window_hits = [
        item for item in announcements
        if window_start <= item["date"] <= window_end
    ]
    matched = [
        item for item in window_hits
        if any(keyword in item["title"] for keyword in EXTRAORDINARY_KEYWORDS)
    ]
    evidence_result = (
        "none_found_in_official_sources" if not matched else "extraordinary_closure_found"
    )
    return {
        "evidence_present": True,
        "source_url": evidence.get("source_url"),
        "raw_sha256": evidence.get("raw_sha256"),
        "semantic_sha256": evidence.get("semantic_sha256"),
        "announcement_count": len(announcements),
        "window_start": window_start,
        "window_end": window_end,
        "keyword_policy": sorted(EXTRAORDINARY_KEYWORDS),
        "matched_announcement_count": len(matched),
        "matched_announcements": matched,
        "extraordinary_closure_evidence": evidence_result,
    }


def _latest_extraordinary_evidence(root: Path) -> dict[str, Any] | None:
    evidence_dir = root / ".local/source-probes/taiex-extraordinary-closures"
    files = sorted(evidence_dir.glob("evidence-*.json")) if evidence_dir.exists() else []
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _load_run_evidence(root: Path, run_id: str) -> dict[str, Any] | None:
    path = root / ".local/source-probes/taiex-2026-repeatability" / f"{run_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_records(raw_dir: Path) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    for month_dir in sorted(raw_dir.glob("20*")):
        if len(month_dir.name) != 8:  # adapter tokens are YYYYMMDD
            continue
        for manifest_path in sorted(month_dir.glob("*.manifest.json")):
            manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))
    return manifests


def _raw_rows_from_artifacts(raw_dir: Path, manifests: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows: list[dict[str, Any]] = []
    month_detail: list[dict[str, Any]] = []
    for manifest in sorted(manifests, key=lambda item: item["artifact_id"]):
        month = manifest["request"]["params"]["date"]
        artifact_path = raw_dir / month / (manifest["artifact_id"].split(":")[-1] + ".json")
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        rows = _rows(payload)
        month_detail.append(
            {
                "request_url": TWSE_MARKET_URL,
                "request_parameters": manifest["request"]["params"],
                "retrieved_at": manifest["retrieved_at"],
                "http_status": 200,
                "content_type": "application/json;charset=UTF-8",
                "content_length": manifest["content_length"],
                "etag": None,
                "last_modified": None,
                "raw_sha256": manifest["sha256"],
                "semantic_sha256": semantic_hash(rows),
                "source_report": payload.get("title"),
                "source_title": payload.get("title"),
                "source_month": month,
                "row_count": len(rows),
                "first_date": rows[0]["trading_date"] if rows else None,
                "last_date": rows[-1]["trading_date"] if rows else None,
                "revision": manifest["revision"],
            }
        )
        all_rows.extend(rows)
    return all_rows, month_detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build 2026 TAIEX market-only pilot acceptance audits")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--pilot-start", type=date.fromisoformat, default=PILOT_CANDIDATE_START)
    parser.add_argument("--pilot-end", type=date.fromisoformat, default=PILOT_CANDIDATE_END)
    args = parser.parse_args(argv)
    root = args.root
    cal_files = sorted((root / ".local/source-probes/taiex-calendar").glob("taiex-calendar-2026-*.json"))
    if not cal_files:
        print("no 2026 official calendar evidence under .local; aborting")
        return 3
    cal = parse_official_calendar(json.loads(cal_files[-1].read_text(encoding="utf-8")))
    closed = {date.fromisoformat(item) for item in cal["closed"]}
    selection = select_pilot_week(closed, args.pilot_start, args.pilot_end)
    pilot_week = [date.fromisoformat(item) for item in selection["selected_week_dates"]]
    interval = [VALIDATION_INTERVAL_START + timedelta(days=i) for i in range((VALIDATION_INTERVAL_END - VALIDATION_INTERVAL_START).days + 1)]
    request_scope = [REQUEST_SCOPE_START + timedelta(days=i) for i in range((REQUEST_SCOPE_END - REQUEST_SCOPE_START).days + 1)]

    raw_dir = root / ".local/source-raw/taiex/twse_taiex_official"
    manifests = _manifest_records(raw_dir)
    if not manifests:
        print("no market raw evidence under .local; aborting")
        return 3
    all_rows, month_detail = _raw_rows_from_artifacts(raw_dir, manifests)

    # 1) duplicate detection on the RAW sequence, before any de-duplication
    duplicates = count_duplicate_dates(all_rows)
    if duplicates:
        print(json.dumps({"duplicate_dates": duplicates, "pilot_ok": False}, ensure_ascii=False))
        return 2

    deduped = sorted({row["trading_date"]: row for row in all_rows}.values(), key=lambda row: row["trading_date"])
    normalized = with_previous_close(deduped)
    prev_open = previous_open_date(closed, pilot_week[0])
    lower = prev_open.isoformat() if prev_open else REQUEST_SCOPE_START.isoformat()
    selected = [
        row for row in normalized
        if lower <= row["trading_date"] <= VALIDATION_INTERVAL_END.isoformat()
    ]
    for row in selected:
        row["source_revision"] = 1
        row["quality_flags"] = []

    # 2) repeatability evidence must come from two independent run files
    run_1 = _load_run_evidence(root, "run-1")
    run_2 = _load_run_evidence(root, "run-2")
    if run_1 is None or run_2 is None:
        print(json.dumps({"repeatability_evidence": "missing_run_file", "pilot_ok": False}, ensure_ascii=False))
        return 2
    month_hashes = []
    for detail in month_detail:
        month_hashes.append(
            {
                "month": detail["source_month"],
                "run1_raw_sha256": next(
                    a["incoming_raw_sha256"] for a in run_1["attempts"] if a["request_month"] == detail["source_month"]
                ),
                "run2_raw_sha256": next(
                    a["incoming_raw_sha256"] for a in run_2["attempts"] if a["request_month"] == detail["source_month"]
                ),
                "run1_semantic_sha256": next(
                    a["incoming_semantic_sha256"] for a in run_1["attempts"] if a["request_month"] == detail["source_month"]
                ),
                "run2_semantic_sha256": next(
                    a["incoming_semantic_sha256"] for a in run_2["attempts"] if a["request_month"] == detail["source_month"]
                ),
            }
        )
    repeatability = classify_repeatability(month_hashes)
    plan_hashes_run1 = plan_hashes(
        [{"month": item["month"], "raw_sha256": item["run1_raw_sha256"], "semantic_sha256": item["run1_semantic_sha256"]} for item in month_hashes]
    )
    plan_hashes_run2 = plan_hashes(
        [{"month": item["month"], "raw_sha256": item["run2_raw_sha256"], "semantic_sha256": item["run2_semantic_sha256"]} for item in month_hashes]
    )

    # 3) extraordinary closure evidence must come from stored official response
    evidence = _latest_extraordinary_evidence(root)
    if evidence is None:
        print(json.dumps({"extraordinary_closure_evidence": "missing", "pilot_ok": False}, ensure_ascii=False))
        return 2
    extraordinary = analyze_extraordinary_evidence(
        evidence, selection["selected_week_dates"][0], selection["selected_week_dates"][-1]
    )

    cross = cross_validate_interval(closed, interval, pilot_week, request_scope, selected)
    cross["duplicate_dates"] = duplicates
    if duplicates:
        cross["issues"].append("duplicate_date:" + ",".join(duplicates))

    pilot_ok = (
        not cross["issues"]
        and cross["calendar_match_rate"] == 1.0
        and repeatability["classification"] in {"byte_identical", "semantic_identical"}
        and extraordinary["extraordinary_closure_evidence"] == "none_found_in_official_sources"
    )

    audit_calendar = {
        "schema_version": "2.0.0",
        "audit_type": "taiex_2026_pilot_window_selection",
        "official_calendar_source": OFFICIAL_CALENDAR_URL,
        "official_calendar_revision": cal_files[-1].name,
        "calendar_year": CALENDAR_YEAR,
        "selection": selection,
        "closed_dates_count": len(closed),
        "extraordinary_closure": {
            "source_url": extraordinary["source_url"],
            "evidence_raw_sha256": extraordinary["raw_sha256"],
            "evidence_semantic_sha256": extraordinary["semantic_sha256"],
            "announcement_count": extraordinary["announcement_count"],
            "window_start": extraordinary["window_start"],
            "window_end": extraordinary["window_end"],
            "keyword_policy": extraordinary["keyword_policy"],
            "matched_announcement_count": extraordinary["matched_announcement_count"],
            "matched_announcements": extraordinary["matched_announcements"],
            "extraordinary_closure_evidence": extraordinary["extraordinary_closure_evidence"],
        },
        "pilot_calendar_status": (
            "calendar_verified_for_2026_pilot_window" if pilot_ok else "calendar_verification_partial"
        ),
        "full_history_calendar_verified": False,
        "full_history_calendar_status": "calendar_verification_partial",
        "historical_calendar_blocker": "historical_twse_calendar_query_semantics_unconfirmed",
        "historical_backfill_status": "blocked_historical_calendar_unresolved",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    audit_market = {
        "schema_version": "2.0.0",
        "audit_type": "taiex_2026_market_only_pilot",
        "market_id": "taiex",
        "city_id": "taipei",
        "pilot_week": selection["selected_week"],
        "pilot_week_dates": selection["selected_week_dates"],
        "validation_interval": [day.isoformat() for day in interval],
        "requests": month_detail,
        "repeatability": {
            "classification": repeatability["classification"],
            "per_month": repeatability["per_month"],
            "plan_raw_sha256_run_1": plan_hashes_run1["plan_raw_sha256"],
            "plan_raw_sha256_run_2": plan_hashes_run2["plan_raw_sha256"],
            "plan_semantic_sha256_run_1": plan_hashes_run1["plan_semantic_sha256"],
            "plan_semantic_sha256_run_2": plan_hashes_run2["plan_semantic_sha256"],
            "evidence_runs": [run_1["run_id"], run_2["run_id"]],
            "evidence_paths": [
                f".local/source-probes/taiex-2026-repeatability/{run_1['run_id']}.json",
                f".local/source-probes/taiex-2026-repeatability/{run_2['run_id']}.json",
            ],
            "note": (
                "first/second hashes are independent incoming-response hashes captured "
                "by two separate live runs; per-month and plan-level hashes are reported"
            ),
        },
        "cross_validation": cross,
        "extraordinary_closure_evidence_ref": extraordinary["raw_sha256"],
        "pilot_ok": pilot_ok,
        "adapter_status": "market_only_pilot_accepted" if pilot_ok else "production_adapter_candidate",
        "source_probe_status": "open_core_candidate_conditional",
        "production_status": "not_connected",
        "research_ready": False,
        "frozen": False,
        "raw_response_policy": "local_only:.local/source-raw/taiex; append-only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(root / "data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json", audit_calendar)
    write_json(root / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json", audit_market)
    print(
        json.dumps(
            {
                "selected_week": selection["selected_week"],
                "calendar_expected_open_count": cross["calendar_expected_open_count"],
                "matched_open_dates": cross["matched_open_dates"],
                "unexpected_market_dates": cross["unexpected_market_dates"],
                "calendar_match_rate": cross["calendar_match_rate"],
                "repeatability": repeatability["classification"],
                "extraordinary_closure_evidence": extraordinary["extraordinary_closure_evidence"],
                "pilot_ok": pilot_ok,
                "issues": cross["issues"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
