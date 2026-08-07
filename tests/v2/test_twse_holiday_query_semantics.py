"""Zero-network tests for TWSE historical holiday-schedule query semantics.

Covers the bounded-probe classification rules (year match, fallback,
negative control), annual schedule parsing semantics (closed / explicit
open / make-up no-trade days), audit structure, pilot cross-validation, and
the frozen status invariants.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.v2.build_twse_holiday_query_audit import (
    _semantic_hash,
    _year_match_verdict,
)
from scripts.v2.core import load_yaml, repo_root
from scripts.v2.parse_twse_holiday_schedule import (
    classify_row,
    parse_annual_schedule,
)


def _payload(year: int, rows: list[list], title_year: int | None = None) -> dict:
    roc = (title_year if title_year is not None else year) - 1911
    return {
        "stat": "ok",
        "date": f"{year}0101",
        "title": f"{roc} 年市場開休市日期",
        "fields": ["日期", "名稱", "說明"],
        "data": rows,
        "queryYear": year,
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# year-match verdicts
# ---------------------------------------------------------------------------

def test_2020_response_year_matches_title_and_queryyear():
    payload = _payload(2020, [])  # empty data, title 109 年, queryYear 2020
    verdict = _year_match_verdict(payload, 2020)
    assert verdict["year_match_status"] == "requested_year_acknowledged_no_data"
    assert verdict["response_title"] == "109 年市場開休市日期"
    assert verdict["response_queryYear"] == 2020
    assert verdict["title_year_match"] is True


def test_request_2020_but_response_2026_fails():
    # title 115 年 (2026) while requesting 2020 -> fallback, never a match
    payload = _payload(2020, [["2026-01-01", "中華民國開國紀念日", "依規定放假1日。"]], title_year=2026)
    payload["queryYear"] = 2026
    verdict = _year_match_verdict(payload, 2020)
    assert verdict["year_match_status"] == "fallback_to_current_year"
    assert verdict["title_year_match"] is False
    assert verdict["all_dates_in_requested_year"] is False


def test_title_year_mismatch_fails():
    payload = _payload(2025, [["2025-01-01", "中華民國開國紀念日", "依規定放假1日。"]], title_year=2026)
    verdict = _year_match_verdict(payload, 2025)
    assert verdict["year_match_status"] == "fallback_to_current_year"


def test_row_date_cross_year_fails():
    payload = _payload(2025, [["2026-01-01", "中華民國開國紀念日", "依規定放假1日。"]])
    parsed = parse_annual_schedule(payload, 2025)
    assert "cross_year_date:2026-01-01" in parsed["classification_issues"]
    assert parsed["all_dates_in_requested_year"] is False


def test_duplicate_schedule_date_fails():
    payload = _payload(2025, [
        ["2025-01-01", "中華民國開國紀念日", "依規定放假1日。"],
        ["2025-01-01", "重複", "依規定放假1日。"],
    ])
    parsed = parse_annual_schedule(payload, 2025)
    assert "duplicate_date:2025-01-01" in parsed["classification_issues"]
    assert parsed["no_duplicate_dates"] is False


def test_explicit_trading_day_not_closed():
    parsed = parse_annual_schedule(
        _payload(2025, [["2025-01-02", "國曆新年開始交易日", "國曆新年開始交易。"]]), 2025
    )
    assert parsed["open_special_or_explicit_open_dates"] == ["2025-01-02"]
    assert parsed["closed_official_dates"] == []


def test_lunar_new_year_last_trading_day_classified_open():
    assert classify_row("農曆春節前最後交易日", "農曆春節前最後交易。") == "open_special_or_explicit_open"


def test_lunar_new_year_first_trading_day_classified_open():
    assert classify_row("農曆春節後開始交易日", "農曆春節後開始交易。") == "open_special_or_explicit_open"


def test_makeup_no_trade_no_settlement_is_closed():
    # 補行上班，但不交易亦不交割 -> the listed date is closed for the market
    assert classify_row("農曆除夕前一日", "2月10日調整放假，於2月20日補行上班，但不交易亦不交割。") == "closed_official"
    parsed = parse_annual_schedule(
        _payload(2021, [["2021-02-10", "農曆除夕前一日", "於2月20日（星期六）補行上班，但不交易亦不交割。"]]), 2021
    )
    assert parsed["closed_official_dates"] == ["2021-02-10"]
    assert parsed["open_special_or_explicit_open_dates"] == []


def test_settlement_only_market_is_closed():
    assert classify_row("市場無交易，僅辦理結算交割作業", "") == "closed_official"


def test_weekend_rule_does_not_override_explicit_open():
    # explicit trading-day marker wins over the generic weekend-closed rule
    parsed = parse_annual_schedule(
        _payload(2026, [["2026-01-02", "國曆新年開始交易日", "國曆新年開始交易。"]]), 2026
    )
    assert "2026-01-02" in parsed["open_special_or_explicit_open_dates"]
    assert "2026-01-02" not in parsed["closed_official_dates"]


def test_open_closed_conflict_detected():
    rows = [
        ["2025-01-02", "國曆新年開始交易日", "國曆新年開始交易。"],
        ["2025-01-02", "和平紀念日", "依規定放假1日。"],
    ]
    parsed = parse_annual_schedule(_payload(2025, rows), 2025)
    assert parsed["open_closed_exclusive"] is False
    assert "2025-01-02" in parsed["closed_official_dates"]
    assert "2025-01-02" in parsed["open_special_or_explicit_open_dates"]


def test_legacy_fallback_classified_correctly():
    # negative control: queryYear=109 on the old route returns 2026 content
    payload = _payload(2020, [["2026-01-01", "中華民國開國紀念日", "依規定放假1日。"]], title_year=2026)
    payload["queryYear"] = 2026
    verdict = _year_match_verdict(payload, 2020)
    assert verdict["year_match_status"] == "fallback_to_current_year"


# ---------------------------------------------------------------------------
# audit structure / invariants
# ---------------------------------------------------------------------------

def test_annual_raw_artifact_append_only_store():
    # RawArtifactStore persists identical bytes idempotently (revision kept)
    from scripts.v2.core import RawArtifactStore

    store = RawArtifactStore(repo_root() / ".local" / "test-store-holiday")
    kwargs = dict(
        source_id="twse_official_holiday_schedule",
        provider="TWSE",
        logical_name="2099",
        payload=b'{"stat":"ok","data":[]}',
        request={"endpoint": "https://example.invalid", "params": {"date": "2099"}},
        status="official_holiday_schedule",
        licence="Open Government Data License v1.0",
        suffix=".json",
    )
    first = store.persist(**kwargs)
    second = store.persist(**kwargs)
    assert first.revision == 1
    assert second.skipped_identical is True
    assert first.artifact_path.read_bytes() == b'{"stat":"ok","data":[]}'


def test_semantics_audit_paths_posix_relative():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-historical-query-semantics.json").read_text(encoding="utf-8")
    )
    text = json.dumps(audit)
    assert "C:\\" not in text and "D:\\" not in text
    for key in (
        "official_frontend_page",
        "historical_request_endpoint",
        "openapi_holiday_endpoint",
    ):
        value = audit[key]
        assert value.startswith("https://") or value.startswith("/")
    assert audit["negative_control"]["legacy_queryYear_route_status"] == "ignored_or_fallback_current_year"
    assert audit["openapi_holiday_endpoint_role"] == "current_year_snapshot_no_history_parameters"


def test_semantics_audit_probe_verdicts():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-historical-query-semantics.json").read_text(encoding="utf-8")
    )
    probes = audit["probe_years"]
    assert probes["2025"]["year_match_status"] == "historical_year_query_match"
    assert probes["2026"]["year_match_status"] == "historical_year_query_match"
    assert probes["2020"]["year_match_status"] == "requested_year_acknowledged_no_data"
    for rec in probes.values():
        assert len(rec["raw_sha256"]) == 64
        assert len(rec["semantic_sha256"]) == 64


def test_annual_audit_parse_clean():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-annual-schedules-2021-2026.json").read_text(encoding="utf-8")
    )
    assert audit["all_years_parse_clean"] is True
    assert audit["all_years_match_requested_year"] is True
    assert audit["annual_schedule_status"] == "official_2021_2026_loaded"
    for year, rec in audit["year_records"].items():
        assert rec["classification_issues"] == []
        assert rec["no_duplicate_dates"] is True
        assert rec["open_closed_exclusive"] is True
        assert rec["all_dates_in_requested_year"] is True
        assert len(rec["sha256"]) == 64


def test_pilot_2026_cross_validation():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-annual-schedules-2021-2026.json").read_text(encoding="utf-8")
    )
    closed = set(audit["year_records"]["2026"]["closed_official_dates"])
    assert "2026-02-27" in closed  # 和平紀念日補假
    assert "2026-02-28" in closed  # 和平紀念日
    for day in ("2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"):
        assert day not in closed  # pilot week is open


def test_full_history_calendar_verified_still_false():
    for path in (
        "data/audits/v2/taiex-calendar/taiex-historical-query-semantics.json",
        "data/audits/v2/taiex-calendar/taiex-annual-schedules-2021-2026.json",
    ):
        audit = json.loads((repo_root() / path).read_text(encoding="utf-8"))
        assert audit["full_history_calendar_verified"] is False
    config = load_yaml(repo_root() / "config/v2/calendars/taiex-calendar-sources.yaml")
    assert config["years"] == [2021, 2022, 2023, 2024, 2025, 2026]
    assert "?date={year}" in config["resource_url_template"]
    assert "queryYear" not in config["resource_url_template"]


def test_historical_market_backfill_not_run():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-historical-query-semantics.json").read_text(encoding="utf-8")
    )
    assert audit["historical_backfill_run"] is False
    annual = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-annual-schedules-2021-2026.json").read_text(encoding="utf-8")
    )
    assert annual["historical_backfill_run"] is False


def test_weather_not_downloaded_no_weather_changes():
    import subprocess

    status = subprocess.run(
        ["git", "status", "--short"], cwd=repo_root(), capture_output=True, text=True
    ).stdout
    # weather raw/canonical artifacts must not appear as modified
    assert "data/source_raw/v2/weather" not in status
    assert "data/canonical/v2/weather" not in status.replace("??", "").replace("M", "M ")
    assert "data/canonical/v2/weather" not in status


def test_pilot_panel_not_modified():
    import subprocess

    status = subprocess.run(
        ["git", "status", "--short"], cwd=repo_root(), capture_output=True, text=True
    ).stdout
    assert "data/panel/v2/provisional/taipei-taiex-pilot" not in status
    assert "data/canonical/v2/market/taiex-final-pilot" not in status


def test_v1_regression_panel_and_cli_smoke():
    from tests.v2.test_panel_and_cli import fixture_pipeline

    panel = fixture_pipeline()
    assert len(panel) == 40
    assert panel["panel_tier"].eq("provisional").all()


@pytest.mark.skipif(
    not (repo_root() / "data/panel/v2/provisional/city_market_daily.parquet").exists(),
    reason="V0 local artifact only present in local worktree",
)
def test_v0_diff_zero():
    import pandas as pd

    v0 = pd.read_parquet(repo_root() / "data/panel/v2/provisional/city_market_daily.parquet")
    assert v0["city_id"].nunique() == 8
    assert "taipei" not in set(v0["city_id"])
