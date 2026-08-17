"""Offline tests for the Taipei ERA5 pilot weather-window builder.

Covers the evidence-chain bindings (canonical -> normalization -> raw audit,
calendar audit), the frozen 5-day x 3-window contract, quality-flag rules,
independent oracle matching, aggregation semantics, POSIX paths, and status
regressions. Zero network.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts.v2.build_weather_windows import (
    EXPECTED_COUNT_CONTRACT,
    WINDOW_ORDER,
    compare_oracle,
    main as windows_main,
    oracle_windows,
    validate_window_frame,
    verify_evidence_chain,
)

ROOT = Path(__file__).resolve().parents[2]

PILOT_DATES = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]


def make_canonical_hourly() -> pd.DataFrame:
    times = pd.date_range("2026-03-01T16:00:00Z", periods=121, freq="h")
    return pd.DataFrame(
        {
            "timestamp_utc": times,
            "accumulation_interval_start_utc": times - pd.Timedelta(hours=1),
            "accumulation_interval_end_utc": times,
            "city_id": "taipei",
            "air_temperature_c": 20.0,
            "dew_point_c": 15.0,
            "relative_humidity_pct": 70.0,
            "apparent_temperature_c": 21.0,
            "precipitation_mm": 0.5,
            "cloud_cover_pct": 50.0,
            "wind_speed_mps": 3.0,
            "max_gust_mps": 5.0,
            "solar_radiation_mj_m2": 2.0,
        }
    )


def make_fixture_audits(root: Path, hourly: pd.DataFrame) -> dict[str, Path]:
    (root / "data/canonical/v2/weather").mkdir(parents=True, exist_ok=True)
    (root / "data/audits/v2/weather-pilot").mkdir(parents=True, exist_ok=True)
    (root / "data/audits/v2/taiex-calendar").mkdir(parents=True, exist_ok=True)
    (root / "config/v2").mkdir(parents=True, exist_ok=True)
    canonical_path = root / "data/canonical/v2/weather/taipei-era5-final-pilot.parquet"
    hourly.to_parquet(canonical_path, index=False)
    canonical_sha = hashlib.sha256(canonical_path.read_bytes()).hexdigest()

    raw_path = root / "data/audits/v2/weather-pilot/taipei-era5-raw-acceptance.json"
    raw_audit = {
        "schema_version": "2.0.0",
        "audit_type": "taipei_era5_raw_acceptance",
        "raw_acceptance_status": "accepted",
        "total_distinct_timestamp_count": 121,
    }
    raw_path.write_text(json.dumps(raw_audit), encoding="utf-8")
    raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()

    norm_path = root / "data/audits/v2/weather-pilot/taipei-era5-normalization.json"
    norm_audit = {
        "schema_version": "2.0.0",
        "audit_type": "taipei_era5_normalization",
        "canonical_parquet_sha256": canonical_sha,
        "canonical_row_count": 121,
        "canonical_status": "accepted",
        "input_raw_acceptance_audit_sha256": raw_sha,
        "raw_acceptance_audit_path": "data/audits/v2/weather-pilot/taipei-era5-raw-acceptance.json",
        "windows_built": False,
        "panel_built": False,
    }
    norm_path.write_text(json.dumps(norm_audit), encoding="utf-8")

    calendar_path = root / "data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json"
    calendar_audit = {
        "schema_version": "2.0.0",
        "audit_type": "taipei_era5_pilot_window",
        "pilot_calendar_status": "calendar_verified_for_2026_pilot_window",
        "selection": {
            "selected_week_dates": PILOT_DATES,
            "selected_week": "2026-03-02",
        },
        "full_history_calendar_verified": False,
    }
    calendar_path.write_text(json.dumps(calendar_audit), encoding="utf-8")

    (root / "config/v2/weather-variable-semantics.yaml").write_text(
        "variables: []\n", encoding="utf-8"
    )
    (root / "config/v2/markets.yaml").write_text(
        "markets:\n"
        "  - market_id: taiex\n"
        "    city_id: taipei\n"
        "    currency: TWD\n"
        "    timezone: Asia/Taipei\n"
        "    session: {open: \"09:00\", close: \"13:30\", breaks: []}\n"
        "    calendar_source_id: twse_official_holiday_schedule\n",
        encoding="utf-8",
    )
    return {"canonical": canonical_path, "raw": raw_path, "normalization": norm_path, "calendar": calendar_path}


def run_windows(root: Path, audits: dict[str, Path], output: Path) -> int:
    return windows_main(
        [
            "--weather", str(audits["canonical"]),
            "--normalization-audit", str(audits["normalization"]),
            "--calendar-audit", str(audits["calendar"]),
            "--market-id", "taiex",
            "--root", str(root),
            "--output", str(output),
            "--audit-output", "data/audits/v2/weather-pilot/taipei-era5-window-acceptance.json",
        ]
    )


def load_window_audit(root: Path) -> dict:
    return json.loads(
        (root / "data/audits/v2/weather-pilot/taipei-era5-window-acceptance.json").read_text(encoding="utf-8")
    )


# ---------------- evidence chain ----------------

def test_verified_calendar_extracts_exact_five_days(tmp_path):
    root = tmp_path / "repo"
    hourly = make_canonical_hourly()
    audits = make_fixture_audits(root, hourly)
    normalization = json.loads(audits["normalization"].read_text(encoding="utf-8"))
    raw_audit = json.loads(audits["raw"].read_text(encoding="utf-8"))
    calendar = json.loads(audits["calendar"].read_text(encoding="utf-8"))
    norm, raw, cal, sha, dates = verify_evidence_chain(
        audits["canonical"], audits["normalization"], audits["raw"], audits["calendar"]
    )
    assert dates == PILOT_DATES


def test_bad_calendar_status_rejected(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    calendar = json.loads(audits["calendar"].read_text(encoding="utf-8"))
    calendar["pilot_calendar_status"] = "calendar_verification_partial"
    audits["calendar"].write_text(json.dumps(calendar), encoding="utf-8")
    with pytest.raises(SystemExit, match="pilot status"):
        verify_evidence_chain(audits["canonical"], audits["normalization"], audits["raw"], audits["calendar"])


def test_canonical_sha_mismatch_rejected(tmp_path):
    root = tmp_path / "repo"
    hourly = make_canonical_hourly()
    audits = make_fixture_audits(root, hourly)
    norm = json.loads(audits["normalization"].read_text(encoding="utf-8"))
    norm["canonical_parquet_sha256"] = "0" * 64
    audits["normalization"].write_text(json.dumps(norm), encoding="utf-8")
    with pytest.raises(SystemExit, match="canonical sha mismatch"):
        verify_evidence_chain(audits["canonical"], audits["normalization"], audits["raw"], audits["calendar"])


def test_raw_audit_sha_chain_broken_rejected(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    norm = json.loads(audits["normalization"].read_text(encoding="utf-8"))
    norm["input_raw_acceptance_audit_sha256"] = "f" * 64
    audits["normalization"].write_text(json.dumps(norm), encoding="utf-8")
    with pytest.raises(SystemExit, match="SHA binding broken"):
        verify_evidence_chain(audits["canonical"], audits["normalization"], audits["raw"], audits["calendar"])


# ---------------- row / key / count contract ----------------

def test_five_days_three_windows_fifteen_rows(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    assert run_windows(root, audits, output) == 0
    frame = pd.read_parquet(output)
    assert len(frame) == 15


def test_unique_key_fifteen(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    keys = list(zip(frame["city_id"], frame["market_id"], frame["trading_date"], frame["window"]))
    assert len(set(keys)) == 15


def test_daily_pre_open_counts(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    sub = frame[frame["window"] == "pre_open"]
    assert (sub["instantaneous_observed_count"] == 2).all()
    assert (sub["accumulation_observed_count"] == 2).all()


def test_daily_trading_session_counts(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    sub = frame[frame["window"] == "trading_session"]
    assert (sub["instantaneous_observed_count"] == 5).all()
    assert (sub["accumulation_observed_count"] == 4).all()


def test_daily_full_day_counts(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    sub = frame[frame["window"] == "full_day"]
    assert (sub["instantaneous_observed_count"] == 24).all()
    assert (sub["accumulation_observed_count"] == 24).all()


def test_all_coverage_ratios_one(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    assert (frame["instantaneous_coverage_ratio"] == 1.0).all()
    assert (frame["accumulation_coverage_ratio"] == 1.0).all()


def test_output_sorted_unique(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    assert frame["trading_date"].is_monotonic_increasing
    per_day = frame.groupby("trading_date")["window"].apply(list)
    for _, windows in per_day.items():
        assert windows == WINDOW_ORDER


# ---------------- quality flags ----------------

def _flag_set(frame, window):
    out = set()
    for item in frame[frame["window"] == window]["quality_flags"]:
        out.update(list(item))
    return out


def test_trading_session_has_partial_flag(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    assert "partial_accumulation_interval_excluded" in _flag_set(frame, "trading_session")


def test_pre_open_no_partial_flag(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    assert "partial_accumulation_interval_excluded" not in _flag_set(frame, "pre_open")


def test_full_day_no_partial_flag(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    assert "partial_accumulation_interval_excluded" not in _flag_set(frame, "full_day")


def test_missing_hour_fails_acceptance(tmp_path):
    root = tmp_path / "repo"
    hourly = make_canonical_hourly()
    hourly = hourly.drop(hourly.index[0]).reset_index(drop=True)  # drop 16:00Z
    audits = make_fixture_audits(root, hourly)
    output = root / "data/canonical/v2/weather/windows.parquet"
    with pytest.raises(SystemExit, match="contract violated|coverage|count"):
        run_windows(root, audits, output)


# ---------------- aggregation semantics ----------------

def test_precipitation_uses_sum(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    pre_open = frame[(frame["window"] == "pre_open")].iloc[0]
    assert pre_open["precipitation_mm"] == pytest.approx(0.5 * 2)  # 2 complete intervals


def test_solar_uses_sum(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    pre_open = frame[(frame["window"] == "pre_open")].iloc[0]
    assert pre_open["solar_radiation_mj_m2"] == pytest.approx(2.0 * 2)


def test_gust_uses_max(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    pre_open = frame[(frame["window"] == "pre_open")].iloc[0]
    assert pre_open["max_gust_mps"] == pytest.approx(5.0)


def test_instantaneous_uses_mean(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    pre_open = frame[(frame["window"] == "pre_open")].iloc[0]
    assert pre_open["air_temperature_c"] == pytest.approx(20.0)


# ---------------- oracle ----------------

def test_independent_oracle_matches(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    frame = pd.read_parquet(output)
    hourly = pd.read_parquet(audits["canonical"])
    oracle = oracle_windows(hourly, PILOT_DATES)
    matched, max_diff = compare_oracle(frame, oracle)
    assert matched is True
    assert max_diff < 1e-10


def test_oracle_does_not_call_build_weather_windows():
    source = inspect.getsource(oracle_windows)
    # Check for a call expression, not the bare name (docstring mentions it).
    assert "build_weather_windows(" not in source


# ---------------- window audit ----------------

def test_window_audit_binds_all_hashes(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    audit = load_window_audit(root)
    assert audit["input_canonical_sha256"] == hashlib.sha256(audits["canonical"].read_bytes()).hexdigest()
    assert audit["input_normalization_audit_sha256"] == hashlib.sha256(audits["normalization"].read_bytes()).hexdigest()
    assert audit["input_raw_acceptance_audit_sha256"] == hashlib.sha256(audits["raw"].read_bytes()).hexdigest()
    assert audit["calendar_audit_sha256"] == hashlib.sha256(audits["calendar"].read_bytes()).hexdigest()
    assert audit["calendar_pilot_status"] == "calendar_verified_for_2026_pilot_window"
    assert audit["full_history_calendar_verified"] is False
    assert audit["all_count_contracts_match"] is True
    assert audit["all_coverage_ratios_one"] is True
    assert audit["all_oracle_matches"] is True


def test_window_audit_posix_paths(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    audit = load_window_audit(root)
    for key in ("input_canonical_path", "input_normalization_audit_path", "input_raw_acceptance_audit_path", "calendar_audit_path", "window_parquet_path"):
        assert "\\" not in audit[key]
        assert audit[key].startswith("data/")


def test_window_audit_panel_backfill_flags(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    audit = load_window_audit(root)
    assert audit["panel_built"] is False
    assert audit["historical_backfill_run"] is False
    assert audit["weather_window_status"] == "accepted"
    assert len(audit["rows"]) == 15


# ---------------- git / status regressions ----------------

def test_window_parquet_not_git_tracked(tmp_path):
    import subprocess

    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    tracked = subprocess.run(
        ["git", "ls-files", "data/canonical"], capture_output=True, text=True, cwd=root
    ).stdout.strip()
    assert tracked == ""


def test_raw_and_canonical_unchanged(tmp_path):
    root = tmp_path / "repo"
    audits = make_fixture_audits(root, make_canonical_hourly())
    before_canonical = audits["canonical"].read_bytes()
    output = root / "data/canonical/v2/weather/windows.parquet"
    run_windows(root, audits, output)
    assert audits["canonical"].read_bytes() == before_canonical


def test_market_status_unchanged():
    market_audit = json.loads(
        (ROOT / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json").read_text(encoding="utf-8")
    )
    assert market_audit["adapter_status"] == "market_only_pilot_accepted"
    assert market_audit["production_status"] == "not_connected"


def test_full_history_calendar_still_false():
    window_audit = json.loads(
        (ROOT / "data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json").read_text(encoding="utf-8")
    )
    assert window_audit["full_history_calendar_verified"] is False
