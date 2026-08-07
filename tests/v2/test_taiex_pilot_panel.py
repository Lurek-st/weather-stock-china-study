"""Tests for the five-row Taipei-TAIEX end-to-end pilot panel pipeline.

Coverage follows the control-plane acceptance checklist:

* market-only acceptance gates and raw manifest verification
* raw duplicate-date rejection before any de-duplication
* previous-close across closure days (2026-03-02 -> 2026-02-26)
* five official-close / return recalculation oracles
* five-row market canonical + tracked canonical audit binding
* strict weather-wide pivot (15 -> 5 rows, 27 fields, zero nulls)
* strict one-to-one market/weather join with exact key-set equality
* five-row provisional panel; no sample temperature features
* independent end-to-end oracle (rebuilt from tracked audits only)
* panel audit binds window + market canonical audits
* local-only parquets stay untracked; raw/window evidence unmodified
* statuses: statistics_run=false, backfill blocked, production not_connected,
  AEX/KOSPI unchanged, research_ready/frozen false
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pandas as pd
import pytest

from scripts.v2.build_taiex_pilot_market import (
    _parse_rows_with_duplicate_check,
    _verify_artifact,
    main as market_canonical_main,
)
from scripts.v2.build_taiex_pilot_panel import (
    _market_oracle_from_acceptance,
    _weather_wide_from_oracle,
    main as panel_main,
)
from scripts.v2.core import (
    CORE_WEATHER_COLUMNS,
    V2Error,
    build_panel,
    build_weather_windows,
    fixture_frames,
    load_yaml,
    normalize_market_frame,
    repo_root,
    sha256_file,
)

from tests.v2.test_taiex_pilot_acceptance import build_local_evidence
from tests.v2.test_panel_and_cli import fixture_pipeline

PILOT_DATES = [
    "2026-03-02",
    "2026-03-03",
    "2026-03-04",
    "2026-03-05",
    "2026-03-06",
]
ORACLE = {
    "2026-03-02": {"close": 35095.09, "previous": 35414.49, "return": -0.9018907233},
    "2026-03-03": {"close": 34323.65, "previous": 35095.09, "return": -2.1981422472},
    "2026-03-04": {"close": 32828.88, "previous": 34323.65, "return": -4.3549272877},
    "2026-03-05": {"close": 33672.94, "previous": 32828.88, "return": 2.5710898453},
    "2026-03-06": {"close": 33599.54, "previous": 33672.94, "return": -0.2179791845},
}
# Real TWSE ROC rows for the pilot evidence (matches the frozen local raw).
REAL_FEB_ROWS = [["115/02/26", "35,457.76", "35,579.34", "35,171.52", "35,414.49"]]
REAL_MAR_ROWS = [
    ["115/03/02", "35,277.48", "35,345.72", "34,605.36", "35,095.09"],
    ["115/03/03", "35,106.22", "35,264.59", "34,323.65", "34,323.65"],
    ["115/03/04", "34,228.75", "34,228.75", "32,828.88", "32,828.88"],
    ["115/03/05", "33,620.74", "34,319.68", "33,472.91", "33,672.94"],
    ["115/03/06", "33,483.94", "33,829.49", "33,322.52", "33,599.54"],
]


# ---------------------------------------------------------------------------
# helpers: synthetic evidence -> full pipeline in tmp_path (CI portable)
# ---------------------------------------------------------------------------

def _synthetic_weather_window_frame() -> pd.DataFrame:
    rows = []
    for day_index, day in enumerate(PILOT_DATES):
        for window_index, window in enumerate(("pre_open", "trading_session", "full_day")):
            row = {
                "city_id": "taipei",
                "market_id": "taiex",
                "trading_date": day,
                "window": window,
                "hour_count": 5,
                "expected_hour_count": 5,
                "instantaneous_expected_count": 5,
                "instantaneous_observed_count": 5,
                "accumulation_expected_count": 4,
                "accumulation_observed_count": 4,
                "instantaneous_coverage_ratio": 1.0,
                "accumulation_coverage_ratio": 1.0,
                "partial_interval_policy": "partial_accumulation_interval_excluded",
                "quality_flags": [],
                "calendar_status": "normal",
            }
            for variable in CORE_WEATHER_COLUMNS:
                row[variable] = float(1000 * day_index + 100 * window_index + CORE_WEATHER_COLUMNS.index(variable))
            rows.append(row)
    return pd.DataFrame(rows)


def _write_synthetic_weather_evidence(root: Path) -> dict:
    frame = _synthetic_weather_window_frame()
    window_path = root / "data/canonical/v2/weather/taipei-era5-final-pilot-windows-20260302-20260306.parquet"
    window_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(window_path, index=False)
    window_sha = sha256_file(window_path)
    rows = []
    for _, item in frame.iterrows():
        rows.append(
            {
                "trading_date": item["trading_date"],
                "window": item["window"],
                "weather_values": {var: float(item[var]) for var in CORE_WEATHER_COLUMNS},
                "instantaneous_coverage_ratio": 1.0,
                "accumulation_coverage_ratio": 1.0,
                "quality_flags": [],
            }
        )
    audit = {
        "schema_version": "2.0.0",
        "audit_type": "taipei_era5_window_acceptance",
        "city_id": "taipei",
        "market_id": "taiex",
        "selected_trading_dates": list(PILOT_DATES),
        "weather_window_status": "accepted",
        "window_row_count": 15,
        "unique_key_count": 15,
        "all_count_contracts_match": True,
        "all_coverage_ratios_one": True,
        "all_oracle_matches": True,
        "panel_built": False,
        "historical_backfill_run": False,
        "window_parquet_path": "data/canonical/v2/weather/taipei-era5-final-pilot-windows-20260302-20260306.parquet",
        "window_parquet_sha256": window_sha,
        "rows": rows,
    }
    audit_path = root / "data/audits/v2/weather-pilot/taipei-era5-window-acceptance.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"window_parquet_sha256": window_sha, "path": str(window_path)}


def _copy_config(root: Path) -> None:
    source = repo_root() / "config" / "v2"
    (root / "config" / "v2").mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.is_file():
            shutil.copy2(item, root / "config" / "v2" / item.name)


def build_pilot_pipeline(tmp_path: Path) -> Path:
    """Run acceptance + market canonical + panel pipeline on synthetic evidence."""
    root = build_local_evidence(tmp_path, feb_rows=REAL_FEB_ROWS, mar_rows=REAL_MAR_ROWS)
    _copy_config(root)

    # 1. market-only acceptance audit
    from scripts.v2.taiex_pilot_acceptance import main as acceptance_main

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = acceptance_main(["--root", str(root)])
    assert code == 0, buffer.getvalue()

    # 2. market canonical + canonical audit
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = market_canonical_main(["--root", str(root)])
    assert code == 0, buffer.getvalue()

    # 3. synthetic weather window evidence
    _write_synthetic_weather_evidence(root)

    # 4. panel + panel audit
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = panel_main(["--root", str(root)])
    assert code == 0, buffer.getvalue()
    return root


def _market_audit(root: Path) -> dict:
    return json.loads(
        (root / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json").read_text(encoding="utf-8")
    )


def _canonical_audit(root: Path) -> dict:
    return json.loads(
        (root / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-canonical.json").read_text(encoding="utf-8")
    )


def _canonical_frame(root: Path) -> pd.DataFrame:
    return pd.read_parquet(
        root / "data/canonical/v2/market/taiex-final-pilot-20260302-20260306.parquet"
    )


def _panel_frame(root: Path) -> pd.DataFrame:
    return pd.read_parquet(
        root / "data/panel/v2/provisional/taipei-taiex-pilot-20260302-20260306.parquet"
    )


def _panel_audit(root: Path) -> dict:
    return json.loads(
        (root / "data/audits/v2/panel-pilot/taipei-taiex-2026-pilot-panel-acceptance.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# market-side gates
# ---------------------------------------------------------------------------

def test_market_audit_accepted_gate(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    market = _market_audit(root)
    assert market["adapter_status"] == "market_only_pilot_accepted"
    assert market["pilot_ok"] is True
    assert market["production_status"] == "not_connected"
    assert market["repeatability"]["classification"] in {"byte_identical", "semantic_identical"}
    cross = market["cross_validation"]
    assert cross["pilot_week_record_count"] == 5
    assert cross["previous_close_match"] is True
    assert cross["return_recalculation_match"] is True
    assert cross["ohlc_validation_passed"] is True
    assert cross["calendar_match_rate"] == 1.0
    for key in ("duplicate_dates", "missing_market_dates", "unexpected_market_dates", "unresolved_dates", "issues"):
        assert cross[key] == []


def test_market_pilot_dates_exact_five(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    market = _market_audit(root)
    assert market["pilot_week_dates"] == PILOT_DATES
    assert len(market["pilot_week_dates"]) == 5


def test_market_raw_manifest_sha_verified(tmp_path):
    root = build_local_evidence(tmp_path)
    raw_dir = root / ".local/source-raw/taiex/twse_taiex_official"
    manifests = []
    for month in ("20260201", "20260301"):
        for manifest_path in (raw_dir / month).glob("*.manifest.json"):
            manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert len(manifests) == 2
    for manifest in manifests:
        verified = _verify_artifact(raw_dir, manifest)
        assert verified["revision"] == 1
        assert verified["source_month"] == manifest["request"]["params"]["date"]
        assert verified["sha256"] == manifest["sha256"]
        assert verified["content_length"] == manifest["content_length"]


def test_market_raw_duplicate_date_rejected_before_dedupe(tmp_path):
    root = build_local_evidence(tmp_path, feb_rows=REAL_FEB_ROWS, mar_rows=REAL_MAR_ROWS)
    raw_dir = root / ".local/source-raw/taiex/twse_taiex_official"
    manifests = []
    for month in ("20260201", "20260301"):
        for manifest_path in (raw_dir / month).glob("*.manifest.json"):
            manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))
    # duplicate 2026-03-02 within the 20260301 artifact and refresh manifest
    month = "20260301"
    manifest = next(item for item in manifests if item["request"]["params"]["date"] == month)
    path = raw_dir / month / (manifest["artifact_id"].split(":")[-1] + ".json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["data"].append(payload["data"][0])
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    new_manifest = dict(manifest)
    new_manifest["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    new_manifest["content_length"] = path.stat().st_size
    (raw_dir / month / (manifest["artifact_id"].split(":")[-1] + ".manifest.json")).write_text(
        json.dumps(new_manifest, ensure_ascii=False), encoding="utf-8"
    )
    refreshed = []
    for month_dir in ("20260201", "20260301"):
        for manifest_path in (raw_dir / month_dir).glob("*.manifest.json"):
            refreshed.append(json.loads(manifest_path.read_text(encoding="utf-8")))
    with pytest.raises(V2Error, match="duplicate"):
        _parse_rows_with_duplicate_check(raw_dir, refreshed)


def test_previous_close_crosses_closure_days(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    frame = _canonical_frame(root)
    row = frame.loc[frame["trading_date"] == "2026-03-02"].iloc[0]
    assert row["previous_close_source_date"] == "2026-02-26"
    assert float(row["previous_official_close"]) == 35414.49
    # closure days must not be treated as trading days
    raw_dir = root / ".local/source-raw/taiex/twse_taiex_official"
    for month in ("20260201", "20260301"):
        for manifest_path in (raw_dir / month).glob("*.manifest.json"):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            path = raw_dir / month / (manifest["artifact_id"].split(":")[-1] + ".json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            from scripts.v2.probes.probe_taiex import _rows

            dates = [row["trading_date"] for row in _rows(payload)]
            assert "2026-02-27" not in dates
            assert "2026-02-28" not in dates
            assert "2026-03-01" not in dates


@pytest.mark.parametrize("day", PILOT_DATES)
def test_five_close_oracles_match(tmp_path, day):
    root = build_pilot_pipeline(tmp_path)
    frame = _canonical_frame(root)
    row = frame.loc[frame["trading_date"] == day].iloc[0]
    assert float(row["official_close"]) == pytest.approx(ORACLE[day]["close"], rel=1e-12, abs=1e-12)
    assert float(row["previous_official_close"]) == pytest.approx(ORACLE[day]["previous"], rel=1e-12, abs=1e-12)


@pytest.mark.parametrize("day", PILOT_DATES)
def test_five_return_recalculations_match(tmp_path, day):
    root = build_pilot_pipeline(tmp_path)
    frame = _canonical_frame(root)
    row = frame.loc[frame["trading_date"] == day].iloc[0]
    assert float(row["close_to_close_return_pct"]) == pytest.approx(ORACLE[day]["return"], rel=1e-10, abs=1e-12)
    recomputed = round((float(row["official_close"]) / float(row["previous_official_close"]) - 1.0) * 100.0, 10)
    assert recomputed == float(row["close_to_close_return_pct"])


# ---------------------------------------------------------------------------
# market canonical + audit
# ---------------------------------------------------------------------------

def test_market_canonical_five_rows(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    frame = _canonical_frame(root)
    assert len(frame) == 5
    assert list(frame["trading_date"]) == PILOT_DATES


def test_market_canonical_unique_key_five(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    frame = _canonical_frame(root)
    assert frame[["market_id", "trading_date"]].drop_duplicates().shape[0] == 5


def test_market_canonical_sha_bound_to_audit(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _canonical_audit(root)
    actual = sha256_file(root / audit["market_canonical_path"])
    assert actual == audit["market_canonical_sha256"]
    assert audit["market_row_count"] == 5
    assert audit["unique_key_count"] == 5


def test_market_canonical_source_timestamp_semantics(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _canonical_audit(root)
    assert audit["source_timestamp_semantics"] == "raw_retrieved_at"
    frame = _canonical_frame(root)
    assert frame["source_timestamp"].notna().all()
    assert frame["calendar_source_id"].eq("twse_official_holiday_schedule").all()
    assert frame["value_status"].eq("final").all()
    assert frame["currency"].eq("TWD").all()
    assert frame["is_final"].all()


# ---------------------------------------------------------------------------
# strict panel behaviour (build_panel)
# ---------------------------------------------------------------------------

def _market_fixture() -> pd.DataFrame:
    weather, market_raw, _ = fixture_frames(repo_root(), "2026-W28")
    return normalize_market_frame(market_raw)


def _window_fixture() -> pd.DataFrame:
    weather, market_raw, dates = fixture_frames(repo_root(), "2026-W28")
    markets = load_yaml(repo_root() / "config/v2/markets.yaml")["markets"]
    return pd.concat(
        [build_weather_windows(weather, item, dates) for item in markets],
        ignore_index=True,
    )


def test_panel_market_duplicate_rejected_by_default(tmp_path):
    market = _market_fixture()
    windows = _window_fixture()
    duplicated = pd.concat([market, market.iloc[[0]]], ignore_index=True)
    with pytest.raises(V2Error, match="duplicate market keys"):
        build_panel(
            duplicated,
            windows,
            load_yaml(repo_root() / "config/v2/locations.yaml"),
            "provisional",
        )


def test_panel_market_duplicate_keep_last_legacy(tmp_path):
    market = _market_fixture()
    windows = _window_fixture()
    duplicated = pd.concat([market, market.iloc[[0]]], ignore_index=True)
    panel = build_panel(
        duplicated,
        windows,
        load_yaml(repo_root() / "config/v2/locations.yaml"),
        "provisional",
        duplicate_policy="keep_last",
    )
    assert len(panel) == 40


def test_panel_weather_window_duplicate_rejected(tmp_path):
    market = _market_fixture()
    windows = _window_fixture()
    duplicated = pd.concat([windows, windows.iloc[[0]]], ignore_index=True)
    with pytest.raises(V2Error, match="duplicate weather window keys"):
        build_panel(
            market,
            duplicated,
            load_yaml(repo_root() / "config/v2/locations.yaml"),
            "provisional",
        )


@pytest.mark.parametrize("missing_window", ["pre_open", "trading_session", "full_day"])
def test_panel_missing_window_fails(tmp_path, missing_window):
    market = _market_fixture()
    windows = _window_fixture()
    dropped = windows.loc[windows["window"] != missing_window].copy()
    with pytest.raises(V2Error, match="missing window coverage"):
        build_panel(
            market,
            dropped,
            load_yaml(repo_root() / "config/v2/locations.yaml"),
            "provisional",
        )


def test_panel_market_weather_key_mismatch_fails(tmp_path):
    market = _market_fixture()
    windows = _window_fixture()
    # drop one market-served trading day entirely from weather -> market key
    # missing from weather must fail (no left-join tolerance)
    dropped_day = windows["trading_date"].max()
    dropped_market = "bse50"
    shifted = windows.loc[
        ~((windows["trading_date"] == dropped_day) & (windows["market_id"] == dropped_market))
    ].copy()
    with pytest.raises(V2Error, match="missing from weather"):
        build_panel(
            market,
            shifted,
            load_yaml(repo_root() / "config/v2/locations.yaml"),
            "provisional",
        )


def test_panel_sample_temperature_features_switch(tmp_path):
    market = _market_fixture()
    windows = _window_fixture()
    locations = load_yaml(repo_root() / "config/v2/locations.yaml")
    with_features = build_panel(market, windows, locations, "provisional")
    assert "pre_open_temperature_anomaly_c" in with_features
    assert "pre_open_temperature_z" in with_features
    without = build_panel(
        market, windows, locations, "provisional", include_sample_temperature_features=False
    )
    assert "pre_open_temperature_anomaly_c" not in without
    assert "pre_open_temperature_z" not in without


# ---------------------------------------------------------------------------
# weather-wide + one-to-one join + panel
# ---------------------------------------------------------------------------

def test_weather_wide_five_rows(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _panel_audit(root)
    assert audit["weather_wide_row_count"] == 5


def test_weather_wide_twenty_seven_fields(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _panel_audit(root)
    assert audit["weather_window_field_count"] == 27


def test_weather_wide_no_nulls(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _panel_audit(root)
    assert audit["weather_null_count"] == 0


def test_strict_one_to_one_join(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _panel_audit(root)
    assert audit["one_to_one_join"] is True
    assert audit["key_sets_exact_match"] is True


def test_panel_five_rows_and_unique_keys(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    panel = _panel_frame(root)
    assert len(panel) == 5
    assert panel[["city_id", "market_id", "trading_date"]].drop_duplicates().shape[0] == 5
    audit = _panel_audit(root)
    assert audit["panel_row_count"] == 5
    assert audit["unique_key_count"] == 5


def test_panel_weather_finite_and_return_finite(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    panel = _panel_frame(root)
    weather_fields = [
        f"{window}_{var}"
        for window in ("pre_open", "trading_session", "full_day")
        for var in CORE_WEATHER_COLUMNS
    ]
    assert pd.to_numeric(panel[weather_fields].stack(), errors="coerce").notna().all()
    assert pd.to_numeric(panel["close_to_close_return_pct"], errors="coerce").apply(
        lambda value: value == value and abs(value) != float("inf")
    ).all()


def test_panel_dates_ascending(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    panel = _panel_frame(root)
    assert list(panel["trading_date"]) == PILOT_DATES


def test_panel_tier_provisional(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    panel = _panel_frame(root)
    assert panel["panel_tier"].eq("provisional").all()
    audit = _panel_audit(root)
    assert audit["panel_tier"] == "provisional"
    assert audit["panel_scope"] == "pilot_only"


def test_panel_no_sample_anomaly_fields(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    panel = _panel_frame(root)
    assert "pre_open_temperature_anomaly_c" not in panel.columns
    assert "pre_open_temperature_z" not in panel.columns
    audit = _panel_audit(root)
    assert audit["sample_temperature_features_generated"] is False
    assert audit["climatology_baseline_status"] == "not_available_historical_backfill_blocked"


# ---------------------------------------------------------------------------
# independent end-to-end oracle
# ---------------------------------------------------------------------------

def test_independent_weather_oracle_matches(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    window_audit = json.loads(
        (root / "data/audits/v2/weather-pilot/taipei-era5-window-acceptance.json").read_text(encoding="utf-8")
    )
    oracle = _weather_wide_from_oracle(window_audit)
    assert set(oracle) == set(PILOT_DATES)
    assert all(len(values) == 27 for values in oracle.values())
    panel = _panel_frame(root)
    for _, row in panel.iterrows():
        for variable, expected in oracle[row["trading_date"]].items():
            assert float(row[variable]) == pytest.approx(expected, rel=1e-10, abs=1e-12)


def test_independent_market_oracle_matches(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    market_audit = _market_audit(root)
    oracle = _market_oracle_from_acceptance(market_audit)
    assert set(oracle) == set(PILOT_DATES)
    panel = _panel_frame(root)
    for _, row in panel.iterrows():
        for field, expected in oracle[row["trading_date"]].items():
            assert float(row[field]) == pytest.approx(expected, rel=1e-10, abs=1e-12)


def test_all_end_to_end_oracle_matches(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _panel_audit(root)
    assert audit["all_end_to_end_oracle_matches"] is True
    assert audit["max_weather_abs_diff"] <= 1e-12
    assert audit["max_market_abs_diff"] <= 1e-12


def test_panel_audit_binds_window_audit(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _panel_audit(root)
    window_audit_path = root / audit["weather_window_audit_path"]
    assert sha256_file(window_audit_path) == audit["weather_window_audit_sha256"]
    window_parquet_path = root / audit["window_parquet_path"]
    assert sha256_file(window_parquet_path) == audit["window_parquet_sha256"]


def test_panel_audit_binds_market_canonical_audit(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _panel_audit(root)
    canonical_audit_path = root / audit["market_canonical_audit_path"]
    assert sha256_file(canonical_audit_path) == audit["market_canonical_audit_sha256"]
    canonical_path = root / audit["market_canonical_path"]
    assert sha256_file(canonical_path) == audit["market_canonical_sha256"]
    market_acceptance_path = root / audit["market_acceptance_audit_path"]
    assert sha256_file(market_acceptance_path) == audit["market_acceptance_audit_sha256"]


def test_panel_audit_rows_cover_five_market_days(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _panel_audit(root)
    assert [row["trading_date"] for row in audit["rows"]] == PILOT_DATES
    for row in audit["rows"]:
        assert row["official_close"] > 0
        assert row["previous_official_close"] is not None


# ---------------------------------------------------------------------------
# status flags
# ---------------------------------------------------------------------------

def test_statistics_run_false(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    assert _panel_audit(root)["statistics_run"] is False


def test_historical_backfill_run_false(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    assert _panel_audit(root)["historical_backfill_run"] is False
    window_audit = json.loads(
        (root / "data/audits/v2/weather-pilot/taipei-era5-window-acceptance.json").read_text(encoding="utf-8")
    )
    assert window_audit["historical_backfill_run"] is False
    assert _canonical_audit(root)["historical_backfill_run"] is False


def test_panel_status_accepted_research_not_ready(tmp_path):
    root = build_pilot_pipeline(tmp_path)
    audit = _panel_audit(root)
    assert audit["panel_status"] == "accepted"
    assert audit["research_ready"] is False
    assert audit["frozen"] is False


# ---------------------------------------------------------------------------
# real-repository read-only checks (CI portable; local-only files skipped)
# ---------------------------------------------------------------------------

def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_panel_parquet_untracked():
    path = "data/panel/v2/provisional/taipei-taiex-pilot-20260302-20260306.parquet"
    assert path in _git("check-ignore", "-v", path)
    assert path not in _git("ls-files")


def test_market_canonical_parquet_untracked():
    path = "data/canonical/v2/market/taiex-final-pilot-20260302-20260306.parquet"
    assert path in _git("check-ignore", "-v", path)
    assert path not in _git("ls-files")


def test_weather_window_raw_canonical_untracked():
    for path in [
        "data/canonical/v2/weather/taipei-era5-final-pilot-windows-20260302-20260306.parquet",
        "data/canonical/v2/weather/taipei-era5-final-pilot-20260302-20260306.parquet",
        ".local/source-raw/taiex/twse_taiex_official/20260201/r0001-66f10e8da761.json",
    ]:
        assert path not in _git("ls-files"), f"{path} must stay untracked"


def test_taiex_production_status_unchanged():
    audit = json.loads(
        (repo_root() / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json").read_text(encoding="utf-8")
    )
    assert audit["production_status"] == "not_connected"
    assert audit["research_ready"] is False
    assert audit["frozen"] is False


def test_aex_kospi_statuses_unchanged():
    scope = load_yaml(repo_root() / "config/v2/pilot-scope.yaml")
    by_id = {row["market_id"]: row for row in scope["excluded_or_deferred_markets"]}
    assert by_id["aex_dnb"]["status"] == "technical_access_blocked"
    assert by_id["kospi"]["status"] == "credential_required"


def test_v1_regression_fixture_pipeline_still_passes():
    panel = fixture_pipeline()
    assert len(panel) == 40
    assert "pre_open_temperature_anomaly_c" in panel.columns
    assert panel["panel_tier"].eq("provisional").all()


@pytest.mark.skipif(
    not (repo_root() / "data/panel/v2/provisional/city_market_daily.parquet").exists(),
    reason="V0 local artifact only present in local worktree",
)
def test_v0_panel_artifacts_not_modified_by_pilot():
    """The pilot writes a NEW parquet under data/panel/v2/provisional and must
    never touch the legacy V0 eight-city bundle."""
    v0 = pd.read_parquet(repo_root() / "data/panel/v2/provisional/city_market_daily.parquet")
    assert "taipei" not in set(v0["city_id"])
    assert v0["city_id"].nunique() == 8
    pilot = pd.read_parquet(
        repo_root() / "data/panel/v2/provisional/taipei-taiex-pilot-20260302-20260306.parquet"
    )
    assert set(pilot["city_id"]) == {"taipei"}
    manifest = json.loads(
        (repo_root() / "data/panel/v2/provisional/panel_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["row_count"] == 40
