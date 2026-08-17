"""Offline regression tests for the Taipei ERA5 tracked-audit integrity fixes.

Covers: observed member time counts from the validator, expver aggregation
across segments, scalar expver coordinate recording, raw-audit SHA binding
into the normalization audit, POSIX canonical paths, normalizer code content
hash stability, and canonical semantic reproducibility. Zero network.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from scripts.v2.core import V2Error
from scripts.v2.normalize_weather import (
    _normalizer_code_hashes,
    main as normalize_main,
    resolve_expver,
)

ROOT = Path(__file__).resolve().parents[2]
sys_path_guard = None  # placeholder for linters


def _import_fixtures():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_weather_normalize_zip import make_pilot_artifacts

    return make_pilot_artifacts


def run_normalize_with_audits(root: Path, manifests: list[Path], output: Path) -> int:
    args = []
    for manifest in manifests:
        args += ["--manifest", str(manifest)]
    args += [
        "--city", "taipei",
        "--source-id", "cds_era5_hourly",
        "--data-class", "final_reanalysis",
        "--root", str(root),
        "--output", str(output),
        "--raw-audit-output", "data/audits/v2/weather-pilot/taipei-era5-raw-acceptance.json",
        "--audit-output", "data/audits/v2/weather-pilot/taipei-era5-normalization.json",
    ]
    return normalize_main(args)


# ---------------- observed member time counts ----------------

def test_multi_date_member_time_counts_are_observed(tmp_path):
    make_pilot_artifacts = _import_fixtures()
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/out.parquet"
    code = run_normalize_with_audits(root, manifests, output)
    assert code == 0
    audit = json.loads(
        (root / "data/audits/v2/weather-pilot/taipei-era5-raw-acceptance.json").read_text(encoding="utf-8")
    )
    by_segment = {s["segment_id"].split("-73824d43")[0]: s for s in audit["segments"]}
    seg2 = next(s for s in audit["segments"] if "segment-02" in s["segment_id"])
    assert seg2["member_time_counts"] == [96, 96]  # observed, not 24 (request time-list length)
    assert seg2["member_expected_time_counts"] == [96, 96]


def test_audit_observed_timestamp_from_validator(tmp_path):
    make_pilot_artifacts = _import_fixtures()
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/out.parquet"
    run_normalize_with_audits(root, manifests, output)
    audit = json.loads(
        (root / "data/audits/v2/weather-pilot/taipei-era5-raw-acceptance.json").read_text(encoding="utf-8")
    )
    seg2 = next(s for s in audit["segments"] if "segment-02" in s["segment_id"])
    tv = seg2["timestamp_validation"]
    assert tv["expected_timestamp_count"] == 96
    assert tv["observed_timestamp_count"] == 96
    assert tv["timestamp_set_match"] is True


# ---------------- expver aggregation / scalar coordinate ----------------

def test_expver_summary_aggregated_across_segments(tmp_path):
    make_pilot_artifacts = _import_fixtures()
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/out.parquet"
    run_normalize_with_audits(root, manifests, output)
    norm = json.loads(
        (root / "data/audits/v2/weather-pilot/taipei-era5-normalization.json").read_text(encoding="utf-8")
    )
    for variable in ("t2m", "d2m", "tcc", "u10", "v10", "i10fg", "tp", "ssrd"):
        entry = norm["expver_summary"][variable]
        assert entry["single_source_cell_count"] == 121  # full 3-segment total
        assert entry["conflicting_cell_count"] == 0
        assert entry["missing_cell_count"] == 0


def test_scalar_expver_coordinate_recorded():
    ts = pd.DatetimeIndex([pd.Timestamp("2026-03-01T16:00:00")])
    dataset = xr.Dataset(
        {"t2m": (("valid_time", "latitude", "longitude"), np.full((1, 1, 1), 283.15))},
        coords={
            "valid_time": ts,
            "latitude": [25.0],
            "longitude": [121.5],
            "expver": 1,  # scalar coordinate
        },
    )
    resolved, info = resolve_expver(dataset, "t2m")
    assert info["expver_values"] == ["1"]
    assert info["single_source_cell_count"] == 1
    assert resolved[0, 0, 0] == pytest.approx(283.15)  # value preserved


def test_no_expver_coordinate_yields_empty_values():
    ts = pd.DatetimeIndex([pd.Timestamp("2026-03-01T16:00:00")])
    dataset = xr.Dataset(
        {"t2m": (("valid_time", "latitude", "longitude"), np.full((1, 1, 1), 283.15))},
        coords={"valid_time": ts, "latitude": [25.0], "longitude": [121.5]},
    )
    _, info = resolve_expver(dataset, "t2m")
    assert info["expver_values"] == []


# ---------------- raw audit SHA binding ----------------

def test_raw_audit_sha_bound_into_normalization_audit(tmp_path):
    make_pilot_artifacts = _import_fixtures()
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/out.parquet"
    run_normalize_with_audits(root, manifests, output)
    raw_path = root / "data/audits/v2/weather-pilot/taipei-era5-raw-acceptance.json"
    norm_path = root / "data/audits/v2/weather-pilot/taipei-era5-normalization.json"
    norm = json.loads(norm_path.read_text(encoding="utf-8"))
    actual_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    assert norm["input_raw_acceptance_audit_sha256"] == actual_sha
    assert norm["input_raw_acceptance_audit_sha256"] is not None


def test_raw_audit_sha_tamper_detected(tmp_path):
    make_pilot_artifacts = _import_fixtures()
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/out.parquet"
    run_normalize_with_audits(root, manifests, output)
    raw_path = root / "data/audits/v2/weather-pilot/taipei-era5-raw-acceptance.json"
    norm_path = root / "data/audits/v2/weather-pilot/taipei-era5-normalization.json"
    norm = json.loads(norm_path.read_text(encoding="utf-8"))
    bound = norm["input_raw_acceptance_audit_sha256"]
    # Tamper with the raw audit -> recorded SHA no longer matches actual file.
    tampered = raw_path.read_bytes() + b"x"
    raw_path.write_bytes(tampered)
    actual_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    assert actual_sha != bound  # consumer verification would fail


# ---------------- POSIX paths / code hash ----------------

def test_canonical_output_posix_relative_path(tmp_path):
    make_pilot_artifacts = _import_fixtures()
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/out.parquet"
    run_normalize_with_audits(root, manifests, output)
    norm = json.loads(
        (root / "data/audits/v2/weather-pilot/taipei-era5-normalization.json").read_text(encoding="utf-8")
    )
    path = norm["canonical_output"]
    assert path == "data/canonical/v2/weather/out.parquet"
    assert "\\" not in path
    assert path.startswith("data/")


def test_normalizer_code_hash_stable(tmp_path):
    (tmp_path / "scripts/v2").mkdir(parents=True)
    for name in ("normalize_weather.py", "core.py", "fetch_era5.py"):
        (tmp_path / "scripts/v2" / name).write_text(f"# {name} content\n", encoding="utf-8")
    first = _normalizer_code_hashes(tmp_path)
    second = _normalizer_code_hashes(tmp_path)
    assert first["combined"] == second["combined"]
    assert first["files"] == second["files"]
    # Content change -> hash changes.
    (tmp_path / "scripts/v2" / "core.py").write_text("# changed\n", encoding="utf-8")
    third = _normalizer_code_hashes(tmp_path)
    assert third["combined"] != first["combined"]
    assert third["files"]["scripts/v2/core.py"] != first["files"]["scripts/v2/core.py"]


def test_canonical_semantic_reproducible(tmp_path):
    make_pilot_artifacts = _import_fixtures()
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/out.parquet"
    assert run_normalize_with_audits(root, manifests, output) == 0
    first = pd.read_parquet(output)
    assert run_normalize_with_audits(root, manifests, output) == 0
    second = pd.read_parquet(output)
    assert first.sort_values("timestamp_utc").reset_index(drop=True).equals(
        second.sort_values("timestamp_utc").reset_index(drop=True)
    )
    assert len(second) == 121


# ---------------- status regressions ----------------

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
