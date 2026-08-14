"""Stage 5E-2: 1991-2020 ERA5 backfill planner (pure, no network).

Builds the engineering budget for the future climatology backfill:
per market x 30 years x 365 common-calendar days, the historical UTC exposure
window and temporal-support timestamps are computed with the frozen
Stage 5B clocks + historical IANA mapping (tzdata 2026.3 via the runtime
guard).  Four timestamp layers are kept strictly separate:
  scientific_daily_exposures
  scientific_temporal_support_timestamps
  transport_requested_timestamps   (CDS date x time Cartesian)
  raw_expected_timestamps          (== transport_requested at planning level)

Spatial geometry is assumed to be four grid points per market for COUNTS ONLY;
this is NOT a live CDS request area (global spatial anchors are unresolved).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterator

from scripts.v2.climatology.common_calendar import common_day_to_actual_date
from scripts.v2.core_open_regimes import all_market_ids, resolve_core_open
from scripts.v2.session_time import resolve_open_window
from scripts.v2.timezone_provider import validate_provider_runtime

BASELINE_START = date(1991, 1, 1)
BASELINE_END = date(2020, 12, 31)
COMMON_DAYS = 365
YEARS = range(1991, 2021)
GRID_POINTS_PER_MARKET = 4
SPATIAL_GEOMETRY_ASSUMPTION = "four_grid_points_per_market"
WINDOW_MINUTES = 120


def research_markets() -> list[str]:
    """The 8 formal research markets (order = core-open registry order)."""
    return all_market_ids()


def market_spec(market_id: str) -> dict[str, Any]:
    """Frozen target-regime clock for a market (Stage 5B-2 registry)."""
    r = resolve_core_open(market_id, "2023-06-15")
    return {
        "market_id": market_id,
        "timezone": r["timezone"],
        "core_open_local": r["core_open_local"],
    }


def daily_exposure_rows(market_id: str) -> Iterator[dict[str, Any]]:
    """Yield one row per common-calendar day over the baseline period.

    Row: {year, common_doy, actual_date, timezone, core_open_local, offset_str,
    window_start_utc, window_end_utc, support_utc (list of ISO)}.
    """
    spec = market_spec(market_id)
    tz_name = spec["timezone"]
    clock = time.fromisoformat(spec["core_open_local"])
    for year in YEARS:
        for doy in range(1, COMMON_DAYS + 1):
            actual = common_day_to_actual_date(doy, year)
            r = resolve_open_window(actual, tz_name, clock, WINDOW_MINUTES)
            yield {
                "year": year,
                "common_doy": doy,
                "actual_date": actual.isoformat(),
                "timezone": tz_name,
                "core_open_local": spec["core_open_local"],
                "offset_str": r["utc_offset_str"],
                "window_start_utc": r["window_start_utc"],
                "window_end_utc": r["window_end_utc"],
                "support_utc": r["hourly_support_timestamps"],
            }


def market_scientific_counts(market_id: str) -> dict[str, Any]:
    """Scientific layer counts: daily exposures + support timestamps.

    ``support_count`` is the SUM of per-day support lengths; ``support_set`` is
    the number of UNIQUE support timestamps.  A Feb 29 never appears as an
    exposure date (common calendar), but its UTC hours may appear in support.
    """
    validate_provider_runtime()
    support_set: set[str] = set()
    support_sum = 0
    exposure_count = 0
    feb29_in_support = 0
    per_year: dict[int, dict[str, int]] = {}
    for row in daily_exposure_rows(market_id):
        exposure_count += 1
        support_sum += len(row["support_utc"])
        support_set.update(row["support_utc"])
        per_year.setdefault(row["year"], {"exposures": 0, "support": 0})
        per_year[row["year"]]["exposures"] += 1
        per_year[row["year"]]["support"] += len(row["support_utc"])
        # Feb 29 may appear as a support UTC timestamp (cross-date bracket)
        if any(t.startswith(f"{row['year']}-02-29") for t in row["support_utc"]):
            feb29_in_support += 1
    return {
        "market_id": market_id,
        "daily_exposure_count": exposure_count,
        "support_count_sum": support_sum,
        "support_set_count": len(support_set),
        "days_with_feb29_support": feb29_in_support,
        "per_year": per_year,
    }


def _batch_key(year: int, month: int, strategy: str) -> tuple:
    if strategy == "yearly":
        return (year,)
    if strategy == "quarterly":
        return (year, (month - 1) // 3 + 1)
    if strategy == "monthly":
        return (year, month)
    raise ValueError(f"unknown strategy {strategy}")


def batch_plan(market_id: str, strategy: str) -> dict[str, Any]:
    """Transport-layer plan for one batching strategy.

    Groups daily exposures into request units (monthly/quarterly/yearly), then
    for each unit computes the CDS date x time Cartesian transport, the support
    subset, extras, amplification, and CDS field count (TCC-only, 1 level).
    """
    validate_provider_runtime()
    units: dict[tuple, dict[str, Any]] = {}
    for row in daily_exposure_rows(market_id):
        key = _batch_key(row["year"], int(row["actual_date"][5:7]), strategy)
        unit = units.setdefault(
            key,
            {"dates": set(), "times": set(), "support": set(), "days": 0},
        )
        unit["days"] += 1
        for ts in row["support_utc"]:
            unit["dates"].add(ts[:10])
            unit["times"].add(ts[11:16])
            unit["support"].add(ts)

    batches: list[dict[str, Any]] = []
    for key in sorted(units):
        u = units[key]
        dates = sorted(u["dates"])
        times = sorted(u["times"])
        transport = len(dates) * len(times)
        support = len(u["support"])
        extras = transport - support
        batches.append(
            {
                "batch_id": "_".join(str(k) for k in key),
                "year": key[0],
                "period": f"{key[0]}-{key[1]:02d}" if len(key) == 2 else str(key[0]),
                "days": u["days"],
                "request_dates": dates,
                "request_times": times,
                "requested_date_count": len(dates),
                "requested_time_union_count": len(times),
                "support_count": support,
                "transport_count": transport,
                "extras_count": extras,
                "amplification": round(transport / support, 6) if support else None,
                "cds_field_count": transport,  # TCC-only: 1 var x 1 level x timesteps
                "grid_cell_values": transport * GRID_POINTS_PER_MARKET,
            }
        )
    return {
        "market_id": market_id,
        "strategy": strategy,
        "request_count": len(batches),
        "batches": batches,
    }


def strategy_summary(strategy: str, markets: list[str] | None = None) -> dict[str, Any]:
    markets = markets or research_markets()
    totals = {
        "request_count": 0,
        "transport_count": 0,
        "support_count": 0,
        "extras_count": 0,
        "cds_field_count": 0,
        "grid_cell_values": 0,
    }
    per_market = []
    all_amps: list[float] = []
    all_fields: list[int] = []
    for m in markets:
        plan = batch_plan(m, strategy)
        amps = [b["amplification"] for b in plan["batches"] if b["amplification"]]
        fields = [b["cds_field_count"] for b in plan["batches"]]
        all_amps.extend(amps)
        all_fields.extend(fields)
        sub = {
            "market_id": m,
            "request_count": plan["request_count"],
            "transport_count": sum(b["transport_count"] for b in plan["batches"]),
            "support_count": sum(b["support_count"] for b in plan["batches"]),
            "extras_count": sum(b["extras_count"] for b in plan["batches"]),
            "cds_field_count": sum(b["cds_field_count"] for b in plan["batches"]),
            "grid_cell_values": sum(b["grid_cell_values"] for b in plan["batches"]),
            "max_fields_per_request": max(fields),
            "median_fields_per_request": sorted(fields)[len(fields) // 2],
            "p95_fields_per_request": sorted(fields)[int(len(fields) * 0.95)],
            "median_amplification": sorted(amps)[len(amps) // 2],
        }
        per_market.append(sub)
        for k in totals:
            totals[k] += sub[k]
    return {
        "strategy": strategy,
        "markets": per_market,
        "totals": totals,
        "aggregate_max_fields_per_request": max(p["max_fields_per_request"] for p in per_market),
    }


def planning_request_id(market_id: str, period: str, payload: dict[str, Any]) -> str:
    """Deterministic planning request id (NOT a live immutable id).

    The live request id must additionally bind the spatial anchor hash, which
    is unresolved until global spatial qualification.
    """
    canonical = json.dumps(
        {"market_id": market_id, "period": period, **payload},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
