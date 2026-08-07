"""Zero-network tests for TWSE 2020 annual schedule recovery.

Covers: official eshop announcement binding, archival recovery parsing and
classification, year/duplicate/exclusivity gates, Wayback-as-transport
semantics, current-RWD empty evidence preservation, 2020-2026 merged audit
(2021-2026 hashes unchanged), frozen status invariants, and the narrowed
fetch retry policy (network/5xx only).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from scripts.v2.core import V2Error, load_yaml, repo_root
from scripts.v2.fetch_twse_holiday_schedule import _is_retryable
from scripts.v2.parse_twse_holiday_schedule import classify_row, parse_annual_schedule
from scripts.v2.recover_twse_2020_schedule import (
    ARCHIVED_URL,
    ORIGINAL_SCHEDULE_URL,
    parse_archival_html,
    rows_to_dates,
)

# Minimal 2020 archival HTML table (mirrors the recovered 2020-06-04 snapshot)
ARCHIVAL_2020_HTML = """
<html><head><title>市場開休市日期</title></head><body>
<h2>中華民國109年有價證券集中交易市場開（休）市日期表</h2>
<table>
<tr><th>名稱</th><th>日期</th><th>星期</th><th>說明</th></tr>
<tr><td>中華民國開國紀念日</td><td>1月1日</td><td>三</td><td>依規定放假1日。</td></tr>
<tr><td>國曆新年開始交易日</td><td>1月2日</td><td>四</td><td>國曆新年開始交易。</td></tr>
<tr><td>農曆春節前最後交易日</td><td>1月20日</td><td>一</td><td>1月21日及1月22日市場無交易，僅辦理結算交割作業。</td></tr>
<tr><td>農曆除夕前一日</td><td>1月23日</td><td>四</td><td>1月23日（星期四）調整放假，於2月15日（星期六）補行上班，但不交易亦不交割。</td></tr>
<tr><td>農曆除夕</td><td>1月24日</td><td>五</td><td>依規定放假1日。</td></tr>
<tr><td>農曆春節</td><td>1月25日1月26日1月27日1月28日1月29日</td><td>六日一二三</td><td>依規定於1月25日至1月27日放假3日。</td></tr>
<tr><td>農曆春節後開始交易日</td><td>1月30日</td><td>四</td><td>農曆春節後開始交易。</td></tr>
<tr><td>和平紀念日</td><td>2月28日</td><td>五</td><td>依規定放假1日。</td></tr>
<tr><td>兒童節民族掃墓節</td><td>4月2日4月3日4月4日</td><td>四五六</td><td>依規定放假1日。</td></tr>
<tr><td>勞動節</td><td>5月1日</td><td>五</td><td>依規定放假1日。</td></tr>
<tr><td>端午節</td><td>6月25日6月26日</td><td>四五</td><td>依規定放假1日。6月26日調整放假，於6月20日補行上班，但不交易亦不交割。</td></tr>
<tr><td>中秋節</td><td>10月1日10月2日</td><td>四五</td><td>依規定放假1日。</td></tr>
<tr><td>國慶日</td><td>10月9日10月10日</td><td>五六</td><td>10月10日適逢星期六，10月9日補假1日。</td></tr>
</table>
</body></html>
"""


def _payload_2020() -> dict:
    rows = rows_to_dates(parse_archival_html(ARCHIVAL_2020_HTML), 2020)
    return {
        "stat": "ok",
        "date": "20200101",
        "title": "109 年市場開休市日期",
        "fields": ["日期", "名稱", "說明"],
        "data": [[row["date"], row["name"], row.get("note", "")] for row in rows],
        "queryYear": 2020,
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# 2020 archival acceptance
# ---------------------------------------------------------------------------

def test_official_2020_archive_accepted():
    parsed = parse_annual_schedule(_payload_2020(), 2020)
    assert parsed["classification_issues"] == []
    assert parsed["all_dates_in_requested_year"] is True
    assert parsed["no_duplicate_dates"] is True
    assert parsed["open_closed_exclusive"] is True
    assert len(parsed["closed_official_dates"]) >= 15
    # settlement-only days extracted from the note
    assert "2020-01-21" in parsed["closed_official_dates"]
    assert "2020-01-22" in parsed["closed_official_dates"]


def test_archived_content_wrong_year_rejected():
    payload = _payload_2020()
    payload["data"] = [["2021-01-01", "中華民國開國紀念日", "依規定放假1日。"]]
    parsed = parse_annual_schedule(payload, 2020)
    assert "cross_year_date:2021-01-01" in parsed["classification_issues"]
    assert parsed["all_dates_in_requested_year"] is False


def test_empty_archive_rejected():
    rows = rows_to_dates(parse_archival_html("<html><table><tr><th>名稱</th></tr></table></html>"), 2020)
    assert rows == []


def test_partial_schedule_rejected():
    payload = _payload_2020()
    # keep only a few January rows -> suspiciously small
    payload["data"] = [row for row in payload["data"] if row[0].startswith("2020-01")]
    parsed = parse_annual_schedule(payload, 2020)
    assert len(parsed["closed_official_dates"]) + len(parsed["open_special_or_explicit_open_dates"]) < 15


def test_duplicate_date_rejected():
    payload = _payload_2020()
    payload["data"].append(["2020-01-01", "重複", "依規定放假1日。"])
    parsed = parse_annual_schedule(payload, 2020)
    assert "duplicate_date:2020-01-01" in parsed["classification_issues"]
    assert parsed["no_duplicate_dates"] is False


def test_cross_year_date_rejected():
    payload = _payload_2020()
    payload["data"].append(["2021-12-31", "跨年", ""])
    parsed = parse_annual_schedule(payload, 2020)
    assert "cross_year_date:2021-12-31" in parsed["classification_issues"]


def test_explicit_open_classified_correctly():
    parsed = parse_annual_schedule(_payload_2020(), 2020)
    assert "2020-01-02" in parsed["open_special_or_explicit_open_dates"]  # 國曆新年開始交易日
    assert "2020-01-20" in parsed["open_special_or_explicit_open_dates"]  # 農曆春節前最後交易日
    assert "2020-01-30" in parsed["open_special_or_explicit_open_dates"]  # 農曆春節後開始交易日
    assert "2020-01-02" not in parsed["closed_official_dates"]


def test_makeup_no_trade_closed():
    assert classify_row("農曆除夕前一日", "1月23日調整放假，於2月15日補行上班，但不交易亦不交割。") == "closed_official"


def test_open_closed_conflict_rejected():
    payload = _payload_2020()
    payload["data"].append(["2020-01-02", "和平紀念日", "依規定放假1日。"])
    parsed = parse_annual_schedule(payload, 2020)
    assert parsed["open_closed_exclusive"] is False


# ---------------------------------------------------------------------------
# archive authority / transport semantics
# ---------------------------------------------------------------------------

def test_archive_authority_is_twse():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-2020-annual-schedule-recovery.json").read_text(encoding="utf-8")
    )
    assert audit["recovery_source"]["authority"] == "TWSE"
    assert audit["recovery_source"]["original_url"] == ORIGINAL_SCHEDULE_URL
    assert audit["recovery_source"]["retrieval_transport"] == "web_archive"
    assert audit["recovery_source"]["retrieved_url"] == ARCHIVED_URL


def test_wayback_only_as_transport():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-2020-annual-schedule-recovery.json").read_text(encoding="utf-8")
    )
    # Internet Archive must not be presented as the authority
    assert audit["recovery_source"]["authority"] != "Internet Archive"
    assert audit["official_eshop_announcement"]["url"].startswith("https://eshop.twse.com.tw")
    assert audit["official_eshop_announcement"]["publication_date"] == "2019-12-01"


def test_current_rwd_empty_evidence_preserved():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-2020-annual-schedule-recovery.json").read_text(encoding="utf-8")
    )
    assert audit["current_rwd_status"] == "requested_year_acknowledged_no_data"
    assert audit["current_rwd_empty_evidence"]["preserved"] is True
    # the empty RWD artifact must exist on disk when the local worktree has it
    # (CI checkout has no .local; the tracked audit is the portable evidence)
    rwd_dir = repo_root() / ".local/source-raw/taiex-calendar/twse_official_holiday_schedule/2020"
    if not rwd_dir.exists():
        return
    artifacts = [p for p in rwd_dir.glob("r*-*.json") if not p.name.endswith(".manifest.json")]
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["data"] == []


# ---------------------------------------------------------------------------
# merged 2020-2026 audit
# ---------------------------------------------------------------------------

def test_merged_audit_seven_years():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026.json").read_text(encoding="utf-8")
    )
    assert audit["years"] == ["2020", "2021", "2022", "2023", "2024", "2025", "2026"]
    assert audit["annual_schedule_status"] == "official_2020_2026_loaded"
    assert audit["all_years_match_requested_year"] is True
    assert audit["all_years_parse_clean"] is True
    assert "recovery_source" in audit["year_records"]["2020"]


def test_original_2021_2026_hashes_unchanged():
    merged = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026.json").read_text(encoding="utf-8")
    )
    original = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-annual-schedules-2021-2026.json").read_text(encoding="utf-8")
    )
    for year in ["2021", "2022", "2023", "2024", "2025", "2026"]:
        assert merged["year_records"][year]["sha256"] == original["year_records"][year]["sha256"]
        assert merged["year_records"][year]["closed_official_dates"] == original["year_records"][year]["closed_official_dates"]
        assert merged["year_records"][year]["explicit_open_dates"] == original["year_records"][year]["explicit_open_dates"]


def test_recovery_audit_posix_relative():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-2020-annual-schedule-recovery.json").read_text(encoding="utf-8")
    )
    text = json.dumps(audit)
    assert "C:\\" not in text and "D:\\" not in text
    assert audit["recovery_source"]["retrieved_url"].startswith("https://web.archive.org")


# ---------------------------------------------------------------------------
# status invariants
# ---------------------------------------------------------------------------

def test_full_history_calendar_verified_still_false():
    for path in (
        "data/audits/v2/taiex-calendar/taiex-2020-annual-schedule-recovery.json",
        "data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026.json",
        "data/audits/v2/taiex-calendar/taiex-historical-query-semantics.json",
    ):
        audit = json.loads((repo_root() / path).read_text(encoding="utf-8"))
        assert audit["full_history_calendar_verified"] is False


def test_extraordinary_closure_still_pending():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026.json").read_text(encoding="utf-8")
    )
    assert audit["historical_backfill_run"] is False
    assert "extraordinary" in audit["extraordinary_closure_note"].lower()
    assert audit["annual_schedule_status"] == "official_2020_2026_loaded"


def test_config_archival_year_source():
    config = load_yaml(repo_root() / "config/v2/calendars/taiex-calendar-sources.yaml")
    assert config["years"] == [2021, 2022, 2023, 2024, 2025, 2026]  # RWD range unchanged
    arch = config["archival_year_sources"]
    assert arch[2020]["status"] == "official_archival_recovered"
    assert config["canonical_annual_schedule_audit"] == "data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026.json"


def test_pilot_panel_unmodified():
    status = subprocess.run(
        ["git", "status", "--short"], cwd=repo_root(), capture_output=True, text=True
    ).stdout
    assert "data/panel/v2/provisional/taipei-taiex-pilot" not in status
    assert "data/canonical/v2/market/taiex-final-pilot" not in status


# ---------------------------------------------------------------------------
# retry policy narrowing
# ---------------------------------------------------------------------------

def test_retry_only_network_and_5xx():
    import requests

    class Resp:
        def __init__(self, code):
            self.status_code = code

    assert _is_retryable(requests.Timeout("t")) is True
    assert _is_retryable(requests.ConnectionError("c")) is True
    assert _is_retryable(requests.HTTPError("h", response=Resp(500))) is True
    assert _is_retryable(requests.HTTPError("h", response=Resp(503))) is True


def test_4xx_not_retried():
    import requests

    class Resp:
        def __init__(self, code):
            self.status_code = code

    assert _is_retryable(requests.HTTPError("h", response=Resp(404))) is False
    assert _is_retryable(requests.HTTPError("h", response=Resp(429))) is False


def test_json_decode_not_retried():
    import json

    assert _is_retryable(json.JSONDecodeError("j", "doc", 0)) is False


def test_store_error_not_retried():
    from scripts.v2.core import V2Error

    assert _is_retryable(V2Error("append-only collision")) is False
    assert _is_retryable(ValueError("bad value")) is False


def test_v1_regression_smoke():
    from tests.v2.test_panel_and_cli import fixture_pipeline

    panel = fixture_pipeline()
    assert len(panel) == 40


@pytest.mark.skipif(
    not (repo_root() / "data/panel/v2/provisional/city_market_daily.parquet").exists(),
    reason="V0 local artifact only present in local worktree",
)
def test_v0_diff_zero():
    v0 = pd.read_parquet(repo_root() / "data/panel/v2/provisional/city_market_daily.parquet")
    assert v0["city_id"].nunique() == 8
    assert "taipei" not in set(v0["city_id"])
