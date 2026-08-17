"""Stage 5C: TAIEX spatial measurement qualification (anchor + bilinear pilot).

Frozen scientific decisions (control plane):
- PRIMARY weather anchor = TWSE official exchange location (Taipei 101).
- PRIMARY spatial estimator = 4-point bilinear on the ERA5 0.25x0.25 grid.
- ROBUSTNESS estimator = nearest grid point.
- LEGACY anchor (sensitivity only) = Taipei City Hall municipal reference point.

This module builds the anchor provenance, computes the surrounding 2x2 ERA5
stencil, and (with --live) downloads a minimal total-cloud-cover sample for the
pilot week pre-open window (07:00-09:00 Asia/Taipei), then computes bilinear /
nearest / legacy-sensitivity TCC and writes three audits.

No statistics, no return data, no climatology, no backfill.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import SCHEMA_VERSION, V2Error, RawArtifactStore, repo_root, write_json
from scripts.v2.spatial.grid_geometry import (
    bilinear_value,
    bilinear_weights,
    describe_cell,
    nearest_grid_point,
    surrounding_cell,
)
from scripts.v2.fetch_era5 import (
    DATASET,
    cds_readiness,
    validate_download_container,
)

AUDIT_DIR = "data/audits/v2/spatial"
RAW_BASE = ".local/source-raw/v2/spatial"

# --- Frozen anchors ------------------------------------------------------
EXCHANGE_ANCHOR = {
    "anchor_id": "twse_exchange_taipei_101",
    "anchor_role": "primary",
    "anchor_type": "official_exchange_location",
    "market_id": "taiex",
    "official_postal_address": "3F, 9F-12F, 15F, No.7, Sec.5, Xinyi Rd., Taipei City 110615, Taiwan (R.O.C.)",
    "official_source_url": "https://www.twse.com.tw/en/about/company/map.html",
    "building": "Taipei 101",
    "coordinate": {"latitude": 25.0338352, "longitude": 121.5644995},
    "coordinate_derivation_method": "geocoding",
    "geocoder": "Nominatim (OpenStreetMap)",
    "geocoder_query": "Taipei 101",
    "geocoder_result": {
        "osm_type": "way",
        "osm_id": 1159328965,
        "name": "台北101",
        "address_road": "信義路五段",
    },
    "geocoding_date": "2026-08-13",
    "cross_check": "reverse geocode of the coordinate resolves to Taipei 101 premises (市府路45號)",
}

LEGACY_ANCHOR = {
    "anchor_id": "taipei_city_hall_municipal",
    "anchor_role": "legacy_anchor_sensitivity",
    "anchor_type": "municipal_reference_point",
    "market_id": "taiex",
    "coordinate": {"latitude": 25.0375, "longitude": 121.5646},
}

# --- Pilot window --------------------------------------------------------
PILOT_DATES = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5), date(2026, 3, 6)]
PILOT_TIMEZONE = "Asia/Taipei"
PRE_OPEN_LOCAL_HOURS = [7, 8]  # 07:00-08:59 local, two whole-hour TCC values
VARIABLE = "total_cloud_cover"
NETCDF_VARIABLE = "tcc"

STENCIL_PAD_DEGREES = 0.05  # < 0.25, so it never crosses the next grid line


def pre_open_utc_plan() -> dict[str, Any]:
    """UTC request plan for the pre-open window of each pilot trading day."""
    tz = ZoneInfo(PILOT_TIMEZONE)
    utc_dates: dict[str, set[int]] = {}
    for day in PILOT_DATES:
        for hour in PRE_OPEN_LOCAL_HOURS:
            local = datetime.combine(day, time(hour, 0), tz)
            utc = local.astimezone(timezone.utc)
            utc_dates.setdefault(utc.date().isoformat(), set()).add(utc.hour)
    dates = sorted(utc_dates)
    return {
        "pilot_trading_dates": [d.isoformat() for d in PILOT_DATES],
        "pre_open_local_hours": PRE_OPEN_LOCAL_HOURS,
        "utc_dates": dates,
        "utc_times_by_date": {d: [f"{h:02d}:00" for h in sorted(utc_dates[d])] for d in dates},
    }


def stencil_area(corners: dict[str, Any]) -> list[float]:
    """CDS area [N, W, S, E] covering the 4 corners, padded without crossing a grid line."""
    pad = STENCIL_PAD_DEGREES
    north = corners["NW"].latitude + pad
    west = corners["SW"].longitude - pad
    south = corners["SW"].latitude - pad
    east = corners["SE"].longitude + pad
    return [round(north, 6), round(west, 6), round(south, 6), round(east, 6)]


def anchor_record(anchor: dict[str, Any]) -> dict[str, Any]:
    return {
        "anchor_id": anchor["anchor_id"],
        "anchor_role": anchor["anchor_role"],
        "anchor_type": anchor["anchor_type"],
        "market_id": anchor["market_id"],
        "latitude": anchor["coordinate"]["latitude"],
        "longitude": anchor["coordinate"]["longitude"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TAIEX spatial measurement qualification")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--live", action="store_true", help="download the minimal TCC stencil")
    args = parser.parse_args(argv)
    root = args.root

    plan = pre_open_utc_plan()
    exchange_corners = surrounding_cell(
        EXCHANGE_ANCHOR["coordinate"]["latitude"], EXCHANGE_ANCHOR["coordinate"]["longitude"]
    )
    legacy_corners = surrounding_cell(
        LEGACY_ANCHOR["coordinate"]["latitude"], LEGACY_ANCHOR["coordinate"]["longitude"]
    )
    exchange_weights = bilinear_weights(
        EXCHANGE_ANCHOR["coordinate"]["latitude"],
        EXCHANGE_ANCHOR["coordinate"]["longitude"],
        exchange_corners,
    )
    legacy_weights = bilinear_weights(
        LEGACY_ANCHOR["coordinate"]["latitude"],
        LEGACY_ANCHOR["coordinate"]["longitude"],
        legacy_corners,
    )

    # Anchor provenance audit (no network).
    anchor_audit = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_primary_weather_anchor",
        "primary_anchor": EXCHANGE_ANCHOR,
        "legacy_anchor": LEGACY_ANCHOR,
        "primary_spatial_estimator": "bilinear",
        "robustness_spatial_estimator": "nearest_grid",
        "grid_resolution_degrees": 0.25,
        "cell": describe_cell(exchange_corners),
        "bilinear_weights": {k: round(v, 12) for k, v in exchange_weights.items()},
        "weight_sum": round(sum(exchange_weights.values()), 12),
        "legacy_cell": describe_cell(legacy_corners),
        "legacy_bilinear_weights": {k: round(v, 12) for k, v in legacy_weights.items()},
        "anchor_distance_km_note": "exchange anchor is ~0.4 km south of the legacy municipal point; both fall in the same 0.25-degree ERA5 cell",
    }
    write_json(root / AUDIT_DIR / "taiex-primary-weather-anchor.json", anchor_audit)

    if not args.live:
        print(json.dumps({"mode": "dry_run", "plan": plan, "anchor": anchor_audit}, ensure_ascii=False, indent=2))
        return 0

    # --- Live minimal TCC stencil ---
    readiness = cds_readiness(root)
    if not readiness["cdsapi_installed"]:
        raise SystemExit("cdsapi not installed; refusing live request")
    if readiness["cdsapirc_status"] != "present_shape_valid":
        raise SystemExit(f"cdsapirc not usable ({readiness['cdsapirc_status']})")
    if readiness["dataset_terms_status"] != "user_confirmed_outside_task":
        raise SystemExit("dataset terms acceptance unverified")

    import cdsapi

    client = cdsapi.Client()
    store = RawArtifactStore(root / RAW_BASE)
    area = stencil_area(exchange_corners)
    # One request: the pre-open UTC dates/times are merged across days via CDS
    # date x time cartesian product (times repeated per date).
    all_times = sorted({t for times in plan["utc_times_by_date"].values() for t in times})
    request: dict[str, Any] = {
        "product_type": ["reanalysis"],
        "variable": [VARIABLE],
        "date": plan["utc_dates"],
        "time": all_times,
        "data_format": "netcdf",
        "download_format": "zip",
        "area": area,
    }
    import tempfile

    with tempfile.TemporaryDirectory(prefix="weather-stock-v2-spatial-") as tmp:
        target = Path(tmp) / "taiex-tcc-stencil.download"
        client.retrieve(DATASET, request, str(target))
        validation = validate_download_container(target, request, {"area": area}, expected_netcdf_variables=[NETCDF_VARIABLE])
        if not validation["container_validation_passed"]:
            raise V2Error("spatial stencil download container validation failed")
        logical = "taiex-tcc-stencil-preopen"
        result = store.persist(
            source_id="cds_era5_hourly_spatial",
            provider="ECMWF Copernicus Climate Change Service",
            logical_name=logical,
            payload=target.read_bytes(),
            request=request,
            status="final",
            licence="CC-BY-4.0 catalogue terms and attribution",
            suffix=".zip",
            validation_metadata={
                "container_type": validation["container_type"],
                "container_sha256": validation["container_sha256"],
                "member_count": validation["member_count"],
                "observed_variable_union": validation["observed_variable_union"],
                "container_validation_passed": validation["container_validation_passed"],
            },
        )
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    print(json.dumps({
        "status": "live_stencil_downloaded",
        "artifact_id": manifest["artifact_id"],
        "sha256": manifest["sha256"],
        "container_sha256": validation["container_sha256"],
        "member_count": validation["member_count"],
        "observed_variable_union": validation["observed_variable_union"],
        "spatial_members": [m["spatial"] for m in validation["member_summaries"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
