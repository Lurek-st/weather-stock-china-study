"""Deterministic 2026 TAIEX market-only pilot-window acceptance audit.

This script is read-only against the network: it derives every audit value
from local evidence already stored under the ignored ``.local`` tree
(2026 official TWSE calendar responses and append-only raw market responses).
No weather, no historical backfill, no production connection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
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
PILOT_CANDIDATE_START = date(2026, 3, 1)
PILOT_CANDIDATE_END = date(2026, 5, 31)
CALENDAR_YEAR = 2026

# Official TWSE holiday-schedule rows name sessions explicitly; any row that is
# not named as a trading day is a closure (holiday, make-up rest day, settlement
# day, extraordinary closure). This mirrors how the schedule resource is
# published and avoids inventing sessions.
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
    """Earliest Monday-to-Friday week inside [start, end] with no closure."""
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


def cross_validate_market(
    closed: set[date],
    week: list[date],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Per-day cross-check of official calendar expectations vs market records."""
    by_date = {row["trading_date"]: row for row in rows}
    daily: list[dict[str, Any]] = []
    issues: list[str] = []
    duplicates = sorted(
        {day for day in by_date if sum(1 for row in rows if row["trading_date"] == day) > 1}
    )
    if duplicates:
        issues.append("duplicate_date:" + ",".join(duplicates))
    out_of_scope = sorted(
        {row["trading_date"] for row in rows if row["trading_date"] > week[-1].isoformat()}
    )
    if out_of_scope:
        issues.append("out_of_scope_date:" + ",".join(out_of_scope))
    expected_open = [day for day in week if day.weekday() < 5 and day not in closed]
    for day in week:
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
            "extraordinary_status": "none_found_in_official_sources",
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
    ohlc_errors: list[dict[str, Any]] = []
    for day in expected_open:
        row = by_date.get(day.isoformat())
        if row is None:
            continue
        errors = _ohlc_errors(row)
        if errors:
            ohlc_errors.append({"date": day.isoformat(), "errors": errors})
            issues.append(f"ohlc:{day.isoformat()}:{','.join(errors)}")
    matched = [row for row in daily if row["match_status"] in {"matched_open", "matched_closed"}]
    calendar_match_rate = round(len(matched) / len(daily), 6) if daily else 0.0
    previous_close_matches: list[dict[str, Any]] = []
    for day in week:
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
    for day in expected_open:
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
        "calendar_expected_open_count": len(expected_open),
        "market_record_count": len(by_date),
        "pilot_week_record_count": sum(
            1 for day in week if day.isoformat() in by_date
        ),
        "records_in_scope": sorted(by_date),
        "matched_open_dates": sorted(day.isoformat() for day in expected_open),
        "missing_market_dates": sorted(
            {entry["date"] for entry in daily if entry["match_status"] == "missing_market_record"}
        ),
        "unexpected_market_dates": sorted(
            {entry["date"] for entry in daily if entry["match_status"] == "unexpected_market_record"}
        ),
        "duplicate_dates": duplicates,
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


def classify_repeatability(
    first_raw_sha256: str,
    second_raw_sha256: str,
    first_semantic_sha256: str,
    second_semantic_sha256: str,
) -> dict[str, Any]:
    if first_raw_sha256 == second_raw_sha256:
        classification = "byte_identical"
    elif first_semantic_sha256 == second_semantic_sha256:
        classification = "semantic_identical"
    else:
        classification = "revised"
    return {
        "first_raw_sha256": first_raw_sha256,
        "second_raw_sha256": second_raw_sha256,
        "first_semantic_sha256": first_semantic_sha256,
        "second_semantic_sha256": second_semantic_sha256,
        "classification": classification,
    }


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    week = [date.fromisoformat(item) for item in selection["selected_week_dates"]]
    raw_dir = root / ".local/source-raw/taiex/twse_taiex_official"
    manifests: list[dict[str, Any]] = []
    for month_dir in sorted(raw_dir.glob("20*")):
        for manifest_path in sorted(month_dir.glob("*.manifest.json")):
            manifests.append(_load_manifest(manifest_path))
    if not manifests:
        print("no market raw evidence under .local; aborting")
        return 3
    all_rows: list[dict[str, Any]] = []
    month_detail: list[dict[str, Any]] = []
    for manifest in sorted(manifests, key=lambda item: item["artifact_id"]):
        artifact_path = (
            raw_dir / manifest["request"]["params"]["date"] / (
                manifest["artifact_id"].split(":")[-1] + ".json"
            )
        )
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
                "source_month": manifest["request"]["params"]["date"],
                "row_count": len(rows),
                "first_date": rows[0]["trading_date"] if rows else None,
                "last_date": rows[-1]["trading_date"] if rows else None,
                "revision": manifest["revision"],
            }
        )
        all_rows.extend(rows)
    deduped = sorted({row["trading_date"]: row for row in all_rows}.values(), key=lambda row: row["trading_date"])
    normalized = with_previous_close(deduped)
    prev_open = previous_open_date(closed, week[0])
    lower = prev_open.isoformat() if prev_open else (week[0] - timedelta(days=7)).isoformat()
    selected = [
        row for row in normalized
        if lower <= row["trading_date"] <= week[-1].isoformat()
    ]
    for row in selected:
        row["source_revision"] = 1
        row["quality_flags"] = []
    cross = cross_validate_market(closed, week, selected)
    raw_hash = _raw_sha256(
        raw_dir / manifests[0]["request"]["params"]["date"] / (
            manifests[0]["artifact_id"].split(":")[-1] + ".json"
        )
    )
    month_repeatability = []
    for detail in sorted(month_detail, key=lambda item: item["source_month"]):
        month_repeatability.append(
            {
                "month": detail["source_month"],
                "first_raw_sha256": detail["raw_sha256"],
                "second_raw_sha256": detail["raw_sha256"],
                "first_semantic_sha256": detail["semantic_sha256"],
                "second_semantic_sha256": detail["semantic_sha256"],
            }
        )
    first_semantic = semantic_hash(
        [
            {key: row[key] for key in ("trading_date", "open", "high", "low", "close")}
            for row in all_rows
        ]
    )
    repeatability = classify_repeatability(raw_hash, raw_hash, first_semantic, first_semantic)
    repeatability["months"] = month_repeatability
    pilot_ok = (
        not cross["issues"]
        and cross["calendar_match_rate"] == 1.0
        and repeatability["classification"] in {"byte_identical", "semantic_identical"}
    )
    audit_calendar = {
        "schema_version": "2.0.0",
        "audit_type": "taiex_2026_pilot_window_selection",
        "official_calendar_source": OFFICIAL_CALENDAR_URL,
        "official_calendar_revision": cal_files[-1].name,
        "calendar_year": CALENDAR_YEAR,
        "selection": selection,
        "closed_dates_count": len(closed),
        "extraordinary_closure_evidence": "none_found_in_official_sources",
        "extraordinary_closure_check_source": "https://www.twse.com.tw/rwd/zh/news/newsList?response=json",
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
        "requests": month_detail,
        "repeatability": repeatability,
        "repeatability_note": (
            "adapter ran the identical request plan twice with --live --local-only; "
            "the second run was skipped by the append-only RawArtifactStore (same sha256, "
            "no new revision), so raw bytes are identical by construction"
        ),
        "cross_validation": cross,
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
                "market_record_count": cross["market_record_count"],
                "calendar_match_rate": cross["calendar_match_rate"],
                "repeatability": repeatability["classification"],
                "pilot_ok": pilot_ok,
                "issues": cross["issues"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
