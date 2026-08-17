"""Stage 5E-3B cross-market production qualification audit builder.

Records the service-health snapshot, authorization matrix, pure planning
matrix, request identity matrix, per-market raw/derived/oracle/idempotency
results, aggregate counts, and the formal-unit accounting.

If the stage was deferred for CDS service degradation, the builder still
emits a deterministic preflight/defer audit (research_usable stays false).
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.production_quarter import (
    AUDIT_PATH,
    AUTHORIZED_UNITS,
    EXPECTED_AGGREGATE,
    EXPECTED_TOTAL_FORMAL_UNITS,
    SP500_EXISTING_UNIT,
    aggregate_counts,
    cds_request_for,
    extract_unit_exposures,
    final_request_id_for,
    identity_payload,
    lookup_accepted_request,
    market_spec,
    quarter_daily_plan,
    quarter_transport,
    unit_plan_counts,
)
from scripts.v2.climatology.production_canary import (
    CANARY_DATASET,
    EXPECTED_TIMEZONE_CANARY_HASH,
    REQUEST_IDENTITY_CONTRACT_VERSION,
    load_global_registry_hash,
    load_timezone_fingerprint,
)
from scripts.v2.core import RawArtifactStore, repo_root, write_json

RAW_BASE = "data/source_raw/v2/climatology"


def service_health_snapshot() -> dict[str, Any]:
    """Live re-check of the official CDS service status (recorded, no retrieve)."""
    return {
        "service_health_checked_at": "2026-08-14T19:31:00+08:00",
        "dataset_catalog_status": "available",
        "service_status_source": (
            "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels "
            "(overview + download tabs fetched 2026-08-14 19:31 CST; "
            "cads:sanity_check status=available, timestamp=2026-08-14T11:16:42Z; "
            "two independent fetches identical); "
            "https://cds.climate.copernicus.eu/live (140 dataset sanity checks: "
            "134 available / 3 down / 2 warning / 1 expired; target dataset available; "
            "control layer earlier observed a 'Degraded access to the data' banner, "
            "re-checked live before execution)"
        ),
        "control_layer_observed_banner": "degraded_access_to_the_data (earlier in 2026-08-14)",
        "live_recheck_status": "available",
        "dataset_available": True,
    }


def build(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    agg = aggregate_counts()
    store = RawArtifactStore(root / RAW_BASE)

    per_market: dict[str, Any] = {}
    raw_artifacts = []
    derived_entries = []
    for unit in AUTHORIZED_UNITS:
        mid = unit["market_id"]
        request_id = final_request_id_for(unit)
        lookup = lookup_accepted_request(store, request_id)
        c = unit_plan_counts(unit)
        derived = None
        if lookup["found"]:
            try:
                derived = extract_unit_exposures(unit, root)
            except Exception as exc:
                derived = {"error": f"{type(exc).__name__}: {exc}", "row_count": 0, "missing_count": None}
        per_market[mid] = {
            "market_id": mid,
            "period": unit["period"],
            "timezone": market_spec(mid)["timezone"],
            "core_open_local": market_spec(mid)["core_open_local"],
            "final_request_id": request_id,
            "planning": {
                "exposures": c["daily_exposures"],
                "support": c["support"],
                "transport": c["transport"],
                "extras": c["extras"],
                "fields": c["fields"],
                "amplification": c["amplification"],
                "first_transport_date": c["first_transport_date"],
                "last_transport_date": c["last_transport_date"],
                "request_times": c["request_times"],
                "feb29_daily_exposure": c["feb29_daily_exposure"],
                "feb29_transport_support_present": c["feb29_transport_support_present"],
            },
            "raw": {
                "accepted": lookup["found"],
                "artifact_id": lookup.get("artifact_id") if lookup["found"] else None,
                "sha256": lookup.get("sha256") if lookup["found"] else None,
                "bytes": lookup.get("bytes") if lookup["found"] else None,
            },
            "derived": (
                {
                    "row_count": derived.get("row_count"),
                    "missing": derived.get("missing_count"),
                    "first_date": derived.get("first_date"),
                    "last_date": derived.get("last_date"),
                    "consumed_support": derived.get("firewall", {}).get("consumed_support"),
                    "consumed_extras": derived.get("firewall", {}).get("consumed_extras"),
                }
                if derived
                else None
            ),
            "oracle": None,  # populated by the runner; see stage execution notes
            "idempotency": None,
        }
        if lookup["found"]:
            raw_artifacts.append(
                {"market_id": mid, "period": unit["period"], "sha256": lookup["sha256"], "bytes": lookup["bytes"]}
            )
        if derived and derived.get("row_count"):
            derived_entries.append(
                {"market_id": mid, "period": unit["period"], "rows": derived["row_count"], "missing": derived["missing_count"]}
            )

    new_qualified = sum(1 for m in per_market.values() if m["raw"]["accepted"] and m["derived"] and m["derived"]["missing"] == 0)
    formal_units_total = 1 + new_qualified  # sp500 + new
    remaining = EXPECTED_TOTAL_FORMAL_UNITS - formal_units_total

    audit = {
        "schema_version": "2.0.0",
        "audit_type": "cross_market_production_qualification",
        "scope": "Stage 5E-3B: 7 authorized quarterly production units + existing sp500 2007Q1",
        "request_identity_contract": REQUEST_IDENTITY_CONTRACT_VERSION,
        "timezone_canary_hash": EXPECTED_TIMEZONE_CANARY_HASH,
        "service_health": service_health_snapshot(),
        "authorization": {
            "stage_authorized": True,
            "new_authorized_units": 7,
            "execution_order": ["sse_composite", "ftse100", "dax", "nifty50", "topix", "szse_component", "bse50"],
            "sp500_re_required_denied": True,
        },
        "existing_qualified": {"sp500": SP500_EXISTING_UNIT["period"]},
        "per_market": per_market,
        "planning_aggregate": agg["totals"],
        "planning_expected": EXPECTED_AGGREGATE,
        "planning_match": agg["totals"] == EXPECTED_AGGREGATE,
        "raw_artifacts": raw_artifacts,
        "derived": derived_entries,
        "aggregate": {
            "new_units_qualified": new_qualified,
            "formal_units_qualified_total": formal_units_total,
            "remaining_unfetched_units": remaining,
            "live_calls_this_stage": None,  # runner records actual
            "full_backfill_started": False,
            "full_climatology_started": False,
            "cloudz_generated": False,
        },
        "gates": {
            "production_markets_qualified": "8_of_8" if new_qualified == 7 else f"{new_qualified+1}_of_8",
            "cross_market_production_qualified": new_qualified == 7,
            "live_backfill_authorized": False,
            "research_ready": False,
            "frozen": False,
        },
        "historical_backfill_run": False,
    }
    write_json(root / AUDIT_PATH, audit)
    print(
        json.dumps(
            {
                "audit_path": AUDIT_PATH,
                "formal_units_qualified_total": formal_units_total,
                "remaining_unfetched_units": remaining,
                "planning_match": audit["planning_match"],
            },
            ensure_ascii=False,
        )
    )
    return audit


if __name__ == "__main__":
    build()
    raise SystemExit(0)
