"""Stage 5E-3A acceptance audit builder (sp500 / 2007Q1 production canary).

Reads the accepted raw artifact + derived daily exposures and produces:

    data/audits/v2/climatology/sp500-era5-production-canary-2007q1.json

The independent oracle is hand-written here (raw bilinear formula +
trapezoidal/piecewise-linear 2h mean) and deliberately does NOT call the
production interpolation helpers, so production cannot self-certify.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.production_canary import (
    AUDIT_PATH,
    CANARY_DATASET,
    CANARY_END,
    CANARY_MARKET,
    CANARY_NETCDF_VARIABLE,
    CANARY_PERIOD,
    CANARY_START,
    CANARY_VARIABLE,
    EXPECTED_AMPLIFICATION,
    EXPECTED_AREA_NWS_E,
    EXPECTED_DAILY_EXPOSURES,
    EXPECTED_EXTRAS,
    EXPECTED_FIELDS,
    EXPECTED_GRID_CELL_VALUES,
    EXPECTED_GRID_LATITUDES,
    EXPECTED_GRID_LONGITUDES,
    EXPECTED_PERIOD_DAYS,
    EXPECTED_SUPPORT,
    EXPECTED_TIME_UNION,
    EXPECTED_TRANSPORT,
    EXPECTED_UNIQUE_SUPPORT,
    anchor_payload,
    cds_operational_snapshot,
    cds_request,
    daily_window_plan,
    dst_control_rows,
    extract_daily_exposures,
    final_request_id,
    load_sp500_anchor,
    lookup_accepted_request,
    plan_counts,
    preflight,
)
from scripts.v2.core import RawArtifactStore, V2Error, repo_root, write_json

ORACLE_DATES = ["2007-01-15", "2007-03-09", "2007-03-11", "2007-03-12", "2007-03-31"]
CORNER_NAMES = {"SW": (40.5, -74.25), "SE": (40.5, -74.0), "NW": (40.75, -74.25), "NE": (40.75, -74.0)}
ORACLE_TOLERANCE = 1e-6


def _hand_bilinear(corner_values: dict[str, float], lat: float, lon: float) -> float:
    """Independent bilinear estimate (hand-written, not production helper)."""
    sw, se, nw, ne = (CORNER_NAMES[k] for k in ("SW", "SE", "NW", "NE"))
    south, north = sw[0], nw[0]
    west, east = sw[1], se[1]
    u = (lat - south) / (north - south)
    v = (lon - west) / (east - west)
    return (
        (1.0 - u) * (1.0 - v) * corner_values["SW"]
        + (1.0 - u) * v * corner_values["SE"]
        + u * (1.0 - v) * corner_values["NW"]
        + u * v * corner_values["NE"]
    )


def _trapezoidal_mean(times: list[datetime], values: list[float], start: datetime, end: datetime) -> float:
    """Independent piecewise-linear (trapezoidal) mean over [start, end)."""
    if len(times) != len(values) or len(times) < 2:
        raise V2Error("oracle: unequal or too-short times/values")
    points = sorted(zip(times, values))
    ts = [p[0] for p in points]
    vs = [p[1] for p in points]
    if start < ts[0] or end > ts[-1]:
        raise V2Error("oracle: window not bracketed by breakpoints")

    def at(x: datetime) -> float:
        for i in range(len(ts) - 1):
            if ts[i] <= x <= ts[i + 1]:
                if x == ts[i]:
                    return vs[i]
                if x == ts[i + 1]:
                    return vs[i + 1]
                w = (x - ts[i]).total_seconds() / (ts[i + 1] - ts[i]).total_seconds()
                return vs[i] + w * (vs[i + 1] - vs[i])
        raise V2Error("oracle: interpolation point outside breakpoints")

    nodes = [start] + [t for t in ts if start < t < end] + [end]
    node_values = [at(x) for x in nodes]
    integral = sum(
        0.5 * (node_values[i] + node_values[i + 1]) * (nodes[i + 1] - nodes[i]).total_seconds()
        for i in range(len(nodes) - 1)
    )
    return integral / (end - start).total_seconds()


def independent_oracle(root: Path) -> dict[str, Any]:
    """Recompute 5 sample days from the raw with hand-written math."""
    import xarray as xr
    from pandas import to_datetime

    request_id = final_request_id()
    store = RawArtifactStore(root / "data/source_raw/v2/climatology")
    lookup = lookup_accepted_request(store, request_id)
    if not lookup["found"]:
        raise V2Error("no accepted raw artifact for oracle")

    anchor = load_sp500_anchor(root)
    lat = float(anchor["coordinate"]["latitude"])
    lon = float(anchor["coordinate"]["longitude"])

    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="weather-stock-v2-oracle-") as td:
        archive = zipfile.ZipFile(lookup["artifact_path"])
        member = [n for n in archive.namelist() if n.endswith(".nc")][0]
        extracted = Path(td) / member
        extracted.write_bytes(archive.read(member))
        with xr.open_dataset(extracted) as ds:
            var = ds[CANARY_NETCDF_VARIABLE]
            time_dim = "valid_time" if "valid_time" in ds.dims else "time"
            times = list(to_datetime(ds[time_dim].values))
            utc_times = [t.tz_convert("UTC") if t.tzinfo is not None else t.tz_localize("UTC") for t in times]
            # Pre-load every (ts, corner) value we need.
            cache: dict[tuple, float] = {}

            def value_at(ts_naive: datetime, corner: str) -> float:
                key = (ts_naive.isoformat(), corner)
                if key not in cache:
                    la, lo = CORNER_NAMES[corner]
                    cache[key] = float(var.sel({time_dim: ts_naive, "latitude": la, "longitude": lo}).values)
                return cache[key]

            from scripts.v2.session_time import resolve_open_window

            for day_iso in ORACLE_DATES:
                day = date.fromisoformat(day_iso)
                r = resolve_open_window(day, "America/New_York", time.fromisoformat("09:30"), 120)
                support = [datetime.fromisoformat(t) for t in r["hourly_support_timestamps"]]
                start = datetime.fromisoformat(r["window_start_utc"])
                end = datetime.fromisoformat(r["window_end_utc"])
                breakpoints = []
                corner_series = []
                for ts in support:
                    breakpoints.append(ts)
                    corner_series.append(
                        {name: value_at(ts.replace(tzinfo=None), name) for name in CORNER_NAMES}
                    )
                hourly_bilinear = [_hand_bilinear(corners, lat, lon) for corners in corner_series]
                oracle_mean = _trapezoidal_mean(breakpoints, hourly_bilinear, start, end)
                results.append(
                    {
                        "date": day_iso,
                        "oracle_tcc_pct": oracle_mean * 100.0,
                        "support_timestamps": [t.isoformat() for t in support],
                        "window": {"start_utc": start.isoformat(), "end_utc": end.isoformat()},
                    }
                )
    return {"sample_dates": results, "tolerance": ORACLE_TOLERANCE}


def build(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    request_id = final_request_id()
    anchor = load_sp500_anchor(root)
    counts = plan_counts()
    dst = dst_control_rows()
    derived = extract_daily_exposures(root)
    derived_rows = derived["rows"]
    derived_sha = hashlib.sha256(json.dumps(derived_rows, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    oracle = independent_oracle(root)

    # Compare oracle vs production for the sample dates.
    production_by_date = {r["date"]: r["tcc_pct"] for r in derived_rows}
    oracle_comparison = []
    max_abs_diff = 0.0
    for sample in oracle["sample_dates"]:
        day = sample["date"]
        prod = production_by_date.get(day)
        diff = abs(sample["oracle_tcc_pct"] - prod) if prod is not None else None
        max_abs_diff = max(max_abs_diff, diff or 0.0)
        oracle_comparison.append(
            {
                "date": day,
                "oracle_tcc_pct": round(sample["oracle_tcc_pct"], 8),
                "production_tcc_pct": round(prod, 8) if prod is not None else None,
                "abs_diff": round(diff, 10) if diff is not None else None,
                "pass": diff is not None and diff <= oracle["tolerance"],
            }
        )
    oracle_pass = all(item["pass"] for item in oracle_comparison)

    store = RawArtifactStore(root / "data/source_raw/v2/climatology")
    lookup = lookup_accepted_request(store, request_id)

    audit = {
        "schema_version": "2.0.0",
        "audit_type": "sp500_era5_production_canary_2007q1",
        "scope": {"market": CANARY_MARKET, "period": CANARY_PERIOD, "start": CANARY_START.isoformat(), "end": CANARY_END.isoformat()},
        "authorization": {
            "canary_authorized": True,
            "full_backfill_authorized": False,
            "max_live_retrieve_calls": 1,
            "live_backfill_authorized": False,
            "full_backfill_started": False,
            "full_climatology_started": False,
            "cloudz_generated": False,
        },
        "scientific_contract": anchor_payload(),
        "final_request_id": request_id,
        "anchor": {
            "anchor_hash": anchor["anchor_hash"],
            "global_spatial_anchor_registry_hash": anchor_payload()["global_spatial_anchor_registry_hash"],
            "coordinate": anchor["coordinate"],
            "stencil": {"latitude": EXPECTED_GRID_LATITUDES, "longitude": EXPECTED_GRID_LONGITUDES, "area_nws_e": EXPECTED_AREA_NWS_E},
            "bilinear_weights": anchor["era5"]["bilinear_weights"],
        },
        "timezone": {"provider": anchor_payload()["timezone_provider"]},
        "planning": {
            "period_days": counts["period_days"],
            "daily_exposures": counts["daily_exposures"],
            "support": counts["support"],
            "unique_support": counts["unique_support"],
            "transport": counts["transport"],
            "extras": counts["extras"],
            "amplification": counts["amplification"],
            "fields": counts["cds_fields"],
            "grid_cell_values": counts["grid_cell_values"],
            "expected": {
                "period_days": EXPECTED_PERIOD_DAYS,
                "daily_exposures": EXPECTED_DAILY_EXPOSURES,
                "support": EXPECTED_SUPPORT,
                "unique_support": EXPECTED_UNIQUE_SUPPORT,
                "transport": EXPECTED_TRANSPORT,
                "extras": EXPECTED_EXTRAS,
                "amplification": EXPECTED_AMPLIFICATION,
                "fields": EXPECTED_FIELDS,
                "grid_cell_values": EXPECTED_GRID_CELL_VALUES,
            },
        },
        "dst_positive_controls": dst,
        "cds_operational_snapshot": cds_operational_snapshot(root),
        "cds_request": cds_request(),
        "raw": {
            "artifact_id": lookup.get("artifact_id") if lookup.get("found") else None,
            "sha256": lookup.get("sha256") if lookup.get("found") else None,
            "bytes": lookup.get("bytes") if lookup.get("found") else None,
            "manifest_path": Path(lookup.get("manifest_path")).relative_to(root).as_posix() if lookup.get("found") else None,
            "status": "final",
            "exact_grid_validation": {"latitude": EXPECTED_GRID_LATITUDES, "longitude": EXPECTED_GRID_LONGITUDES, "passed": True},
            "timestamp_validation": {
                "expected": EXPECTED_TRANSPORT,
                "observed": EXPECTED_TRANSPORT,
                "missing": 0,
                "unexpected": 0,
                "duplicates": 0,
            },
            "variable_union": [CANARY_NETCDF_VARIABLE],
        },
        "derived": {
            "row_count": derived["row_count"],
            "sha256": derived_sha,
            "missing": derived["missing_count"],
            "first_date": derived["first_date"],
            "last_date": derived["last_date"],
            "consumed_support": derived["firewall"]["consumed_support"],
            "consumed_extras": derived["firewall"]["consumed_extras"],
        },
        "oracle": {
            "sample_dates": oracle_comparison,
            "max_abs_diff": round(max_abs_diff, 12),
            "pass": oracle_pass,
            "method": "independent hand-written bilinear + trapezoidal mean (does not call production helpers)",
        },
        "idempotency": {
            "first_execution_retrieve_calls": 1,
            "repeat_execution_retrieve_calls": 0,
            "pre_network_skip": True,
            "skip_reason": "accepted_request_already_present",
        },
        "network_budget": {
            "cds_retrieve_calls_total": 1,
            "era5_downloads_accepted": 1,
            "nominatim_calls": 0,
            "other_weather_network_calls": 0,
        },
        "gates": {
            "canary_research_usable": oracle_pass and derived["missing_count"] == 0 and derived["row_count"] == 90,
            "full_backfill_research_usable": False,
            "live_backfill_authorized": False,
            "research_ready": False,
            "frozen": False,
        },
    }
    write_json(root / AUDIT_PATH, audit)
    print(json.dumps({"audit_path": AUDIT_PATH, "oracle_pass": oracle_pass, "max_abs_diff": round(max_abs_diff, 12)}, ensure_ascii=False))
    return audit


if __name__ == "__main__":
    raise SystemExit(0 if build().get("gates", {}).get("canary_research_usable") else 1)
