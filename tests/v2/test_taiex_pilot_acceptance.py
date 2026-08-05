"""Tests for the repaired 2026 TAIEX market-only pilot-window acceptance audit.

Covers: duplicate detection before de-dup, full validation interval (including
closures and weekends), independent two-run repeatability evidence, and
evidence-derived extraordinary-closure conclusions.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.v2.adapters.market import taiex as taiex_adapter
from scripts.v2.core import RawArtifactStore, load_yaml, repo_root
from scripts.v2.probes.probe_taiex import _rows, with_previous_close
from scripts.v2.taiex_pilot_acceptance import (
    analyze_extraordinary_evidence,
    classify_repeatability,
    count_duplicate_dates,
    cross_validate_interval,
    parse_official_calendar,
    plan_hashes,
    previous_open_date,
    select_pilot_week,
)

CALENDAR_PAYLOAD = {
    "stat": "ok",
    "title": "115 年市場開休市日期",
    "data": [
        ["2026-01-01", "中華民國開國紀念日", "依規定放假1日。"],
        ["2026-01-02", "國曆新年開始交易日", "國曆新年開始交易。"],
        ["2026-02-27", "和平紀念日", "和平紀念日為2月28日適逢星期六，於2月27日（星期五）補假。"],
        ["2026-02-28", "和平紀念日", "依規定放假1日。"],
        ["2026-04-03", "兒童節及民族掃墓節", "於4月3日（星期五）補假。"],
        ["2026-04-06", "民族掃墓節", "民族掃墓節為4月5日適逢星期日，於4月6日（星期一）補假。"],
        ["2026-05-01", "勞動節", "依規定放假1日。"],
    ],
}

VALIDATION_INTERVAL = [
    date(2026, 2, 26) + timedelta(days=i) for i in range(11)
]
REQUEST_SCOPE = [
    date(2026, 2, 26) + timedelta(days=i) for i in range(9)
]
PILOT_WEEK = [
    date(2026, 3, 2) + timedelta(days=i) for i in range(5)
]


def closed_set() -> set[date]:
    return {date.fromisoformat(item) for item in parse_official_calendar(CALENDAR_PAYLOAD)["closed"]}


def market_rows() -> list[dict]:
    return [
        {"trading_date": "2026-02-26", "open": "35000.00", "high": "35200.00", "low": "34900.00", "close": "35050.00"},
        {"trading_date": "2026-03-02", "open": "34900.00", "high": "35050.00", "low": "34700.00", "close": "34800.00"},
        {"trading_date": "2026-03-03", "open": "34800.00", "high": "34900.00", "low": "34600.00", "close": "34650.00"},
        {"trading_date": "2026-03-04", "open": "34650.00", "high": "34700.00", "low": "34400.00", "close": "34500.00"},
        {"trading_date": "2026-03-05", "open": "34500.00", "high": "34600.00", "low": "34300.00", "close": "34400.00"},
        {"trading_date": "2026-03-06", "open": "34400.00", "high": "34500.00", "low": "34200.00", "close": "34300.00"},
    ]


def enriched_rows(rows=None):
    return with_previous_close(sorted(rows or market_rows(), key=lambda row: row["trading_date"]))


def market_payload(month: str, rows: list[list]) -> dict:
    return {
        "stat": "OK",
        "title": f"115年{month[4:6]}月 發行量加權股價指數歷史資料",
        "date": month,
        "fields": ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"],
        "data": rows,
    }


def make_manifest(month: str, payload: dict) -> dict:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return {
        "schema_version": "2.0.0",
        "artifact_id": f"twse_taiex_official:{month}:r0001-{digest[:12]}",
        "source_id": "twse_taiex_official",
        "provider": "TWSE",
        "retrieved_at": "2026-08-05T00:00:00+00:00",
        "sha256": digest,
        "content_length": len(raw),
        "licence": "Open Government Data License v1.0",
        "status": "retrieved",
        "revision": 1,
        "retries": 0,
        "error": None,
        "request": {"url": "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST", "params": {"date": month, "response": "json"}},
    }


def _write_artifact(raw_dir: Path, month: str, payload: dict) -> dict:
    manifest = make_manifest(month, payload)
    stem = manifest["artifact_id"].split(":")[-1]
    (raw_dir / month).mkdir(parents=True, exist_ok=True)
    (raw_dir / month / f"{stem}.json").write_bytes(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    (raw_dir / month / f"{stem}.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def _payload_rows(month: str) -> list[list]:
    if month == "20260201":
        return [["115/02/26", "35,000.00", "35,200.00", "34,900.00", "35,050.00"]]
    return [
        ["115/03/02", "34,900.00", "35,050.00", "34,700.00", "34,800.00"],
        ["115/03/03", "34,800.00", "34,900.00", "34,600.00", "34,650.00"],
        ["115/03/04", "34,650.00", "34,700.00", "34,400.00", "34,500.00"],
        ["115/03/05", "34,500.00", "34,600.00", "34,300.00", "34,400.00"],
        ["115/03/06", "34,400.00", "34,500.00", "34,200.00", "34,300.00"],
    ]


def build_local_evidence(
    tmp_path: Path,
    *,
    feb_rows: list[list] | None = None,
    mar_rows: list[list] | None = None,
    run_evidence: bool = True,
    closure_evidence: bool = True,
    tamper_run2_hash: bool = False,
) -> Path:
    cal_dir = tmp_path / ".local/source-probes/taiex-calendar"
    cal_dir.mkdir(parents=True, exist_ok=True)
    (cal_dir / "taiex-calendar-2026-fixture.json").write_text(
        json.dumps(CALENDAR_PAYLOAD, ensure_ascii=False), encoding="utf-8"
    )
    raw_dir = tmp_path / ".local/source-raw/taiex/twse_taiex_official"
    months = {
        "20260201": feb_rows if feb_rows is not None else _payload_rows("20260201"),
        "20260301": mar_rows if mar_rows is not None else _payload_rows("20260301"),
    }
    hashes = {}
    for month, rows in months.items():
        manifest = _write_artifact(raw_dir, month, market_payload(month, rows))
        hashes[month] = {
            "raw": manifest["sha256"],
            "semantic": hashlib.sha256(
                json.dumps(
                    sorted(
                        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        for row in _rows(market_payload(month, rows))
                    ),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
        }
    if run_evidence:
        run_dir = tmp_path / ".local/source-probes/taiex-2026-repeatability"
        run_dir.mkdir(parents=True, exist_ok=True)
        for run_id in ("run-1", "run-2"):
            attempts = []
            for month in ("20260201", "20260301"):
                raw_hash = hashes[month]["raw"]
                semantic_hash = hashes[month]["semantic"]
                if tamper_run2_hash and run_id == "run-2" and month == "20260301":
                    raw_hash = "f" * 64
                    semantic_hash = "e" * 64
                attempts.append(
                    {
                        "request_month": month,
                        "incoming_raw_sha256": raw_hash,
                        "incoming_semantic_sha256": semantic_hash,
                        "incoming_row_count": 5,
                        "artifact_action": "skipped_as_identical",
                        "artifact_revision": 1,
                        "artifact_path": f".local/source-raw/taiex/twse_taiex_official/{month}/r0001-x.json",
                        "skipped_as_identical": True,
                    }
                )
            run = {
                "run_id": run_id,
                "attempt_number": 1 if run_id == "run-1" else 2,
                "started_at": "2026-08-05T00:00:00+00:00",
                "completed_at": "2026-08-05T00:00:01+00:00",
                "attempts": attempts,
            }
            (run_dir / f"{run_id}.json").write_text(
                json.dumps(run, ensure_ascii=False), encoding="utf-8"
            )
    if closure_evidence:
        ev_dir = tmp_path / ".local/source-probes/taiex-extraordinary-closures"
        ev_dir.mkdir(parents=True, exist_ok=True)
        evidence = {
            "source_url": "https://www.twse.com.tw/rwd/zh/news/newsList",
            "retrieved_at": "2026-08-05T00:00:00+00:00",
            "http_status": 200,
            "raw_sha256": "a" * 64,
            "semantic_sha256": "b" * 64,
            "announcement_count": 1,
            "announcements": [
                {
                    "date": "2026-03-03",
                    "title": "例行市場統計公告",
                    "url_or_identifier": "id-1",
                    "classification": "routine",
                }
            ],
        }
        (ev_dir / "evidence-20260805T000000Z.json").write_text(
            json.dumps(evidence, ensure_ascii=False), encoding="utf-8"
        )
    return tmp_path


def run_audit(root: Path) -> tuple[int, str]:
    from scripts.v2.taiex_pilot_acceptance import main as audit_main

    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = audit_main(["--root", str(root)])
    return code, buffer.getvalue()


# ---------------- selection / calendar ----------------

def test_weekend_selection_uses_earliest_eligible_week():
    selection = select_pilot_week(closed_set(), date(2026, 3, 1), date(2026, 5, 31))
    assert selection["selected_week"] == "2026-03-02"
    assert selection["selected_week_dates"] == [day.isoformat() for day in PILOT_WEEK]


def test_closure_moves_selection_to_next_week():
    closed = closed_set() | set(PILOT_WEEK)
    selection = select_pilot_week(closed, date(2026, 3, 1), date(2026, 5, 31))
    assert selection["selected_week"] == "2026-03-09"
    assert any(item["week_start"] == "2026-03-02" for item in selection["excluded_weeks"])


def test_previous_open_crosses_weekend_and_official_closure():
    assert previous_open_date(closed_set(), date(2026, 3, 2)) == date(2026, 2, 26)
    assert previous_open_date(closed_set(), date(2026, 5, 4)) == date(2026, 4, 30)


# ---------------- duplicate detection before de-dup ----------------

def test_duplicate_dates_counted_on_raw_sequence_before_dedupe():
    rows = market_rows() + [dict(market_rows()[1])]
    duplicates = count_duplicate_dates(rows)
    assert duplicates == ["2026-03-02"]
    # a dict-based de-dup would silently remove it
    assert len({row["trading_date"]: row for row in rows}) < len(rows)


def test_duplicate_in_raw_payload_fails_through_main_path(tmp_path):
    rows = _payload_rows("20260301")
    rows.append(["115/03/02", "34,900.00", "35,050.00", "34,700.00", "34,800.00"])  # duplicate
    root = build_local_evidence(tmp_path, mar_rows=rows)
    code, output = run_audit(root)
    assert code == 2
    assert "duplicate_dates" in output
    assert "2026-03-02" in output


# ---------------- full validation interval ----------------

def test_record_on_official_closure_2026_02_27_fails():
    extra = {"trading_date": "2026-02-27", "open": "35000.00", "high": "35100.00", "low": "34900.00", "close": "35050.00"}
    result = cross_validate_interval(closed_set(), VALIDATION_INTERVAL, PILOT_WEEK, REQUEST_SCOPE, enriched_rows(market_rows() + [extra]))
    assert "2026-02-27" in result["unexpected_market_dates"]
    assert any(issue.startswith("unexpected_market_record:2026-02-27") for issue in result["issues"])


def test_record_on_weekend_2026_02_28_fails():
    extra = {"trading_date": "2026-02-28", "open": "35000.00", "high": "35100.00", "low": "34900.00", "close": "35050.00"}
    result = cross_validate_interval(closed_set(), VALIDATION_INTERVAL, PILOT_WEEK, REQUEST_SCOPE, enriched_rows(market_rows() + [extra]))
    assert "2026-02-28" in result["unexpected_market_dates"]


def test_record_on_weekend_2026_03_01_fails():
    extra = {"trading_date": "2026-03-01", "open": "35000.00", "high": "35100.00", "low": "34900.00", "close": "35050.00"}
    result = cross_validate_interval(closed_set(), VALIDATION_INTERVAL, PILOT_WEEK, REQUEST_SCOPE, enriched_rows(market_rows() + [extra]))
    assert "2026-03-01" in result["unexpected_market_dates"]


@pytest.mark.parametrize("day", ["2026-03-07", "2026-03-08"])
def test_record_on_pilot_weekend_fails(day):
    extra = {"trading_date": day, "open": "34000.00", "high": "34100.00", "low": "33900.00", "close": "34050.00"}
    result = cross_validate_interval(closed_set(), VALIDATION_INTERVAL, PILOT_WEEK, REQUEST_SCOPE, enriched_rows(market_rows() + [extra]))
    assert day in result["unexpected_market_dates"]
    assert any(issue.startswith(f"unexpected_market_record:{day}") for issue in result["issues"])


def test_closure_record_fails_through_main_path(tmp_path):
    feb_rows = [["115/02/26", "35,000.00", "35,200.00", "34,900.00", "35,050.00"],
                ["115/02/27", "35,000.00", "35,100.00", "34,900.00", "35,000.00"]]  # 2/27 is closed
    root = build_local_evidence(tmp_path, feb_rows=feb_rows)
    code, _ = run_audit(root)
    assert code == 0
    market = json.loads(
        (root / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json").read_text(encoding="utf-8")
    )
    assert market["pilot_ok"] is False
    assert "2026-02-27" in market["cross_validation"]["unexpected_market_dates"]
    assert market["adapter_status"] == "production_adapter_candidate"


def test_missing_open_date_not_in_matched_open_dates():
    rows = [row for row in market_rows() if row["trading_date"] != "2026-03-04"]
    result = cross_validate_interval(closed_set(), VALIDATION_INTERVAL, PILOT_WEEK, REQUEST_SCOPE, enriched_rows(rows))
    assert "2026-03-04" in result["missing_market_dates"]
    assert "2026-03-04" not in result["matched_open_dates"]
    assert result["calendar_match_rate"] < 1.0


def test_count_semantics():
    result = cross_validate_interval(closed_set(), VALIDATION_INTERVAL, PILOT_WEEK, REQUEST_SCOPE, enriched_rows())
    assert result["request_scope_record_count"] == 6
    assert result["validation_interval_record_count"] == 6
    assert result["pilot_week_record_count"] == 5
    assert result["matched_open_dates"] == ["2026-02-26", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]


def test_previous_close_and_return_and_ohlc_pass_on_full_interval():
    result = cross_validate_interval(closed_set(), VALIDATION_INTERVAL, PILOT_WEEK, REQUEST_SCOPE, enriched_rows())
    assert result["previous_close_match"] is True
    assert result["return_recalculation_match"] is True
    assert result["ohlc_validation_passed"] is True
    assert result["calendar_match_rate"] == 1.0
    assert result["issues"] == []


def test_ohlc_invalid_relationship_fails():
    rows = [dict(row) for row in enriched_rows()]
    for row in rows:
        if row["trading_date"] == "2026-03-03":
            row["low"] = "30000.00"
            row["high"] = "29999.00"
    result = cross_validate_interval(closed_set(), VALIDATION_INTERVAL, PILOT_WEEK, REQUEST_SCOPE, rows)
    assert result["ohlc_validation_passed"] is False
    assert any("ohlc:2026-03-03" in issue for issue in result["issues"])


# ---------------- independent run evidence ----------------

def test_missing_run_evidence_fails(tmp_path):
    root = build_local_evidence(tmp_path, run_evidence=False)
    code, output = run_audit(root)
    assert code == 2
    assert "repeatability_evidence" in output


def test_missing_second_run_evidence_fails(tmp_path):
    root = build_local_evidence(tmp_path)
    (root / ".local/source-probes/taiex-2026-repeatability/run-2.json").unlink()
    code, output = run_audit(root)
    assert code == 2


def test_run_hashes_are_independent_objects(tmp_path):
    root = build_local_evidence(tmp_path)
    run_1 = json.loads((root / ".local/source-probes/taiex-2026-repeatability/run-1.json").read_text(encoding="utf-8"))
    run_2 = json.loads((root / ".local/source-probes/taiex-2026-repeatability/run-2.json").read_text(encoding="utf-8"))
    assert run_1["run_id"] != run_2["run_id"]
    assert run_1["attempts"] and run_2["attempts"]
    for attempt_1, attempt_2 in zip(run_1["attempts"], run_2["attempts"]):
        assert attempt_1 is not attempt_2
        assert "incoming_raw_sha256" in attempt_1 and "incoming_raw_sha256" in attempt_2


def test_run2_hash_change_changes_classification(tmp_path):
    root = build_local_evidence(tmp_path, tamper_run2_hash=True)
    code, _ = run_audit(root)
    market = json.loads(
        (root / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json").read_text(encoding="utf-8")
    )
    assert market["repeatability"]["classification"] in {"semantic_identical", "revised"}


def test_plan_hash_changes_when_any_month_hash_changes():
    base = [
        {"month": "20260201", "raw_sha256": "a" * 64, "semantic_sha256": "b" * 64},
        {"month": "20260301", "raw_sha256": "c" * 64, "semantic_sha256": "d" * 64},
    ]
    changed = [
        {"month": "20260201", "raw_sha256": "a" * 64, "semantic_sha256": "b" * 64},
        {"month": "20260301", "raw_sha256": "e" * 64, "semantic_sha256": "d" * 64},
    ]
    first = plan_hashes(base)
    second = plan_hashes(changed)
    assert first["plan_raw_sha256"] != second["plan_raw_sha256"]
    assert first["plan_semantic_sha256"] == second["plan_semantic_sha256"]


def test_classify_repeatability():
    months = [
        {"month": "20260201", "run1_raw_sha256": "a" * 64, "run2_raw_sha256": "a" * 64, "run1_semantic_sha256": "b" * 64, "run2_semantic_sha256": "b" * 64},
        {"month": "20260301", "run1_raw_sha256": "c" * 64, "run2_raw_sha256": "c" * 64, "run1_semantic_sha256": "d" * 64, "run2_semantic_sha256": "d" * 64},
    ]
    assert classify_repeatability(months)["classification"] == "byte_identical"
    revised = [dict(item) for item in months]
    revised[1]["run2_raw_sha256"] = "f" * 64
    revised[1]["run2_semantic_sha256"] = "e" * 64
    assert classify_repeatability(revised)["classification"] == "revised"


def test_full_sha256_length():
    result = plan_hashes([{"month": "20260201", "raw_sha256": "a" * 64, "semantic_sha256": "b" * 64}])
    for value in result.values():
        assert len(value) == 64


# ---------------- extraordinary closure evidence ----------------

def test_extraordinary_evidence_missing_blocks_upgrade(tmp_path):
    root = build_local_evidence(tmp_path, closure_evidence=False)
    code, output = run_audit(root)
    assert code == 2
    assert "extraordinary_closure_evidence" in output


def test_extraordinary_none_found_only_when_no_match():
    evidence = {
        "source_url": "u", "raw_sha256": "a" * 64, "semantic_sha256": "b" * 64,
        "announcement_count": 1,
        "announcements": [{"date": "2026-03-03", "title": "例行市場統計公告", "url_or_identifier": "x", "classification": "routine"}],
    }
    result = analyze_extraordinary_evidence(evidence, "2026-03-02", "2026-03-06")
    assert result["extraordinary_closure_evidence"] == "none_found_in_official_sources"
    assert result["matched_announcement_count"] == 0


def test_extraordinary_found_when_keyword_matches():
    evidence = {
        "source_url": "u", "raw_sha256": "a" * 64, "semantic_sha256": "b" * 64,
        "announcement_count": 1,
        "announcements": [{"date": "2026-03-03", "title": "颱風影響集中市場交易之公告", "url_or_identifier": "x", "classification": "extraordinary"}],
    }
    result = analyze_extraordinary_evidence(evidence, "2026-03-02", "2026-03-06")
    assert result["extraordinary_closure_evidence"] == "extraordinary_closure_found"
    assert result["matched_announcement_count"] == 1


def test_extraordinary_evidence_referenced_in_audit(tmp_path):
    root = build_local_evidence(tmp_path)
    code, _ = run_audit(root)
    assert code == 0
    market = json.loads(
        (root / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json").read_text(encoding="utf-8")
    )
    assert market["extraordinary_closure_evidence_ref"] == "a" * 64


# ---------------- end-to-end success + frozen statuses ----------------

def test_pilot_acceptance_passes_and_keeps_frozen_statuses(tmp_path):
    root = build_local_evidence(tmp_path)
    code, _ = run_audit(root)
    assert code == 0
    window = json.loads(
        (root / "data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json").read_text(encoding="utf-8")
    )
    market = json.loads(
        (root / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json").read_text(encoding="utf-8")
    )
    assert market["pilot_ok"] is True
    assert window["pilot_calendar_status"] == "calendar_verified_for_2026_pilot_window"
    assert window["full_history_calendar_verified"] is False
    assert window["historical_backfill_status"] == "blocked_historical_calendar_unresolved"
    assert market["adapter_status"] == "market_only_pilot_accepted"
    assert market["source_probe_status"] == "open_core_candidate_conditional"
    assert market["production_status"] == "not_connected"
    assert market["research_ready"] is False
    assert market["frozen"] is False
    assert market["repeatability"]["classification"] == "byte_identical"
    assert market["repeatability"]["evidence_runs"] == ["run-1", "run-2"]
    cross = market["cross_validation"]
    assert cross["request_scope_record_count"] == 6
    assert cross["validation_interval_record_count"] == 6
    assert cross["pilot_week_record_count"] == 5
    assert cross["unexpected_market_dates"] == []
    assert len(cross["matched_open_dates"]) == 6


# ---------------- raw store / adapter / governance ----------------

def test_raw_evidence_lives_under_local_and_is_not_overwritten(tmp_path):
    store = RawArtifactStore(tmp_path / ".local/source-raw/taiex")
    kwargs = dict(
        source_id="twse_taiex_official",
        provider="TWSE",
        logical_name="20260301",
        request={"params": {"date": "20260301", "response": "json"}},
        status="retrieved",
        licence="Open Government Data License v1.0",
        suffix=".json",
    )
    first = store.persist(payload=b"same-bytes", **kwargs)
    second = store.persist(payload=b"same-bytes", **kwargs)
    assert first.revision == 1
    assert second.skipped_identical is True
    assert first.artifact_path.is_relative_to(tmp_path / ".local")
    changed = store.persist(payload=b"different-bytes", **kwargs)
    assert changed.revision == 2
    assert first.artifact_path.read_bytes() == b"same-bytes"


def test_adapter_default_command_is_dry_run_and_live_requires_local_only(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["taiex", "--start", "2026-03-02", "--end", "2026-03-06", "--root", str(tmp_path)]
    )
    assert taiex_adapter.main() == 0
    audit = json.loads(
        (tmp_path / "data/audits/v2/taiex-adapter-acceptance/dry-run.json").read_text(encoding="utf-8")
    )
    assert audit["mode"] == "dry_run"
    assert audit["network_requests"] == 0
    monkeypatch.setattr(
        sys, "argv", ["taiex", "--start", "2026-03-02", "--end", "2026-03-06", "--live", "--root", str(tmp_path)]
    )
    with pytest.raises(SystemExit) as exc:
        taiex_adapter.main()
    assert exc.value.code == 2


def test_aex_and_kospi_statuses_remain_unchanged():
    scope = load_yaml(repo_root() / "config/v2/pilot-scope.yaml")
    by_id = {row["market_id"]: row for row in scope["excluded_or_deferred_markets"]}
    assert by_id["aex_dnb"]["status"] == "technical_access_blocked"
    assert by_id["kospi"]["status"] == "credential_required"


def test_rows_parse_roc_dates_to_iso():
    payload = market_payload(
        "20260301", [["115/03/02", "34,900.00", "35,050.00", "34,700.00", "34,800.00"]]
    )
    rows = _rows(payload)
    assert rows[0]["trading_date"] == "2026-03-02"
    assert rows[0]["open"] == "34900.00"
