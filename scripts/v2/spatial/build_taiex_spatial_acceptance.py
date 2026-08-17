"""Stage 5C: compute bilinear/nearest/legacy TCC from the downloaded stencil.

Reads the downloaded 2x2 TCC stencil, extracts the four corner values for the
exact pre-open timestamps, computes the frozen bilinear (primary), nearest
(robustness) and legacy-municipal bilinear (sensitivity) estimates, and runs
an INDEPENDENT bilinear oracle (hand-written formula, no shared helper) before
writing the acceptance audit.

No statistics, no return data, no climatology, no backfill.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import SCHEMA_VERSION, V2Error, repo_root, sha256_file, write_json
from scripts.v2.spatial.grid_geometry import (
    bilinear_value,
    bilinear_weights,
    nearest_grid_point,
    surrounding_cell,
)
from scripts.v2.spatial.build_taiex_spatial_pilot import (
    EXCHANGE_ANCHOR,
    LEGACY_ANCHOR,
    NETCDF_VARIABLE,
    pre_open_utc_plan,
)

RAW_BASE = ".local/source-raw/v2/spatial/cds_era5_hourly_spatial/taiex-tcc-stencil-preopen"
AUDIT_DIR = "data/audits/v2/spatial"
ACCEPTANCE_AUDIT = "data/audits/v2/spatial/taiex-spatial-interpolation-acceptance.json"


def pre_open_timestamps() -> list[str]:
    plan = pre_open_utc_plan()
    stamps = []
    for d in plan["utc_dates"]:
        for t in plan["utc_times_by_date"][d]:
            stamps.append(f"{d}T{t}:00+00:00")
    return stamps


def _corner_index(latitudes: list[float], longitudes: list[float], lat: float, lon: float) -> tuple[int, int]:
    """Locate a corner coordinate in the (possibly descending) grid arrays."""
    lat_idx = [i for i, v in enumerate(latitudes) if abs(float(v) - lat) < 1e-9]
    lon_idx = [i for i, v in enumerate(longitudes) if abs(float(v) - lon) < 1e-9]
    if len(lat_idx) != 1 or len(lon_idx) != 1:
        raise V2Error(f"corner ({lat}, {lon}) not found in stencil grid")
    return lat_idx[0], lon_idx[0]


def independent_oracle(anchor_lat: float, anchor_lon: float, sw, se, nw, ne, south, north, west, east) -> float:
    """Hand-written bilinear (no shared helper) for independent verification."""
    u = (anchor_lat - south) / (north - south)
    v = (anchor_lon - west) / (east - west)
    return (1 - u) * (1 - v) * sw + (1 - u) * v * se + u * (1 - v) * nw + u * v * ne


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute and audit TAIEX spatial interpolation")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    raw_dir = root / RAW_BASE
    zips = sorted(p for p in raw_dir.glob("r*-*.zip"))
    if not zips:
        raise V2Error("no spatial stencil raw artifact found; run the live pilot first")
    zip_path = zips[-1]

    import xarray as xr

    stamps = pre_open_timestamps()
    with zipfile.ZipFile(zip_path) as archive:
        member = [n for n in archive.namelist() if n.endswith(".nc")][0]
        with tempfile.TemporaryDirectory(prefix="weather-stock-v2-spatial-read-") as td:
            extracted = Path(td) / member
            extracted.write_bytes(archive.read(member))
            ds = xr.open_dataset(extracted)
            latitudes = [float(v) for v in ds["latitude"].values]
            longitudes = [float(v) for v in ds["longitude"].values]
            time_var = ds["valid_time"] if "valid_time" in ds else ds["time"]
            time_strs = [str(t) for t in time_var.values]
            tcc_array = ds["tcc"].values.copy()  # full array into memory
            ds.close()

    corners = surrounding_cell(
        EXCHANGE_ANCHOR["coordinate"]["latitude"], EXCHANGE_ANCHOR["coordinate"]["longitude"]
    )
    legacy_corners = surrounding_cell(
        LEGACY_ANCHOR["coordinate"]["latitude"], LEGACY_ANCHOR["coordinate"]["longitude"]
    )
    w = bilinear_weights(
        EXCHANGE_ANCHOR["coordinate"]["latitude"],
        EXCHANGE_ANCHOR["coordinate"]["longitude"],
        corners,
    )
    w_legacy = bilinear_weights(
        LEGACY_ANCHOR["coordinate"]["latitude"],
        LEGACY_ANCHOR["coordinate"]["longitude"],
        legacy_corners,
    )
    nearest_name, nearest_point = nearest_grid_point(
        EXCHANGE_ANCHOR["coordinate"]["latitude"],
        EXCHANGE_ANCHOR["coordinate"]["longitude"],
        corners,
    )

    south = corners["SW"].latitude
    north = corners["NW"].latitude
    west = corners["SW"].longitude
    east = corners["SE"].longitude

    idx = {k: _corner_index(latitudes, longitudes, v.latitude, v.longitude) for k, v in corners.items()}

    rows: list[dict[str, Any]] = []
    max_oracle_diff = 0.0
    for stamp in stamps:
        t_idx = None
        for i, ts in enumerate(time_strs):
            if ts.startswith(stamp[:13]):
                t_idx = i
                break
        if t_idx is None:
            raise V2Error(f"timestamp {stamp} not found in stencil")
        corner_vals = {}
        for k in ("SW", "SE", "NW", "NE"):
            li, lo = idx[k]
            corner_vals[k] = float(tcc_array[t_idx, li, lo])
        bilinear = bilinear_value(corner_vals, w)
        nearest_val = corner_vals[nearest_name]
        legacy_bilinear = bilinear_value(corner_vals, w_legacy)
        oracle = independent_oracle(
            EXCHANGE_ANCHOR["coordinate"]["latitude"],
            EXCHANGE_ANCHOR["coordinate"]["longitude"],
            corner_vals["SW"], corner_vals["SE"], corner_vals["NW"], corner_vals["NE"],
            south, north, west, east,
        )
        max_oracle_diff = max(max_oracle_diff, abs(bilinear - oracle))
        rows.append(
            {
                "timestamp_utc": stamp,
                "corner_tcc": {k: round(corner_vals[k], 6) for k in ("SW", "SE", "NW", "NE")},
                "bilinear_tcc": round(bilinear, 6),
                "nearest_tcc": round(nearest_val, 6),
                "nearest_grid_point": nearest_name,
                "legacy_municipal_bilinear_tcc": round(legacy_bilinear, 6),
                "legacy_minus_primary": round(legacy_bilinear - bilinear, 6),
                "oracle_tcc": round(oracle, 6),
                "oracle_abs_diff": round(abs(bilinear - oracle), 9),
            }
        )

    audit = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_spatial_interpolation_acceptance",
        "primary_anchor": EXCHANGE_ANCHOR,
        "legacy_anchor": LEGACY_ANCHOR,
        "primary_spatial_estimator": "bilinear",
        "robustness_spatial_estimator": "nearest_grid",
        "grid_resolution_degrees": 0.25,
        "raw_zip_sha256": sha256_file(zip_path),
        "raw_artifact": str(zip_path.relative_to(root)),
        "latitude_values": latitudes,
        "longitude_values": longitudes,
        "stencil_corners": {k: {"latitude": v.latitude, "longitude": v.longitude} for k, v in corners.items()},
        "bilinear_weights": {k: round(v, 12) for k, v in w.items()},
        "weight_sum": round(sum(w.values()), 12),
        "nearest_grid_point": nearest_name,
        "nearest_grid_coordinate": {"latitude": nearest_point.latitude, "longitude": nearest_point.longitude},
        "legacy_bilinear_weights": {k: round(v, 12) for k, v in w_legacy.items()},
        "rows": rows,
        "oracle_max_abs_diff": round(max_oracle_diff, 9),
        "oracle_passed": max_oracle_diff < 1e-9,
        "no_outcome_dependence": True,
        "historical_backfill_run": False,
        "code_sha256": {
            "grid_geometry.py": sha256_file(root / "scripts/v2/spatial/grid_geometry.py"),
            "build_taiex_spatial_pilot.py": sha256_file(root / "scripts/v2/spatial/build_taiex_spatial_pilot.py"),
            "build_taiex_spatial_acceptance.py": sha256_file(root / "scripts/v2/spatial/build_taiex_spatial_acceptance.py"),
        },
        "reproducibility": "acceptance audit is deterministic (byte-identical across repeated builds)",
    }
    write_json(root / ACCEPTANCE_AUDIT, audit)

    # Stencil pilot audit: record the raw download evidence (grid, variable,
    # timestamps, raw hash) so the 2x2 stencil provenance is explicit.
    manifest_path = zip_path.with_name(zip_path.name.replace(".zip", ".manifest.json"))
    import json as _json
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    stencil_audit = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_era5_spatial_stencil_pilot",
        "primary_anchor": EXCHANGE_ANCHOR,
        "variable": "total_cloud_cover",
        "netcdf_variable": NETCDF_VARIABLE,
        "grid_resolution_degrees": 0.25,
        "raw_artifact_id": manifest["artifact_id"],
        "raw_sha256": manifest["sha256"],
        "raw_content_length": manifest["content_length"],
        "retrieved_at": manifest["retrieved_at"],
        "request": manifest["request"],
        "stencil_corners": {k: {"latitude": v.latitude, "longitude": v.longitude} for k, v in corners.items()},
        "latitude_values": latitudes,
        "longitude_values": longitudes,
        "returned_grid_is_2x2": len(latitudes) == 2 and len(longitudes) == 2,
        "pre_open_utc_timestamps": stamps,
        "historical_backfill_run": False,
    }
    write_json(root / "data/audits/v2/spatial/taiex-era5-spatial-stencil-pilot.json", stencil_audit)
    print(json.dumps(
        {
            "row_count": len(rows),
            "oracle_max_abs_diff": round(max_oracle_diff, 9),
            "oracle_passed": audit["oracle_passed"],
            "weights": audit["bilinear_weights"],
            "weight_sum": audit["weight_sum"],
            "nearest_grid_point": nearest_name,
            "first_row": rows[0],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
