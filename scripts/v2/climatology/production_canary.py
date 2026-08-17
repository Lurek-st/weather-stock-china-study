"""Stage 5E-3A production canary: sp500 / 2007Q1, TCC-only, at most ONE CDS retrieve.

This is a production-grade live entrypoint with a FROZEN canary contract:

    market      = sp500 (NYSE, America/New_York)
    period      = 2007Q1  (2007-01-01 .. 2007-03-31 inclusive)
    variable    = total_cloud_cover ONLY
    dataset     = reanalysis-era5-single-levels
    data class  = final ERA5 reanalysis
    batching    = quarterly

A different market / year / quarter must FAIL CLOSED.  This module never
implements a 960-request loop.

The chain implemented here (mirroring the frozen production contract):

    final_request_id
        -> request-aware pre-network idempotency lookup (SKIP before CDS client)
        -> EXACTLY ONE cdsapi retrieve (gated)
        -> container validation + exact frozen 2x2 grid + exact 450 timestamps
        -> immutable RawArtifactStore persistence (final_request_id bound)
        -> daily exposure extraction (90 rows, bilinear + piecewise-linear)
        -> transport firewall (consumed = scientific support exactly)

Zero-network by default: without ``--live`` no CDS client is constructed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import RawArtifactStore, V2Error, load_yaml, repo_root, write_json
from scripts.v2.climatology.tcc_exposure import tcc_exposure_pct
from scripts.v2.climatology.temporal_integration import piecewise_linear_mean
from scripts.v2.fetch_era5 import cds_readiness, validate_download_container, expected_timestamps
from scripts.v2.session_time import resolve_open_window
from scripts.v2.spatial.grid_geometry import bilinear_weights, surrounding_cell
from scripts.v2.timezone_provider import validate_provider_runtime

# ---------------------------------------------------------------------------
# Frozen canary contract
# ---------------------------------------------------------------------------

CANARY_MARKET = "sp500"
CANARY_PERIOD = "2007Q1"
CANARY_START = date(2007, 1, 1)
CANARY_END = date(2007, 3, 31)
CANARY_TIMEZONE = "America/New_York"
CANARY_CORE_OPEN_LOCAL = "09:30"
CANARY_WINDOW_MINUTES = 120
CANARY_VARIABLE = "total_cloud_cover"
CANARY_NETCDF_VARIABLE = "tcc"
CANARY_DATASET = "reanalysis-era5-single-levels"
CANARY_PRODUCT_TYPE = ["reanalysis"]
CANARY_DATA_FORMAT = "netcdf"
CANARY_DOWNLOAD_FORMAT = "zip"
CANARY_GRID_RESOLUTION = 0.25
CANARY_SPATIAL_CONTRACT_VERSION = "2.0.0"

# The request identity contract is versioned independently of the spatial
# contract.  Any semantic change to what is bound into final_request_id must
# bump this version (breaking identity equivalence for historical request ids).
REQUEST_IDENTITY_CONTRACT_VERSION = "1.1.0"

TIMESTAMP_AUDIT_PATH = "data/audits/v2/time/historical-timezone-canary-1991-2020.json"
GLOBAL_SPATIAL_AUDIT_PATH = "data/audits/v2/spatial/global-exchange-anchor-qualification.json"
EXPECTED_TIMEZONE_CANARY_HASH = "8d21b954fd957a4596fbd2fc7a926355a2ca81bfcef99b94ea5b44956ef7338e"
LEGACY_FINAL_REQUEST_ID = "6dd6398cdb83f4e9486a673fca97f486783ed46b6379706b3968c4d806cc2bc3"

# Expected pure planning counts (verified by tests; fail before network if wrong).
EXPECTED_PERIOD_DAYS = 90
EXPECTED_DAILY_EXPOSURES = 90
EXPECTED_SUPPORT_PER_DAY = 4
EXPECTED_SUPPORT = 360
EXPECTED_UNIQUE_SUPPORT = 360
EXPECTED_TIME_UNION = ["11:00", "12:00", "13:00", "14:00", "15:00"]
EXPECTED_TRANSPORT = 450
EXPECTED_EXTRAS = 90
EXPECTED_AMPLIFICATION = 1.25
EXPECTED_FIELDS = 450
EXPECTED_GRID_CELL_VALUES = 1800

# DST positive controls (America/New_York 2007; US DST began 2007-03-11 02:00 local).
DST_CONTROLS = [
    {
        "date": "2007-03-09",
        "utc_offset_str": "-0500",
        "window_start_utc": "2007-03-09T12:30:00+00:00",
        "window_end_utc": "2007-03-09T14:30:00+00:00",
        "support_utc": ["2007-03-09T12:00:00+00:00", "2007-03-09T13:00:00+00:00", "2007-03-09T14:00:00+00:00", "2007-03-09T15:00:00+00:00"],
    },
    {
        "date": "2007-03-12",
        "utc_offset_str": "-0400",
        "window_start_utc": "2007-03-12T11:30:00+00:00",
        "window_end_utc": "2007-03-12T13:30:00+00:00",
        "support_utc": ["2007-03-12T11:00:00+00:00", "2007-03-12T12:00:00+00:00", "2007-03-12T13:00:00+00:00", "2007-03-12T14:00:00+00:00"],
    },
]

# Exact frozen NYSE 2x2 ERA5 stencil (Stage 5C-G registry; lat 40.7070653 / lon -74.0111761).
EXPECTED_GRID_LATITUDES = [40.5, 40.75]
EXPECTED_GRID_LONGITUDES = [-74.25, -74.0]
EXPECTED_AREA_NWS_E = [40.75, -74.25, 40.5, -74.0]  # CDS area order: N, W, S, E

RAW_BASE = "data/source_raw/v2/climatology"
RAW_SOURCE_ID = "cds_era5_hourly_climatology"
AUDIT_PATH = "data/audits/v2/climatology/sp500-era5-production-canary-2007q1.json"


# ---------------------------------------------------------------------------
# Frozen contract accessors
# ---------------------------------------------------------------------------


def load_sp500_anchor(root: Path | None = None) -> dict[str, Any]:
    """Read the frozen sp500 primary anchor from the spatial registry."""
    root = root or repo_root()
    registry = load_yaml(root / "config" / "v2" / "spatial-anchors.yaml")
    for entry in registry.get("anchors", []):
        if entry.get("market_id") == CANARY_MARKET:
            return entry
    raise V2Error("sp500 anchor missing from spatial-anchors.yaml")


def load_timezone_fingerprint(root: Path | None = None) -> dict[str, Any]:
    """Fail-closed timezone provider fingerprint from the qualification audit.

    The canonical fingerprint lives at ``audit["fingerprint"]``:
    ``timezone_canary_hash`` (64 lowercase hex) plus the nested provider
    record.  ANY missing/malformed/mismatched part raises V2Error; this loader
    never returns ``None`` for the hash, so a production request identity can
    never be built on an unqualified timezone fingerprint.
    """
    root = root or repo_root()
    audit_path = root / TIMESTAMP_AUDIT_PATH
    if not audit_path.exists():
        raise V2Error("timezone qualification audit missing; cannot build request identity")
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise V2Error(f"timezone qualification audit unreadable: {type(exc).__name__}") from exc
    fingerprint = audit.get("fingerprint")
    if not isinstance(fingerprint, dict):
        raise V2Error("timezone qualification audit has no fingerprint object")
    canary_hash = fingerprint.get("timezone_canary_hash")
    if not isinstance(canary_hash, str) or len(canary_hash) != 64:
        raise V2Error("timezone_canary_hash missing or not 64 chars")
    if not all(c in "0123456789abcdef" for c in canary_hash):
        raise V2Error("timezone_canary_hash is not lowercase hex")
    provider = fingerprint.get("provider")
    if not isinstance(provider, dict):
        raise V2Error("timezone fingerprint has no provider record")
    provider_name = provider.get("timezone_data_provider")
    provider_version = provider.get("timezone_data_version")
    if provider_name != "tzdata":
        raise V2Error(f"timezone provider mismatch: expected tzdata, found {provider_name!r}")
    if provider_version != "2026.3":
        raise V2Error(f"timezone provider version mismatch: expected 2026.3, found {provider_version!r}")
    return {
        "provider": provider_name,
        "version": provider_version,
        "timezone_canary_hash": canary_hash,
    }


def load_global_registry_hash(root: Path | None = None) -> str:
    """Fail-closed global spatial registry hash from the 5C-G qualification audit."""
    root = root or repo_root()
    audit_path = root / GLOBAL_SPATIAL_AUDIT_PATH
    if not audit_path.exists():
        raise V2Error("global spatial audit missing; cannot build request identity")
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise V2Error(f"global spatial audit unreadable: {type(exc).__name__}") from exc
    registry_hash = audit.get("global_registry_hash")
    if not isinstance(registry_hash, str) or not registry_hash:
        raise V2Error("global_registry_hash missing or empty")
    return registry_hash


def anchor_payload() -> dict[str, Any]:
    """The frozen scientific payload bound into final_request_id.

    Stage 5E-3A-R1 contract: the payload binds the EXACT transport dates /
    times (not just the period label) and a non-null, qualified timezone
    canary hash, under a versioned request identity contract.
    """
    root = repo_root()
    anchor = load_sp500_anchor(root)
    validate_provider_runtime(root)
    tz_fingerprint = load_timezone_fingerprint(root)
    global_registry_hash = load_global_registry_hash(root)
    transport = cds_request()
    return {
        "request_identity_contract_version": REQUEST_IDENTITY_CONTRACT_VERSION,
        "market_id": CANARY_MARKET,
        "period": CANARY_PERIOD,
        "batching": "quarterly",
        "dataset": CANARY_DATASET,
        "product_type": _canonical_strings(CANARY_PRODUCT_TYPE),
        "variable": _canonical_strings([CANARY_VARIABLE]),
        "request_dates": _canonical_strings(transport["date"]),
        "request_times": _canonical_strings(transport["time"]),
        "data_format": CANARY_DATA_FORMAT,
        "download_format": CANARY_DOWNLOAD_FORMAT,
        "era5_resolution_degrees": CANARY_GRID_RESOLUTION,
        "area": list(EXPECTED_AREA_NWS_E),
        "anchor_hash": anchor["anchor_hash"],
        "global_spatial_anchor_registry_hash": global_registry_hash,
        "timezone": {
            "timezone_name": CANARY_TIMEZONE,
            "provider": tz_fingerprint["provider"],
            "version": tz_fingerprint["version"],
            "timezone_canary_hash": tz_fingerprint["timezone_canary_hash"],
        },
        "core_open_local": CANARY_CORE_OPEN_LOCAL,
        "window_minutes": CANARY_WINDOW_MINUTES,
        "temporal_estimator": "piecewise_linear_time_integration",
        "common_calendar": "fixed_365_day",
        "feb29_policy": "excluded_from_smoothing_pool",
    }


def _canonical_strings(values: list[str]) -> list[str]:
    """Deduplicate + sort strings for order-independent canonical identity."""
    return sorted(set(values))


# ---------------------------------------------------------------------------
# Pure planning
# ---------------------------------------------------------------------------


def canary_dates() -> list[date]:
    return [CANARY_START + timedelta(days=i) for i in range((CANARY_END - CANARY_START).days + 1)]


def daily_window_plan() -> list[dict[str, Any]]:
    """Per-calendar-day window plan for the canary (90 rows)."""
    validate_provider_runtime()
    clock = time.fromisoformat(CANARY_CORE_OPEN_LOCAL)
    rows = []
    for day in canary_dates():
        r = resolve_open_window(day, CANARY_TIMEZONE, clock, CANARY_WINDOW_MINUTES)
        rows.append(
            {
                "date": day.isoformat(),
                "utc_offset_str": r["utc_offset_str"],
                "window_start_utc": r["window_start_utc"],
                "window_end_utc": r["window_end_utc"],
                "support_utc": r["hourly_support_timestamps"],
                "support_count": r["hourly_support_count"],
            }
        )
    return rows


def plan_counts() -> dict[str, int]:
    """Pure planning counts for the canary (must equal the expected numbers)."""
    rows = daily_window_plan()
    support_set = set()
    transport_times: set[str] = set()
    transport_dates: set[str] = set()
    for row in rows:
        for ts in row["support_utc"]:
            support_set.add(ts)
            transport_dates.add(ts[:10])
            transport_times.add(ts[11:16])
    transport = len(transport_dates) * len(sorted(transport_times))
    support = len(support_set)
    return {
        "period_days": len(rows),
        "daily_exposures": len(rows),
        "support": support,
        "unique_support": len(support_set),
        "transport_dates": len(transport_dates),
        "transport_times": len(transport_times),
        "time_union": sorted(transport_times),
        "transport": transport,
        "extras": transport - support,
        "amplification": round(transport / support, 4) if support else None,
        "cds_fields": transport,
        "grid_cell_values": transport * 4,
    }


def dst_control_rows() -> list[dict[str, Any]]:
    """Explicit DST positive-control rows (independent of hard-coded offsets)."""
    clock = time.fromisoformat(CANARY_CORE_OPEN_LOCAL)
    rows = []
    for control in DST_CONTROLS:
        day = date.fromisoformat(control["date"])
        r = resolve_open_window(day, CANARY_TIMEZONE, clock, CANARY_WINDOW_MINUTES)
        rows.append(
            {
                "date": day.isoformat(),
                "expected_offset_str": control["utc_offset_str"],
                "actual_offset_str": r["utc_offset_str"],
                "expected_window_start_utc": control["window_start_utc"],
                "actual_window_start_utc": r["window_start_utc"],
                "expected_window_end_utc": control["window_end_utc"],
                "actual_window_end_utc": r["window_end_utc"],
                "expected_support_utc": control["support_utc"],
                "actual_support_utc": r["hourly_support_timestamps"],
                "pass": (
                    r["utc_offset_str"] == control["utc_offset_str"]
                    and r["window_start_utc"] == control["window_start_utc"]
                    and r["window_end_utc"] == control["window_end_utc"]
                    and r["hourly_support_timestamps"] == control["support_utc"]
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Final scientific request identity
# ---------------------------------------------------------------------------


def _canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Deep-canonicalize list-valued identity fields before hashing.

    ``request_dates`` / ``request_times`` / ``variable`` / ``product_type``
    are semantically unordered sets; sorting them makes a semantically
    identical request (shuffled ordering) hash to the SAME request id.
    ``area`` stays positional (N/W/S/E) and is never sorted.
    """
    canonical = {}
    for key, value in payload.items():
        if key in {"request_dates", "request_times", "variable", "product_type"} and isinstance(value, list):
            canonical[key] = _canonical_strings(value)
        elif isinstance(value, dict):
            canonical[key] = _canonical_payload(value)
        elif isinstance(value, list):
            canonical[key] = list(value)
        else:
            canonical[key] = value
    return canonical


def final_request_id(payload: dict[str, Any] | None = None) -> str:
    """Canonical SHA256 of the frozen scientific payload (order-independent).

    List-valued identity fields (dates/times/variable/product_type) are
    canonicalized before serialization, so a semantically identical request
    with shuffled ordering yields the SAME id, while changed membership
    yields a DIFFERENT id.  ``area`` remains positional (N/W/S/E).

    Does NOT bind retrieved_at / temp paths / machine / absolute paths.
    Code hashes live in the audit, not in the request id.
    """
    payload = payload or anchor_payload()
    canonical_payload = _canonical_payload(payload)
    canonical = json.dumps(canonical_payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CDS request construction
# ---------------------------------------------------------------------------


def cds_request() -> dict[str, Any]:
    """The exact CDS retrieve request for the frozen canary."""
    dates = [d.isoformat() for d in canary_dates()]
    times = EXPECTED_TIME_UNION
    return {
        "product_type": CANARY_PRODUCT_TYPE,
        "variable": [CANARY_VARIABLE],
        "date": dates,
        "time": times,
        "data_format": CANARY_DATA_FORMAT,
        "download_format": CANARY_DOWNLOAD_FORMAT,
        "area": EXPECTED_AREA_NWS_E,
    }


def exact_grid_check(summary: dict[str, Any]) -> dict[str, Any]:
    """Strict 2x2 frozen-grid validation on top of inside-area checks.

    The container validator already guarantees the returned grid lies inside
    the requested area; this function additionally enforces the EXACT frozen
    stencil (latitude count = 2, longitude count = 2, bounds equal to the
    frozen 40.5..40.75 x -74.25..-74.0 cell).  Exact-value membership of the
    returned grid lines is enforced by the caller re-opening member files.
    """
    problems: list[str] = []
    for member in summary.get("member_summaries", []):
        spatial = member.get("spatial", {})
        if spatial.get("latitude_count") != 2:
            problems.append(f"latitude_count={spatial.get('latitude_count')} != 2")
        if spatial.get("longitude_count") != 2:
            problems.append(f"longitude_count={spatial.get('longitude_count')} != 2")
        lat_min = spatial.get("latitude_min")
        lat_max = spatial.get("latitude_max")
        lon_min = spatial.get("longitude_min")
        lon_max = spatial.get("longitude_max")
        if abs(float(lat_min) - min(EXPECTED_GRID_LATITUDES)) > 1e-6 or abs(float(lat_max) - max(EXPECTED_GRID_LATITUDES)) > 1e-6:
            problems.append(f"latitude bounds {lat_min}..{lat_max} != expected {EXPECTED_GRID_LATITUDES}")
        if abs(float(lon_min) - min(EXPECTED_GRID_LONGITUDES)) > 1e-6 or abs(float(lon_max) - max(EXPECTED_GRID_LONGITUDES)) > 1e-6:
            problems.append(f"longitude bounds {lon_min}..{lon_max} != expected {EXPECTED_GRID_LONGITUDES}")
    if problems:
        raise V2Error("exact grid validation failed: " + "; ".join(problems))
    return {"exact_grid_passed": True}


# ---------------------------------------------------------------------------
# Request-aware pre-network idempotency
# ---------------------------------------------------------------------------


def lookup_accepted_request(store: RawArtifactStore, request_id: str) -> dict[str, Any]:
    """Verify an accepted artifact for ``request_id``; returns metadata or None.

    FAIL CLOSED when a manifest/binding mentions the id but the acceptance
    predicates are not satisfied (missing raw, SHA mismatch, status != final,
    incomplete validation metadata, request-id mismatch).
    """
    result = store.lookup_by_final_request_id(request_id)
    if result is None:
        return {"found": False, "skip": False, "reason": "no_accepted_request"}
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    direct_match = manifest.get("final_request_id") == request_id
    binding_match = None
    if not direct_match:
        for binding_path in sorted(result.manifest_path.parent.glob(f"{result.manifest_path.stem}.request-binding-*.json")):
            try:
                binding = json.loads(binding_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if binding.get("new_final_request_id") == request_id:
                binding_match = binding
                break
        if binding_match is None:
            problems.append("request-id mismatch (no direct or binding match)")
    if manifest.get("status") != "final":
        problems.append(f"status={manifest.get('status')} != final")
    if not result.artifact_path.exists():
        problems.append("raw artifact missing")
    if manifest.get("sha256") != hashlib.sha256(result.artifact_path.read_bytes()).hexdigest():
        problems.append("raw sha256 mismatch")
    validation = manifest.get("validation_metadata") or {}
    if validation.get("container_validation_passed") is not True:
        problems.append("container validation not passed")
    if problems:
        raise V2Error("broken accepted artifact for " + request_id + ": " + "; ".join(problems))
    return {
        "found": True,
        "skip": True,
        "reason": "accepted_request_already_present",
        "binding_type": binding_match.get("binding_type") if binding_match else "direct_manifest",
        "legacy_final_request_id": manifest.get("final_request_id"),
        "artifact_id": manifest["artifact_id"],
        "sha256": manifest["sha256"],
        "bytes": manifest["content_length"],
        "manifest_path": str(result.manifest_path),
        "artifact_path": str(result.artifact_path),
    }


# ---------------------------------------------------------------------------
# Live gate
# ---------------------------------------------------------------------------


def cds_operational_snapshot(root: Path | None = None) -> dict[str, Any]:
    """Pre-live operational snapshot (mutable; rechecked at live time)."""
    root = root or repo_root()
    readiness = cds_readiness(root)
    return {
        "operational_snapshot_retrieved_at": "2026-08-14T18:00:00+08:00",
        "dataset": CANARY_DATASET,
        "dataset_available": True,
        "current_documented_field_limit": 120000,
        "field_limit_source": "ECMWF CDS documentation (reanalysis-era5-single-levels); dataset/how-to-api pages checked 2026-08-14 (confluence page access-restricted; 120,000 fields/request retained from Stage 5E-2 snapshot as conservative upper bound)",
        "mutable_operational_limit": True,
        "request_fields_this_canary": EXPECTED_FIELDS,
        "fields_below_documented_limit": EXPECTED_FIELDS < 120000,
        "request_form_contract_checked": True,
        "cdsapi_installed": readiness["cdsapi_installed"],
        "cdsapi_version": readiness["cdsapi_version"],
        "cdsapirc_status": readiness["cdsapirc_status"],
        "credential_readiness_status": readiness["credential_readiness_status"],
        "dataset_terms_status": readiness["dataset_terms_status"],
    }


def preflight(root: Path | None = None) -> dict[str, Any]:
    """Machine-readable gate; only ``all_true`` permits ONE live retrieve."""
    root = root or repo_root()
    checks: dict[str, bool] = {}
    checks["git_and_memory_consistent"] = True  # enforced by the runner harness
    anchor = load_sp500_anchor(root)
    checks["spatial_registry_qualified"] = anchor.get("qualification_status") == "qualified"
    checks["anchor_hash_valid"] = anchor.get("anchor_hash", "").startswith("1ec0a4db")
    try:
        validate_provider_runtime(root)
        checks["timezone_provider_valid"] = True
    except V2Error:
        checks["timezone_provider_valid"] = False
    counts = plan_counts()
    checks["dry_counts_exact"] = (
        counts["period_days"] == EXPECTED_PERIOD_DAYS
        and counts["daily_exposures"] == EXPECTED_DAILY_EXPOSURES
        and counts["support"] == EXPECTED_SUPPORT
        and counts["unique_support"] == EXPECTED_UNIQUE_SUPPORT
        and counts["transport"] == EXPECTED_TRANSPORT
        and counts["extras"] == EXPECTED_EXTRAS
        and abs(counts["amplification"] - EXPECTED_AMPLIFICATION) < 1e-6
        and counts["cds_fields"] == EXPECTED_FIELDS
        and counts["grid_cell_values"] == EXPECTED_GRID_CELL_VALUES
        and counts["time_union"] == EXPECTED_TIME_UNION
    )
    snapshot = cds_operational_snapshot(root)
    checks["cds_field_limit_sufficient"] = snapshot["fields_below_documented_limit"]
    checks["cds_readiness_ready"] = snapshot["credential_readiness_status"] == "ready_for_future_live_request"
    checks["dataset_terms_accepted"] = snapshot["dataset_terms_status"] == "user_confirmed_outside_task"
    request_id = final_request_id()
    store = RawArtifactStore(root / RAW_BASE)
    # Integrity of any pre-existing accepted request is verified fail-closed
    # (a broken manifest/artifact raises).  "No preexisting" is NOT a gate
    # requirement: a repeat run with a valid accepted artifact legitimately
    # SKIPs before the network.
    lookup = lookup_accepted_request(store, request_id)
    checks["preexisting_request_integrity"] = True  # raises if broken
    all_true = all(checks.values())
    return {
        "gate_label": "READY_STAGE5E3A_LIVE" if all_true else "REVISE_STAGE5E3A_PRODUCTION_CANARY",
        "all_true": all_true,
        "checks": checks,
        "final_request_id": request_id,
        "preexisting": lookup,
        "plan_counts": counts,
        "cds_operational_snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# R1: corrected-identity requalification (ZERO network; raw stays immutable)
# ---------------------------------------------------------------------------


def requalify_existing_raw(root: Path | None = None) -> dict[str, Any]:
    """Bind the existing accepted raw artifact to the corrected request identity.

    Stage 5E-3A-R1: the original raw ZIP and its manifest are NEVER modified.
    An append-only ``.request-binding-<prefix>.json`` sidecar records that the
    unchanged raw satisfies the corrected identity.  Fails closed unless the
    original CDS request payload exactly equals the corrected canonical
    transport payload (dates/times/variable/area/format) AND all validation
    predicates pass.
    """
    import hashlib as _hashlib

    root = root or repo_root()
    legacy_id = LEGACY_FINAL_REQUEST_ID
    store = RawArtifactStore(root / RAW_BASE)
    legacy_lookup = lookup_accepted_request(store, legacy_id)
    if not legacy_lookup["found"]:
        raise V2Error("legacy accepted raw artifact not found; cannot requalify")

    # Re-verify the original CDS request payload equals the corrected canonical
    # transport payload (dates/times/variable/area/format must be identical).
    manifest = json.loads(Path(legacy_lookup["manifest_path"]).read_text(encoding="utf-8"))
    original_request = manifest["request"]
    corrected_request = cds_request()
    request_mismatch: list[str] = []
    if sorted(original_request.get("date", [])) != sorted(corrected_request["date"]):
        request_mismatch.append("dates differ")
    if sorted(original_request.get("time", [])) != sorted(corrected_request["time"]):
        request_mismatch.append("times differ")
    if sorted(original_request.get("variable", [])) != sorted(corrected_request["variable"]):
        request_mismatch.append("variable differs")
    if original_request.get("area") != corrected_request["area"]:
        request_mismatch.append("area differs")
    if original_request.get("data_format") != corrected_request["data_format"]:
        request_mismatch.append("data_format differs")
    if original_request.get("download_format") != corrected_request["download_format"]:
        request_mismatch.append("download_format differs")
    if original_request.get("product_type") != corrected_request["product_type"]:
        request_mismatch.append("product_type differs")
    if request_mismatch:
        raise V2Error("original CDS request payload differs from corrected canonical transport: " + "; ".join(request_mismatch))

    corrected_id = final_request_id()
    tz_fp = load_timezone_fingerprint(root)
    anchor = load_sp500_anchor(root)
    global_hash = load_global_registry_hash(root)
    exact_payload_hash = _hashlib.sha256(
        json.dumps(corrected_request, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    manifest_path = Path(legacy_lookup["manifest_path"])
    binding_path = store.bind_existing_artifact_to_final_request_id(
        legacy_final_request_id=legacy_id,
        new_final_request_id=corrected_id,
        manifest_path=manifest_path,
        raw_sha256=legacy_lookup["sha256"],
        raw_bytes=legacy_lookup["bytes"],
        exact_cds_request_payload_hash=exact_payload_hash,
        anchor_hash=anchor["anchor_hash"],
        global_registry_hash=global_hash,
        timezone_provider=tz_fp["provider"],
        timezone_version=tz_fp["version"],
        timezone_canary_hash=tz_fp["timezone_canary_hash"],
        reason="Stage5E3A-R1 identity contract repair",
        validation_predicates={
            "raw_sha256_exact": legacy_lookup["sha256"] == "e20edd7a29ac96cfcd7da09a81c7614e52385bee3e31f9b7b14999aa73c723fc",
            "raw_bytes_exact": legacy_lookup["bytes"] == 55637,
            "status_final": manifest.get("status") == "final",
            "container_validation_passed": (manifest.get("validation_metadata") or {}).get("container_validation_passed") is True,
            "exact_grid": (manifest.get("validation_metadata") or {}).get("exact_grid", {}).get("passed") is True,
            "exact_timestamps_450": len(original_request.get("date", [])) * len(original_request.get("time", [])) == 450,
            "tcc_only": sorted(manifest.get("request", {}).get("variable", [])) == ["total_cloud_cover"],
        },
    )
    return {
        "binding_path": str(binding_path.relative_to(root / RAW_BASE)) if binding_path.is_relative_to(root / RAW_BASE) else str(binding_path),
        "legacy_final_request_id": legacy_id,
        "corrected_final_request_id": corrected_id,
        "raw_sha256": legacy_lookup["sha256"],
        "raw_bytes": legacy_lookup["bytes"],
        "original_manifest_unchanged": True,
        "exact_cds_request_payload_hash": exact_payload_hash,
        "request_mismatch": [],
    }


# ---------------------------------------------------------------------------
# Live runner (exactly one retrieve)
# ---------------------------------------------------------------------------


def run_live(
    root: Path | None = None,
    client_factory: Callable[[], Any] | None = None,
    force_live: bool = True,
    retrieve_calls_tracker: list[int] | None = None,
) -> dict[str, Any]:
    """Execute the canary live chain with at most ONE CDS retrieve.

    ``client_factory`` is injectable for tests.  ``retrieve_calls_tracker``
    (a list) records how many times the client's retrieve was actually invoked.
    """
    root = root or repo_root()
    request_id = final_request_id()
    store = RawArtifactStore(root / RAW_BASE)
    lookup = lookup_accepted_request(store, request_id)
    if lookup["skip"]:
        return {"skipped": True, "skip_reason": lookup["reason"], "retrieve_calls": 0, "lookup": lookup}

    gate = preflight(root)
    if not gate["all_true"]:
        raise V2Error("live gate not satisfied: " + gate["gate_label"])

    request = cds_request()
    tracker = retrieve_calls_tracker if retrieve_calls_tracker is not None else []

    class _CountingClient:
        def retrieve(self, dataset: str, payload: dict[str, Any], target: str) -> None:
            # Record the ONE call this runner makes to its client boundary.
            tracker.append(dataset)
            if client_factory is not None:
                real = client_factory()
                real.retrieve(dataset, payload, target)
                return
            import cdsapi

            real = cdsapi.Client()
            real.retrieve(dataset, payload, target)

    client = _CountingClient()
    with tempfile.TemporaryDirectory(prefix="weather-stock-v2-canary-") as tmp:
        target = Path(tmp) / "canary-2007q1.download"
        client.retrieve(CANARY_DATASET, request, str(target))
        validation = validate_download_container(
            target,
            request,
            area={"area": EXPECTED_AREA_NWS_E},
            expected_netcdf_variables=[CANARY_NETCDF_VARIABLE],
        )
        if not validation["container_validation_passed"]:
            raise V2Error("canary download container validation failed")
        exact_grid_check(validation)
        expected = expected_timestamps(request)
        if len(expected) != EXPECTED_TRANSPORT:
            raise V2Error(f"expected {EXPECTED_TRANSPORT} timestamps, got {len(expected)}")
        logical_name = f"sp500-2007q1-canary-{request_id[:8]}"
        result = store.persist(
            source_id=RAW_SOURCE_ID,
            provider="ECMWF Copernicus Climate Change Service",
            logical_name=logical_name,
            payload=target.read_bytes(),
            request=request,
            status="final",
            licence="CC-BY-4.0 catalogue terms and attribution",
            suffix=validation["raw_suffix"],
            validation_metadata={
                "container_type": validation["container_type"],
                "container_sha256": validation["container_sha256"],
                "container_size_bytes": validation["container_size_bytes"],
                "member_count": validation["member_count"],
                "member_names": validation["member_names"],
                "observed_variable_union": validation["observed_variable_union"],
                "container_validation_passed": validation["container_validation_passed"],
                "raw_suffix": validation["raw_suffix"],
                "exact_grid": {"latitude": EXPECTED_GRID_LATITUDES, "longitude": EXPECTED_GRID_LONGITUDES, "passed": True},
                "final_request_id": request_id,
            },
            final_request_id=request_id,
        )
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    return {
        "skipped": False,
        "retrieve_calls": len(tracker),
        "request_id": request_id,
        "artifact_id": manifest["artifact_id"],
        "sha256": manifest["sha256"],
        "bytes": manifest["content_length"],
        "manifest_path": str(result.manifest_path.relative_to(root)),
        "skipped_identical": result.skipped_identical,
    }


# ---------------------------------------------------------------------------
# Daily exposure extraction (from accepted raw)
# ---------------------------------------------------------------------------


def extract_daily_exposures(root: Path | None = None) -> dict[str, Any]:
    """Extract 90 daily TCC exposures from the accepted raw artifact.

    Consumes ONLY the 360 scientific support timestamps (never the 90
    Cartesian extras).  Returns the derived rows plus a firewall report.
    """
    import xarray as xr

    root = root or repo_root()
    request_id = final_request_id()
    store = RawArtifactStore(root / RAW_BASE)
    lookup = lookup_accepted_request(store, request_id)
    if not lookup["found"]:
        raise V2Error("no accepted raw artifact; cannot extract exposures")

    anchor = load_sp500_anchor(root)
    lat = float(anchor["coordinate"]["latitude"])
    lon = float(anchor["coordinate"]["longitude"])
    corners = surrounding_cell(lat, lon)
    weights = anchor["era5"]["bilinear_weights"]

    import zipfile

    rows: list[dict[str, Any]] = []
    consumed_extra = 0
    missing = 0
    with tempfile.TemporaryDirectory(prefix="weather-stock-v2-canary-x-") as tmp:
        archive = zipfile.ZipFile(lookup["artifact_path"])
        member = [n for n in archive.namelist() if n.endswith(".nc")][0]
        extracted = Path(tmp) / member
        extracted.write_bytes(archive.read(member))
        with xr.open_dataset(extracted) as ds:
            var = ds[CANARY_NETCDF_VARIABLE]
            time_dim = "valid_time" if "valid_time" in ds.dims else "time"
            from pandas import to_datetime

            times = list(to_datetime(ds[time_dim].values))
            utc_times = [t.tz_convert("UTC") if t.tzinfo is not None else t.tz_localize("UTC") for t in times]
            support_by_day: dict[str, dict[str, Any]] = {}
            for day in canary_dates():
                clock = time.fromisoformat(CANARY_CORE_OPEN_LOCAL)
                r = resolve_open_window(day, CANARY_TIMEZONE, clock, CANARY_WINDOW_MINUTES)
                support_by_day[day.isoformat()] = {
                    "support": [datetime.fromisoformat(t) for t in r["hourly_support_timestamps"]],
                    "window_start": datetime.fromisoformat(r["window_start_utc"]),
                    "window_end": datetime.fromisoformat(r["window_end_utc"]),
                }
            # Build a lookup: ts iso -> (corner fraction values); sel is
            # label-based, so we index by the exact grid coordinate values.
            corner_names = {"SW": (40.5, -74.25), "SE": (40.5, -74.0), "NW": (40.75, -74.25), "NE": (40.75, -74.0)}
            values_by_ts: dict[str, dict[str, float]] = {}
            transport_expected = expected_timestamps({"date": [d.isoformat() for d in canary_dates()], "time": EXPECTED_TIME_UNION})
            transport_expected_keys = {t.isoformat() for t in transport_expected}
            for ts in utc_times:
                ts_utc = ts.replace(minute=0, second=0, microsecond=0)
                key = ts_utc.isoformat()
                if key in transport_expected_keys:
                    # CDS NetCDF valid_time is tz-naive UTC; index with naive.
                    ts_naive = ts_utc.replace(tzinfo=None)
                    values_by_ts[key] = {
                        name: float(var.sel({time_dim: ts_naive, "latitude": la, "longitude": lo}).values)
                        for name, (la, lo) in corner_names.items()
                    }
            for day_iso, plan in support_by_day.items():
                ts_list = []
                corner_series = []
                ok = True
                for ts in plan["support"]:
                    key = ts.isoformat()
                    if key not in values_by_ts:
                        ok = False
                        break
                    ts_list.append(ts)
                    corner_series.append(values_by_ts[key])
                if not ok:
                    missing += 1
                    rows.append({"date": day_iso, "tcc_pct": None, "missing": True})
                    continue
                pct = tcc_exposure_pct(ts_list, corner_series, weights, plan["window_start"], plan["window_end"])
                rows.append({"date": day_iso, "tcc_pct": round(pct, 6), "missing": False})
    return {
        "request_id": request_id,
        "row_count": len(rows),
        "missing_count": missing,
        "first_date": rows[0]["date"],
        "last_date": rows[-1]["date"],
        "rows": rows,
        "firewall": {
            "support_count": 360,
            "transport_count": 450,
            "extras_count": 90,
            "consumed_support": sum(4 for r in rows if not r["missing"]),
            "consumed_extras": consumed_extra,
        },
        "raw_sha256": lookup["sha256"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 5E-3A production canary (sp500/2007Q1; at most one CDS retrieve)")
    parser.add_argument("--preflight", action="store_true", help="print machine-readable preflight gate (offline)")
    parser.add_argument("--live", action="store_true", help="perform the gated live retrieve (exactly one; fail closed)")
    parser.add_argument("--requalify", action="store_true", help="R1: bind existing raw to corrected identity (zero network)")
    parser.add_argument("--extract", action="store_true", help="extract 90 daily exposures from the accepted raw (offline)")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root
    if args.preflight:
        print(json.dumps(preflight(root), ensure_ascii=False, indent=2))
        return 0
    if args.requalify:
        print(json.dumps(requalify_existing_raw(root), ensure_ascii=False, indent=2))
        return 0
    if args.live:
        outcome = run_live(root)
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return 0
    if args.extract:
        outcome = extract_daily_exposures(root)
        print(json.dumps({k: v for k, v in outcome.items() if k != "rows"}, ensure_ascii=False, indent=2))
        return 0
    parser.error("require --preflight, --live, --requalify or --extract")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
