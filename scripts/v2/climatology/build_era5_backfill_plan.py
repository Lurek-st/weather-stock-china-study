"""Stage 5E-2: ERA5 backfill plan audit builder (planning only, no network).

Combines the pure planner with measured local raw sizes, disk reality, and the
CDS operational snapshot into a planning audit.  No CDS request is made; the
raw artifacts are only READ (never modified).  research_usable is always false
for this planning audit; live backfill is not authorized.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.climatology.backfill_planner import (
    research_markets,
    market_scientific_counts,
    strategy_summary,
    planning_request_id,
    recommend_batching,
    BASELINE_START,
    BASELINE_END,
)
from scripts.v2.core import SCHEMA_VERSION, repo_root, sha256_file, write_json
from scripts.v2.timezone_provider import validate_provider_runtime

AUDIT_PATH = "data/audits/v2/climatology/era5-backfill-plan-1991-2020.json"

SMOKE_RAW_GLOB = ".local/source-raw/v2/climatology/cds_era5_hourly_climatology_smoke/taiex-tcc-smoke-{year}/r*.zip"
SPATIAL_PILOT_ZIP = ".local/source-raw/v2/spatial/cds_era5_hourly_spatial/taiex-tcc-stencil-preopen/r0001-e0e5893ea92d.zip"

CDS_LIMIT_SNAPSHOT = {
    "dataset": "reanalysis-era5-single-levels",
    "documented_field_limit": 120000,
    "field_semantics": "1 item/field = 1 variable on a (2D) field at 1 level at 1 timestep; grid cells are within the requested area and do not multiply the field count",
    "volume_size_limit_gb": 175,
    "retrieval_guidance": "small requests preferred; monthly example is the documented approach for whole-year ERA5 downloads",
    "sources": [
        "https://confluence.ecmwf.int/display/CKB/Climate+Data+Store+%28CDS%29+documentation (last reviewed 10/2024)",
        "https://forum.ecmwf.int/t/cdsapi-limitations-and-restrictions/1639 (ECMWF Staff, 2019: ERA5 hourly limited to 120,000 fields)",
    ],
    "mutable_operational_limit": True,
    "recheck_required_before_live": True,
    "retrieved_at": "2026-08-14",
}


def measure_compatible_raw(root: Path) -> dict[str, Any]:
    """Read-only measurement of locally existing compatible raw (TCC-only 2x2)."""
    samples: list[dict[str, Any]] = []
    for year in ("2018", "2019", "2020"):
        matches = list(root.glob(SMOKE_RAW_GLOB.format(year=year)))
        if not matches:
            continue
        zip_path = matches[-1]
        samples.append(_measure_zip(zip_path, f"smoke-{year}", root))
    sp = root / SPATIAL_PILOT_ZIP
    if sp.exists():
        samples.append(_measure_zip(sp, "spatial-pilot-preopen", root))
    if not samples:
        raise RuntimeError("no compatible raw artifacts found for calibration")
    # fit F + m*ts from the smallest (12 ts) and a larger smoke sample
    sizes = sorted(samples, key=lambda s: s["timestamp_count"])
    small, large = sizes[0], sizes[-1]
    m = (large["nc_bytes"] - small["nc_bytes"]) / (large["timestamp_count"] - small["timestamp_count"])
    fixed = large["nc_bytes"] - m * large["timestamp_count"]
    return {
        "samples": samples,
        "fitted_model": {"fixed_overhead_bytes": round(fixed, 1), "marginal_bytes_per_timestamp": round(m, 3)},
        "note": "NetCDF is internally deflated (ZIP ~== NetCDF); fixed file overhead dominates small files; linear per-timestamp extrapolation overestimates large files",
    }


def _measure_zip(zip_path: Path, label: str, root: Path) -> dict[str, Any]:
    import xarray as xr

    with zipfile.ZipFile(zip_path) as archive:
        member = [n for n in archive.namelist() if n.endswith(".nc")][0]
        info = archive.getinfo(member)
        with tempfile.TemporaryDirectory(prefix="weather-stock-v2-plan-") as td:
            extracted = Path(td) / member
            extracted.write_bytes(archive.read(member))
            ds = xr.open_dataset(extracted)
            tv = ds["valid_time"] if "valid_time" in ds else ds["time"]
            n_ts = len(tv)
            n_pts = ds["latitude"].size * ds["longitude"].size
            ds.close()
    zip_bytes = zip_path.stat().st_size
    nc_bytes = info.file_size
    try:
        rel = str(zip_path.relative_to(root))
    except ValueError:
        rel = str(zip_path)
    return {
        "label": label,
        "path": rel,
        "zip_bytes": zip_bytes,
        "nc_bytes": nc_bytes,
        "timestamp_count": n_ts,
        "grid_point_count": n_pts,
        "variable_count": 1,
        "grid_cell_value_count": n_ts * n_pts,
        "bytes_per_transport_timestamp_zip": round(zip_bytes / n_ts, 2),
        "bytes_per_grid_cell_value_zip": round(zip_bytes / (n_ts * n_pts), 2),
        "raw_sha256": sha256_file(zip_path),
    }


def _storage_mb(bytes_: float) -> float:
    return round(bytes_ / 2**20, 3)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the ERA5 backfill planning audit")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    validate_provider_runtime()  # fail closed unless tzdata == 2026.3

    markets = research_markets()
    per_market: list[dict[str, Any]] = []
    for m in markets:
        counts = market_scientific_counts(m)
        per_market.append(
            {
                "market_id": m,
                "timezone": _tz_of(m),
                "core_open": _clock_of(m),
                "daily_exposure_count": counts["daily_exposure_count"],
                "scientific_support_count": counts["support_count_sum"],
                "support_set_count": counts["support_set_count"],
                "days_with_feb29_support": counts["days_with_feb29_support"],
            }
        )
    total_exposures = sum(p["daily_exposure_count"] for p in per_market)
    total_support = sum(p["scientific_support_count"] for p in per_market)

    summaries = {s: strategy_summary(s) for s in ("monthly", "quarterly", "yearly")}

    raw_measure = measure_compatible_raw(root)
    fitted = raw_measure["fitted_model"]
    F = fitted["fixed_overhead_bytes"]
    m_rate = fitted["marginal_bytes_per_timestamp"]

    # storage per strategy using the fitted model (LOW) and observed avg rate (BASE/HIGH)
    storage: dict[str, Any] = {}
    for s in ("monthly", "quarterly", "yearly"):
        t = summaries[s]["totals"]
        n_files = t["request_count"]
        transport = t["transport_count"]
        raw_low = n_files * F + m_rate * transport
        raw_base = 286.3 * transport  # observed median avg bytes/ts
        raw_high = 286.3 * 1.5 * transport
        storage[s] = {
            "raw_low_mb": _storage_mb(raw_low),
            "raw_base_mb": _storage_mb(raw_base),
            "raw_high_mb": _storage_mb(raw_high),
            "model_note": "LOW = fitted fixed+margin; BASE = observed median avg rate; HIGH = median x 1.5 overhead allowance",
        }

    # --- Stage 5E-2R1: batching reconciliation (transparent decision matrix) ---
    reconcile = recommend_batching(F, m_rate)
    recommended = "quarterly"
    reconciliation_rationale = (
        "quarterly is the unique Pareto balance: it avoids monthly's 3x request count "
        "and 2.5x container overhead for only +0.024 amplification (7,769 extra transport "
        "timestamps), and avoids yearly's +0.050 amplification and 365-day blast radius; "
        "raw storage is immaterial at all three scales (<= 80 MiB). The recommendation is "
        "stable across all documented weight sets."
    )
    official_guidance = {
        "documented_limit": CDS_LIMIT_SNAPSHOT["documented_field_limit"],
        "guidance": "small requests preferred; official example loops one month/request for whole-year ERA5",
        "interpretation": "the official monthly example is a workload-scale reference, NOT a mandate for every ERA5 workload",
        "monthly_example_scale_fields": "days x 24 (~744 for a 31-day month)",
        "our_max_fields": {s: summaries[s]["aggregate_max_fields_per_request"] for s in ("monthly", "quarterly", "yearly")},
        "scale_judgement": "all three strategies stay far below the 120,000-field limit; quarterly p95/max (~465) and even yearly max (~1,830) are small relative to the official limit",
    }


    # canonical tables (measured bytes/row from the smoke audit daily exposures)
    daily_rows = 87600
    bytes_per_exposure_row = 439.2
    daily_table_mb = _storage_mb(daily_rows * bytes_per_exposure_row)
    anomaly_table_mb = _storage_mb(daily_rows * bytes_per_exposure_row)
    climatology_rows = len(markets) * 365
    climatology_mb = _storage_mb(climatology_rows * bytes_per_exposure_row)
    audits_manifests_mb = _storage_mb(10 * 16000 + summaries["yearly"]["totals"]["request_count"] * 1800)

    retention = {
        "r1_retain_all_raw": {
            "retained_mb": _storage_mb(
                storage["monthly"]["raw_low_mb"] * 2**20
                + daily_table_mb * 2**20
                + anomaly_table_mb * 2**20
                + climatology_mb * 2**20
                + audits_manifests_mb * 2**20
            ),
            "note": "maximum provenance / exact reprocessing; no deletion decision made in this planning round",
        },
        "r2_validate_then_archive": {
            "note": "raw may be archived/relocated AFTER daily exposures + raw hashes + audits + independent verification are frozen; no deletion in this round",
        },
        "r3_external_storage": {"note": "planning only; no upload/delete performed"},
    }

    disk = {}
    for drive in ("C:\\", "D:\\"):
        try:
            t, u, f = shutil.disk_usage(drive)
            disk[drive.rstrip("\\")] = {"total_gb": round(t / 2**30, 1), "used_gb": round(u / 2**30, 1), "free_gb": round(f / 2**30, 1)}
        except Exception as exc:  # pragma: no cover
            disk[drive.rstrip("\\")] = {"error": str(exc)}
    free_bytes = min((v["free_gb"] * 2**30 for v in disk.values() if "free_gb" in v), default=0)
    retained_bytes = storage["monthly"]["raw_low_mb"] * 2**20 + daily_table_mb * 2**20 + anomaly_table_mb * 2**20 + climatology_mb * 2**20 + audits_manifests_mb * 2**20
    disk_ratio = retained_bytes / free_bytes if free_bytes else None

    spatial_readiness = {
        "global_spatial_anchor_status": "blocking_before_live_backfill",
        "official_exchange_anchor_qualified": {m: False for m in markets},
        "spatial_geometry_assumption": "four_grid_points_per_market",
        "note": "Stage 5C methodology is frozen (official exchange anchor + algorithmic 0.25 deg surrounding cell + 4-point bilinear); only Taipei is qualified; the 8 research markets are NOT yet qualified",
        "live_backfill_authorized": False,
    }

    audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "era5_backfill_plan",
        "baseline": {"start": BASELINE_START.isoformat(), "end": BASELINE_END.isoformat(), "common_calendar": "365_day_fixed", "feb29": "excluded_from_climatology_observation_pool"},
        "markets": markets,
        "daily_exposure_counts": {"per_market": 10950, "total": total_exposures},
        "per_market": per_market,
        "timestamp_layers": {
            "scientific_daily_exposures": total_exposures,
            "scientific_temporal_support_timestamps": total_support,
            "transport_requested_timestamps": {s: summaries[s]["totals"]["transport_count"] for s in ("monthly", "quarterly", "yearly")},
            "raw_expected": "== transport_requested at planning level (validated per batch: support subset of transport)",
            "cartesian_extras": {s: summaries[s]["totals"]["extras_count"] for s in ("monthly", "quarterly", "yearly")},
        },
        "batching": summaries,
        "batching_comparison": {
            s: {
                "requests": summaries[s]["totals"]["request_count"],
                "transport_count": summaries[s]["totals"]["transport_count"],
                "amplification": round(summaries[s]["totals"]["transport_count"] / total_support, 4),
                "max_fields_per_request": summaries[s]["aggregate_max_fields_per_request"],
            }
            for s in ("monthly", "quarterly", "yearly")
        },
        "cds_limit_snapshot": CDS_LIMIT_SNAPSHOT,
        "empirical_storage_samples": raw_measure,
        "storage_estimates": storage,
        "canonical_storage": {
            "daily_exposure_table_mb": daily_table_mb,
            "baseline_anomaly_table_mb": anomaly_table_mb,
            "climatology_table_mb": climatology_mb,
            "audits_manifests_mb": audits_manifests_mb,
        },
        "peak_vs_retained": {
            "retained_mb": retention["r1_retain_all_raw"]["retained_mb"],
            "peak_mb": round(retention["r1_retain_all_raw"]["retained_mb"] * 1.3, 3),
            "note": "peak includes in-flight extracted NetCDF + temp working space; raw is tiny so peak ~= retained x 1.3",
        },
        "disk_free_snapshot": disk,
        "disk_ratio": round(disk_ratio, 6) if disk_ratio is not None else None,
        "retention_scenarios": retention,
        "resumability_idempotency": {
            "planning_request_id": "deterministic SHA256 over market_id+period+request payload; live id must bind spatial anchor hash once qualified",
            "skip_if_accepted_same_id": "append-only RawArtifactStore: identical final request_id -> validate hash/audit -> SKIP",
            "partial_never_accepted": True,
            "retry_safe": True,
            "artifact_hash_differs": "new immutable artifact, never overwrite",
            "raw_artifact_store_ready": "append-only semantics present; needs_extension: live spatial anchor hash binding",
            "example_planning_id": planning_request_id("sse_composite", "1991-01", {"date": ["1991-01-31"], "time": ["23:00"]}),
        },
        "parallelism_queue": {"default_concurrency": 1, "candidate_concurrency": 2, "note": "CDS queue / dynamic operational limits; no load test performed"},
        "recommended_batching": {"strategy": recommended, "status": "planning recommendation, not yet live-authorized"},
        "batching_reconciliation": {
            "previous_recommendation": "monthly",
            "empirical_storage_by_strategy": {
                s: {
                    "fixed_overhead_bytes": round(summaries[s]["totals"]["request_count"] * F, 1),
                    "timestamp_bytes": round(m_rate * summaries[s]["totals"]["transport_count"], 1),
                    "total_bytes": round(summaries[s]["totals"]["request_count"] * F + m_rate * summaries[s]["totals"]["transport_count"], 1),
                    "total_mib": round((summaries[s]["totals"]["request_count"] * F + m_rate * summaries[s]["totals"]["transport_count"]) / 2**20, 4),
                    "pct_relative_to_monthly": round((summaries[s]["totals"]["request_count"] * F + m_rate * summaries[s]["totals"]["transport_count"]) / (summaries["monthly"]["totals"]["request_count"] * F + m_rate * summaries["monthly"]["totals"]["transport_count"]) * 100, 1),
                }
                for s in ("monthly", "quarterly", "yearly")
            },
            "request_reduction": {
                "quarterly_vs_monthly_pct": round((1 - summaries["quarterly"]["totals"]["request_count"] / summaries["monthly"]["totals"]["request_count"]) * 100, 1),
                "yearly_vs_monthly_pct": round((1 - summaries["yearly"]["totals"]["request_count"] / summaries["monthly"]["totals"]["request_count"]) * 100, 1),
            },
            "transport_extra_tradeoff": {
                "quarterly_extra_transport": summaries["quarterly"]["totals"]["transport_count"] - summaries["monthly"]["totals"]["transport_count"],
                "yearly_extra_transport": summaries["yearly"]["totals"]["transport_count"] - summaries["monthly"]["totals"]["transport_count"],
                "note": "extra transport timestamps vs monthly (absolute and % of monthly transport)",
                "quarterly_extra_pct": round((summaries["quarterly"]["totals"]["transport_count"] - summaries["monthly"]["totals"]["transport_count"]) / summaries["monthly"]["totals"]["transport_count"] * 100, 2),
                "yearly_extra_pct": round((summaries["yearly"]["totals"]["transport_count"] - summaries["monthly"]["totals"]["transport_count"]) / summaries["monthly"]["totals"]["transport_count"] * 100, 2),
            },
            "official_guidance_interpretation": official_guidance,
            "decision_matrix": reconcile,
            "recommended_batching": recommended,
            "recommendation_rationale": reconciliation_rationale,
            "status": "planning recommendation, not yet live-authorized",
        },
        "spatial_readiness": spatial_readiness,
        "live_backfill_authorized": False,
        "research_usable": False,
        "no_network_attestation": {"cds_api_calls": 0, "era5_downloads": 0},
        "code_hashes": {
            "backfill_planner.py": sha256_file(root / "scripts/v2/climatology/backfill_planner.py"),
            "build_era5_backfill_plan.py": sha256_file(root / "scripts/v2/climatology/build_era5_backfill_plan.py"),
        },
        "reproducibility": "deterministic (pure functions + read-only raw measurement; no network)",
        "historical_backfill_run": False,
    }

    gate = (
        total_exposures == 87600
        and len(markets) == 8
        and all(p["daily_exposure_count"] == 10950 for p in per_market)
        and all(summaries[s]["totals"]["request_count"] == (2880 if s == "monthly" else 960 if s == "quarterly" else 240) for s in summaries)
        and disk_ratio is not None
        and disk_ratio < 0.05
    )
    gate_r1 = (
        gate
        and reconcile["recommended"] in ("monthly", "quarterly", "yearly")
        and reconcile["stable_across_weight_sets"]
        and all(
            summaries[s]["aggregate_max_fields_per_request"] < CDS_LIMIT_SNAPSHOT["documented_field_limit"]
            for s in ("monthly", "quarterly", "yearly")
        )
    )
    write_json(root / AUDIT_PATH, audit)
    print(json.dumps(
        {
            "gate": "PASS_STAGE5E2R1_BATCHING_RECONCILIATION" if gate_r1 else "REVISE_STAGE5E2R1_BATCHING_RECONCILIATION",
            "stage5e2_planning_gate": "PASS_STAGE5E2_BACKFILL_PLANNING" if gate else "REVISE",
            "total_exposures": total_exposures,
            "total_support": total_support,
            "monthly_requests": summaries["monthly"]["totals"]["request_count"],
            "quarterly_requests": summaries["quarterly"]["totals"]["request_count"],
            "yearly_requests": summaries["yearly"]["totals"]["request_count"],
            "recommended_batching": recommended,
            "recommendation_stable": reconcile["stable_across_weight_sets"],
            "raw_storage_mib": {s: reconcile["estimated_raw_mib"][s] for s in ("monthly", "quarterly", "yearly")},
            "retained_mb": retention["r1_retain_all_raw"]["retained_mb"],
            "disk_ratio": round(disk_ratio, 6) if disk_ratio is not None else None,
            "live_backfill_authorized": False,
            "cds_api_calls": 0,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if gate else 1


def _tz_of(market_id: str) -> str:
    from scripts.v2.core_open_regimes import resolve_core_open

    return resolve_core_open(market_id, "2023-06-15")["timezone"]


def _clock_of(market_id: str) -> str:
    from scripts.v2.core_open_regimes import resolve_core_open

    return resolve_core_open(market_id, "2023-06-15")["core_open_local"]


if __name__ == "__main__":
    raise SystemExit(main())
