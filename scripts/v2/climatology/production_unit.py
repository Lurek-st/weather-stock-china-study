"""Stage 5E-3C generic quarterly production unit engine (NO authorization).

This module is the AUTHORIZATION-FREE scientific core extracted from the
Stage 5E-3B ``production_quarter`` runner (which remains a stage-specific
wrapper over it).  It owns the ONE frozen scientific planner used by every
future full-backfill unit:

    plan_unit / unit_plan_counts / cds_request_for / identity_payload /
    final_request_id_for / extract_unit_exposures / execute_unit_once

It NEVER authorizes anything.  Authorization policy lives in the stage
wrappers (5E-3B) and in the full-backfill controller (5E-3C+).  Any caller
that wants to fire a CDS retrieve must pass its OWN authorization gate
BEFORE calling ``execute_unit_once``.

This file deliberately contains NO network calls: it only constructs the
exact scientific payload, computes request identities, and (when the caller
has already authorized) executes one bounded retrieve via an injectable
client factory.  ``execute_unit_once`` performs request-aware pre-network
idempotency lookup so a repeat run SKIPs before any client is constructed.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import RawArtifactStore, V2Error, load_yaml, repo_root, write_json
from scripts.v2.core_open_regimes import resolve_core_open
from scripts.v2.fetch_era5 import expected_timestamps, validate_download_container
from scripts.v2.session_time import resolve_open_window
from scripts.v2.spatial.grid_geometry import surrounding_cell
from scripts.v2.timezone_provider import validate_provider_runtime

from scripts.v2.climatology.production_canary import (
    CANARY_DATASET,
    CANARY_DOWNLOAD_FORMAT,
    CANARY_GRID_RESOLUTION,
    CANARY_NETCDF_VARIABLE,
    CANARY_VARIABLE,
    REQUEST_IDENTITY_CONTRACT_VERSION,
    _canonical_strings,
    load_global_registry_hash,
    load_timezone_fingerprint,
    lookup_accepted_request,
)
from scripts.v2.climatology.tcc_exposure import tcc_exposure_pct

RAW_BASE = "data/source_raw/v2/climatology"
RAW_SOURCE_ID = "cds_era5_hourly_climatology"


class ClientConstructionError(RuntimeError):
    """Real CDS client construction failed before a retrieve could begin."""


class PayloadValidationError(V2Error):
    """Downloaded bytes failed the frozen raw-payload validation contract."""


# ---------------------------------------------------------------------------
# Frozen market contract
# ---------------------------------------------------------------------------


def market_spec(market_id: str) -> dict[str, Any]:
    """Frozen target-regime clock from the Stage 5B-2 core-open registry."""
    r = resolve_core_open(market_id, "2023-06-15")
    return {"market_id": market_id, "timezone": r["timezone"], "core_open_local": r["core_open_local"]}


def load_anchor(market_id: str) -> dict[str, Any]:
    registry = load_yaml(repo_root() / "config" / "v2" / "spatial-anchors.yaml")
    for entry in registry.get("anchors", []):
        if entry.get("market_id") == market_id:
            if entry.get("qualification_status") != "qualified":
                raise V2Error(f"anchor for {market_id} not qualified")
            return entry
    raise V2Error(f"anchor missing for {market_id}")


# ---------------------------------------------------------------------------
# Pure planning
# ---------------------------------------------------------------------------


def quarter_dates(unit: dict[str, Any]) -> list[date]:
    return [unit["start"] + timedelta(days=i) for i in range((unit["end"] - unit["start"]).days + 1)]


def common_daily_dates(unit: dict[str, Any]) -> list[date]:
    """Daily scientific exposure dates (common calendar; Feb 29 excluded)."""
    return [d for d in quarter_dates(unit) if not (d.month == 2 and d.day == 29)]


def quarter_daily_plan(unit: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-exposure-day window plan (frozen target-regime clock + IANA history)."""
    validate_provider_runtime()
    spec = market_spec(unit["market_id"])
    clock = time.fromisoformat(spec["core_open_local"])
    rows = []
    for day in common_daily_dates(unit):
        r = resolve_open_window(day, spec["timezone"], clock, 120)
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


def quarter_transport(unit: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Exact CDS transport: date x time Cartesian over the exposure window.

    Support timestamps span UTC midnight, so transport dates are the union of
    every UTC date that any support timestamp touches (the first is often the
    day BEFORE the local quarter start), and transport times are the union of
    support hour-of-day values.  ``rows`` (from :func:`quarter_daily_plan`) may
    be passed to avoid recomputation; the semantics are identical either way.
    """
    rows = rows if rows is not None else quarter_daily_plan(unit)
    support_set: set[str] = set()
    for row in rows:
        support_set.update(row["support_utc"])
    transport_dates = sorted({ts[:10] for ts in support_set})
    transport_times = sorted({ts[11:16] for ts in support_set})
    transport = len(transport_dates) * len(transport_times)
    return {
        "dates": transport_dates,
        "times": transport_times,
        "transport_count": transport,
        "support_count": len(support_set),
        "extras": transport - len(support_set),
        "amplification": round(transport / len(support_set), 4) if support_set else None,
        "first_date": transport_dates[0] if transport_dates else None,
        "last_date": transport_dates[-1] if transport_dates else None,
    }


def unit_plan_counts(unit: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    spec = market_spec(unit["market_id"])
    rows = rows if rows is not None else quarter_daily_plan(unit)
    t = quarter_transport(unit, rows=rows)
    return {
        "market_id": unit["market_id"],
        "period": unit["period"],
        "timezone": spec["timezone"],
        "core_open_local": spec["core_open_local"],
        "daily_exposures": len(rows),
        "support": t["support_count"],
        "transport": t["transport_count"],
        "extras": t["extras"],
        "amplification": t["amplification"],
        "fields": t["transport_count"],
        "grid_cell_values": t["transport_count"] * 4,
        "request_dates": t["dates"],
        "request_times": t["times"],
        "first_transport_date": t["first_date"],
        "last_transport_date": t["last_date"],
        "feb29_daily_exposure": any(d.month == 2 and d.day == 29 for d in common_daily_dates(unit)),
        "feb29_transport_support_present": any(
            ts.startswith(f"{unit['start'].year}-02-29") or ts.startswith("2020-02-29")
            for row in rows
            for ts in row["support_utc"]
        ),
    }


def cds_request_for(unit: dict[str, Any]) -> dict[str, Any]:
    t = quarter_transport(unit)
    return {
        "product_type": ["reanalysis"],
        "variable": [CANARY_VARIABLE],
        "date": t["dates"],
        "time": t["times"],
        "data_format": "netcdf",
        "download_format": CANARY_DOWNLOAD_FORMAT,
        "area": _anchor_area(unit["market_id"]),
    }


def _anchor_area(market_id: str) -> list[float]:
    anchor = load_anchor(market_id)
    corners = anchor["era5"]["corners"]
    north = max(v["latitude"] for v in corners.values())
    south = min(v["latitude"] for v in corners.values())
    west = min(v["longitude"] for v in corners.values())
    east = max(v["longitude"] for v in corners.values())
    return [north, west, south, east]


def identity_payload(unit: dict[str, Any]) -> dict[str, Any]:
    """Corrected v1.1.0 request identity payload for a unit (fail-closed)."""
    root = repo_root()
    validate_provider_runtime(root)
    tz_fp = load_timezone_fingerprint(root)
    anchor = load_anchor(unit["market_id"])
    spec = market_spec(unit["market_id"])
    req = cds_request_for(unit)
    return {
        "request_identity_contract_version": REQUEST_IDENTITY_CONTRACT_VERSION,
        "market_id": unit["market_id"],
        "period": unit["period"],
        "batching": "quarterly",
        "dataset": CANARY_DATASET,
        "product_type": _canonical_strings(["reanalysis"]),
        "variable": _canonical_strings([CANARY_VARIABLE]),
        "request_dates": _canonical_strings(req["date"]),
        "request_times": _canonical_strings(req["time"]),
        "data_format": "netcdf",
        "download_format": CANARY_DOWNLOAD_FORMAT,
        "era5_resolution_degrees": CANARY_GRID_RESOLUTION,
        "area": list(req["area"]),
        "anchor_hash": anchor["anchor_hash"],
        "global_spatial_anchor_registry_hash": load_global_registry_hash(root),
        "timezone": {
            "timezone_name": spec["timezone"],
            "provider": tz_fp["provider"],
            "version": tz_fp["version"],
            "timezone_canary_hash": tz_fp["timezone_canary_hash"],
        },
        "core_open_local": spec["core_open_local"],
        "window_minutes": 120,
        "temporal_estimator": "piecewise_linear_time_integration",
        "common_calendar": "fixed_365_day",
        "feb29_policy": "excluded_from_smoothing_pool",
    }


def final_request_id_for(unit: dict[str, Any]) -> str:
    canonical = json.dumps(identity_payload(unit), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Bounded single-unit executor (no authorization here!)
# ---------------------------------------------------------------------------


def execute_unit_once(
    unit: dict[str, Any],
    root: Path | None = None,
    client_factory: Callable[[], Any] | None = None,
    retrieve_calls_tracker: list[int] | None = None,
    raw_base: str | None = None,
) -> dict[str, Any]:
    """Execute ONE production unit with at most ONE CDS retrieve.

    This is the generic engine primitive.  It does NOT check authorization:
    the CALLER must have already authorized this exact unit.  Request-aware
    pre-network idempotency lookup still runs first, so a repeat run SKIPs
    before any client is constructed.
    """
    root = root or repo_root()
    raw_base = raw_base or RAW_BASE
    request_id = final_request_id_for(unit)
    store = RawArtifactStore(root / raw_base)
    lookup = lookup_accepted_request(store, request_id)
    if lookup["skip"]:
        return {"skipped": True, "market_id": unit["market_id"], "period": unit["period"],
                "skip_reason": lookup["reason"], "retrieve_calls": 0, "request_id": request_id}

    request = cds_request_for(unit)
    tracker = retrieve_calls_tracker if retrieve_calls_tracker is not None else []

    class _CountingClient:
        def retrieve(self, dataset: str, payload: dict[str, Any], target: str) -> None:
            tracker.append(dataset)
            if client_factory is not None:
                try:
                    transport_client = client_factory()
                except Exception as exc:  # noqa: BLE001 - retain reliable phase
                    raise ClientConstructionError("CDS client construction failed") from exc
                transport_client.retrieve(dataset, payload, target)
                return
            import cdsapi

            cdsapi.Client().retrieve(dataset, payload, target)

    client = _CountingClient()
    with tempfile.TemporaryDirectory(prefix="weather-stock-v2-q-") as tmp:
        target = Path(tmp) / "quarter.download"
        client.retrieve(CANARY_DATASET, request, str(target))
        try:
            validation = validate_download_container(
                target,
                request,
                area={"area": request["area"]},
                expected_netcdf_variables=[CANARY_NETCDF_VARIABLE],
            )
        except V2Error as exc:
            raise PayloadValidationError(
                f"{unit['market_id']} {unit['period']} container validation failed"
            ) from exc
        if not validation["container_validation_passed"]:
            raise PayloadValidationError(
                f"{unit['market_id']} {unit['period']} container validation failed"
            )
        expected = expected_timestamps(request)
        if len(expected) != len(request["date"]) * len(request["time"]):
            raise PayloadValidationError(
                f"{unit['market_id']} {unit['period']} transport timestamp count mismatch"
            )
        logical_name = f"{unit['market_id']}-{unit['period'].lower()}-prod-{request_id[:8]}"
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
                "final_request_id": request_id,
            },
            final_request_id=request_id,
        )
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    return {
        "skipped": False,
        "market_id": unit["market_id"],
        "period": unit["period"],
        "retrieve_calls": len(tracker),
        "request_id": request_id,
        "artifact_id": manifest["artifact_id"],
        "sha256": manifest["sha256"],
        "bytes": manifest["content_length"],
        "manifest_path": str(result.manifest_path.relative_to(root)),
    }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_unit_exposures(
    unit: dict[str, Any],
    root: Path | None = None,
    raw_base: str | None = None,
) -> dict[str, Any]:
    """Daily exposures from accepted raw; transport firewall enforced."""
    import xarray as xr
    from pandas import to_datetime

    root = root or repo_root()
    raw_base = raw_base or RAW_BASE
    request_id = final_request_id_for(unit)
    store = RawArtifactStore(root / raw_base)
    lookup = lookup_accepted_request(store, request_id)
    if not lookup["found"]:
        raise V2Error(f"no accepted raw for {unit['market_id']} {unit['period']}")

    anchor = load_anchor(unit["market_id"])
    spec = market_spec(unit["market_id"])
    weights = anchor["era5"]["bilinear_weights"]
    corners = surrounding_cell(float(anchor["coordinate"]["latitude"]), float(anchor["coordinate"]["longitude"]))
    corner_names = {k: (c.latitude, c.longitude) for k, c in corners.items()}

    import zipfile

    rows: list[dict[str, Any]] = []
    missing = 0
    with tempfile.TemporaryDirectory(prefix="weather-stock-v2-xq-") as tmp:
        archive = zipfile.ZipFile(lookup["artifact_path"])
        member = [n for n in archive.namelist() if n.endswith(".nc")][0]
        extracted = Path(tmp) / member
        extracted.write_bytes(archive.read(member))
        with xr.open_dataset(extracted) as ds:
            var = ds[CANARY_NETCDF_VARIABLE]
            time_dim = "valid_time" if "valid_time" in ds.dims else "time"
            times = list(to_datetime(ds[time_dim].values))
            utc_times = [t.tz_convert("UTC") if t.tzinfo is not None else t.tz_localize("UTC") for t in times]
            transport_expected = expected_timestamps(cds_request_for(unit))
            transport_keys = {t.isoformat() for t in transport_expected}
            values_by_ts: dict[str, dict[str, float]] = {}
            for ts in utc_times:
                ts_utc = ts.replace(minute=0, second=0, microsecond=0)
                key = ts_utc.isoformat()
                if key in transport_keys:
                    values_by_ts[key] = {
                        name: float(var.sel({time_dim: ts_utc.replace(tzinfo=None), "latitude": la, "longitude": lo}).values)
                        for name, (la, lo) in corner_names.items()
                    }
            clock = time.fromisoformat(spec["core_open_local"])
            for day in common_daily_dates(unit):
                r = resolve_open_window(day, spec["timezone"], clock, 120)
                support = [datetime.fromisoformat(t) for t in r["hourly_support_timestamps"]]
                start = datetime.fromisoformat(r["window_start_utc"])
                end = datetime.fromisoformat(r["window_end_utc"])
                ts_list = []
                corner_series = []
                ok = True
                for ts in support:
                    key = ts.isoformat()
                    if key not in values_by_ts:
                        ok = False
                        break
                    ts_list.append(ts)
                    corner_series.append(values_by_ts[key])
                if not ok:
                    missing += 1
                    rows.append({"date": day.isoformat(), "tcc_pct": None, "missing": True})
                    continue
                pct = tcc_exposure_pct(ts_list, corner_series, weights, start, end)
                rows.append({"date": day.isoformat(), "tcc_pct": round(pct, 6), "missing": False})
    return {
        "request_id": request_id,
        "market_id": unit["market_id"],
        "period": unit["period"],
        "row_count": len(rows),
        "missing_count": missing,
        "first_date": rows[0]["date"] if rows else None,
        "last_date": rows[-1]["date"] if rows else None,
        "rows": rows,
        "firewall": {
            "support_count": len(set().union(*[set(r["support_utc"]) for r in quarter_daily_plan(unit)])),
            "transport_count": quarter_transport(unit)["transport_count"],
            "extras_count": quarter_transport(unit)["extras"],
            "consumed_support": sum(len(r["support_utc"]) for r in quarter_daily_plan(unit)) if missing == 0 else 0,
            "consumed_extras": 0,
        },
        "raw_sha256": lookup["sha256"],
    }
