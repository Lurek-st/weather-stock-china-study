from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts.v2.core import (
    backfill_estimate,
    build_panel,
    build_weather_windows,
    fixture_frames,
    load_yaml,
    normalize_market_frame,
    repo_root,
    write_panel_bundle,
)
from scripts.v2.validate_v2_data import validate_panel


def fixture_pipeline():
    root = repo_root()
    weather, market_raw, dates = fixture_frames(root, "2026-W28")
    markets = load_yaml(root / "config" / "v2" / "markets.yaml")["markets"]
    windows = pd.concat(
        [build_weather_windows(weather, item, dates) for item in markets],
        ignore_index=True,
    )
    panel = build_panel(
        normalize_market_frame(market_raw),
        windows,
        load_yaml(root / "config" / "v2" / "locations.yaml"),
        "provisional",
    )
    return panel


def test_eight_city_market_mapping_and_windows():
    panel = fixture_pipeline()
    assert len(panel) == 40
    assert panel["city_id"].nunique() == 8
    assert panel["market_id"].nunique() == 8
    for prefix in ("pre_open", "trading_session", "full_day"):
        assert f"{prefix}_air_temperature_c" in panel
    assert panel["close_to_close_return_pct"].notna().sum() == 32


def test_continuous_weather_and_temperature_anomaly_preserved():
    panel = fixture_pipeline()
    assert "pre_open_temperature_anomaly_c" in panel
    assert "pre_open_temperature_z" in panel
    assert panel["pre_open_air_temperature_c"].dtype.kind == "f"


def test_panel_bundle_csv_parquet_and_dictionary_close(tmp_path):
    panel = fixture_pipeline()
    write_panel_bundle(panel, tmp_path / "data" / "panel" / "v2" / "provisional", {"status": "test"})
    assert validate_panel(tmp_path, "provisional") == []


def test_provisional_frozen_isolation(tmp_path):
    panel = fixture_pipeline()
    write_panel_bundle(panel, tmp_path / "data" / "panel" / "v2" / "provisional", {})
    frozen = panel.copy()
    frozen["panel_tier"] = "frozen"
    write_panel_bundle(frozen, tmp_path / "data" / "panel" / "v2" / "frozen", {})
    assert set(pd.read_csv(tmp_path / "data" / "panel" / "v2" / "provisional" / "city_market_daily.csv")["panel_tier"]) == {"provisional"}
    assert set(pd.read_csv(tmp_path / "data" / "panel" / "v2" / "frozen" / "city_market_daily.csv")["panel_tier"]) == {"frozen"}


def test_three_year_backfill_dry_run_estimate():
    result = backfill_estimate(repo_root(), date(2023, 1, 1), date(2025, 12, 31))
    assert result["estimated_market_records"] > 6000
    assert result["estimated_city_weather_hours"] == 1096 * 24 * 8
    assert result["estimated_era5_requests"] == 24
    assert result["missing_or_manual_sources"]


def make_cli_root(tmp_path: Path) -> Path:
    source = repo_root()
    for folder in ("config", "schemas"):
        shutil.copytree(source / folder / "v2", tmp_path / folder / "v2")
    shutil.copytree(
        source / "tests" / "v2" / "fixtures",
        tmp_path / "tests" / "v2" / "fixtures",
    )
    return tmp_path


def run_cli(script: str, root: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(repo_root() / "scripts" / "v2" / script), "--root", str(root), *args],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_mature_week_dry_run_has_no_panel(tmp_path):
    root = make_cli_root(tmp_path)
    result = run_cli("run_mature_week.py", root, "--week", "2026-W28", "--dry-run")
    assert result.returncode == 0
    assert not (root / "data" / "panel").exists()


def test_offline_mature_week_end_to_end(tmp_path):
    root = make_cli_root(tmp_path)
    result = run_cli(
        "run_mature_week.py",
        root,
        "--week",
        "2026-W28",
        "--offline",
        "--allow-provisional",
    )
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["panel_rows"] == 40
    assert report["cities"] == report["markets"] == 8
    assert report["audit_passed"]


def test_unmatured_fixture_rejected_without_allow_flag(tmp_path):
    root = make_cli_root(tmp_path)
    result = run_cli("run_mature_week.py", root, "--week", "2026-W28", "--offline")
    assert result.returncode == 4
    assert "provisional_ready" in result.stdout


def test_offline_fixture_cannot_be_frozen(tmp_path):
    root = make_cli_root(tmp_path)
    result = run_cli(
        "run_mature_week.py",
        root,
        "--week",
        "2026-W28",
        "--offline",
        "--allow-provisional",
        "--freeze",
    )
    assert result.returncode == 4
    assert "ERA5T provisional" in result.stdout


def test_live_run_refuses_unavailable_registered_sources(tmp_path):
    root = make_cli_root(tmp_path)
    result = run_cli("run_mature_week.py", root, "--week", "2026-W28")
    assert result.returncode == 4
    assert "registered acquisition" in result.stdout


def test_prepared_final_canonical_week_can_be_frozen(tmp_path):
    root = make_cli_root(tmp_path)
    weather, market_raw, dates = fixture_frames(root, "2026-W28")
    weather["data_class"] = "final_reanalysis"
    weather["is_final"] = True
    market = normalize_market_frame(market_raw)
    (root / "data" / "canonical" / "v2" / "weather").mkdir(parents=True)
    (root / "data" / "canonical" / "v2" / "market").mkdir(parents=True)
    (root / "data" / "canonical" / "v2" / "calendars").mkdir(parents=True)
    weather.to_parquet(
        root / "data" / "canonical" / "v2" / "weather" / "2026-W28.parquet",
        index=False,
    )
    market.to_parquet(
        root / "data" / "canonical" / "v2" / "market" / "2026-W28.parquet",
        index=False,
    )
    (root / "data" / "canonical" / "v2" / "calendars" / "2026-W28.json").write_text(
        json.dumps({"trading_dates": dates}), encoding="utf-8"
    )
    result = run_cli("run_mature_week.py", root, "--week", "2026-W28", "--freeze")
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout)
    assert report["status"] == "frozen"
    assert (root / "data" / "panel" / "v2" / "frozen" / "panel_manifest.json").exists()


@pytest.mark.parametrize("flag", ["--offline", "--refresh", "--allow-provisional", "--freeze", "--dry-run"])
def test_cli_help_exposes_required_flags(flag):
    result = subprocess.run(
        [sys.executable, str(repo_root() / "scripts" / "v2" / "run_mature_week.py"), "--help"],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert flag in result.stdout


def test_backfill_cli_requires_dry_run(tmp_path):
    root = make_cli_root(tmp_path)
    result = run_cli(
        "backfill_history.py",
        root,
        "--start",
        "2023-01-01",
        "--end",
        "2025-12-31",
    )
    assert result.returncode == 4


def test_backfill_cli_dry_run(tmp_path):
    root = make_cli_root(tmp_path)
    result = run_cli(
        "backfill_history.py",
        root,
        "--start",
        "2023-01-01",
        "--end",
        "2025-12-31",
        "--dry-run",
    )
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["estimated_market_records"] > 6000
