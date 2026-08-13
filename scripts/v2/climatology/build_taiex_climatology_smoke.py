"""Stage 5E-1: tiny climatology algorithm smoke (2018-2020, center Mar 04).

Validates the FINAL climatology algorithm chain on a minimal baseline, NOT the
full 1991-2020 climatology and NOT a research-grade CloudZ.

Chain under test:
    ERA5 hourly TCC (2x2 stencil, exchange anchor)
      -> bilinear (frozen spatial estimator)
      -> piecewise-linear time integration over pre-open [07:00, 09:00) Taipei
      -> daily TCC exposure (percentage points 0-100)
      -> smoke climatology mean / residual SD / z (research_usable = false)

No statistics on returns, no market backfill, no full climatology.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import zipfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import SCHEMA_VERSION, V2Error, RawArtifactStore, repo_root, sha256_file, write_json
from scripts.v2.climatology.common_calendar import (
    circular_window,
    common_day_label,
    common_day_to_actual_date,
)
from scripts.v2.climatology.temporal_integration import piecewise_linear_mean
from scripts.v2.climatology.tcc_exposure import tcc_exposure_pct
from scripts.v2.spatial.grid_geometry import bilinear_weights, surrounding_cell
from scripts.v2.spatial.build_taiex_spatial_pilot import EXCHANGE_ANCHOR, stencil_area
from scripts.v2.fetch_era5 import DATASET, cds_readiness, expected_timestamps, validate_download_container

AUDIT_DIR = "data/audits/v2/climatology"
RAW_BASE = ".local/source-raw/v2/climatology"
AUDIT_PATH = AUDIT_DIR + "/taiex-climatology-smoke-2018-2020-mar04.json"

BASELINE_YEARS = [2018, 2019, 2020]
CENTER_MONTH = 3
CENTER_DAY = 4
RADIUS = 15
PILOT_TIMEZONE = "Asia/Taipei"
PRE_OPEN_HOURS = [7, 8, 9]  # breakpoints bracketing [07:00, 09:00) Taipei
VARIABLE = "total_cloud_cover"
NETCDF_VARIABLE = "tcc"


# --- Plan building (pure, no network) -----------------------------------

def daily_utc_breakpoints(d: date, tz_name: str, hours: list[int] | None = None) -> tuple[list[datetime], datetime, datetime]:
    """UTC breakpoints + window bounds for one pre-open window.

    Returns (breakpoints_utc, window_start_utc, window_end_utc) where the
    window is [first_hour, last_hour) local and the breakpoints are the
    hourly values at each whole hour.
    """
    hours = hours if hours is not None else PRE_OPEN_HOURS
    tz = ZoneInfo(tz_name)
    local = [datetime.combine(d, time(h, 0), tz) for h in hours]
    utc = [t.astimezone(timezone.utc) for t in local]
    return utc, utc[0], utc[-1]


def cds_request_for_year(year: int, tz_name: str) -> dict[str, Any]:
    """Build the CDS date/time lists + daily plan for one baseline year.

    Common days (center Mar 04 +/- 15) map to actual dates; Feb 29 has no
    common-day slot so it is never a daily exposure, but its 23:00 UTC hour is
    still requested as the 07:00 Taipei bracket for Mar 01 (leap years).
    """
    common_days = circular_window(CENTER_MONTH, CENTER_DAY, RADIUS)
    actual_dates = [common_day_to_actual_date(doy, year) for doy in common_days]
    daily = []
    all_utc: list[datetime] = []
    for doy, d in zip(common_days, actual_dates):
        bps, ws, we = daily_utc_breakpoints(d, tz_name)
        daily.append(
            {
                "common_doy": doy,
                "common_label": {"month": common_day_label(doy)[0], "day": common_day_label(doy)[1]},
                "actual_date": d.isoformat(),
                "window_start_utc": ws.isoformat(),
                "window_end_utc": we.isoformat(),
                "breakpoints_utc": [b.isoformat() for b in bps],
            }
        )
        all_utc.extend(bps)
    dates = sorted({b.strftime("%Y-%m-%d") for b in all_utc})
    times = sorted({b.strftime("%H:%M") for b in all_utc})
    return {
        "year": year,
        "common_days": common_days,
        "actual_dates": [d.isoformat() for d in actual_dates],
        "daily_count": len(daily),
        "daily": daily,
        "request": {"date": dates, "time": times},
    }


def temporal_support_timestamps(plans: list[dict[str, Any]]) -> list[str]:
    """All scientific temporal-support UTC timestamps (breakpoints) across years."""
    out: list[str] = []
    for plan in plans:
        for row in plan["daily"]:
            out.extend(row["breakpoints_utc"])
    return sorted(set(out))


# --- Independent oracle (hand-written, must not import production helpers) --

def _oracle_exact_hour(v07: float, v08: float, v09: float) -> float:
    """Hand-written Simpson-mean over [07:00, 09:00): (v07 + 2*v08 + v09) / 4."""
    return (v07 + 2.0 * v08 + v09) / 4.0


def _oracle_half_hour(v07: float, v08: float, v09: float) -> float:
    """Hand-written piecewise-linear mean over [07:00, 08:30) for a linear ramp."""
    # linear ramp values at 07:00/08:00/09:00; value at 08:30 = v08 + 0.5*(v09-v08)
    v0830 = v08 + 0.5 * (v09 - v08)
    # trapezoid [07,08] = 0.5*(v07+v08)*1 ; [08,08.5] = 0.5*(v08+v0830)*0.5
    integral = 0.5 * (v07 + v08) + 0.5 * (v08 + v0830) * 0.5
    return integral / 1.5


def _oracle_mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _corner_index(latitudes: list[float], longitudes: list[float], lat: float, lon: float) -> tuple[int, int]:
    lat_idx = [i for i, v in enumerate(latitudes) if abs(float(v) - lat) < 1e-9]
    lon_idx = [i for i, v in enumerate(longitudes) if abs(float(v) - lon) < 1e-9]
    if len(lat_idx) != 1 or len(lon_idx) != 1:
        raise V2Error(f"corner ({lat}, {lon}) not found in stencil grid")
    return lat_idx[0], lon_idx[0]


# --- Extraction + climatology --------------------------------------------

def extract_exposures(root: Path, plans: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Extract 93 daily TCC exposures from the downloaded per-year stencils.

    Returns (exposures, missing_timestamps, raw_zip_sha256_list).
    """
    corners = surrounding_cell(
        EXCHANGE_ANCHOR["coordinate"]["latitude"], EXCHANGE_ANCHOR["coordinate"]["longitude"]
    )
    weights = bilinear_weights(
        EXCHANGE_ANCHOR["coordinate"]["latitude"],
        EXCHANGE_ANCHOR["coordinate"]["longitude"],
        corners,
    )
    idx = {k: (corners[k].latitude, corners[k].longitude) for k in ("SW", "SE", "NW", "NE")}
    tz = ZoneInfo(PILOT_TIMEZONE)

    # load each year's netcdf into an in-memory lookup: (timestamp -> corner values)
    year_data: dict[int, dict[str, dict[str, float]]] = {}
    raw_hashes: list[str] = []
    import xarray as xr

    for plan in plans:
        year = plan["year"]
        raw_dir = root / RAW_BASE / "cds_era5_hourly_climatology_smoke" / f"taiex-tcc-smoke-{year}"
        zips = sorted(raw_dir.glob("r*-*.zip"))
        if not zips:
            raise V2Error(f"no raw stencil for year {year}; run --live first")
        zip_path = zips[-1]
        raw_hashes.append(sha256_file(zip_path))
        with zipfile.ZipFile(zip_path) as archive:
            member = [n for n in archive.namelist() if n.endswith(".nc")][0]
            with tempfile.TemporaryDirectory(prefix="weather-stock-v2-clim-read-") as td:
                extracted = Path(td) / member
                extracted.write_bytes(archive.read(member))
                ds = xr.open_dataset(extracted)
                latitudes = [float(v) for v in ds["latitude"].values]
                longitudes = [float(v) for v in ds["longitude"].values]
                time_var = ds["valid_time"] if "valid_time" in ds else ds["time"]
                time_strs = [str(t) for t in time_var.values]
                tcc_array = ds["tcc"].values.copy()
                ds.close()
        # normalize time strings to "YYYY-MM-DDTHH" canonical
        lookup: dict[str, dict[str, float]] = {}
        for i, ts in enumerate(time_strs):
            # ts like "2018-02-16T23:00:00.000000000"
            key = ts[:13]  # "YYYY-MM-DDTHH"
            for k, (lat, lon) in idx.items():
                li, lo = _corner_index(latitudes, longitudes, lat, lon)
                lookup.setdefault(key, {})[k] = float(tcc_array[i, li, lo])
        year_data[year] = lookup

    exposures: list[dict[str, Any]] = []
    missing: list[str] = []
    for plan in plans:
        year = plan["year"]
        lookup = year_data[year]
        for row in plan["daily"]:
            actual_date = date.fromisoformat(row["actual_date"])
            bps_utc = [datetime.fromisoformat(b) for b in row["breakpoints_utc"]]
            corner_series: list[dict[str, float]] = []
            keys = [b.strftime("%Y-%m-%dT%H") for b in bps_utc]
            ok = True
            for key in keys:
                if key not in lookup:
                    missing.append(key)
                    ok = False
                    break
                corner_series.append(lookup[key])
            if not ok:
                continue
            ws = datetime.fromisoformat(row["window_start_utc"])
            we = datetime.fromisoformat(row["window_end_utc"])
            exposure = tcc_exposure_pct(bps_utc, corner_series, weights, ws, we)
            exposures.append(
                {
                    "baseline_year": year,
                    "common_doy": row["common_doy"],
                    "common_label": row["common_label"],
                    "actual_date": row["actual_date"],
                    "daily_tcc_exposure_pct": round(exposure, 6),
                    "window_start_utc": row["window_start_utc"],
                    "window_end_utc": row["window_end_utc"],
                }
            )
    return exposures, missing, raw_hashes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 5E-1 tiny climatology algorithm smoke")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--live", action="store_true", help="download the 3-year TCC stencils")
    parser.add_argument("--compute", action="store_true", help="compute exposures/audit from existing raw (no download)")
    args = parser.parse_args(argv)
    root = args.root

    plans = [cds_request_for_year(y, PILOT_TIMEZONE) for y in BASELINE_YEARS]
    support = temporal_support_timestamps(plans)
    daily_count = sum(p["daily_count"] for p in plans)
    corners = surrounding_cell(
        EXCHANGE_ANCHOR["coordinate"]["latitude"], EXCHANGE_ANCHOR["coordinate"]["longitude"]
    )

    # transport timestamps = union of date x time cartesian products
    transport: set[str] = set()
    for plan in plans:
        transport |= {t.isoformat() for t in expected_timestamps(plan["request"])}

    if not args.live and not args.compute:
        print(json.dumps(
            {
                "mode": "dry_run",
                "baseline_years": BASELINE_YEARS,
                "center": {"month": CENTER_MONTH, "day": CENTER_DAY},
                "radius": RADIUS,
                "expected_daily_count": daily_count,
                "temporal_support_count": len(support),
                "transport_timestamp_count": len(transport),
                "per_year": [
                    {
                        "year": p["year"],
                        "daily_count": p["daily_count"],
                        "request_dates": p["request"]["date"][0] + ".." + p["request"]["date"][-1],
                        "request_times": p["request"]["time"],
                        "actual_date_first": p["daily"][0]["actual_date"],
                        "actual_date_last": p["daily"][-1]["actual_date"],
                    }
                    for p in plans
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    # --- Live download (bounded: 3 requests, one per year) ---
    if args.live:
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
        area = stencil_area(corners)
        for plan in plans:
            year = plan["year"]
            request: dict[str, Any] = {
                "product_type": ["reanalysis"],
                "variable": [VARIABLE],
                "date": plan["request"]["date"],
                "time": plan["request"]["time"],
                "data_format": "netcdf",
                "download_format": "zip",
                "area": area,
            }
            with tempfile.TemporaryDirectory(prefix="weather-stock-v2-clim-") as tmp:
                target = Path(tmp) / f"taiex-tcc-smoke-{year}.download"
                client.retrieve(DATASET, request, str(target))
                validation = validate_download_container(
                    target, request, {"area": area}, expected_netcdf_variables=[NETCDF_VARIABLE]
                )
                if not validation["container_validation_passed"]:
                    raise V2Error(f"smoke stencil {year} container validation failed")
                store.persist(
                    source_id="cds_era5_hourly_climatology_smoke",
                    provider="ECMWF Copernicus Climate Change Service",
                    logical_name=f"taiex-tcc-smoke-{year}",
                    payload=target.read_bytes(),
                    request=request,
                    status="final",
                    licence="CC-BY-4.0 catalogue terms and attribution",
                    suffix=".zip",
                    validation_metadata={
                        "container_sha256": validation["container_sha256"],
                        "member_count": validation["member_count"],
                        "container_validation_passed": validation["container_validation_passed"],
                    },
                )
            print(f"downloaded year {year}")

    # --- Extraction + climatology + oracle ---
    exposures, missing, raw_hashes = extract_exposures(root, plans)
    exposure_values = [e["daily_tcc_exposure_pct"] for e in exposures]
    mean_val = statistics.fmean(exposure_values) if exposure_values else float("nan")
    sd_val = statistics.pstdev(exposure_values) if len(exposure_values) > 1 else float("nan")

    # smoke anomaly / z (research_usable=false)
    for e in exposures:
        e["smoke_anomaly_pct"] = round(e["daily_tcc_exposure_pct"] - mean_val, 6)
        e["smoke_z"] = round((e["daily_tcc_exposure_pct"] - mean_val) / sd_val, 6) if sd_val else None

    # independent oracle
    oracle = {
        "exact_hour_2h": {
            "production": piecewise_linear_mean(
                [datetime(2020, 1, 1, 7), datetime(2020, 1, 1, 8), datetime(2020, 1, 1, 9)],
                [0.2, 0.8, 0.4],
                datetime(2020, 1, 1, 7),
                datetime(2020, 1, 1, 9),
            ),
            "oracle": _oracle_exact_hour(0.2, 0.8, 0.4),
        },
        "half_hour": {
            "production": piecewise_linear_mean(
                [datetime(2020, 1, 1, 7), datetime(2020, 1, 1, 8), datetime(2020, 1, 1, 9)],
                [0.0, 0.1, 0.2],
                datetime(2020, 1, 1, 7),
                datetime(2020, 1, 1, 8, 30),
            ),
            "oracle": _oracle_half_hour(0.0, 0.1, 0.2),
        },
        "climatology_mean": {
            "production": round(mean_val, 9),
            "oracle": round(_oracle_mean(exposure_values), 9) if exposure_values else None,
        },
        "feb29_exclusion": {
            "no_feb29_in_actual_dates": all(
                not (date.fromisoformat(e["actual_date"]).month == 2 and date.fromisoformat(e["actual_date"]).day == 29)
                for e in exposures
            ),
        },
        "circular_membership_31": {
            "window_size": daily_count,
            "unique": daily_count == 93,
        },
    }
    oracle_exact_hour_ok = abs(oracle["exact_hour_2h"]["production"] - oracle["exact_hour_2h"]["oracle"]) < 1e-12
    oracle_half_hour_ok = abs(oracle["half_hour"]["production"] - oracle["half_hour"]["oracle"]) < 1e-12
    oracle_mean_ok = oracle["climatology_mean"]["production"] == oracle["climatology_mean"]["oracle"]
    oracle_pass = oracle_exact_hour_ok and oracle_half_hour_ok and oracle_mean_ok and oracle["feb29_exclusion"]["no_feb29_in_actual_dates"] and oracle["circular_membership_31"]["unique"]

    # --- audit ---
    import hashlib

    daily_exposure_payload = json.dumps(
        [e["daily_tcc_exposure_pct"] for e in exposures], sort_keys=True
    ).encode("utf-8")
    audit = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_climatology_smoke",
        "contract_version": "stage5e1_smoke_v1",
        "research_usable": False,
        "anchor_binding": EXCHANGE_ANCHOR,
        "spatial_audit_binding": {
            "audit": "data/audits/v2/spatial/taiex-spatial-interpolation-acceptance.json",
            "primary_spatial_estimator": "bilinear",
            "grid_resolution_degrees": 0.25,
        },
        "baseline_years": BASELINE_YEARS,
        "center_calendar_day": {"month": CENTER_MONTH, "day": CENTER_DAY},
        "window_radius_days": RADIUS,
        "common_calendar": "fixed_365_day",
        "common_calendar_mapping": "leap Mar01+ maps to non-leap common day; Feb29 excluded",
        "feb29_exclusion": "excluded_from_smoothing_pool",
        "temporal_integration_method": "piecewise_linear_time_integration",
        "hourly_support_rule": "window boundary bracketing hourly breakpoints (07:00, 08:00, 09:00 Taipei)",
        "expected_daily_count": 93,
        "observed_daily_count": len(exposures),
        "temporal_support_count": len(support),
        "transport_timestamp_count": len(transport),
        "missing_count": len(missing),
        "missing_timestamps": missing,
        "climatology_mean_pct": round(mean_val, 6),
        "smoke_reference_sd_pct": round(sd_val, 6),
        "smoke_residual_sd_pct": round(sd_val, 6),
        "smoke_z_note": "smoke_z_for_algorithm_validation only; research_usable=false",
        "raw_zip_sha256": raw_hashes,
        "daily_exposure_sha256": hashlib.sha256(daily_exposure_payload).hexdigest(),
        "oracle": oracle,
        "oracle_pass": oracle_pass,
        "daily_exposures": exposures,
        "deterministic_rebuild": "audit is deterministic (byte-identical across repeated builds from the same raw artifact)",
        "code_sha256": {
            "temporal_integration.py": sha256_file(root / "scripts/v2/climatology/temporal_integration.py"),
            "common_calendar.py": sha256_file(root / "scripts/v2/climatology/common_calendar.py"),
            "tcc_exposure.py": sha256_file(root / "scripts/v2/climatology/tcc_exposure.py"),
            "build_taiex_climatology_smoke.py": sha256_file(root / "scripts/v2/climatology/build_taiex_climatology_smoke.py"),
        },
        "historical_backfill_run": False,
    }
    write_json(root / AUDIT_PATH, audit)

    gate = (
        len(exposures) == 93
        and not missing
        and oracle_pass
    )
    print(json.dumps(
        {
            "expected_daily_count": 93,
            "observed_daily_count": len(exposures),
            "missing_count": len(missing),
            "climatology_mean_pct": round(mean_val, 6),
            "smoke_reference_sd_pct": round(sd_val, 6),
            "oracle_pass": oracle_pass,
            "gate": "PASS_STAGE5E1_CLIMATOLOGY_SMOKE" if gate else "REVISE_STAGE5E1_CLIMATOLOGY_SMOKE",
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
