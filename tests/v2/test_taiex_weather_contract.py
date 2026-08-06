"""Tests for the frozen Taipei ERA5 pilot contract and dry-run readiness.

Includes an offline simulation of the live branch using an injected fake CDS
client, finality-gate checks, path-leak and staging-safety checks, and
dry-run side-effect control.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import xarray as xr

from scripts.v2.core import (
    V2Error,
    build_weather_windows,
    load_yaml,
    normalize_weather_frame,
    repo_root,
)
from scripts.v2.fetch_era5 import (
    NETCDF_VARIABLES,
    cds_readiness,
    expected_timestamps,
    finality_check,
    main as era5_main,
    pilot_config,
    utc_request_plan,
    validate_netcdf,
)

ROOT = repo_root()
TAIEX_MARKET = next(
    market for market in load_yaml(ROOT / "config" / "v2" / "markets.yaml")["markets"]
    if market["market_id"] == "taiex"
)


def date_from(value: str):
    from datetime import date as _date

    return _date.fromisoformat(value)


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


class FakeCDSClient:
    """Offline fake; records calls and writes minimal legal NetCDF fixtures.

    ``fault`` optionally corrupts the timestamp set: "drop_last" removes the
    final hour, "extra_hour" appends an unrequested hour, "duplicate" repeats
    the first timestamp.
    """

    def __init__(self, fault: str | None = None):
        self.calls = []
        self.fault = fault

    def retrieve(self, dataset, request, target):
        self.calls.append({"dataset": dataset, "request": request})
        timestamps = []
        for day in request["date"]:
            for hour in request["time"]:
                timestamps.append(pd.Timestamp(f"{day}T{hour}"))  # hour is "HH:00"
        ts = pd.DatetimeIndex(timestamps)
        if self.fault == "drop_last":
            ts = ts[:-1]
        elif self.fault == "extra_hour":
            ts = ts.append(pd.DatetimeIndex([ts[-1] + pd.Timedelta(hours=1)]))
        elif self.fault == "duplicate":
            ts = ts.append(pd.DatetimeIndex([ts[0]]))
        fixture = xr.Dataset(
            {
                "t2m": ("valid_time", [283.15] * len(ts)),
                "d2m": ("valid_time", [278.15] * len(ts)),
                "tp": ("valid_time", [0.001] * len(ts)),
                "tcc": ("valid_time", [0.5] * len(ts)),
                "u10": ("valid_time", [3.0] * len(ts)),
                "v10": ("valid_time", [4.0] * len(ts)),
                "i10fg": ("valid_time", [5.0] * len(ts)),
                "ssrd": ("valid_time", [360000.0] * len(ts)),
            },
            coords={"valid_time": ts},
        )
        fixture.to_netcdf(target)


def fake_readiness() -> dict:
    return {
        "cdsapi_installed": True,
        "cdsapi_version": "0.0.0",
        "cdsapirc_status": "present_shape_valid",
        "url_field_present": True,
        "key_field_present": True,
        "dataset_terms_status": "user_confirmed_outside_task",
        "terms_confirmation_file_present": True,
        "credential_readiness_status": "ready_for_future_live_request",
    }


def make_live_root(tmp_path: Path) -> Path:
    for folder in ("config", "schemas"):
        shutil.copytree(ROOT / folder / "v2", tmp_path / folder / "v2")
    return tmp_path


# ---------------- configuration registration ----------------

def test_taipei_in_locations():
    locations = {row["city_id"]: row for row in load_yaml(ROOT / "config/v2/locations.yaml")["locations"]}
    assert "taipei" in locations
    assert locations["taipei"]["timezone"] == "Asia/Taipei"
    assert locations["taipei"]["coordinate_evidence_status"] == "official_address_with_derived_coordinate"


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
    code = era5_main(["--pilot-only", "--final"])
    assert code == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["city_id"] == "taipei"
    assert audit["market_id"] == "taiex"


def test_default_command_zero_network(capsys):
    code = era5_main(["--pilot-only"])
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


def test_three_request_segments_with_stable_ids():
    plan = utc_plan()
    assert plan["request_count"] == 3
    assert plan["request_segments"][0]["segment_id"] == "segment-01-2026-03-01"
    assert plan["request_segments"][1]["segment_id"] == "segment-02-2026-03-02-to-2026-03-05"
    assert plan["request_segments"][2]["segment_id"] == "segment-03-2026-03-06"


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


def test_trading_expected_hour_count_has_no_4_5_conflict(taipei_windows):
    row = taipei_windows.loc["trading_session"]
    assert row["expected_hour_count"] == 5
    assert row["instantaneous_expected_count"] == 5
    assert row["accumulation_expected_count"] == 4


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
    assert "missing_hours" in windows.loc["pre_open", "quality_flags"]
    assert "missing_instantaneous_hours" in windows.loc["pre_open", "quality_flags"]


def test_missing_accumulation_interval_flagged():
    hourly = taipei_hourly()
    local_09 = pd.Timestamp("2026-03-02T09:00:00+08:00").tz_convert("UTC")  # ends 08:00-09:00 interval
    hourly = hourly.loc[hourly["timestamp_utc"] != local_09]
    normalized = normalize_weather_frame(hourly, city_id="taipei", source_id="fixture", data_class="final_reanalysis")
    windows = build_weather_windows(normalized, TAIEX_MARKET, [date_from("2026-03-02")]).set_index("window")
    assert "missing_accumulation_intervals" in windows.loc["pre_open", "quality_flags"]


# ---------------- finality gate ----------------

def test_finality_march_2026_allowed_in_august():
    result = finality_check(date_from("2026-03-02"), date_from("2026-03-06"), True, datetime(2026, 8, 6).date())
    assert result["final_eligibility_date"] == "2026-07-01"
    assert result["finality_status"] == "expected_final_by_official_latency"
    assert result["expected_data_class"] == "final_reanalysis"


def test_finality_march_2026_not_allowed_in_april():
    result = finality_check(date_from("2026-03-02"), date_from("2026-03-06"), True, datetime(2026, 4, 1).date())
    assert result["finality_status"] == "not_yet_eligible_for_final"
    assert result["expected_data_class"] == "provisional_reanalysis"


def test_finality_current_month_not_allowed():
    result = finality_check(date_from("2026-08-01"), date_from("2026-08-07"), True, datetime(2026, 8, 6).date())
    assert result["finality_status"] == "not_yet_eligible_for_final"


def test_finality_without_final_flag_keeps_provisional():
    result = finality_check(date_from("2026-03-02"), date_from("2026-03-06"), False, datetime(2026, 8, 6).date())
    assert result["expected_data_class"] == "provisional_reanalysis"
    assert result["finality_status"] == "provisional_by_explicit_flag"


def test_live_final_rejected_when_gate_not_met(tmp_path):
    root = make_live_root(tmp_path)
    with pytest.raises(SystemExit, match="finality gate"):
        era5_main(
            ["--city", "taipei", "--start-date", "2026-07-01", "--end-date", "2026-07-03", "--final", "--live", "--root", str(root)],
            client_factory=lambda: FakeCDSClient(),
            readiness=fake_readiness(),
        )


# ---------------- live offline simulation ----------------

def test_live_simulation_runs_three_segments(tmp_path, capsys):
    root = make_live_root(tmp_path)
    client = FakeCDSClient()
    code = era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: client,
        readiness=fake_readiness(),
    )
    assert code == 0
    assert len(client.calls) == 3
    assert client.calls[0]["dataset"] == "reanalysis-era5-single-levels"
    assert client.calls[0]["request"]["date"] == ["2026-03-01"]
    assert client.calls[0]["request"]["time"][0] == "16:00"
    assert client.calls[0]["request"]["time"][-1] == "23:00"
    assert len(client.calls[0]["request"]["time"]) == 8
    assert client.calls[1]["request"]["date"] == ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
    assert len(client.calls[1]["request"]["time"]) == 24
    assert client.calls[2]["request"]["date"] == ["2026-03-06"]
    assert client.calls[2]["request"]["time"][0] == "00:00"
    assert client.calls[2]["request"]["time"][-1] == "16:00"
    assert len(client.calls[2]["request"]["time"]) == 17
    assert "2m_temperature" in client.calls[0]["request"]["variable"]
    assert len(client.calls[0]["request"]["area"]) == 4
    # results must not contain absolute local paths
    serialized = capsys.readouterr().out
    assert ":\\" not in serialized
    assert "D:" not in serialized


def test_live_uses_utc_dates_list_not_singular_key(tmp_path):
    root = make_live_root(tmp_path)
    client = FakeCDSClient()
    era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: client,
        readiness=fake_readiness(),
    )
    # multi-date segment passes the full date list; a KeyError on
    # segment["utc_date"] would have raised before retrieve was reached
    assert client.calls[1]["request"]["date"] == ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]


def test_live_uses_temp_staging_no_fixed_path(tmp_path, capsys):
    root = make_live_root(tmp_path)
    era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: FakeCDSClient(),
        readiness=fake_readiness(),
    )
    assert not (root / ".local/cds-stage").exists()


def test_live_results_report_relative_manifest(tmp_path, capsys):
    root = make_live_root(tmp_path)
    era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: FakeCDSClient(),
        readiness=fake_readiness(),
    )
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 3
    for item in results:
        assert "segment_id" in item
        assert "artifact_id" in item
        assert "revision" in item
        assert "sha256" in item
        assert not Path(item["manifest_path"]).is_absolute()
        assert ":\\" not in item["manifest_path"]
        assert item["manifest_path"].replace("\\", "/").startswith("data/source_raw/")


# ---------------- staging validation ----------------

def test_staged_empty_file_fails(tmp_path):
    path = tmp_path / "empty.nc"
    path.write_bytes(b"")
    with pytest.raises(V2Error, match="empty"):
        validate_netcdf(path, {"date": ["2026-03-01"], "time": ["16:00"]})


def test_staged_non_netcdf_fails(tmp_path):
    path = tmp_path / "bad.nc"
    path.write_bytes(b"this is not a netcdf file")
    with pytest.raises(V2Error):
        validate_netcdf(path, {"date": ["2026-03-01"], "time": ["16:00"]})


def test_staged_missing_variable_fails(tmp_path):
    timestamps = pd.DatetimeIndex(["2026-03-01T16:00:00"])
    dataset = xr.Dataset({"t2m": ("valid_time", [283.15])}, coords={"valid_time": timestamps})
    path = tmp_path / "partial.nc"
    dataset.to_netcdf(path)
    with pytest.raises(V2Error, match="missing requested variables"):
        validate_netcdf(path, {"date": ["2026-03-01"], "time": ["16:00"]})


# ---------------- CDS credential readiness ----------------

def test_cds_readiness_missing(tmp_path):
    readiness = cds_readiness(ROOT, home=tmp_path)
    assert readiness["cdsapirc_status"] == "missing"
    assert readiness["credential_readiness_status"] in {"cdsapi_missing", "credential_file_missing"}


def test_cds_readiness_invalid_shape(tmp_path):
    (tmp_path / ".cdsapirc").write_text("not: [valid", encoding="utf-8")
    readiness = cds_readiness(ROOT, home=tmp_path)
    assert readiness["cdsapirc_status"] == "invalid_shape"
    assert readiness["credential_readiness_status"] == "credential_file_invalid"


def test_cds_readiness_returns_booleans_not_values(tmp_path):
    (tmp_path / ".cdsapirc").write_text("url: https://example.invalid/api\nkey: TESTKEY\n", encoding="utf-8")
    readiness = cds_readiness(ROOT, home=tmp_path)
    assert readiness["url_field_present"] is True
    assert readiness["key_field_present"] is True
    serialized = json.dumps(readiness)
    assert "TESTKEY" not in serialized
    assert "https://example.invalid/api" not in serialized


def test_terms_confirmation_missing_blocks_ready(tmp_path):
    (tmp_path / ".cdsapirc").write_text("url: https://example.invalid/api\nkey: TESTKEY\n", encoding="utf-8")
    # Isolated root without a terms confirmation file -> acceptance_unverified.
    readiness = cds_readiness(tmp_path, home=tmp_path)
    assert readiness["dataset_terms_status"] == "acceptance_unverified"
    assert readiness["credential_readiness_status"] == "dataset_terms_acceptance_unverified"


def test_terms_confirmation_file_present_and_valid_in_repo():
    # The user explicitly confirmed dataset terms outside the task; the
    # local-only record exists and is recognised by the readiness logic.
    from scripts.v2.fetch_era5 import _terms_confirmation

    assert (ROOT / ".local/agreements/cds-era5-single-levels.json").exists()
    assert _terms_confirmation(ROOT)["dataset_terms_status"] == "user_confirmed_outside_task"


# ---------------- dry-run side effects ----------------

def test_default_dry_run_writes_no_files(tmp_path):
    root = make_live_root(tmp_path)
    code = era5_main(["--pilot-only", "--final", "--root", str(root)])
    assert code == 0
    assert not (root / "data/audits/v2/weather-pilot").exists()


def test_audit_output_writes_only_with_flag(tmp_path):
    root = make_live_root(tmp_path)
    code = era5_main(
        ["--pilot-only", "--final", "--root", str(root),
         "--audit-output", "data/audits/v2/weather-pilot/taipei-era5-dry-run.json"]
    )
    assert code == 0
    audit_path = root / "data/audits/v2/weather-pilot/taipei-era5-dry-run.json"
    assert audit_path.exists()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["live_requests_run"] is False
    assert audit["weather_data_downloaded"] is False
    assert audit["historical_backfill_run"] is False
    assert audit["requested_hour_count"] == 121
    assert audit["request_count"] == 3
    assert audit["final_eligibility_date"] == "2026-07-01"
    assert audit["expected_data_class"] == "final_reanalysis"
    assert audit["coordinate_evidence"]["coordinate_evidence_status"] == "official_address_with_derived_coordinate"


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


# ---------------- exact UTC timestamp-set validation ----------------

def make_netcdf(path: Path, timestamps) -> None:
    values = []
    for timestamp in timestamps:
        if getattr(timestamp, "tzinfo", None) is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)  # naive UTC
        values.append(timestamp)
    ts = pd.DatetimeIndex(values)
    dataset = xr.Dataset(
        {name: ("valid_time", [283.15] * len(ts)) for name in NETCDF_VARIABLES},
        coords={"valid_time": ts},
    )
    dataset.to_netcdf(path)


def single_day_request() -> dict:
    return {"date": ["2026-03-01"], "time": [f"{h:02d}:00" for h in range(16, 24)]}


def test_single_day_full_set_passes(tmp_path):
    request = single_day_request()
    expected = expected_timestamps(request)
    assert len(expected) == 8
    path = tmp_path / "ok.nc"
    make_netcdf(path, sorted(expected))
    summary = validate_netcdf(path, request)
    assert summary["netcdf_validation_passed"] is True
    assert summary["expected_timestamp_count"] == 8
    assert summary["observed_timestamp_count"] == 8
    assert summary["timestamp_set_match"] is True
    assert summary["missing_timestamps"]["count"] == 0
    assert summary["unexpected_timestamps"]["count"] == 0


def test_multi_day_96_points_pass(tmp_path):
    request = {
        "date": ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"],
        "time": [f"{h:02d}:00" for h in range(24)],
    }
    expected = expected_timestamps(request)
    assert len(expected) == 96
    path = tmp_path / "ok.nc"
    make_netcdf(path, sorted(expected))
    summary = validate_netcdf(path, request)
    assert summary["netcdf_validation_passed"] is True
    assert summary["observed_timestamp_count"] == 96


def test_missing_hour_rejected(tmp_path):
    request = single_day_request()
    expected = sorted(expected_timestamps(request))
    path = tmp_path / "missing.nc"
    make_netcdf(path, expected[:-1])  # drop 23:00
    with pytest.raises(V2Error, match="missing"):
        validate_netcdf(path, request)


def test_wrong_hour_rejected(tmp_path):
    request = single_day_request()
    expected = sorted(expected_timestamps(request))
    wrong = [datetime(2026, 3, 1, 5, tzinfo=timezone.utc)] + expected[1:-1]  # 05:00 instead of 16:00
    path = tmp_path / "wrong.nc"
    make_netcdf(path, sorted(wrong))
    with pytest.raises(V2Error):
        validate_netcdf(path, request)


def test_extra_hour_rejected(tmp_path):
    from datetime import timedelta as _timedelta

    request = single_day_request()
    expected = sorted(expected_timestamps(request))
    extra = expected + [expected[-1] + _timedelta(hours=1)]  # 2026-03-02 00:00 not requested
    path = tmp_path / "extra.nc"
    make_netcdf(path, extra)
    with pytest.raises(V2Error, match="unexpected"):
        validate_netcdf(path, request)


def test_duplicate_timestamp_rejected(tmp_path):
    request = single_day_request()
    expected = sorted(expected_timestamps(request))
    duplicated = expected + [expected[0]]
    path = tmp_path / "dup.nc"
    make_netcdf(path, duplicated)
    with pytest.raises(V2Error, match="duplicate"):
        validate_netcdf(path, request)


def test_non_hourly_timestamp_rejected(tmp_path):
    request = single_day_request()
    timestamps = [datetime(2026, 3, 1, h, 30, tzinfo=timezone.utc) for h in range(16, 24)]  # :30 offsets
    path = tmp_path / "half.nc"
    make_netcdf(path, timestamps)
    with pytest.raises(V2Error, match="non-hourly"):
        validate_netcdf(path, request)


def test_naive_timestamps_treated_as_utc(tmp_path):
    request = single_day_request()
    naive = [datetime(2026, 3, 1, h) for h in range(16, 24)]  # no tzinfo
    path = tmp_path / "naive.nc"
    make_netcdf(path, naive)
    summary = validate_netcdf(path, request)
    assert summary["netcdf_validation_passed"] is True


def test_live_segments_all_timestamp_set_match(tmp_path, capsys):
    root = make_live_root(tmp_path)
    era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: FakeCDSClient(),
        readiness=fake_readiness(),
    )
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 3
    for item in results:
        assert item["timestamp_validation"]["timestamp_set_match"] is True
        assert item["timestamp_validation"]["netcdf_validation_passed"] is True


def test_live_validation_failure_persists_nothing(tmp_path):
    root = make_live_root(tmp_path)
    client = FakeCDSClient(fault="drop_last")
    with pytest.raises(V2Error, match="missing"):
        era5_main(
            ["--pilot-only", "--final", "--live", "--root", str(root)],
            client_factory=lambda: client,
            readiness=fake_readiness(),
        )
    store_dir = root / "data/source_raw/v2/weather"
    manifests = list(store_dir.rglob("*.manifest.json")) if store_dir.exists() else []
    assert manifests == []


def test_validation_error_has_no_absolute_path(tmp_path):
    request = single_day_request()
    expected = sorted(expected_timestamps(request))
    path = tmp_path / "missing.nc"
    make_netcdf(path, expected[:-1])
    with pytest.raises(V2Error) as exc:
        validate_netcdf(path, request)
    message = str(exc.value)
    assert ":\\" not in message
    assert "D:" not in message
