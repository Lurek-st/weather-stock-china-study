"""ERA5/ERA5T single-city pilot request planner (zero-network by default).

Without ``--live`` this module never imports the CDS client, never touches the
network, and only emits a request plan plus a readiness audit. ``--pilot-only``
resolves the primary city/market from ``pilot-scope.yaml``; ``--city`` validates
the location/market chain. UTC coverage is computed from the local window and
the variable time semantics (instantaneous vs interval accumulation).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import load_yaml, repo_root, write_json

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
AREA_HALF_SPAN_DEGREES = 0.13
PILOT_DEFAULT_START = date(2026, 3, 2)
PILOT_DEFAULT_END = date(2026, 3, 6)
PILOT_TIMEZONE = "Asia/Taipei"


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
    """Minimal UTC coverage from a local window, split into date segments."""
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
    segments = merged
    return {
        "utc_request_start": hours[0].isoformat(),
        "utc_request_end_inclusive": hours[-1].isoformat(),
        "request_segments": segments,
        "requested_utc_dates": [day.isoformat() for day in sorted(by_date)],
        "requested_hour_count": len(hours),
        "request_count": len(segments),
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


def finality_check(period_start: date, period_end: date, final: bool, executed_at: date) -> dict[str, Any]:
    return {
        "target_period_start": period_start.isoformat(),
        "target_period_end": period_end.isoformat(),
        "retrieval_planned_at": executed_at.isoformat(),
        "official_latency_policy": (
            "ERA5T near-real-time provisional data is later replaced by the final ERA5 "
            "release for the corresponding month; full finalization typically lags by "
            "about two to three months."
        ),
        "expected_data_class": "final_reanalysis" if final else "provisional_reanalysis",
        "finality_status": "expected_final_by_official_latency" if final else "provisional_by_explicit_flag",
    }


def cds_readiness(root: Path, home: Path | None = None) -> dict[str, Any]:
    """Read-only local checks; never reads key values, never contacts the API."""
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
    elif not os.access(cdsapirc_path, os.R_OK):
        cdsapirc_status = "unreadable"
    else:
        try:
            parsed = load_yaml(cdsapirc_path)
        except Exception:
            cdsapirc_status = "invalid_shape"
        else:
            if isinstance(parsed, dict) and "url" in parsed and "key" in parsed:
                cdsapirc_status = "present_shape_valid"
            else:
                cdsapirc_status = "invalid_shape"
    # Dataset terms acceptance is only ever recorded from a non-sensitive,
    # auditable user confirmation stored in this repository; none exists yet.
    dataset_terms_status = "acceptance_unverified"
    if not cdsapi_installed:
        readiness = "cdsapi_missing"
    elif cdsapirc_status == "missing":
        readiness = "credential_file_missing"
    elif cdsapirc_status in {"unreadable", "invalid_shape"}:
        readiness = "credential_file_invalid"
    elif dataset_terms_status != "user_confirmed_outside_task":
        readiness = "dataset_terms_acceptance_unverified"
    else:
        readiness = "ready_for_future_live_request"
    return {
        "cdsapi_installed": cdsapi_installed,
        "cdsapi_version": cdsapi_version,
        "cdsapirc_status": cdsapirc_status,
        "cdsapirc_fields_present": cdsapirc_status == "present_shape_valid",
        "dataset_terms_status": dataset_terms_status,
        "credential_readiness_status": readiness,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERA5/ERA5T Taipei pilot request planner (zero network unless --live)")
    parser.add_argument("--city", type=str, help="city_id registered in locations.yaml")
    parser.add_argument("--pilot-only", action="store_true", help="use primary_city from pilot-scope.yaml")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--final", action="store_true", help="target final ERA5 data class")
    parser.add_argument("--dry-run", action="store_true", help="explicit dry-run (default behaviour is already offline)")
    parser.add_argument("--live", action="store_true", help="perform the actual CDS retrieval (not run this round)")
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
    area = build_area(location)
    semantics = load_semantics(root)
    finality = finality_check(start_date, end_date, args.final, datetime.now(timezone.utc).date())
    readiness = cds_readiness(root)

    def build_audit() -> dict[str, Any]:
        return {
            "schema_version": "2.0.0",
            "audit_type": "taipei_era5_dry_run" if not args.live else "taipei_era5_live",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "city_id": resolved["city_id"],
            "market_id": resolved["market_id"],
            "coordinate": {"latitude": location["latitude"], "longitude": location["longitude"]},
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
            "expected_data_class": finality["expected_data_class"],
            "finality_status": finality["finality_status"],
            "cdsapi_installed": readiness["cdsapi_installed"],
            "cdsapi_version": readiness["cdsapi_version"],
            "cdsapirc_status": readiness["cdsapirc_status"],
            "dataset_terms_status": readiness["dataset_terms_status"],
            "credential_readiness_status": readiness["credential_readiness_status"],
            "live_requests_run": False,
            "weather_data_downloaded": False,
            "historical_backfill_run": False,
        }

    if not args.live:
        audit = build_audit()
        if resolved["city_id"] == "taipei":
            write_json(root / "data/audits/v2/weather-pilot/taipei-era5-dry-run.json", audit)
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return 0

    # Live branch: implemented but intentionally not executed this round.
    if not (args.live and (args.pilot_only or args.city)):
        parser.error("live mode requires --pilot-only or --city")
    if not readiness["cdsapi_installed"]:
        raise SystemExit("cdsapi not installed; refusing live request")
    if readiness["cdsapirc_status"] != "present_shape_valid":
        raise SystemExit(f"cdsapirc not usable ({readiness['cdsapirc_status']}); refusing live request")
    if readiness["dataset_terms_status"] != "user_confirmed_outside_task":
        raise SystemExit("dataset terms acceptance unverified; refusing live request")
    if finality["finality_status"] != "expected_final_by_official_latency":
        raise SystemExit("finality gate not met for final ERA5; refusing live request")
    import cdsapi

    from scripts.v2.core import RawArtifactStore

    client = cdsapi.Client()
    store = RawArtifactStore(root / "data" / "source_raw" / "v2" / "weather")
    results = []
    for segment in plan["request_segments"]:
        request: dict[str, Any] = {
            "product_type": ["reanalysis"],
            "variable": VARIABLES,
            "date": [segment["utc_date"]],
            "time": segment["times"],
            "data_format": "netcdf",
            "download_format": "unarchived",
            "area": area["area"],
        }
        target = root / ".local" / "cds-stage" / f"{resolved['city_id']}-{segment['utc_date']}.nc"
        target.parent.mkdir(parents=True, exist_ok=True)
        client.retrieve(DATASET, request, str(target))
        result = store.persist(
            source_id="cds_era5_hourly" if args.final else "cds_era5t_hourly",
            provider="ECMWF Copernicus Climate Change Service",
            logical_name=f"{resolved['city_id']}-{segment['utc_date']}",
            payload=target.read_bytes(),
            request=request,
            status="final" if args.final else "provisional",
            licence="CC-BY-4.0 catalogue terms and attribution",
            suffix=".nc",
        )
        results.append(str(result.manifest_path))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
