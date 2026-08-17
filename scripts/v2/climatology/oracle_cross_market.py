"""Stage 5E-3B independent real-raw oracle (hand-written, no production helpers).

For every authorized unit, recompute 3+ sample daily exposures from the
accepted raw with hand-written bilinear + trapezoidal math (NOT the production
helpers), including the required DST-transition samples.  Strict tolerance.
"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.production_quarter import (
    AUTHORIZED_UNITS,
    final_request_id_for,
    load_anchor,
    lookup_accepted_request,
    market_spec,
    quarter_transport,
)
from scripts.v2.core import RawArtifactStore, V2Error, repo_root
from scripts.v2.session_time import resolve_open_window

RAW_BASE = "data/source_raw/v2/climatology"
NETCDF_VAR = "tcc"
ORACLE_TOLERANCE = 1e-6

# Required sample dates per market (transition dates + ordinary days).
ORACLE_DATES: dict[str, list[str]] = {
    "sse_composite": ["1991-09-14", "1991-09-15", "1991-08-15"],
    "ftse100": ["1995-10-21", "1995-10-22", "1995-11-15"],
    "dax": ["1996-10-26", "1996-10-27", "1996-11-15"],
    "nifty50": ["1991-07-01", "1991-08-15", "1991-09-30"],
    "topix": ["2020-02-28", "2020-03-01", "2020-01-15"],
    "szse_component": ["1992-07-01", "1992-08-15", "1992-09-30"],
    "bse50": ["1991-01-01", "1991-02-15", "1991-03-31"],
}


def _hand_bilinear(corner_values: dict[str, float], lat: float, lon: float, corners) -> float:
    sw = corners["SW"]
    se = corners["SE"]
    nw = corners["NW"]
    ne = corners["NE"]
    u = (lat - sw.latitude) / (nw.latitude - sw.latitude)
    v = (lon - sw.longitude) / (se.longitude - sw.longitude)
    return (
        (1 - u) * (1 - v) * corner_values["SW"]
        + (1 - u) * v * corner_values["SE"]
        + u * (1 - v) * corner_values["NW"]
        + u * v * corner_values["NE"]
    )


def _trapezoidal_mean(times: list[datetime], values: list[float], start: datetime, end: datetime) -> float:
    if len(times) != len(values) or len(times) < 2:
        raise V2Error("oracle: unequal or too-short times/values")
    points = sorted(zip(times, values))
    ts = [p[0] for p in points]
    vs = [p[1] for p in points]
    if start < ts[0] or end > ts[-1]:
        raise V2Error("oracle: window not bracketed")

    def at(x: datetime) -> float:
        for i in range(len(ts) - 1):
            if ts[i] <= x <= ts[i + 1]:
                if x == ts[i]:
                    return vs[i]
                if x == ts[i + 1]:
                    return vs[i + 1]
                w = (x - ts[i]).total_seconds() / (ts[i + 1] - ts[i]).total_seconds()
                return vs[i] + w * (vs[i + 1] - vs[i])
        raise V2Error("oracle: outside breakpoints")

    nodes = [start] + [t for t in ts if start < t < end] + [end]
    nv = [at(x) for x in nodes]
    integral = sum(
        0.5 * (nv[i] + nv[i + 1]) * (nodes[i + 1] - nodes[i]).total_seconds() for i in range(len(nodes) - 1)
    )
    return integral / (end - start).total_seconds()


def oracle_for_unit(unit: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    import xarray as xr
    from pandas import to_datetime

    root = root or repo_root()
    request_id = final_request_id_for(unit)
    store = RawArtifactStore(root / RAW_BASE)
    lookup = lookup_accepted_request(store, request_id)
    if not lookup["found"]:
        return {"market_id": unit["market_id"], "period": unit["period"], "available": False}

    anchor = load_anchor(unit["market_id"])
    lat = float(anchor["coordinate"]["latitude"])
    lon = float(anchor["coordinate"]["longitude"])
    spec = market_spec(unit["market_id"])
    from scripts.v2.spatial.grid_geometry import surrounding_cell

    corners = surrounding_cell(lat, lon)
    corner_names = {k: (c.latitude, c.longitude) for k, c in corners.items()}

    samples: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="weather-stock-v2-oracle3b-") as td:
        archive = zipfile.ZipFile(lookup["artifact_path"])
        member = [n for n in archive.namelist() if n.endswith(".nc")][0]
        extracted = Path(td) / member
        extracted.write_bytes(archive.read(member))
        with xr.open_dataset(extracted) as ds:
            var = ds[NETCDF_VAR]
            time_dim = "valid_time" if "valid_time" in ds.dims else "time"
            times = list(to_datetime(ds[time_dim].values))
            utc_times = [t.tz_convert("UTC") if t.tzinfo is not None else t.tz_localize("UTC") for t in times]
            cache: dict[tuple, float] = {}

            def value_at(ts_naive: datetime, corner: str) -> float:
                key = (ts_naive.isoformat(), corner)
                if key not in cache:
                    la, lo = corner_names[corner]
                    cache[key] = float(var.sel({time_dim: ts_naive, "latitude": la, "longitude": lo}).values)
                return cache[key]

            clock = time.fromisoformat(spec["core_open_local"])
            for day_iso in ORACLE_DATES[unit["market_id"]]:
                day = date.fromisoformat(day_iso)
                r = resolve_open_window(day, spec["timezone"], clock, 120)
                support = [datetime.fromisoformat(t) for t in r["hourly_support_timestamps"]]
                start = datetime.fromisoformat(r["window_start_utc"])
                end = datetime.fromisoformat(r["window_end_utc"])
                breakpoints = []
                corner_series = []
                for ts in support:
                    breakpoints.append(ts)
                    corner_series.append({name: value_at(ts.replace(tzinfo=None), name) for name in corner_names})
                hourly = [_hand_bilinear(cs, lat, lon, corners) for cs in corner_series]
                oracle_mean = _trapezoidal_mean(breakpoints, hourly, start, end)
                samples.append(
                    {
                        "date": day_iso,
                        "oracle_tcc_pct": oracle_mean * 100.0,
                        "offset": r["utc_offset_str"],
                        "window": {"start_utc": start.isoformat(), "end_utc": end.isoformat()},
                    }
                )
    return {
        "market_id": unit["market_id"],
        "period": unit["period"],
        "available": True,
        "sample_dates": samples,
        "tolerance": ORACLE_TOLERANCE,
    }


def compare_with_production(unit: dict[str, Any], oracle: dict[str, Any], production_rows: list[dict[str, Any]]) -> dict[str, Any]:
    prod_by_date = {r["date"]: r["tcc_pct"] for r in production_rows}
    comparison = []
    max_diff = 0.0
    for sample in oracle["sample_dates"]:
        day = sample["date"]
        prod = prod_by_date.get(day)
        diff = abs(sample["oracle_tcc_pct"] - prod) if prod is not None else None
        max_diff = max(max_diff, diff or 0.0)
        comparison.append(
            {
                "date": day,
                "offset": sample["offset"],
                "oracle_tcc_pct": round(sample["oracle_tcc_pct"], 8),
                "production_tcc_pct": round(prod, 8) if prod is not None else None,
                "abs_diff": round(diff, 10) if diff is not None else None,
                "pass": diff is not None and diff <= oracle["tolerance"],
            }
        )
    return {"comparison": comparison, "max_abs_diff": round(max_diff, 12), "pass": all(c["pass"] for c in comparison)}
