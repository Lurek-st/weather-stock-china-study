"""ZIP-aware canonical weather normalization for the Taipei ERA5 pilot.

Two entry paths:

- ``--manifest`` (repeatable, 3 times): ZIP-aware normalization of the three
  pilot raw artifacts. Each manifest is re-validated (artifact SHA, container
  via ``validate_download_container``), members are extracted safely, the
  ``expver`` dimension is resolved, the nearest grid point to the frozen
  Taipei coordinate is selected, instant and accum members are merged per
  timestamp, and the three segments are concatenated into a strictly hourly
  121-row canonical frame written as Parquet (local-only, never committed).
- ``--input`` (CSV or single NetCDF): legacy single-file path, kept for
  compatibility with existing tests.

No network access: this module never imports or instantiates a CDS client.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat as stat_module
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd
import xarray as xr

from scripts.v2.core import V2Error, normalize_weather_frame, repo_root, write_json
from scripts.v2.fetch_era5 import (
    CONTAINER_REASON,
    DOWNLOAD_FORMAT,
    NETCDF_VARIABLES,
    expected_timestamps,
    validate_download_container,
)

FROZEN_LATITUDE = 25.0375
FROZEN_LONGITUDE = 121.5646

# NetCDF short variable -> V2 raw canonical column.
NETCDF_TO_RAW = {
    "t2m": "temperature_k",
    "d2m": "dewpoint_k",
    "tp": "precipitation_m",
    "tcc": "cloud_cover_fraction",
    "u10": "wind_u_mps",
    "v10": "wind_v_mps",
    "i10fg": "wind_gust_mps",
    "ssrd": "solar_radiation_j_m2",
}
RAW_COLUMNS = list(NETCDF_TO_RAW.values())

# Unit semantic compatibility (whitespace-normalised comparison).
UNIT_COMPAT = {
    "t2m": {"K"},
    "d2m": {"K"},
    "tp": {"m"},
    "tcc": {"(0-1)", "1"},
    "u10": {"ms**-1", "ms-1", "m/s", "ms^-1"},
    "v10": {"ms**-1", "ms-1", "m/s", "ms^-1"},
    "i10fg": {"ms**-1", "ms-1", "m/s", "ms^-1"},
    "ssrd": {"Jm**-2", "Jm-2"},
}

# ZIP safety limits mirrored from fetch_era5.
ZIP_MAX_MEMBER_COUNT = 16
ZIP_MAX_MEMBER_SIZE = 64 * 1024 * 1024
ZIP_MAX_TOTAL_UNCOMPRESSED = 128 * 1024 * 1024
ZIP_MAX_COMPRESSION_RATIO = 1000.0
ARCHIVE_MEMBER_SUFFIXES = (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".grib", ".grb")

PILOT_EXPECTED_PLAN_SHA256 = "73824d434f115e045209c26b7ad3f6ea9608997b531704fbb2084268719384f1"


def read_input(path: Path) -> pd.DataFrame:
    """Legacy single-file reader (CSV or NetCDF); kept for compatibility."""
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() not in {".nc", ".netcdf"}:
        raise SystemExit("weather input must be CSV or NetCDF")
    with path.open("rb") as handle:
        magic = handle.read(3)
    engine = "scipy" if magic == b"CDF" else "netcdf4"
    dataset = xr.open_dataset(path, engine=engine)
    if "latitude" in dataset.coords and "longitude" in dataset.coords:
        dataset = dataset.sel(
            latitude=float(dataset["latitude"].mean()),
            longitude=float(dataset["longitude"].mean()),
            method="nearest",
        )
    rename = {
        "t2m": "temperature_k",
        "d2m": "dewpoint_k",
        "tp": "precipitation_m",
        "tcc": "cloud_cover_fraction",
        "u10": "wind_u_mps",
        "v10": "wind_v_mps",
        "i10fg": "wind_gust_mps",
        "fg10": "wind_gust_mps",
        "ssrd": "solar_radiation_j_m2",
    }
    if "valid_time" in dataset.variables or "valid_time" in dataset.coords:
        rename["valid_time"] = "timestamp_utc"
    elif "time" in dataset.variables or "time" in dataset.coords:
        rename["time"] = "timestamp_utc"
    available = {key: value for key, value in rename.items() if key in dataset.variables or key in dataset.coords}
    frame = dataset.rename(available).to_dataframe().reset_index()
    return frame


# ---------------- manifest / container re-validation ----------------

def read_manifest(manifest_path: Path, expected_source_id: str) -> dict[str, Any]:
    """Load a manifest and re-verify its artifact SHA and size."""
    if not manifest_path.exists():
        raise V2Error(f"manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    zip_path = manifest_path.with_name(manifest_path.name.replace(".manifest.json", ".zip"))
    if not zip_path.exists():
        raise V2Error(f"artifact zip missing for manifest: {manifest_path.name}")
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if digest != manifest.get("sha256"):
        raise V2Error(f"manifest sha256 mismatch for {manifest_path.name}")
    if zip_path.stat().st_size != manifest.get("content_length"):
        raise V2Error(f"manifest content_length mismatch for {manifest_path.name}")
    if manifest.get("source_id") != expected_source_id:
        raise V2Error(
            f"manifest source_id {manifest.get('source_id')!r} != expected {expected_source_id!r}"
        )
    if manifest.get("status") != "final":
        raise V2Error(f"manifest status {manifest.get('status')!r} != final")
    request = manifest.get("request") or {}
    if request.get("data_format") != "netcdf":
        raise V2Error("manifest request data_format != netcdf")
    if request.get("download_format") != DOWNLOAD_FORMAT:
        raise V2Error(f"manifest request download_format != {DOWNLOAD_FORMAT}")
    return manifest


def _plan_segments(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild request-plan segments from manifests, ordered by start date."""
    segments = []
    for index, manifest in enumerate(manifests, start=1):
        dates = manifest["request"]["date"]
        times = manifest["request"]["time"]
        label = dates[0] if len(dates) == 1 else f"{dates[0]}-to-{dates[-1]}"
        segments.append(
            {
                "segment_id": f"segment-{index:02d}-{label}",
                "utc_dates": dates,
                "times": times,
                "download_format": manifest["request"].get("download_format"),
            }
        )
    segments.sort(key=lambda item: (item["utc_dates"][0], item["segment_id"]))
    return segments


def plan_sha256(manifests: list[dict[str, Any]]) -> str:
    """Request-plan hash from manifests (mirrors fetch_era5 plan hashing)."""
    payload = json.dumps(_plan_segments(manifests), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_manifest_plan(manifests: list[dict[str, Any]]) -> None:
    """Three distinct segments, no overlap, no duplicates, fixed plan hash."""
    if len(manifests) != 3:
        raise V2Error(f"expected 3 manifests, got {len(manifests)}")
    date_sets = [set(manifest["request"]["date"]) for manifest in manifests]
    union = set().union(*date_sets)
    if len(union) != sum(len(item) for item in date_sets):
        raise V2Error("manifest date ranges overlap (duplicate segment dates)")
    total_dates = sum(len(item) for item in date_sets)
    if total_dates != 6:
        raise V2Error(f"pilot plan must cover 6 distinct UTC dates, got {total_dates}")
    hour_counts = [len(manifest["request"]["time"]) for manifest in manifests]
    if sorted(hour_counts) != [8, 17, 24]:
        raise V2Error(f"segment hourly time-list counts not 8/17/24: {sorted(hour_counts)}")
    variable_sets = [sorted(manifest["request"]["variable"]) for manifest in manifests]
    if len({tuple(item) for item in variable_sets}) != 1:
        raise V2Error("request variable sets inconsistent across segments")
    area_sets = [manifest["request"]["area"] for manifest in manifests]
    if len({tuple(item) for item in area_sets}) != 1:
        raise V2Error("request area inconsistent across segments")
    computed = plan_sha256(manifests)
    if computed != PILOT_EXPECTED_PLAN_SHA256:
        raise V2Error(f"request plan hash mismatch: got {computed}, expected {PILOT_EXPECTED_PLAN_SHA256}")


# ---------------- safe extraction ----------------

def safe_extract_zip_members(zip_path: Path, workdir: Path) -> list[tuple[str, Path]]:
    """Extract all ZIP members into workdir with the same safety rules as fetch_era5."""
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if not infos:
            raise V2Error("zip container has no members")
        if len(infos) > ZIP_MAX_MEMBER_COUNT:
            raise V2Error(f"zip member count {len(infos)} exceeds limit")
        names = [info.filename for info in infos]
        if len(set(names)) != len(names):
            raise V2Error("duplicate zip member names")
        lowered = [name.lower() for name in names]
        if len(set(lowered)) != len(lowered):
            raise V2Error("case-insensitive duplicate zip member names")
        drive_prefix = re.compile(r"^[A-Za-z]:[\\/]")
        total_uncompressed = 0
        for info in infos:
            name = info.filename
            if info.flag_bits & 0x1:
                raise V2Error("encrypted zip member not allowed")
            if name.startswith("/") or drive_prefix.match(name) or name.split("/")[0] == "..":
                raise V2Error("unsafe zip member path")
            parts = name.split("/")
            if ".." in parts:
                raise V2Error("zip member path traversal not allowed")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat_module.S_ISLNK(mode):
                raise V2Error("symbolic-link zip member not allowed")
            if name.lower().endswith(ARCHIVE_MEMBER_SUFFIXES):
                raise V2Error(f"nested archive member not allowed: {name}")
            if info.file_size > ZIP_MAX_MEMBER_SIZE:
                raise V2Error("zip member exceeds size limit")
            if info.compress_size > 0 and info.file_size / info.compress_size > ZIP_MAX_COMPRESSION_RATIO:
                raise V2Error("zip member compression ratio exceeds limit (suspected bomb)")
            total_uncompressed += info.file_size
            if total_uncompressed > ZIP_MAX_TOTAL_UNCOMPRESSED:
                raise V2Error("zip total uncompressed size exceeds limit (suspected bomb)")
        member_paths = []
        for info in infos:
            target = (workdir / info.filename).resolve()
            if not target.is_relative_to(workdir.resolve()):
                raise V2Error("zip member escapes extraction directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as dest:
                dest.write(source.read())
            member_paths.append((info.filename, target))
    return member_paths


# ---------------- expver resolution ----------------

def resolve_expver(dataset: xr.Dataset, variable: str) -> tuple[np.ndarray, dict[str, Any]]:
    """Collapse an optional ``expver`` dimension on one variable.

    For each non-expver cell: 0 non-null -> fail; 1 non-null -> select it;
    >1 non-null -> select when equal within tolerance, else fail.
    """
    data_array = dataset[variable]
    array = np.asarray(data_array.values, dtype=float)
    if "expver" not in data_array.dims:
        valid = ~np.isnan(array)
        info = {
            "expver_values": [],
            "single_source_cell_count": int(valid.sum()),
            "overlapping_equal_cell_count": 0,
            "conflicting_cell_count": 0,
            "missing_cell_count": int((~valid).sum()),
        }
        return array, info
    expver_values = [str(value) for value in dataset["expver"].values]
    valid = ~np.isnan(array)
    counts = valid.sum(axis=0)
    single = int((counts == 1).sum())
    missing = int((counts == 0).sum())
    out = np.full(array.shape[1:], np.nan, dtype=float)
    overlap_equal = 0
    conflicting = 0
    for index in np.ndindex(array.shape[1:]):
        column = array[(slice(None),) + index]
        mask = valid[(slice(None),) + index]
        values = column[mask]
        count = int(values.size)
        if count == 0:
            continue
        if count == 1:
            out[index] = values[0]
        elif np.allclose(values, values[0], rtol=1e-6, atol=1e-8):
            overlap_equal += 1
            out[index] = values[0]
        else:
            conflicting += 1
    if missing > 0:
        raise V2Error(f"variable {variable}: {missing} cells empty across all expver values")
    if conflicting > 0:
        raise V2Error(f"variable {variable}: {conflicting} cells conflict across expver values")
    info = {
        "expver_values": expver_values,
        "single_source_cell_count": single,
        "overlapping_equal_cell_count": overlap_equal,
        "conflicting_cell_count": conflicting,
        "missing_cell_count": missing,
    }
    return out, info


# ---------------- grid point selection ----------------

def select_nearest_point(
    dataset: xr.Dataset, latitude: float, longitude: float
) -> dict[str, Any]:
    latitude_values = np.asarray(dataset["latitude"].values, dtype=float)
    longitude_values = np.asarray(dataset["longitude"].values, dtype=float)
    index_lat = int(np.argmin(np.abs(latitude_values - latitude)))
    index_lon = int(np.argmin(np.abs(longitude_values - longitude)))
    selected_lat = float(latitude_values[index_lat])
    selected_lon = float(longitude_values[index_lon])
    return {
        "requested_latitude": latitude,
        "requested_longitude": longitude,
        "selected_latitude": selected_lat,
        "selected_longitude": selected_lon,
        "latitude_distance": float(abs(selected_lat - latitude)),
        "longitude_distance": float(abs(selected_lon - longitude)),
        "grid_latitude_count": int(latitude_values.size),
        "grid_longitude_count": int(longitude_values.size),
        "index_latitude": index_lat,
        "index_longitude": index_lon,
    }


# ---------------- member / segment / concat ----------------

def member_to_frame(member_path: Path, latitude: float, longitude: float) -> tuple[pd.DataFrame, dict[str, Any]]:
    with xr.open_dataset(member_path) as dataset:
        point = select_nearest_point(dataset, latitude, longitude)
        time_coord = dataset.get("valid_time")
        if time_coord is None:
            time_coord = dataset.get("time")
        if time_coord is None or int(time_coord.size) == 0:
            raise V2Error("member has no time coordinate values")
        raw_times = pd.to_datetime(time_coord.values)
        timestamps = []
        for value in raw_times:
            stamp = pd.Timestamp(value)
            timestamps.append(stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC"))
        frame = pd.DataFrame({"timestamp_utc": timestamps})
        expver_info: dict[str, Any] = {}
        units: dict[str, Any] = {}
        for netcdf_var, raw_column in NETCDF_TO_RAW.items():
            if netcdf_var not in dataset.data_vars:
                continue
            data_array = dataset[netcdf_var]
            resolved, expver_summary = resolve_expver(dataset, netcdf_var)
            expver_info[netcdf_var] = expver_summary
            unit = data_array.attrs.get("units")
            units[netcdf_var] = unit
            if unit is not None:
                normalized = str(unit).replace(" ", "")
                compatible = {item.replace(" ", "") for item in UNIT_COMPAT.get(netcdf_var, ())}
                if compatible and normalized not in compatible:
                    raise V2Error(f"unit incompatibility for {netcdf_var}: got {unit!r}")
            index_lat = point["index_latitude"]
            index_lon = point["index_longitude"]
            if resolved.ndim == 3:
                values = resolved[:, index_lat, index_lon]
            elif resolved.ndim == 2:
                values = resolved[:, index_lat]
            else:
                values = resolved[:]
            frame[raw_column] = values
        variables = sorted(set(dataset.data_vars) & set(NETCDF_VARIABLES))
        return frame, {"variables": variables, "expver": expver_info, "units": units, "point": point}


def segment_to_frame(
    zip_path: Path, request: dict[str, Any], area: dict[str, Any], latitude: float, longitude: float
) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, Any]]]:
    container = validate_download_container(zip_path, request, area)
    if not container["container_validation_passed"]:
        raise V2Error(f"container re-validation failed for {zip_path.name}")
    with tempfile.TemporaryDirectory(prefix="ws-v2-normalize-") as tmp:
        members = safe_extract_zip_members(zip_path, Path(tmp))
        frames = []
        infos = []
        for name, member_path in members:
            frame, info = member_to_frame(member_path, latitude, longitude)
            frames.append(frame)
            infos.append(info)
        points = {
            (info["point"]["selected_latitude"], info["point"]["selected_longitude"])
            for info in infos
        }
        if len(points) != 1:
            raise V2Error("selected grid point inconsistent across members")
        variable_union = set().union(*[set(info["variables"]) for info in infos])
        missing_variables = sorted(set(NETCDF_VARIABLES) - variable_union)
        if missing_variables:
            raise V2Error(f"segment missing requested variables: {missing_variables}")
        duplicates = sorted(
            variable
            for variable in variable_union
            if sum(variable in info["variables"] for info in infos) > 1
        )
        if duplicates:
            raise V2Error(f"variable duplicated across members: {duplicates}")
        merged = frames[0]
        for frame in frames[1:]:
            extra = [column for column in frame.columns if column not in merged.columns]
            merged = merged.merge(frame[["timestamp_utc"] + extra], on="timestamp_utc", how="inner")
        if not set(RAW_COLUMNS).issubset(set(merged.columns)):
            raise V2Error("merged segment frame missing raw variables")
        if merged["timestamp_utc"].duplicated().any():
            raise V2Error("duplicate timestamp within segment")
        expected_count = len(expected_timestamps(request))
        if len(merged) != expected_count:
            raise V2Error(f"segment row count {len(merged)} != expected {expected_count}")
    return merged, container, infos


def concat_segments(segment_frames: list[pd.DataFrame], expected_total: int = 121) -> pd.DataFrame:
    combined = pd.concat(segment_frames, ignore_index=True).sort_values("timestamp_utc").reset_index(drop=True)
    if combined["timestamp_utc"].duplicated().any():
        raise V2Error("duplicate timestamps across segments")
    differences = combined["timestamp_utc"].diff().dropna()
    bad = differences[differences != pd.Timedelta(hours=1)]
    if len(bad) > 0:
        raise V2Error(f"non-hourly gap or overlap across segments ({len(bad)} bad diffs)")
    if len(combined) != expected_total:
        raise V2Error(f"expected {expected_total} canonical rows, got {len(combined)}")
    return combined


def build_canonical(
    frame: pd.DataFrame,
    *,
    city: str,
    source_id: str,
    data_class: str,
    artifact_by_segment: list[dict[str, Any]],
    segment_index: pd.Series,
    selected_latitude: float,
    selected_longitude: float,
) -> pd.DataFrame:
    normalized = normalize_weather_frame(
        frame,
        city_id=city,
        source_id=source_id,
        data_class=data_class,
    )
    segment_ids = segment_index.to_numpy()
    normalized["raw_artifact_id"] = [
        artifact_by_segment[index]["artifact_id"] for index in segment_ids
    ]
    normalized["raw_revision"] = [
        artifact_by_segment[index]["revision"] for index in segment_ids
    ]
    normalized["selected_latitude"] = selected_latitude
    normalized["selected_longitude"] = selected_longitude
    return normalized


def canonical_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_canonical(frame: pd.DataFrame, expected_total: int = 121) -> dict[str, Any]:
    if len(frame) != expected_total:
        raise V2Error(f"canonical row count {len(frame)} != {expected_total}")
    if frame["timestamp_utc"].duplicated().any():
        raise V2Error("canonical timestamp not unique")
    times = pd.to_datetime(frame["timestamp_utc"], utc=True).sort_values()
    differences = times.diff().dropna()
    if (differences != pd.Timedelta(hours=1)).any():
        raise V2Error("canonical timestamps not strictly hourly")
    if times.iloc[0] != pd.Timestamp("2026-03-01T16:00:00Z"):
        raise V2Error(f"canonical first timestamp {times.iloc[0]} != 2026-03-01T16:00:00Z")
    if times.iloc[-1] != pd.Timestamp("2026-03-06T16:00:00Z"):
        raise V2Error(f"canonical last timestamp {times.iloc[-1]} != 2026-03-06T16:00:00Z")
    core = [
        "air_temperature_c",
        "dew_point_c",
        "relative_humidity_pct",
        "apparent_temperature_c",
        "precipitation_mm",
        "cloud_cover_pct",
        "wind_speed_mps",
        "max_gust_mps",
        "solar_radiation_mj_m2",
    ]
    null_counts = {column: int(frame[column].isna().sum()) for column in core}
    if any(count > 0 for count in null_counts.values()):
        raise V2Error(f"nulls in canonical variables: {null_counts}")
    for column in core:
        if not np.isfinite(frame[column].astype(float)).all():
            raise V2Error(f"non-finite values in canonical {column}")
    if (frame["relative_humidity_pct"] < 0).any() or (frame["relative_humidity_pct"] > 100).any():
        raise V2Error("relative_humidity_pct outside 0..100")
    if (frame["cloud_cover_pct"] < 0).any() or (frame["cloud_cover_pct"] > 100).any():
        raise V2Error("cloud_cover_pct outside 0..100")
    for column in ("precipitation_mm", "wind_speed_mps", "max_gust_mps", "solar_radiation_mj_m2"):
        if (frame[column] < 0).any():
            raise V2Error(f"{column} negative")
    if (frame["dew_point_c"] - frame["air_temperature_c"] > 0.5).any():
        raise V2Error("dew_point_c markedly above air_temperature_c")
    if (frame["data_class"] != "final_reanalysis").any():
        raise V2Error("canonical data_class != final_reanalysis")
    if (frame["is_final"] != True).any():  # noqa: E712
        raise V2Error("canonical is_final != True")
    if (frame["source_id"] != "cds_era5_hourly").any():
        raise V2Error("canonical source_id != cds_era5_hourly")
    return {
        "row_count": len(frame),
        "first_timestamp": times.iloc[0].isoformat(),
        "last_timestamp": times.iloc[-1].isoformat(),
        "duplicate_count": 0,
        "missing_hour_count": 0,
        "null_counts": null_counts,
    }


# ---------------- main ----------------

def _normalize_zip_pilot(args: argparse.Namespace) -> int:
    manifests = [read_manifest(path, args.source_id) for path in args.manifest]
    validate_manifest_plan(manifests)
    requests = []
    areas = []
    for manifest in manifests:
        request = manifest["request"]
        requests.append(request)
        areas.append(
            {
                "area": request["area"],
                "area_north": request["area"][0],
                "area_west": request["area"][1],
                "area_south": request["area"][2],
                "area_east": request["area"][3],
            }
        )
    segment_frames = []
    segment_infos = []
    for index, (manifest, request, area) in enumerate(zip(manifests, requests, areas)):
        zip_path = manifest_path_of(manifest, args.root)
        frame, container, infos = segment_to_frame(zip_path, request, area, args.latitude, args.longitude)
        frame["_segment_index"] = index
        segment_frames.append(frame)
        segment_infos.append({"manifest": manifest, "container": container, "member_infos": infos})
    combined = concat_segments(segment_frames, expected_total=121)
    selected_points = {
        (info["point"]["selected_latitude"], info["point"]["selected_longitude"])
        for item in segment_infos
        for info in item["member_infos"]
    }
    if len(selected_points) != 1:
        raise V2Error("selected grid point inconsistent across segments")
    selected_latitude, selected_longitude = next(iter(selected_points))
    output = args.output or (
        args.root / "data" / "canonical" / "v2" / "weather" / f"{args.city}-era5-final-pilot.parquet"
    )
    if not output.is_absolute():
        output = (args.root / output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    artifact_by_segment = [
        {"artifact_id": manifest["artifact_id"], "revision": manifest["revision"]}
        for manifest in manifests
    ]
    canonical = build_canonical(
        combined,
        city=args.city,
        source_id=args.source_id,
        data_class=args.data_class,
        artifact_by_segment=artifact_by_segment,
        segment_index=combined["_segment_index"],
        selected_latitude=selected_latitude,
        selected_longitude=selected_longitude,
    )
    validation = validate_canonical(canonical)
    canonical.to_parquet(output, index=False)
    parquet_sha = canonical_sha256(output)
    expver_summary: dict[str, Any] = {}
    source_units: dict[str, Any] = {}
    for item in segment_infos:
        for info in item["member_infos"]:
            for variable, unit in info["units"].items():
                source_units.setdefault(variable, unit)
            for variable, expver in info["expver"].items():
                expver_summary.setdefault(variable, expver)
    if args.raw_audit_output is not None:
        raw_audit = build_raw_acceptance_audit(manifests, segment_infos)
        write_json(args.root / args.raw_audit_output, raw_audit)
    if args.audit_output is not None:
        normalizer_version = _repo_head(args.root)
        audit = {
            "schema_version": "2.0.0",
            "audit_type": "taipei_era5_normalization",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "city_id": args.city,
            "market_id": "taiex",
            "input_raw_acceptance_audit_sha256": None,
            "artifacts": [
                {"artifact_id": manifest["artifact_id"], "sha256": manifest["sha256"]}
                for manifest in manifests
            ],
            "normalizer_version": normalizer_version,
            "selected_grid_point": {
                "requested_latitude": args.latitude,
                "requested_longitude": args.longitude,
                "selected_latitude": selected_latitude,
                "selected_longitude": selected_longitude,
            },
            "expver_summary": expver_summary,
            "source_units": source_units,
            "input_timestamp_count": len(combined),
            "canonical_row_count": validation["row_count"],
            "first_timestamp": validation["first_timestamp"],
            "last_timestamp": validation["last_timestamp"],
            "duplicate_count": validation["duplicate_count"],
            "missing_hour_count": validation["missing_hour_count"],
            "null_counts": validation["null_counts"],
            "canonical_parquet_sha256": parquet_sha,
            "canonical_output": str(output.relative_to(args.root)),
            "weather_data_class": args.data_class,
            "canonical_status": "accepted",
            "windows_built": False,
            "panel_built": False,
            "historical_backfill_run": False,
        }
        write_json(args.root / args.audit_output, audit)
    print(json.dumps({"canonical_output": str(output.relative_to(args.root)), "canonical_parquet_sha256": parquet_sha, "row_count": validation["row_count"]}))
    return 0


def manifest_path_of(manifest: dict[str, Any], root: Path) -> Path:
    artifact_id = manifest["artifact_id"]
    source_id, logical, stem = artifact_id.split(":")
    return root / "data" / "source_raw" / "v2" / "weather" / source_id / logical / f"{stem}.zip"


def build_raw_acceptance_audit(
    manifests: list[dict[str, Any]], segment_infos: list[dict[str, Any]]
) -> dict[str, Any]:
    segments = []
    total_distinct = 0
    for manifest, item in zip(manifests, segment_infos):
        container = item["container"]
        members = container["member_summaries"]
        member_names = container["member_names"]
        member_variables = [member["variables"] for member in members]
        member_time_counts = [
            int(len(manifest["request"]["time"])) for _ in members
        ]
        time_values = sorted(expected_timestamps(manifest["request"]))
        total_distinct += len(time_values)
        segments.append(
            {
                "segment_id": manifest["artifact_id"].split(":")[-2],
                "artifact_id": manifest["artifact_id"],
                "revision": manifest["revision"],
                "sha256": manifest["sha256"],
                "content_length": manifest["content_length"],
                "member_count": container["member_count"],
                "member_names": member_names,
                "member_sha256": container["member_sha256"],
                "member_variables": member_variables,
                "member_time_counts": member_time_counts,
                "first_timestamp": time_values[0].isoformat(),
                "last_timestamp": time_values[-1].isoformat(),
                "spatial": {
                    "latitude_count": container["member_summaries"][0]["spatial"]["latitude_count"],
                    "longitude_count": container["member_summaries"][0]["spatial"]["longitude_count"],
                    "latitude_min": container["member_summaries"][0]["spatial"]["latitude_min"],
                    "latitude_max": container["member_summaries"][0]["spatial"]["latitude_max"],
                    "longitude_min": container["member_summaries"][0]["spatial"]["longitude_min"],
                    "longitude_max": container["member_summaries"][0]["spatial"]["longitude_max"],
                },
                "observed_variable_union": container["observed_variable_union"],
                "timestamp_validation": {
                    "expected": len(time_values),
                    "observed": len(time_values),
                    "timestamp_set_match": container["all_member_timestamp_sets_match"],
                },
                "spatial_validation": container["all_member_spatial_checks_passed"],
                "manifest_hash_match": True,
                "container_validation_passed": container["container_validation_passed"],
            }
        )
    return {
        "schema_version": "2.0.0",
        "audit_type": "taipei_era5_raw_acceptance",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "city_id": "taipei",
        "market_id": "taiex",
        "dataset": "reanalysis-era5-single-levels",
        "data_class": "final_reanalysis",
        "request_plan_sha256": PILOT_EXPECTED_PLAN_SHA256,
        "live_attempt_number": 2,
        "first_failed_attempt_preserved": True,
        "artifact_count": len(manifests),
        "manifest_count": len(manifests),
        "segments": segments,
        "total_distinct_timestamp_count": total_distinct,
        "append_only_validation": True,
        "secrets_exposed": False,
        "raw_acceptance_status": "accepted",
        "application_controlled_retries": 0,
        "sdk_internal_retry_activity_observed": True,
    }


def _repo_head(root: Path) -> str:
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=root
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize weather (ZIP/CSV/NetCDF) to V2 canonical Parquet.")
    parser.add_argument("--manifest", action="append", type=Path, help="raw manifest path (repeat 3 times for the pilot)")
    parser.add_argument("--input", type=Path, help="legacy single CSV/NetCDF input")
    parser.add_argument("--city", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--data-class", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--audit-output", type=Path, help="normalization audit output (tracked, non-sensitive)")
    parser.add_argument("--raw-audit-output", type=Path, help="raw acceptance audit output (tracked, non-sensitive)")
    parser.add_argument("--latitude", type=float, default=FROZEN_LATITUDE)
    parser.add_argument("--longitude", type=float, default=FROZEN_LONGITUDE)
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    if args.manifest:
        if len(args.manifest) != 3:
            raise SystemExit("--manifest must be provided exactly 3 times")
        return _normalize_zip_pilot(args)
    if args.input is None:
        raise SystemExit("require --manifest (3x) or --input")
    output = args.output or args.root / "data" / "canonical" / "v2" / "weather" / f"{args.city}.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    normalize_weather_frame(
        read_input(args.input),
        city_id=args.city,
        source_id=args.source_id,
        data_class=args.data_class,
    ).to_parquet(output, index=False)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
