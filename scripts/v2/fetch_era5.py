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


def validate_netcdf(path: Path, request: dict[str, Any]) -> None:
    """Validate a staged NetCDF file before it enters the raw store."""
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
            missing = [name for name in NETCDF_VARIABLES if name not in set(dataset.data_vars)]
            if missing:
                raise V2Error(f"netcdf missing requested variables: {sorted(missing)}")
            times = dataset.get("valid_time")
            if times is None:
                times = dataset.get("time")
            if times is None or int(times.size) == 0:
                raise V2Error("netcdf has no time dimension values")
            requested_dates = set(request.get("date", []))
            from pandas import to_datetime

            observed_dates = {str(value)[:10] for value in to_datetime(times.values)}
            if not observed_dates.issubset(requested_dates):
                raise V2Error("netcdf timestamps exceed the requested plan")
    except V2Error:
        raise
    except Exception as exc:
        raise V2Error(f"staged netcdf cannot be opened as NetCDF: {type(exc).__name__}") from exc


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
            {"segment_id": s["segment_id"], "utc_dates": s["utc_dates"], "times": s["times"]}
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
            "download_format": "unarchived",
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
                "download_format": "unarchived",
                "area": area["area"],
            }
            target = Path(tmp) / f"{segment['segment_id']}.nc"
            client.retrieve(DATASET, request, str(target))
            validate_netcdf(target, request)
            logical_name = f"{resolved['city_id']}-{data_class_label}-{segment['segment_id']}-{plan_sha256[:8]}"
            result = store.persist(
                source_id="cds_era5_hourly" if finality["expected_data_class"] == "final_reanalysis" else "cds_era5t_hourly",
                provider="ECMWF Copernicus Climate Change Service",
                logical_name=logical_name,
                payload=target.read_bytes(),
                request=request,
                status="final" if finality["expected_data_class"] == "final_reanalysis" else "provisional",
                licence="CC-BY-4.0 catalogue terms and attribution",
                suffix=".nc",
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
                }
            )
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
