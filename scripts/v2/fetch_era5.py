"""ERA5/ERA5T single-city pilot request planner and gated live client.

Zero-network by default: without ``--live`` the CDS client is never imported
and nothing is written unless ``--audit-output`` is given. The live branch is
fully implemented and testable with an injected client factory, but it is
gated on credential structure, dataset-terms confirmation and a conservative
finality eligibility date.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import RawArtifactStore, V2Error, load_yaml, repo_root, write_json

DATASET = "reanalysis-era5-single-levels"
VARIABLES = [
    "2m_temperature",
    "2m_dewpoint_temperature",
    "total_precipitation",
    "total_cloud_cover",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "instantaneous_10m_wind_gust",
    "surface_solar_radiation_downwards",
]
# NetCDF short variable names produced by the ERA5 single-levels dataset.
NETCDF_VARIABLES = ["t2m", "d2m", "tp", "tcc", "u10", "v10", "i10fg", "ssrd"]
AREA_HALF_SPAN_DEGREES = 0.13
PILOT_DEFAULT_START = date(2026, 3, 2)
PILOT_DEFAULT_END = date(2026, 3, 6)
PILOT_TIMEZONE = "Asia/Taipei"
TERMS_CONFIRMATION_PATH = ".local/agreements/cds-era5-single-levels.json"

# Download container contract. Mixed GRIB stepType variables (instantaneous +
# accumulation) make CDS emit several NetCDF members; multiple members are
# returned inside a ZIP even when download_format=unarchived was requested.
# This project therefore treats ZIP as the formal transfer container and
# validates every NetCDF member before the raw ZIP is persisted.
DOWNLOAD_FORMAT = "zip"
EXPECTED_DOWNLOAD_CONTAINER = "zip"
CONTAINER_REASON = "mixed_grib_step_types_produce_multiple_netcdf_members"

# Magic-number detection (extension/Content-Disposition are never trusted).
ZIP_MAGIC = b"PK\x03\x04"
ZIP_EMPTY_MAGIC = b"PK\x05\x06"
NETCDF_CLASSIC_MAGICS = (b"CDF\x01", b"CDF\x02")
NETCDF4_MAGIC = b"\x89HDF\r\n\x1a\n"

# ZIP safety limits (conservative, sized for the small pilot area).
ZIP_MAX_MEMBER_COUNT = 16
ZIP_MAX_MEMBER_SIZE = 64 * 1024 * 1024  # 64 MiB per member
ZIP_MAX_TOTAL_UNCOMPRESSED = 128 * 1024 * 1024  # 128 MiB total
ZIP_MAX_COMPRESSION_RATIO = 1000.0  # uncompressed/compressed ceiling (bomb guard)
ARCHIVE_MEMBER_SUFFIXES = (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".grib", ".grb")


def load_semantics(root: Path) -> dict[str, dict[str, Any]]:
    registry = load_yaml(root / "config" / "v2" / "weather-variable-semantics.yaml")
    mapping: dict[str, dict[str, Any]] = {}
    for entry in registry.get("variables", []):
        mapping.setdefault(entry["source_variable"], entry)
    return mapping


def pilot_config(root: Path) -> dict[str, Any]:
    scope = load_yaml(root / "config" / "v2" / "pilot-scope.yaml")
    market_id = scope["primary_market"]
    city_id = scope["primary_city"]
    locations = {row["city_id"]: row for row in load_yaml(root / "config" / "v2" / "locations.yaml")["locations"]}
    markets = {row["market_id"]: row for row in load_yaml(root / "config" / "v2" / "markets.yaml")["markets"]}
    if city_id not in locations:
        raise SystemExit(f"primary_city {city_id} not registered in locations.yaml")
    if market_id not in markets:
        raise SystemExit(f"primary_market {market_id} not registered in markets.yaml")
    if markets[market_id]["city_id"] != city_id:
        raise SystemExit(f"market {market_id} not linked to city {city_id}")
    return {"market_id": market_id, "city_id": city_id, "location": locations[city_id], "market": markets[market_id]}


def resolve_city(root: Path, city_id: str) -> dict[str, Any]:
    locations = {row["city_id"]: row for row in load_yaml(root / "config" / "v2" / "locations.yaml")["locations"]}
    markets = {row["market_id"]: row for row in load_yaml(root / "config" / "v2" / "markets.yaml")["markets"]}
    if city_id not in locations:
        raise SystemExit(f"city {city_id} not registered in locations.yaml")
    location = locations[city_id]
    market_id = location["market_id"]
    if market_id not in markets:
        raise SystemExit(f"market {market_id} for city {city_id} not registered in markets.yaml")
    return {"market_id": market_id, "city_id": city_id, "location": location, "market": markets[market_id]}


def utc_request_plan(local_start: datetime, local_end_exclusive: datetime) -> dict[str, Any]:
    """Minimal UTC coverage from a local window, split into merged segments."""
    start_utc = local_start.astimezone(timezone.utc)
    end_inclusive_utc = local_end_exclusive.astimezone(timezone.utc)
    hours: list[datetime] = []
    current = start_utc.replace(minute=0, second=0, microsecond=0)
    if current < start_utc:
        current += timedelta(hours=1)
    while current <= end_inclusive_utc:
        hours.append(current)
        current += timedelta(hours=1)
    if not hours:
        raise ValueError("empty UTC request range")
    by_date: dict[date, list[int]] = {}
    for hour in hours:
        by_date.setdefault(hour.date(), []).append(hour.hour)
    merged: list[dict[str, Any]] = []
    for day, times in sorted(by_date.items()):
        time_list = [f"{h:02d}:00" for h in sorted(times)]
        if merged and merged[-1]["times"] == time_list:
            merged[-1]["utc_dates"].append(day.isoformat())
            merged[-1]["hour_count"] += len(time_list)
        else:
            merged.append({"utc_dates": [day.isoformat()], "times": time_list, "hour_count": len(time_list)})
    for index, segment in enumerate(merged, start=1):
        dates = segment["utc_dates"]
        label = dates[0] if len(dates) == 1 else f"{dates[0]}-to-{dates[-1]}"
        segment["segment_id"] = f"segment-{index:02d}-{label}"
    return {
        "utc_request_start": hours[0].isoformat(),
        "utc_request_end_inclusive": hours[-1].isoformat(),
        "request_segments": merged,
        "requested_utc_dates": [day.isoformat() for day in sorted(by_date)],
        "requested_hour_count": len(hours),
        "request_count": len(merged),
        "left_padding_reason": "convert_first_local_midnight_to_utc",
        "right_padding_reason": "include_final_full_day_accumulation_endpoint",
    }


def build_area(location: dict[str, Any]) -> dict[str, Any]:
    latitude = float(location["latitude"])
    longitude = float(location["longitude"])
    span = AREA_HALF_SPAN_DEGREES
    return {
        "center_latitude": latitude,
        "center_longitude": longitude,
        "area_north": round(latitude + span, 6),
        "area_west": round(longitude - span, 6),
        "area_south": round(latitude - span, 6),
        "area_east": round(longitude + span, 6),
        "area_half_span_degrees": span,
        "area": [round(latitude + span, 6), round(longitude - span, 6), round(latitude - span, 6), round(longitude + span, 6)],
    }


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    return date(total // 12, total % 12 + 1, value.day)


def finality_check(period_start: date, period_end: date, final: bool, executed_at: date) -> dict[str, Any]:
    """Conservative finality gate: target month + 3 full months + 1 day."""
    target_month_first = date(period_end.year, period_end.month, 1)
    first_of_next_month = _add_months(target_month_first, 1)
    final_eligibility_date = _add_months(first_of_next_month, 3)
    eligible = executed_at >= final_eligibility_date
    if final and eligible:
        expected_data_class = "final_reanalysis"
        finality_status = "expected_final_by_official_latency"
    elif final and not eligible:
        expected_data_class = "provisional_reanalysis"
        finality_status = "not_yet_eligible_for_final"
    else:
        expected_data_class = "provisional_reanalysis"
        finality_status = "provisional_by_explicit_flag"
    return {
        "target_period_start": period_start.isoformat(),
        "target_period_end": period_end.isoformat(),
        "retrieval_planned_at": executed_at.isoformat(),
        "official_latency_policy": (
            "ERA5T near-real-time provisional data is later replaced by the final ERA5 "
            "release for the corresponding month; full finalization typically lags by "
            "about two to three months."
        ),
        "final_eligibility_date": final_eligibility_date.isoformat(),
        "finality_status": finality_status,
        "expected_data_class": expected_data_class,
    }


def _terms_confirmation(root: Path) -> dict[str, Any]:
    """Read a local-only, non-sensitive terms confirmation (if present)."""
    path = root / TERMS_CONFIRMATION_PATH
    if not path.exists():
        return {
            "confirmation_file_present": False,
            "dataset_terms_status": "acceptance_unverified",
        }
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {
            "confirmation_file_present": True,
            "dataset_terms_status": "acceptance_unverified",
        }
    valid = (
        isinstance(parsed, dict)
        and parsed.get("dataset") == DATASET
        and parsed.get("accepted_in_browser") is True
        and parsed.get("confirmation_source") == "explicit_user_confirmation"
    )
    return {
        "confirmation_file_present": True,
        "dataset_terms_status": "user_confirmed_outside_task" if valid else "acceptance_unverified",
    }


def cds_readiness(root: Path, home: Path | None = None) -> dict[str, Any]:
    """Read-only local checks; never retains or returns credential values."""
    spec = importlib.util.find_spec("cdsapi")
    cdsapi_installed = spec is not None
    cdsapi_version: str | None = None
    if cdsapi_installed:
        try:
            from importlib import metadata

            cdsapi_version = metadata.version("cdsapi")
        except Exception:
            cdsapi_version = None
    cdsapirc_path = (home or Path.home()) / ".cdsapirc"
    if not cdsapirc_path.exists():
        cdsapirc_status = "missing"
        url_field_present = False
        key_field_present = False
    elif not os.access(cdsapirc_path, os.R_OK):
        cdsapirc_status = "unreadable"
        url_field_present = False
        key_field_present = False
    else:
        try:
            parsed = load_yaml(cdsapirc_path)
        except Exception:
            cdsapirc_status = "invalid_shape"
            url_field_present = False
            key_field_present = False
        else:
            if isinstance(parsed, dict):
                url_field_present = "url" in parsed
                key_field_present = "key" in parsed
                cdsapirc_status = "present_shape_valid" if (url_field_present and key_field_present) else "invalid_shape"
            else:
                cdsapirc_status = "invalid_shape"
                url_field_present = False
                key_field_present = False
    terms = _terms_confirmation(root)
    if not cdsapi_installed:
        readiness = "cdsapi_missing"
    elif cdsapirc_status == "missing":
        readiness = "credential_file_missing"
    elif cdsapirc_status in {"unreadable", "invalid_shape"}:
        readiness = "credential_file_invalid"
    elif terms["dataset_terms_status"] != "user_confirmed_outside_task":
        readiness = "dataset_terms_acceptance_unverified"
    else:
        readiness = "ready_for_future_live_request"
    return {
        "cdsapi_installed": cdsapi_installed,
        "cdsapi_version": cdsapi_version,
        "cdsapirc_status": cdsapirc_status,
        "url_field_present": url_field_present,
        "key_field_present": key_field_present,
        "dataset_terms_status": terms["dataset_terms_status"],
        "terms_confirmation_file_present": terms["confirmation_file_present"],
        "credential_readiness_status": readiness,
    }


def expected_timestamps(request: dict[str, Any]) -> set[datetime]:
    """Expected UTC timestamps from the date x time cartesian product."""
    expected: set[datetime] = set()
    for day in request.get("date", []):
        for hour in request.get("time", []):
            hour_int = int(str(hour).split(":")[0])
            expected.add(datetime.fromisoformat(f"{day}T{hour_int:02d}:00:00+00:00"))
    return expected


def _limited_iso(values: list[datetime], limit: int = 20) -> dict[str, Any]:
    return {"count": len(values), "shown": [value.isoformat() for value in values[:limit]]}


def _timestamp_validation(dataset: Any, request: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Exact UTC timestamp-set checks shared by single-file and member paths."""
    times = dataset.get("valid_time")
    if times is None:
        times = dataset.get("time")
    if times is None or int(times.size) == 0:
        raise V2Error("netcdf has no time dimension values")
    from pandas import to_datetime

    raw_values = list(to_datetime(times.values))
    normalized_values = []
    non_hourly: list[datetime] = []
    for value in raw_values:
        if value.tzinfo is None:
            utc_value = value.tz_localize("UTC")
        else:
            utc_value = value.tz_convert("UTC")
        if (utc_value.minute, utc_value.second, utc_value.microsecond) != (0, 0, 0):
            non_hourly.append(utc_value)
        normalized_values.append(utc_value.replace(minute=0, second=0, microsecond=0))
    normalized_set = set(normalized_values)
    duplicates = len(normalized_values) - len(normalized_set)
    expected = expected_timestamps(request)
    missing_timestamps = sorted(expected - normalized_set)
    unexpected_timestamps = sorted(normalized_set - expected)
    observed_count = len(normalized_set)
    expected_count = len(expected)
    timestamp_set_match = (
        not missing_timestamps
        and not unexpected_timestamps
        and duplicates == 0
        and not non_hourly
        and observed_count == expected_count
    )
    summary = {
        "expected_timestamp_count": expected_count,
        "observed_timestamp_count": observed_count,
        "duplicate_timestamps": duplicates,
        "non_hourly_timestamps": len(non_hourly),
        "missing_timestamps": _limited_iso(missing_timestamps),
        "unexpected_timestamps": _limited_iso(unexpected_timestamps),
        "timestamp_set_match": timestamp_set_match,
    }
    problems: list[str] = []
    if missing_timestamps:
        problems.append(f"missing {len(missing_timestamps)} expected timestamps (showing up to 20: {[v.isoformat() for v in missing_timestamps[:20]]})")
    if unexpected_timestamps:
        problems.append(f"unexpected {len(unexpected_timestamps)} timestamps (showing up to 20: {[v.isoformat() for v in unexpected_timestamps[:20]]})")
    if duplicates:
        problems.append(f"duplicate timestamps: {duplicates}")
    if non_hourly:
        problems.append(f"non-hourly timestamps: {len(non_hourly)}")
    if observed_count != expected_count:
        problems.append(f"observed count {observed_count} != expected count {expected_count}")
    return summary, problems


def _canonical_grid_hash(values: Any) -> str:
    """Stable hash of the sorted, deduplicated grid values (no array output)."""
    import hashlib

    canonical = sorted({round(float(value), 6) for value in values})
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode("utf-8")).hexdigest()


def _spatial_summary(dataset: Any, area: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Record spatial coordinate names/counts/bounds and check them against area."""
    problems: list[str] = []
    latitude = dataset.get("latitude")
    longitude = dataset.get("longitude")
    if latitude is None or longitude is None:
        raise V2Error("netcdf has no latitude/longitude coordinate")
    from pandas import to_numeric
    import numpy as np

    lat_values = np.asarray(to_numeric(list(latitude.values)), dtype=float)
    lon_values = np.asarray(to_numeric(list(longitude.values)), dtype=float)
    summary = {
        "latitude_coordinate_name": latitude.name,
        "longitude_coordinate_name": longitude.name,
        "latitude_count": int(latitude.size),
        "longitude_count": int(longitude.size),
        "latitude_min": round(float(lat_values.min()), 6),
        "latitude_max": round(float(lat_values.max()), 6),
        "longitude_min": round(float(lon_values.min()), 6),
        "longitude_max": round(float(lon_values.max()), 6),
        "latitude_values_sha256": _canonical_grid_hash(lat_values),
        "longitude_values_sha256": _canonical_grid_hash(lon_values),
    }
    if area is not None:
        north, west, south, east = area["area"]
        if float(lat_values.min()) < float(south) - 1e-6 or float(lat_values.max()) > float(north) + 1e-6:
            problems.append(
                f"latitude outside requested area ({float(lat_values.min())}..{float(lat_values.max())} vs {south}..{north})"
            )
        if float(lon_values.min()) < float(west) - 1e-6 or float(lon_values.max()) > float(east) + 1e-6:
            problems.append(
                f"longitude outside requested area ({float(lon_values.min())}..{float(lon_values.max())} vs {west}..{east})"
            )
    return summary, problems


def validate_netcdf(path: Path, request: dict[str, Any]) -> dict[str, Any]:
    """Exact UTC timestamp-set validation for a single full-variable NetCDF.

    Compatibility path: requires all eight requested variables in one file
    (legacy behaviour, used for direct NetCDF responses). Returns a
    machine-readable summary; any failure raises V2Error (bounded listing, no
    local absolute paths) so nothing is persisted.
    """
    if not path.exists():
        raise V2Error("staged netcdf file missing")
    if path.stat().st_size == 0:
        raise V2Error("staged netcdf file empty")
    try:
        import xarray as xr
    except ImportError as exc:
        raise V2Error("xarray required to validate staged netcdf") from exc
    try:
        with xr.open_dataset(path) as dataset:
            data_vars = set(dataset.data_vars)
            variables_present = all(name in data_vars for name in NETCDF_VARIABLES)
            missing_variables = [name for name in NETCDF_VARIABLES if name not in data_vars]
            timestamp_summary, timestamp_problems = _timestamp_validation(dataset, request)
            netcdf_validation_passed = timestamp_summary["timestamp_set_match"] and variables_present
            summary = {
                **timestamp_summary,
                "variables_present": variables_present,
                "netcdf_validation_passed": netcdf_validation_passed,
            }
            problems = list(timestamp_problems)
            if not variables_present:
                problems.append(f"missing requested variables: {sorted(missing_variables)}")
            if problems:
                raise V2Error("netcdf timestamp validation failed: " + "; ".join(problems))
            return summary
    except V2Error:
        raise
    except Exception as exc:
        raise V2Error(f"staged netcdf cannot be opened as NetCDF: {type(exc).__name__}") from exc


def validate_netcdf_member(path: Path, request: dict[str, Any], area: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate one NetCDF member of a ZIP container.

    Members may carry a subset of the requested variables. The exact UTC
    timestamp set must still match the request date x time plan, and the
    spatial grid must lie inside the requested area.
    """
    if not path.exists():
        raise V2Error("zip member netcdf file missing")
    if path.stat().st_size == 0:
        raise V2Error("zip member netcdf file empty")
    try:
        import xarray as xr
    except ImportError as exc:
        raise V2Error("xarray required to validate zip member netcdf") from exc
    try:
        with xr.open_dataset(path) as dataset:
            data_vars = sorted(set(dataset.data_vars))
            timestamp_summary, timestamp_problems = _timestamp_validation(dataset, request)
            spatial_summary, spatial_problems = _spatial_summary(dataset, area)
            member_validation_passed = timestamp_summary["timestamp_set_match"] and not spatial_problems
            summary = {
                **timestamp_summary,
                "variables": data_vars,
                "spatial": spatial_summary,
                "member_validation_passed": member_validation_passed,
            }
            problems = list(timestamp_problems) + list(spatial_problems)
            if problems:
                raise V2Error("zip member netcdf validation failed: " + "; ".join(problems))
            return summary
    except V2Error:
        raise
    except Exception as exc:
        raise V2Error(f"zip member netcdf cannot be opened as NetCDF: {type(exc).__name__}") from exc


def inspect_download_container(path: Path) -> str:
    """Detect container type from magic bytes; never trust file extension."""
    with open(path, "rb") as handle:
        head = handle.read(8)
    if head.startswith(ZIP_MAGIC) or head.startswith(ZIP_EMPTY_MAGIC):
        return "zip"
    if head.startswith(NETCDF_CLASSIC_MAGICS) or head.startswith(NETCDF4_MAGIC):
        return "netcdf"
    raise V2Error("unrecognized download container (magic bytes are neither ZIP nor NetCDF)")


def validate_zip_container(
    zip_path: Path, request: dict[str, Any], area: dict[str, Any] | None = None, workdir: Path | None = None
) -> dict[str, Any]:
    """Safety-check a ZIP and validate every NetCDF member inside a temp dir."""
    import hashlib
    import zipfile

    if not zip_path.exists():
        raise V2Error("staged zip container missing")
    if zip_path.stat().st_size == 0:
        raise V2Error("staged zip container empty")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            if not infos:
                raise V2Error("zip container has no members")
            if len(infos) > ZIP_MAX_MEMBER_COUNT:
                raise V2Error(f"zip member count {len(infos)} exceeds limit {ZIP_MAX_MEMBER_COUNT}")

            names = [info.filename for info in infos]
            if len(set(names)) != len(names):
                raise V2Error("duplicate zip member names")
            lowered = [name.lower() for name in names]
            if len(set(lowered)) != len(lowered):
                raise V2Error("case-insensitive duplicate zip member names")

            import posixpath
            import re
            import stat as stat_module

            drive_prefix = re.compile(r"^[A-Za-z]:[\\/]")
            total_uncompressed = 0
            for info in infos:
                name = info.filename
                if info.flag_bits & 0x1:
                    raise V2Error("encrypted zip member not allowed")
                if name.startswith("/") or drive_prefix.match(name) or posixpath.isabs(name):
                    raise V2Error("absolute or drive-absolute zip member path not allowed")
                parts = name.split("/")
                if ".." in parts or posixpath.normpath(name).startswith(".."):
                    raise V2Error("zip member path traversal not allowed")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat_module.S_ISLNK(mode):
                    raise V2Error("symbolic-link zip member not allowed")
                if name.lower().endswith(ARCHIVE_MEMBER_SUFFIXES):
                    raise V2Error(f"nested archive member not allowed: {name}")
                if info.file_size > ZIP_MAX_MEMBER_SIZE:
                    raise V2Error(f"zip member {name} exceeds per-member size limit")
                if info.compress_size > 0 and info.file_size / info.compress_size > ZIP_MAX_COMPRESSION_RATIO:
                    raise V2Error(f"zip member {name} compression ratio exceeds limit (suspected zip bomb)")
                total_uncompressed += info.file_size
                if total_uncompressed > ZIP_MAX_TOTAL_UNCOMPRESSED:
                    raise V2Error("zip total uncompressed size exceeds limit (suspected zip bomb)")

            cleanup = workdir is None
            if workdir is None:
                workdir = Path(tempfile.mkdtemp(prefix="weather-stock-v2-zip-"))
            try:
                member_paths: list[tuple[str, Path]] = []
                for info in infos:
                    target = (workdir / info.filename).resolve()
                    if not target.is_relative_to(workdir.resolve()):
                        raise V2Error("zip member escapes extraction directory")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, open(target, "wb") as dest:
                        dest.write(source.read())
                    member_paths.append((info.filename, target))

                member_summaries: list[dict[str, Any]] = []
                observed_variable_union: set[str] = set()
                for name, member_path in member_paths:
                    summary = validate_netcdf_member(member_path, request, area)
                    member_summaries.append(
                        {
                            "member_name": name,
                            "member_sha256": hashlib.sha256(member_path.read_bytes()).hexdigest(),
                            "compressed_size": next(info.file_size for info in infos if info.filename == name),
                            "uncompressed_size": member_path.stat().st_size,
                            "variables": summary["variables"],
                            "timestamp_set_match": summary["timestamp_set_match"],
                            "spatial": summary["spatial"],
                            "member_validation_passed": summary["member_validation_passed"],
                        }
                    )
                    observed_variable_union.update(summary["variables"])

                all_timestamp_match = all(item["timestamp_set_match"] for item in member_summaries)
                all_spatial_passed = all(
                    item["spatial"]["latitude_count"] > 0 and item["spatial"]["longitude_count"] > 0 and item["member_validation_passed"]
                    for item in member_summaries
                )
                lat_hashes = {item["spatial"]["latitude_values_sha256"] for item in member_summaries}
                lon_hashes = {item["spatial"]["longitude_values_sha256"] for item in member_summaries}
                if len(lat_hashes) != 1 or len(lon_hashes) != 1:
                    raise V2Error("spatial grid mismatch across zip members")

                requested = set(NETCDF_VARIABLES)
                missing_variables = sorted(requested - observed_variable_union)
                duplicate_variables = sorted(name for name in observed_variable_union if sum(name in s["variables"] for s in member_summaries) > 1)
                all_requested_present = not missing_variables
                container_validation_passed = (
                    all_timestamp_match and all_spatial_passed and all_requested_present and not duplicate_variables
                )
                if duplicate_variables:
                    raise V2Error(f"requested variable appears in multiple zip members: {duplicate_variables}")
                if missing_variables:
                    raise V2Error(f"zip members miss requested variables: {missing_variables}")
                if not container_validation_passed:
                    raise V2Error("zip container validation failed")
                return {
                    "container_type": "zip",
                    "member_count": len(member_paths),
                    "member_names": [name for name, _ in member_paths],
                    "member_sha256": [summary["member_sha256"] for summary in member_summaries],
                    "member_compressed_sizes": [summary["compressed_size"] for summary in member_summaries],
                    "member_uncompressed_sizes": [summary["uncompressed_size"] for summary in member_summaries],
                    "member_summaries": member_summaries,
                    "observed_variable_union": sorted(observed_variable_union),
                    "missing_variables": missing_variables,
                    "duplicate_variables_across_members": duplicate_variables,
                    "all_requested_variables_present": all_requested_present,
                    "all_member_timestamp_sets_match": all_timestamp_match,
                    "all_member_spatial_checks_passed": all_spatial_passed,
                    "container_validation_passed": container_validation_passed,
                    "raw_suffix": ".zip",
                }
            finally:
                if cleanup:
                    shutil.rmtree(workdir, ignore_errors=True)
    except V2Error:
        raise
    except zipfile.BadZipFile as exc:
        raise V2Error(f"corrupt zip container: {type(exc).__name__}") from exc


def validate_download_container(
    path: Path, request: dict[str, Any], area: dict[str, Any] | None = None, workdir: Path | None = None
) -> dict[str, Any]:
    """Unified entry: validate a ZIP (all NetCDF members) or a direct NetCDF.

    Only ``container_validation_passed = true`` allows the raw bytes to enter
    the append-only RawArtifactStore.
    """
    import hashlib

    if not path.exists():
        raise V2Error("staged download container missing")
    if path.stat().st_size == 0:
        raise V2Error("staged download container empty")
    container_type = inspect_download_container(path)
    if container_type == "zip":
        result = validate_zip_container(path, request, area, workdir=workdir)
    else:
        member = validate_netcdf(path, request)
        result = {
            "container_type": "netcdf",
            "member_count": 1,
            "member_names": [path.name],
            "member_sha256": [hashlib.sha256(path.read_bytes()).hexdigest()],
            "member_compressed_sizes": [path.stat().st_size],
            "member_uncompressed_sizes": [path.stat().st_size],
            "member_summaries": [{"member_name": path.name, "member_validation_passed": member["netcdf_validation_passed"]}],
            "observed_variable_union": sorted(NETCDF_VARIABLES),
            "missing_variables": [],
            "duplicate_variables_across_members": [],
            "all_requested_variables_present": True,
            "all_member_timestamp_sets_match": member["timestamp_set_match"],
            "all_member_spatial_checks_passed": False,
            "container_validation_passed": member["netcdf_validation_passed"],
            "raw_suffix": ".nc",
        }
    result["container_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    result["container_size_bytes"] = path.stat().st_size
    result["zip_safety_passed"] = result["container_validation_passed"]
    return result


def main(
    argv: list[str] | None = None,
    client_factory: Callable[[], Any] | None = None,
    readiness: dict[str, Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="ERA5/ERA5T Taipei pilot request planner (zero network unless --live)")
    parser.add_argument("--city", type=str, help="city_id registered in locations.yaml")
    parser.add_argument("--pilot-only", action="store_true", help="use primary_city from pilot-scope.yaml")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--final", action="store_true", help="target final ERA5 data class (subject to finality gate)")
    parser.add_argument("--dry-run", action="store_true", help="explicit dry-run (default behaviour is already offline)")
    parser.add_argument("--live", action="store_true", help="perform the actual CDS retrieval (gated)")
    parser.add_argument("--audit-output", type=Path, help="write the dry-run audit to this path (offline only)")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root
    if args.pilot_only:
        resolved = pilot_config(root)
    elif args.city:
        resolved = resolve_city(root, args.city)
    else:
        parser.error("require --pilot-only or --city")
    location = resolved["location"]
    market = resolved["market"]
    timezone_name = location["timezone"]
    tz = ZoneInfo(timezone_name)
    start_date = args.start_date or (PILOT_DEFAULT_START if args.pilot_only else None)
    end_date = args.end_date or (PILOT_DEFAULT_END if args.pilot_only else None)
    if start_date is None or end_date is None:
        parser.error("--start-date and --end-date are required outside --pilot-only")
    local_start = datetime.combine(start_date, time.min, tz)
    local_end_exclusive = datetime.combine(end_date + timedelta(days=1), time.min, tz)
    plan = utc_request_plan(local_start, local_end_exclusive)
    plan_hash = json.dumps(
        [
            {
                "segment_id": s["segment_id"],
                "utc_dates": s["utc_dates"],
                "times": s["times"],
                "download_format": DOWNLOAD_FORMAT,
            }
            for s in plan["request_segments"]
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    import hashlib

    plan_sha256 = hashlib.sha256(plan_hash.encode("utf-8")).hexdigest()
    area = build_area(location)
    semantics = load_semantics(root)
    executed_at = datetime.now(timezone.utc).date()
    finality = finality_check(start_date, end_date, args.final, executed_at)
    if readiness is None:
        readiness = cds_readiness(root)
    coordinate_evidence = {
        "coordinate_basis": location.get("coordinate_basis"),
        "coordinate_derivation": location.get("coordinate_derivation"),
        "coordinate_source": location.get("coordinate_source"),
        "coordinate_evidence_status": location.get("coordinate_evidence_status"),
        "coordinate_verified_at": str(location["coordinate_verified_at"]) if location.get("coordinate_verified_at") else None,
    }

    def build_audit() -> dict[str, Any]:
        return {
            "schema_version": "2.0.0",
            "audit_type": "taipei_era5_dry_run" if not args.live else "taipei_era5_live",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "city_id": resolved["city_id"],
            "market_id": resolved["market_id"],
            "coordinate": {"latitude": location["latitude"], "longitude": location["longitude"]},
            "coordinate_evidence": coordinate_evidence,
            "timezone": timezone_name,
            "market_session": market["session"],
            "pilot_trading_dates": [d.isoformat() for d in (start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1))],
            "local_window_start": local_start.isoformat(),
            "local_window_end_exclusive": local_end_exclusive.isoformat(),
            "utc_request_start": plan["utc_request_start"],
            "utc_request_end_inclusive": plan["utc_request_end_inclusive"],
            "request_segments": plan["request_segments"],
            "requested_utc_dates": plan["requested_utc_dates"],
            "requested_hour_count": plan["requested_hour_count"],
            "request_count": plan["request_count"],
            "left_padding_reason": plan["left_padding_reason"],
            "right_padding_reason": plan["right_padding_reason"],
            "request_plan_sha256": plan_sha256,
            "variables": VARIABLES,
            "variable_temporal_semantics": {
                name: {
                    "temporal_support_type": entry.get("temporal_support_type"),
                    "interval_duration_hours": entry.get("interval_duration_hours"),
                    "window_aggregation": entry.get("window_aggregation"),
                    "timestamp_represents": entry.get("timestamp_represents"),
                }
                for name, entry in semantics.items()
            },
            "area": area,
            "dataset": DATASET,
            "product_type": ["reanalysis"],
            "data_format": "netcdf",
            "download_format": DOWNLOAD_FORMAT,
            "expected_download_container": EXPECTED_DOWNLOAD_CONTAINER,
            "container_reason": CONTAINER_REASON,
            "target_period_start": finality["target_period_start"],
            "target_period_end": finality["target_period_end"],
            "retrieval_planned_at": finality["retrieval_planned_at"],
            "official_latency_policy": finality["official_latency_policy"],
            "final_eligibility_date": finality["final_eligibility_date"],
            "expected_data_class": finality["expected_data_class"],
            "finality_status": finality["finality_status"],
            "cdsapi_installed": readiness["cdsapi_installed"],
            "cdsapi_version": readiness["cdsapi_version"],
            "cdsapirc_status": readiness["cdsapirc_status"],
            "url_field_present": readiness["url_field_present"],
            "key_field_present": readiness["key_field_present"],
            "dataset_terms_status": readiness["dataset_terms_status"],
            "credential_readiness_status": readiness["credential_readiness_status"],
            "live_requests_run": False,
            "weather_data_downloaded": False,
            "historical_backfill_run": False,
        }

    if not args.live:
        audit = build_audit()
        if args.audit_output is not None:
            write_json(root / args.audit_output, audit)
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return 0

    # Live branch (gated; executes only with a real or injected client).
    if args.final and finality["finality_status"] != "expected_final_by_official_latency":
        raise SystemExit("finality gate not met for final ERA5; refusing live request")
    if not readiness["cdsapi_installed"] and client_factory is None:
        raise SystemExit("cdsapi not installed; refusing live request")
    if readiness["cdsapirc_status"] != "present_shape_valid":
        raise SystemExit(f"cdsapirc not usable ({readiness['cdsapirc_status']}); refusing live request")
    if readiness["dataset_terms_status"] != "user_confirmed_outside_task":
        raise SystemExit("dataset terms acceptance unverified; refusing live request")
    if client_factory is None:
        import cdsapi

        client = cdsapi.Client()
    else:
        client = client_factory()
    data_class_label = "final" if finality["expected_data_class"] == "final_reanalysis" else "provisional"
    store = RawArtifactStore(root / "data" / "source_raw" / "v2" / "weather")
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="weather-stock-v2-era5-") as tmp:
        for segment in plan["request_segments"]:
            request: dict[str, Any] = {
                "product_type": ["reanalysis"],
                "variable": VARIABLES,
                "date": segment["utc_dates"],
                "time": segment["times"],
                "data_format": "netcdf",
                "download_format": DOWNLOAD_FORMAT,
                "area": area["area"],
            }
            target = Path(tmp) / f"{segment['segment_id']}.download"
            client.retrieve(DATASET, request, str(target))
            validation = validate_download_container(target, request, area)
            if not validation["container_validation_passed"]:
                raise V2Error(f"segment {segment['segment_id']} failed download container validation")
            expected_count = len(expected_timestamps(request))
            logical_name = f"{resolved['city_id']}-{data_class_label}-{segment['segment_id']}-{plan_sha256[:8]}"
            result = store.persist(
                source_id="cds_era5_hourly" if finality["expected_data_class"] == "final_reanalysis" else "cds_era5t_hourly",
                provider="ECMWF Copernicus Climate Change Service",
                logical_name=logical_name,
                payload=target.read_bytes(),
                request=request,
                status="final" if finality["expected_data_class"] == "final_reanalysis" else "provisional",
                licence="CC-BY-4.0 catalogue terms and attribution",
                suffix=validation["raw_suffix"],
                validation_metadata={
                    "container_type": validation["container_type"],
                    "container_sha256": validation["container_sha256"],
                    "container_size_bytes": validation["container_size_bytes"],
                    "member_count": validation["member_count"],
                    "member_names": validation["member_names"],
                    "member_sha256": validation["member_sha256"],
                    "observed_variable_union": validation["observed_variable_union"],
                    "container_validation_passed": validation["container_validation_passed"],
                },
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            results.append(
                {
                    "segment_id": segment["segment_id"],
                    "artifact_id": manifest["artifact_id"],
                    "revision": result.revision,
                    "sha256": manifest["sha256"],
                    "skipped_as_identical": result.skipped_identical,
                    "manifest_path": str(result.manifest_path.relative_to(root)),
                    "timestamp_validation": {
                        "expected_timestamp_count": expected_count,
                        "observed_timestamp_count": expected_count if validation["container_validation_passed"] else 0,
                        "timestamp_set_match": validation["all_member_timestamp_sets_match"],
                        "netcdf_validation_passed": validation["container_validation_passed"],
                    },
                    "container_validation": {
                        "container_type": validation["container_type"],
                        "member_count": validation["member_count"],
                        "container_validation_passed": validation["container_validation_passed"],
                    },
                }
            )
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
