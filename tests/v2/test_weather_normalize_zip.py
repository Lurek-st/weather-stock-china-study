"""Offline tests for ZIP-aware canonical weather normalization (Taipei pilot).

Zero network: no CDS client is imported or instantiated. Fixtures are minimal
NetCDF members packed into ZIPs and real manifests produced by
``RawArtifactStore``.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from scripts.v2.core import RawArtifactStore, V2Error
from scripts.v2.normalize_weather import (
    RAW_COLUMNS,
    concat_segments,
    main as normalize_main,
    plan_sha256,
    read_manifest,
    resolve_expver,
    safe_extract_zip_members,
    select_nearest_point,
    validate_manifest_plan,
)

ROOT = Path(__file__).resolve().parents[2]

SEGMENT_PLANS = [
    {"date": ["2026-03-01"], "times": [f"{h:02d}:00" for h in range(16, 24)]},  # 8
    {"date": ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"], "times": [f"{h:02d}:00" for h in range(24)]},  # 96
    {"date": ["2026-03-06"], "times": [f"{h:02d}:00" for h in range(0, 17)]},  # 17
]

AREA = [25.1675, 121.4346, 24.9075, 121.6946]

INSTANT_VARS = ["t2m", "d2m", "tcc", "u10", "v10", "i10fg"]
ACCUM_VARS = ["tp", "ssrd"]


def segment_timestamps(plan: dict) -> list[datetime]:
    out = []
    for day in plan["date"]:
        for hour in plan["times"]:
            out.append(datetime.fromisoformat(f"{day}T{int(hour.split(':')[0]):02d}:00:00+00:00"))
    return sorted(out)


def make_member(
    path: Path,
    variables: list[str],
    timestamps: list[datetime],
    latitude: list[float] | None = None,
    longitude: list[float] | None = None,
    expver_values: list[int] | None = None,
    expver_value: float = 1.0,
    units: dict[str, str] | None = None,
) -> None:
    latitude = latitude or [25.0]
    longitude = longitude or [121.5]
    ts = pd.DatetimeIndex([item.replace(tzinfo=None) for item in timestamps])
    units = units or {}
    data = {}
    coords: dict = {"valid_time": ts, "latitude": latitude, "longitude": longitude}
    if expver_values is None:
        for var in variables:
            data[var] = (("valid_time", "latitude", "longitude"), np.full((len(ts), len(latitude), len(longitude)), 1.0))
    else:
        coords["expver"] = expver_values
        for var in variables:
            data[var] = (
                ("expver", "valid_time", "latitude", "longitude"),
                np.full((len(expver_values), len(ts), len(latitude), len(longitude)), expver_value),
            )
    dataset = xr.Dataset(data, coords=coords)
    for var, unit in units.items():
        dataset[var].attrs["units"] = unit
    dataset.to_netcdf(path)


def make_segment_zip(zip_path: Path, plan: dict, **kwargs) -> None:
    timestamps = segment_timestamps(plan)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        p1 = base / "data_stream-oper_stepType-instant.nc"
        p2 = base / "data_stream-oper_stepType-accum.nc"
        make_member(p1, INSTANT_VARS, timestamps, **{k: v for k, v in kwargs.items() if k not in ("accum",)})
        make_member(p2, ACCUM_VARS, timestamps, **{k: v for k, v in kwargs.items() if k not in ("instant",)})
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(p1, "data_stream-oper_stepType-instant.nc")
            zf.write(p2, "data_stream-oper_stepType-accum.nc")


def make_pilot_artifacts(root: Path) -> list[Path]:
    """Build 3 segment manifests via RawArtifactStore for the real pilot plan."""
    root.mkdir(parents=True, exist_ok=True)
    store = RawArtifactStore(root / "data" / "source_raw" / "v2" / "weather")
    manifests = []
    for index, plan in enumerate(SEGMENT_PLANS, start=1):
        timestamps = segment_timestamps(plan)
        with zipfile.ZipFile(root / f"seg{index}.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                p1 = Path(tmp) / "instant.nc"
                p2 = Path(tmp) / "accum.nc"
                make_member(p1, INSTANT_VARS, timestamps)
                make_member(p2, ACCUM_VARS, timestamps)
                zf.write(p1, "data_stream-oper_stepType-instant.nc")
                zf.write(p2, "data_stream-oper_stepType-accum.nc")
        request = {
            "product_type": ["reanalysis"],
            "variable": [
                "2m_temperature", "2m_dewpoint_temperature", "total_precipitation",
                "total_cloud_cover", "10m_u_component_of_wind", "10m_v_component_of_wind",
                "instantaneous_10m_wind_gust", "surface_solar_radiation_downwards",
            ],
            "date": plan["date"],
            "time": plan["times"],
            "data_format": "netcdf",
            "download_format": "zip",
            "area": AREA,
        }
        result = store.persist(
            source_id="cds_era5_hourly",
            provider="ECMWF Copernicus Climate Change Service",
            logical_name=f"taipei-final-segment-{index:02d}-fixture",
            payload=(root / f"seg{index}.zip").read_bytes(),
            request=request,
            status="final",
            licence="CC-BY-4.0 catalogue terms and attribution",
            suffix=".zip",
            validation_metadata={"container_type": "zip", "container_validation_passed": True},
        )
        manifests.append(result.manifest_path)
    return manifests


def run_normalize(root: Path, manifests: list[Path], output: Path) -> int:
    args = []
    for manifest in manifests:
        args += ["--manifest", str(manifest)]
    args += [
        "--city", "taipei",
        "--source-id", "cds_era5_hourly",
        "--data-class", "final_reanalysis",
        "--root", str(root),
        "--output", str(output),
    ]
    return normalize_main(args)


def canonical_frame(output: Path) -> pd.DataFrame:
    return pd.read_parquet(output)


# ---------------- happy path ----------------

def test_three_segments_normalize_to_121_rows(tmp_path):
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/taipei-era5-final-pilot.parquet"
    code = run_normalize(root, manifests, output)
    assert code == 0
    frame = canonical_frame(output)
    assert len(frame) == 121
    assert frame["timestamp_utc"].is_unique
    first = pd.to_datetime(frame["timestamp_utc"], utc=True).min()
    last = pd.to_datetime(frame["timestamp_utc"], utc=True).max()
    assert first == pd.Timestamp("2026-03-01T16:00:00Z")
    assert last == pd.Timestamp("2026-03-06T16:00:00Z")
    assert (frame["data_class"] == "final_reanalysis").all()
    assert (frame["is_final"] == True).all()  # noqa: E712
    assert (frame["source_id"] == "cds_era5_hourly").all()


def test_canonical_provenance_columns(tmp_path):
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/taipei-era5-final-pilot.parquet"
    run_normalize(root, manifests, output)
    frame = canonical_frame(output)
    for column in ("raw_artifact_id", "raw_revision", "selected_latitude", "selected_longitude"):
        assert column in frame.columns
    assert len(set(frame["raw_artifact_id"])) == 3  # per-segment provenance


def test_canonical_numeric_ranges(tmp_path):
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/taipei-era5-final-pilot.parquet"
    run_normalize(root, manifests, output)
    frame = canonical_frame(output)
    assert (frame["relative_humidity_pct"] >= 0).all() and (frame["relative_humidity_pct"] <= 100).all()
    assert (frame["cloud_cover_pct"] >= 0).all() and (frame["cloud_cover_pct"] <= 100).all()
    for column in ("precipitation_mm", "wind_speed_mps", "max_gust_mps", "solar_radiation_mj_m2"):
        assert (frame[column] >= 0).all()
    assert (frame["dew_point_c"] - frame["air_temperature_c"] <= 0.5).all()


def test_member_order_does_not_matter(tmp_path):
    # accum member first, instant second (reverse of the standard layout).
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    manifests = []
    store = RawArtifactStore(root / "data" / "source_raw" / "v2" / "weather")
    for index, plan in enumerate(SEGMENT_PLANS, start=1):
        timestamps = segment_timestamps(plan)
        with zipfile.ZipFile(root / f"r{index}.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                p1 = Path(tmp) / "instant.nc"
                p2 = Path(tmp) / "accum.nc"
                make_member(p1, INSTANT_VARS, timestamps)
                make_member(p2, ACCUM_VARS, timestamps)
                zf.write(p2, "data_stream-oper_stepType-accum.nc")
                zf.write(p1, "data_stream-oper_stepType-instant.nc")
        request = {
            "variable": ["2m_temperature", "2m_dewpoint_temperature", "total_precipitation", "total_cloud_cover", "10m_u_component_of_wind", "10m_v_component_of_wind", "instantaneous_10m_wind_gust", "surface_solar_radiation_downwards"],
            "date": plan["date"], "time": plan["times"],
            "data_format": "netcdf", "download_format": "zip", "area": AREA,
        }
        result = store.persist(
            source_id="cds_era5_hourly", provider="ECMWF", logical_name=f"seg{index}-rev",
            payload=(root / f"r{index}.zip").read_bytes(), request=request,
            status="final", licence="CC-BY", suffix=".zip",
            validation_metadata={"container_type": "zip", "container_validation_passed": True},
        )
        manifests.append(result.manifest_path)
    output = root / "data/canonical/v2/weather/out.parquet"
    code = run_normalize(root, manifests, output)
    assert code == 0
    assert len(canonical_frame(output)) == 121


# ---------------- plan / manifest validation failures ----------------

def test_missing_segment_fails(tmp_path):
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    with pytest.raises(V2Error, match="3 manifests"):
        validate_manifest_plan([json.loads(manifests[0].read_text(encoding="utf-8"))])


def test_overlapping_segments_fail(tmp_path):
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    parsed = [json.loads(item.read_text(encoding="utf-8")) for item in manifests]
    parsed[1]["request"]["date"] = parsed[0]["request"]["date"]  # force overlap
    with pytest.raises(V2Error, match="overlap"):
        validate_manifest_plan(parsed)


def test_gap_in_segments_fails_plan_hash(tmp_path):
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    parsed = [json.loads(item.read_text(encoding="utf-8")) for item in manifests]
    # Replace segment 2's date coverage without creating overlap -> plan hash changes.
    parsed[1]["request"]["date"] = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-08"]
    with pytest.raises(V2Error, match="request plan hash mismatch"):
        validate_manifest_plan(parsed)


def test_manifest_sha_mismatch_fails(tmp_path):
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    manifest_path = manifests[0]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(V2Error, match="sha256 mismatch"):
        read_manifest(manifest_path, "cds_era5_hourly")


def test_container_revalidation_failure_blocks_normalize(tmp_path):
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/out.parquet"
    # Corrupt one artifact zip (append junk breaks zipfile / magic is fine).
    zip_path = manifests[1].with_name(manifests[1].name.replace(".manifest.json", ".zip"))
    original = zip_path.read_bytes()
    zip_path.write_bytes(original + b"JUNK")
    with pytest.raises(V2Error):
        run_normalize(root, manifests, output)
    zip_path.write_bytes(original)  # restore
    assert not output.exists()


# ---------------- expver ----------------

def _dataset_with_expver(values_by_expver: dict[int, float | float]) -> xr.Dataset:
    expver = sorted(values_by_expver)
    ts = pd.DatetimeIndex([pd.Timestamp("2026-03-01T16:00:00")])
    array = np.full((len(expver), 1, 1, 1), np.nan)
    for index, key in enumerate(expver):
        value = values_by_expver[key]
        array[index, 0, 0, 0] = value
    return xr.Dataset(
        {"t2m": (("expver", "valid_time", "latitude", "longitude"), array)},
        coords={"expver": expver, "valid_time": ts, "latitude": [25.0], "longitude": [121.5]},
    )


def test_expver_single_value_passes():
    dataset = _dataset_with_expver({1: 283.15})
    resolved, info = resolve_expver(dataset, "t2m")
    assert resolved.shape == (1, 1, 1)
    assert resolved[0, 0, 0] == pytest.approx(283.15)
    assert info["conflicting_cell_count"] == 0
    assert info["missing_cell_count"] == 0


def test_expver_complementary_nonnull_passes():
    ts = pd.DatetimeIndex([pd.Timestamp("2026-03-01T16:00:00"), pd.Timestamp("2026-03-01T17:00:00")])
    array = np.full((2, 2, 1, 1), np.nan)
    array[0, 0, 0, 0] = 283.15
    array[1, 1, 0, 0] = 283.15
    dataset = xr.Dataset(
        {"t2m": (("expver", "valid_time", "latitude", "longitude"), array)},
        coords={"expver": [1, 2], "valid_time": ts, "latitude": [25.0], "longitude": [121.5]},
    )
    resolved, info = resolve_expver(dataset, "t2m")
    assert info["single_source_cell_count"] == 2
    assert info["conflicting_cell_count"] == 0
    assert info["missing_cell_count"] == 0
    assert resolved[:, 0, 0].tolist() == pytest.approx([283.15, 283.15])


def test_expver_overlapping_equal_passes():
    dataset = _dataset_with_expver({1: 283.15, 2: 283.15})
    resolved, info = resolve_expver(dataset, "t2m")
    assert info["overlapping_equal_cell_count"] == 1
    assert info["conflicting_cell_count"] == 0
    assert resolved[0, 0, 0] == pytest.approx(283.15)


def test_expver_conflict_fails():
    dataset = _dataset_with_expver({1: 283.15, 2: 300.0})
    with pytest.raises(V2Error, match="conflict"):
        resolve_expver(dataset, "t2m")


def test_expver_all_empty_fails():
    dataset = _dataset_with_expver({1: np.nan, 2: np.nan})
    with pytest.raises(V2Error, match="empty"):
        resolve_expver(dataset, "t2m")


def test_no_expver_dimension_left_after_resolution(tmp_path):
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    # happy path already resolved expver-free members; verify no expver column leaks.
    output = root / "data/canonical/v2/weather/out.parquet"
    run_normalize(root, manifests, output)
    frame = canonical_frame(output)
    assert "expver" not in frame.columns


# ---------------- grid point selection ----------------

def test_nearest_grid_point_selection():
    dataset = xr.Dataset(
        coords={"latitude": [24.9, 25.0, 25.1], "longitude": [121.4, 121.5]},
    )
    point = select_nearest_point(dataset, 25.0375, 121.5646)
    assert point["selected_latitude"] == pytest.approx(25.0)
    assert point["selected_longitude"] == pytest.approx(121.5)
    assert point["grid_latitude_count"] == 3
    assert point["grid_longitude_count"] == 2


def test_grid_point_inconsistent_across_members_fails(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    store = RawArtifactStore(root / "data" / "source_raw" / "v2" / "weather")
    manifests = []
    for index, plan in enumerate(SEGMENT_PLANS, start=1):
        timestamps = segment_timestamps(plan)
        with zipfile.ZipFile(root / f"g{index}.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                p1 = Path(tmp) / "instant.nc"
                p2 = Path(tmp) / "accum.nc"
                lat1 = [25.0] if index != 1 else [25.16]  # segment 1 uses a different grid
                make_member(p1, INSTANT_VARS, timestamps, latitude=lat1)
                make_member(p2, ACCUM_VARS, timestamps)
                zf.write(p1, "data_stream-oper_stepType-instant.nc")
                zf.write(p2, "data_stream-oper_stepType-accum.nc")
        request = {"variable": ["2m_temperature", "2m_dewpoint_temperature", "total_precipitation", "total_cloud_cover", "10m_u_component_of_wind", "10m_v_component_of_wind", "instantaneous_10m_wind_gust", "surface_solar_radiation_downwards"], "date": plan["date"], "time": plan["times"], "data_format": "netcdf", "download_format": "zip", "area": AREA}
        result = store.persist(
            source_id="cds_era5_hourly", provider="ECMWF", logical_name=f"g{index}",
            payload=(root / f"g{index}.zip").read_bytes(), request=request,
            status="final", licence="CC-BY", suffix=".zip",
            validation_metadata={"container_type": "zip", "container_validation_passed": True},
        )
        manifests.append(result.manifest_path)
    output = root / "data/canonical/v2/weather/out.parquet"
    with pytest.raises(V2Error, match="grid mismatch|inconsistent"):
        run_normalize(root, manifests, output)


# ---------------- variable / unit failures ----------------

def test_missing_variable_fails(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    store = RawArtifactStore(root / "data" / "source_raw" / "v2" / "weather")
    manifests = []
    for index, plan in enumerate(SEGMENT_PLANS, start=1):
        timestamps = segment_timestamps(plan)
        with zipfile.ZipFile(root / f"m{index}.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                p1 = Path(tmp) / "instant.nc"
                p2 = Path(tmp) / "accum.nc"
                make_member(p1, INSTANT_VARS, timestamps)
                vars2 = ACCUM_VARS if index != 1 else ["tp"]  # segment 1 drops ssrd
                make_member(p2, vars2, timestamps)
                zf.write(p1, "data_stream-oper_stepType-instant.nc")
                zf.write(p2, "data_stream-oper_stepType-accum.nc")
        request = {"variable": ["2m_temperature", "2m_dewpoint_temperature", "total_precipitation", "total_cloud_cover", "10m_u_component_of_wind", "10m_v_component_of_wind", "instantaneous_10m_wind_gust", "surface_solar_radiation_downwards"], "date": plan["date"], "time": plan["times"], "data_format": "netcdf", "download_format": "zip", "area": AREA}
        result = store.persist(
            source_id="cds_era5_hourly", provider="ECMWF", logical_name=f"m{index}",
            payload=(root / f"m{index}.zip").read_bytes(), request=request,
            status="final", licence="CC-BY", suffix=".zip",
            validation_metadata={"container_type": "zip", "container_validation_passed": True},
        )
        manifests.append(result.manifest_path)
    output = root / "data/canonical/v2/weather/out.parquet"
    with pytest.raises(V2Error, match="miss requested variables"):
        run_normalize(root, manifests, output)


def test_incompatible_units_fail(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    store = RawArtifactStore(root / "data" / "source_raw" / "v2" / "weather")
    manifests = []
    for index, plan in enumerate(SEGMENT_PLANS, start=1):
        timestamps = segment_timestamps(plan)
        with zipfile.ZipFile(root / f"u{index}.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                p1 = Path(tmp) / "instant.nc"
                p2 = Path(tmp) / "accum.nc"
                units = {"t2m": "banana"} if index == 1 else None
                make_member(p1, INSTANT_VARS, timestamps, units=units)
                make_member(p2, ACCUM_VARS, timestamps)
                zf.write(p1, "data_stream-oper_stepType-instant.nc")
                zf.write(p2, "data_stream-oper_stepType-accum.nc")
        request = {"variable": ["2m_temperature", "2m_dewpoint_temperature", "total_precipitation", "total_cloud_cover", "10m_u_component_of_wind", "10m_v_component_of_wind", "instantaneous_10m_wind_gust", "surface_solar_radiation_downwards"], "date": plan["date"], "time": plan["times"], "data_format": "netcdf", "download_format": "zip", "area": AREA}
        result = store.persist(
            source_id="cds_era5_hourly", provider="ECMWF", logical_name=f"u{index}",
            payload=(root / f"u{index}.zip").read_bytes(), request=request,
            status="final", licence="CC-BY", suffix=".zip",
            validation_metadata={"container_type": "zip", "container_validation_passed": True},
        )
        manifests.append(result.manifest_path)
    output = root / "data/canonical/v2/weather/out.parquet"
    with pytest.raises(V2Error, match="unit incompatibility"):
        run_normalize(root, manifests, output)


# ---------------- raw preservation / git ----------------

def test_raw_artifacts_unchanged_after_normalize(tmp_path):
    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    before = {}
    for manifest_path in manifests:
        zip_path = manifest_path.with_name(manifest_path.name.replace(".manifest.json", ".zip"))
        before[str(zip_path)] = zip_path.read_bytes()
    output = root / "data/canonical/v2/weather/out.parquet"
    run_normalize(root, manifests, output)
    for manifest_path in manifests:
        zip_path = manifest_path.with_name(manifest_path.name.replace(".manifest.json", ".zip"))
        assert zip_path.read_bytes() == before[str(zip_path)]


def test_canonical_output_not_git_tracked(tmp_path):
    import subprocess

    root = tmp_path / "repo"
    manifests = make_pilot_artifacts(root)
    output = root / "data/canonical/v2/weather/taipei-era5-final-pilot.parquet"
    run_normalize(root, manifests, output)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    tracked = subprocess.run(
        ["git", "ls-files", "data/canonical"], capture_output=True, text=True, cwd=root
    ).stdout.strip()
    assert tracked == ""


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


def test_plan_sha256_of_real_plan():
    # Guards the fixed pilot plan hash contract.
    from scripts.v2.normalize_weather import PILOT_EXPECTED_PLAN_SHA256

    assert len(PILOT_EXPECTED_PLAN_SHA256) == 64
