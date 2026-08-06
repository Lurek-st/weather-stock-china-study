"""Tests for the frozen Taipei ERA5 pilot contract and dry-run readiness."""
from __future__ import annotations

import json
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from scripts.v2.core import (
    build_weather_windows,
    load_yaml,
    normalize_weather_frame,
    repo_root,
)
from scripts.v2.fetch_era5 import (
    cds_readiness,
    finality_check,
    main as era5_main,
    pilot_config,
    utc_request_plan,
)

ROOT = repo_root()
TAIEX_MARKET = next(
    market for market in load_yaml(ROOT / "config" / "v2" / "markets.yaml")["markets"]
    if market["market_id"] == "taiex"
)
PILOT_DATES = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]


def taipei_hourly() -> pd.DataFrame:
    start_utc = datetime(2026, 2, 28, 16, tzinfo=timezone.utc)  # 2026-03-01 00:00 +08
    hours = pd.date_range(start_utc, periods=24 * 7 + 1, freq="h")
    return pd.DataFrame(
        {
            "timestamp_utc": hours,
            "temperature_k": [283.15] * len(hours),
            "dewpoint_k": [278.15] * len(hours),
            "precipitation_m": [0.001] * len(hours),
            "cloud_cover_fraction": [0.5] * len(hours),
            "wind_u_mps": [3.0] * len(hours),
            "wind_v_mps": [4.0] * len(hours),
            "wind_gust_mps": [5.0] * len(hours),
            "solar_radiation_j_m2": [360000.0] * len(hours),
        }
    )


@pytest.fixture(scope="module")
def taipei_windows():
    normalized = normalize_weather_frame(
        taipei_hourly(),
        city_id="taipei",
        source_id="fixture",
        data_class="final_reanalysis",
    )
    return build_weather_windows(normalized, TAIEX_MARKET, [date_from("2026-03-02")]).set_index("window")


def date_from(value: str):
    from datetime import date as _date

    return _date.fromisoformat(value)


# ---------------- configuration registration ----------------

def test_taipei_in_locations():
    locations = {row["city_id"]: row for row in load_yaml(ROOT / "config/v2/locations.yaml")["locations"]}
    assert "taipei" in locations
    assert locations["taipei"]["timezone"] == "Asia/Taipei"


def test_taiex_in_markets():
    markets = {row["market_id"]: row for row in load_yaml(ROOT / "config/v2/markets.yaml")["markets"]}
    assert "taiex" in markets


def test_taiex_linked_to_taipei():
    assert TAIEX_MARKET["city_id"] == "taipei"


def test_taiex_timezone():
    assert TAIEX_MARKET["timezone"] == "Asia/Taipei"


def test_taiex_currency():
    assert TAIEX_MARKET["currency"] == "TWD"


def test_taiex_session():
    assert TAIEX_MARKET["session"]["open"] == "09:00"
    assert TAIEX_MARKET["session"]["close"] == "13:30"
    assert TAIEX_MARKET["session"]["breaks"] == []


def test_taiex_calendar_source_registered():
    calendars = {row["calendar_source_id"] for row in load_yaml(ROOT / "config/v2/calendar-registry.yaml")["calendars"]}
    assert TAIEX_MARKET["calendar_source_id"] == "twse_official_holiday_schedule"
    assert "twse_official_holiday_schedule" in calendars


# ---------------- pilot-only / zero network ----------------

def test_pilot_only_resolves_taipei():
    config = pilot_config(ROOT)
    assert config["city_id"] == "taipei"
    assert config["market_id"] == "taiex"


def test_pilot_only_generates_only_taipei(capsys):
    code = era5_main(["--pilot-only", "--final"])  # default root = repository
    assert code == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["city_id"] == "taipei"
    assert audit["market_id"] == "taiex"


def test_default_command_zero_network(capsys):
    code = era5_main(["--pilot-only"])  # no --dry-run, no --live
    assert code == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["live_requests_run"] is False
    assert audit["weather_data_downloaded"] is False


def test_dry_run_zero_network_and_no_cdsapi_import(capsys):
    before = "cdsapi" in sys.modules
    code = era5_main(["--pilot-only", "--final", "--dry-run"])
    assert code == 0
    assert ("cdsapi" in sys.modules) == before  # dry-run must not import cdsapi
    audit = json.loads(capsys.readouterr().out)
    assert audit["live_requests_run"] is False
    assert audit["historical_backfill_run"] is False


def test_no_live_flag_never_calls_cds():
    # fetch_era5 main without --live returns the offline audit; the live branch
    # (which imports cdsapi and calls retrieve) sits behind args.live.
    import inspect

    source = inspect.getsource(era5_main)
    assert "--live" in source
    assert "--pilot-only" in source


# ---------------- UTC request planning ----------------

def utc_plan():
    tz = ZoneInfo("Asia/Taipei")
    return utc_request_plan(
        datetime(2026, 3, 2, 0, 0, tzinfo=tz),
        datetime(2026, 3, 7, 0, 0, tzinfo=tz),
    )


def test_utc_start_is_local_midnight_conversion():
    assert utc_plan()["utc_request_start"] == "2026-03-01T16:00:00+00:00"


def test_utc_end_includes_march7_local_midnight():
    assert utc_plan()["utc_request_end_inclusive"] == "2026-03-06T16:00:00+00:00"


def test_request_plan_is_minimal_full_coverage():
    plan = utc_plan()
    assert plan["requested_utc_dates"] == [
        "2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06",
    ]
    assert plan["left_padding_reason"] == "convert_first_local_midnight_to_utc"
    assert plan["right_padding_reason"] == "include_final_full_day_accumulation_endpoint"


def test_three_request_segments():
    plan = utc_plan()
    assert plan["request_count"] == 3
    assert plan["request_segments"][0]["utc_dates"] == ["2026-03-01"]
    assert plan["request_segments"][1]["utc_dates"] == ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
    assert plan["request_segments"][2]["utc_dates"] == ["2026-03-06"]


def test_121_distinct_utc_valid_times():
    plan = utc_plan()
    assert plan["requested_hour_count"] == 121
    assert plan["request_segments"][0]["hour_count"] == 8
    assert plan["request_segments"][1]["hour_count"] == 96
    assert plan["request_segments"][2]["hour_count"] == 17


# ---------------- window counts (TAIEX 4.5h session) ----------------

def test_pre_open_instantaneous_expected_2(taipei_windows):
    assert taipei_windows.loc["pre_open", "instantaneous_expected_count"] == 2


def test_pre_open_accumulation_expected_2(taipei_windows):
    assert taipei_windows.loc["pre_open", "accumulation_expected_count"] == 2


def test_trading_session_instantaneous_expected_5(taipei_windows):
    assert taipei_windows.loc["trading_session", "instantaneous_expected_count"] == 5


def test_trading_session_accumulation_expected_4(taipei_windows):
    assert taipei_windows.loc["trading_session", "accumulation_expected_count"] == 4


def test_13_14_accumulation_interval_excluded(taipei_windows):
    row = taipei_windows.loc["trading_session"]
    assert "partial_accumulation_interval_excluded" in row["quality_flags"]
    assert row["accumulation_observed_count"] == 4


def test_full_day_instantaneous_expected_24(taipei_windows):
    assert taipei_windows.loc["full_day", "instantaneous_expected_count"] == 24


def test_full_day_accumulation_expected_24(taipei_windows):
    assert taipei_windows.loc["full_day", "accumulation_expected_count"] == 24


def test_accumulation_does_not_use_instantaneous_mask(taipei_windows):
    trading = taipei_windows.loc["trading_session"]
    assert trading["instantaneous_observed_count"] == 5
    assert trading["accumulation_observed_count"] == 4


def test_precipitation_uses_sum(taipei_windows):
    assert taipei_windows.loc["pre_open", "precipitation_mm"] == pytest.approx(2.0)


def test_solar_radiation_uses_sum(taipei_windows):
    assert taipei_windows.loc["pre_open", "solar_radiation_mj_m2"] == pytest.approx(0.72)


def test_gust_uses_max():
    hourly = taipei_hourly()
    hourly.loc[hourly["timestamp_utc"] == pd.Timestamp("2026-03-02T00:00:00+08:00").tz_convert("UTC"), "wind_gust_mps"] = 25.0
    normalized = normalize_weather_frame(hourly, city_id="taipei", source_id="fixture", data_class="final_reanalysis")
    windows = build_weather_windows(normalized, TAIEX_MARKET, [date_from("2026-03-02")]).set_index("window")
    assert windows.loc["full_day", "max_gust_mps"] == pytest.approx(25.0)


# ---------------- missing-hour quality flags ----------------

def test_missing_instantaneous_hour_flagged():
    hourly = taipei_hourly()
    local_08 = pd.Timestamp("2026-03-02T08:00:00+08:00").tz_convert("UTC")
    hourly = hourly.loc[hourly["timestamp_utc"] != local_08]
    normalized = normalize_weather_frame(hourly, city_id="taipei", source_id="fixture", data_class="final_reanalysis")
    windows = build_weather_windows(normalized, TAIEX_MARKET, [date_from("2026-03-02")]).set_index("window")
    assert "missing_instantaneous_hours" in windows.loc["pre_open", "quality_flags"]


def test_missing_accumulation_interval_flagged():
    hourly = taipei_hourly()
    local_09 = pd.Timestamp("2026-03-02T09:00:00+08:00").tz_convert("UTC")  # ends 08:00-09:00 interval
    hourly = hourly.loc[hourly["timestamp_utc"] != local_09]
    normalized = normalize_weather_frame(hourly, city_id="taipei", source_id="fixture", data_class="final_reanalysis")
    windows = build_weather_windows(normalized, TAIEX_MARKET, [date_from("2026-03-02")]).set_index("window")
    assert "missing_accumulation_intervals" in windows.loc["pre_open", "quality_flags"]


# ---------------- CDS credential readiness ----------------

def test_cds_readiness_missing(tmp_path):
    readiness = cds_readiness(ROOT, home=tmp_path)
    assert readiness["cdsapirc_status"] == "missing"
    assert readiness["credential_readiness_status"] in {"cdsapi_missing", "credential_file_missing"}


def test_cds_readiness_invalid_shape(tmp_path):
    (tmp_path / ".cdsapirc").write_text("not: [valid", encoding="utf-8")
    readiness = cds_readiness(ROOT, home=tmp_path)
    assert readiness["cdsapirc_status"] == "invalid_shape"
    assert readiness["credential_readiness_status"] in {"credential_file_invalid", "dataset_terms_acceptance_unverified"}


def test_cds_readiness_does_not_leak_key(tmp_path):
    (tmp_path / ".cdsapirc").write_text("url: https://cds.climate.copernicus.eu/api\nkey: TESTKEY\n", encoding="utf-8")
    readiness = cds_readiness(ROOT, home=tmp_path)
    assert readiness["cdsapirc_status"] == "present_shape_valid"
    serialized = json.dumps(readiness)
    assert "TESTKEY" not in serialized
    assert "https://cds.climate.copernicus.eu/api" not in serialized


def test_terms_unverified_blocks_ready(tmp_path):
    (tmp_path / ".cdsapirc").write_text("url: https://cds.climate.copernicus.eu/api\nkey: TESTKEY\n", encoding="utf-8")
    readiness = cds_readiness(ROOT, home=tmp_path)
    assert readiness["dataset_terms_status"] == "acceptance_unverified"
    assert readiness["credential_readiness_status"] != "ready_for_future_live_request"


def test_cds_readiness_never_reports_ready_without_terms(tmp_path):
    (tmp_path / ".cdsapirc").write_text("url: https://cds.climate.copernicus.eu/api\nkey: TESTKEY\n", encoding="utf-8")
    readiness = cds_readiness(ROOT, home=tmp_path)
    assert readiness["credential_readiness_status"] == "dataset_terms_acceptance_unverified"


# ---------------- finality ----------------

def test_finality_expected_final_for_march_2026():
    result = finality_check(date_from("2026-03-02"), date_from("2026-03-06"), True, datetime(2026, 8, 6).date())
    assert result["expected_data_class"] == "final_reanalysis"
    assert result["finality_status"] == "expected_final_by_official_latency"


def test_finality_provisional_without_final_flag():
    result = finality_check(date_from("2026-03-02"), date_from("2026-03-06"), False, datetime(2026, 8, 6).date())
    assert result["expected_data_class"] == "provisional_reanalysis"


# ---------------- dry-run audit file ----------------

def test_dry_run_audit_file_written(capsys):
    code = era5_main(["--pilot-only", "--final"])  # default root writes the repo audit
    assert code == 0
    audit_path = ROOT / "data/audits/v2/weather-pilot/taipei-era5-dry-run.json"
    assert audit_path.exists()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["live_requests_run"] is False
    assert audit["weather_data_downloaded"] is False
    assert audit["historical_backfill_run"] is False
    assert audit["requested_hour_count"] == 121
    assert audit["request_count"] == 3
    assert audit["expected_data_class"] == "final_reanalysis"


# ---------------- frozen status regressions ----------------

def test_aex_status_unchanged():
    scope = load_yaml(ROOT / "config/v2/pilot-scope.yaml")
    by_id = {row["market_id"]: row for row in scope["excluded_or_deferred_markets"]}
    assert by_id["aex_dnb"]["status"] == "technical_access_blocked"


def test_kospi_status_unchanged():
    scope = load_yaml(ROOT / "config/v2/pilot-scope.yaml")
    by_id = {row["market_id"]: row for row in scope["excluded_or_deferred_markets"]}
    assert by_id["kospi"]["status"] == "credential_required"


def test_taiex_market_pilot_status_unchanged():
    market_audit = json.loads(
        (ROOT / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json").read_text(encoding="utf-8")
    )
    assert market_audit["adapter_status"] == "market_only_pilot_accepted"
    assert market_audit["production_status"] == "not_connected"
    assert market_audit["research_ready"] is False
    assert market_audit["frozen"] is False


def test_full_history_calendar_verified_still_false():
    window_audit = json.loads(
        (ROOT / "data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json").read_text(encoding="utf-8")
    )
    assert window_audit["full_history_calendar_verified"] is False
