"""Tests for the 2026 TAIEX market-only pilot-window acceptance audit."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from scripts.v2.adapters.market import taiex as taiex_adapter
from scripts.v2.core import RawArtifactStore, load_yaml, repo_root
from scripts.v2.probes.probe_taiex import _rows, with_previous_close
from scripts.v2.taiex_pilot_acceptance import (
    classify_repeatability,
    cross_validate_market,
    parse_official_calendar,
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


def pilot_week() -> list[date]:
    closed = {date.fromisoformat(item) for item in parse_official_calendar(CALENDAR_PAYLOAD)["closed"]}
    selection = select_pilot_week(closed, date(2026, 3, 1), date(2026, 5, 31))
    return [date.fromisoformat(item) for item in selection["selected_week_dates"]]


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


def closed_set() -> set[date]:
    return {date.fromisoformat(item) for item in parse_official_calendar(CALENDAR_PAYLOAD)["closed"]}


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


def market_payload(month: str, rows: list[list]) -> dict:
    return {
        "stat": "OK",
        "title": f"115年{month[4:6]}月 發行量加權股價指數歷史資料",
        "date": month,
        "fields": ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"],
        "data": rows,
    }


def build_local_evidence(tmp_path: Path) -> Path:
    cal_dir = tmp_path / ".local/source-probes/taiex-calendar"
    cal_dir.mkdir(parents=True, exist_ok=True)
    (cal_dir / "taiex-calendar-2026-fixture.json").write_text(
        json.dumps(CALENDAR_PAYLOAD, ensure_ascii=False), encoding="utf-8"
    )
    raw_dir = tmp_path / ".local/source-raw/taiex/twse_taiex_official"
    feb = market_payload(
        "20260201",
        [["115/02/26", "35,000.00", "35,200.00", "34,900.00", "35,050.00"]],
    )
    mar = market_payload(
        "20260301",
        [
            ["115/03/02", "34,900.00", "35,050.00", "34,700.00", "34,800.00"],
            ["115/03/03", "34,800.00", "34,900.00", "34,600.00", "34,650.00"],
            ["115/03/04", "34,650.00", "34,700.00", "34,400.00", "34,500.00"],
            ["115/03/05", "34,500.00", "34,600.00", "34,300.00", "34,400.00"],
            ["115/03/06", "34,400.00", "34,500.00", "34,200.00", "34,300.00"],
        ],
    )
    for month, payload in (("20260201", feb), ("20260301", mar)):
        manifest = make_manifest(month, payload)
        stem = manifest["artifact_id"].split(":")[-1]
        (raw_dir / month).mkdir(parents=True, exist_ok=True)
        (raw_dir / month / f"{stem}.json").write_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        (raw_dir / month / f"{stem}.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
    return tmp_path


def test_weekend_selection_uses_earliest_eligible_week():
    closed = closed_set()
    selection = select_pilot_week(closed, date(2026, 3, 1), date(2026, 5, 31))
    assert selection["selected_week"] == "2026-03-02"
    assert selection["selected_week_dates"] == [
        "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06",
    ]
    assert selection["excluded_weeks"] == []


def test_closure_moves_selection_to_next_week():
    closed = closed_set() | {date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5), date(2026, 3, 6)}
    selection = select_pilot_week(closed, date(2026, 3, 1), date(2026, 5, 31))
    assert selection["selected_week"] == "2026-03-09"
    assert any(item["week_start"] == "2026-03-02" for item in selection["excluded_weeks"])


def test_previous_open_crosses_weekend_and_official_closure():
    closed = closed_set()
    assert previous_open_date(closed, date(2026, 3, 2)) == date(2026, 2, 26)
    assert previous_open_date(closed, date(2026, 5, 4)) == date(2026, 4, 30)


def test_missing_expected_open_day_fails():
    closed = closed_set()
    week = pilot_week()
    rows = [row for row in enriched_rows() if row["trading_date"] != "2026-03-04"]
    result = cross_validate_market(closed, week, rows)
    assert "missing_market_record:2026-03-04" in result["issues"]
    assert result["missing_market_dates"] == ["2026-03-04"]
    assert result["calendar_match_rate"] < 1.0


def test_market_record_on_official_closure_fails():
    closed = closed_set() | {date(2026, 3, 4)}
    week = pilot_week()
    result = cross_validate_market(closed, week, enriched_rows())
    assert "unexpected_market_record:2026-03-04" in result["issues"]
    assert result["unexpected_market_dates"] == ["2026-03-04"]


def test_duplicate_date_fails():
    closed = closed_set()
    week = pilot_week()
    rows = enriched_rows()
    rows = rows + [{"trading_date": "2026-03-02", "open": "34900.00", "high": "35050.00", "low": "34700.00", "close": "34800.00"}]
    rows = with_previous_close(sorted(rows, key=lambda row: row["trading_date"]))
    result = cross_validate_market(closed, week, rows)
    assert any(issue.startswith("duplicate_date:") for issue in result["issues"])
    assert result["duplicate_dates"] == ["2026-03-02"]


def test_previous_close_matches_preceding_valid_trading_day():
    closed = closed_set()
    week = pilot_week()
    result = cross_validate_market(closed, week, enriched_rows())
    assert result["previous_close_match"] is True
    first = result["previous_close_check"][0]
    assert first["expected_previous_open_date"] == "2026-02-26"
    assert first["expected_previous_close"] == 35050.0


def test_previous_close_mismatch_fails():
    closed = closed_set()
    week = pilot_week()
    rows = enriched_rows()
    rows = [dict(row) for row in rows]
    for row in rows:
        if row["trading_date"] == "2026-03-02":
            row["previous_close"] = 34000.0
    result = cross_validate_market(closed, week, rows)
    assert result["previous_close_match"] is False
    assert any(issue.startswith("previous_close:2026-03-02") for issue in result["issues"])


def test_return_recalculation_is_consistent():
    closed = closed_set()
    week = pilot_week()
    result = cross_validate_market(closed, week, enriched_rows())
    assert result["return_recalculation_match"] is True
    first = result["return_recalculation"][0]
    expected = round((34800.0 / 35050.0 - 1.0) * 100.0, 10)
    assert first["recalculated"] == expected


def test_ohlc_invalid_relationship_fails():
    closed = closed_set()
    week = pilot_week()
    rows = enriched_rows()
    rows = [dict(row) for row in rows]
    for row in rows:
        if row["trading_date"] == "2026-03-03":
            row["low"] = "30000.00"
            row["high"] = "29999.00"
    result = cross_validate_market(closed, week, rows)
    assert result["ohlc_validation_passed"] is False
    assert any("ohlc:2026-03-03" in issue for issue in result["issues"])


def test_semantic_mismatch_is_not_byte_or_semantic_identical():
    result = classify_repeatability("a" * 64, "b" * 64, "c" * 64, "d" * 64)
    assert result["classification"] == "revised"
    byte_identical = classify_repeatability("a" * 64, "a" * 64, "b" * 64, "b" * 64)
    assert byte_identical["classification"] == "byte_identical"
    semantic_identical = classify_repeatability("a" * 64, "b" * 64, "c" * 64, "c" * 64)
    assert semantic_identical["classification"] == "semantic_identical"


def test_pilot_acceptance_keeps_full_history_and_production_status_frozen(tmp_path):
    from scripts.v2.taiex_pilot_acceptance import main as audit_main

    root = build_local_evidence(tmp_path)
    assert audit_main(["--root", str(root)]) == 0
    window = json.loads(
        (root / "data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json").read_text(encoding="utf-8")
    )
    market = json.loads(
        (root / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json").read_text(encoding="utf-8")
    )
    assert window["pilot_calendar_status"] == "calendar_verified_for_2026_pilot_window"
    assert window["full_history_calendar_verified"] is False
    assert window["historical_backfill_status"] == "blocked_historical_calendar_unresolved"
    assert market["adapter_status"] == "market_only_pilot_accepted"
    assert market["source_probe_status"] == "open_core_candidate_conditional"
    assert market["production_status"] == "not_connected"
    assert market["research_ready"] is False
    assert market["frozen"] is False
    assert market["cross_validation"]["pilot_week_record_count"] == 5
    assert market["repeatability"]["classification"] == "byte_identical"


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
    assert first.artifact_path == second.artifact_path
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
