"""Stage 5C-R1: build the layered timestamp-contract audit for the spatial pilot.

Reads the accepted local raw stencil artifact (manifest request + NetCDF
``valid_time``) and the acceptance audit rows, reconstructs the four timestamp
sets, validates the relations, and writes a versioned audit that makes the
transport-vs-scientific layering explicit.

No network, no re-download, no rewrite of the raw artifact.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import SCHEMA_VERSION, V2Error, repo_root, sha256_file, write_json
from scripts.v2.spatial.build_taiex_spatial_pilot import EXCHANGE_ANCHOR, PILOT_TIMEZONE, pre_open_utc_plan
from scripts.v2.spatial.timestamp_contract import (
    assess_contract,
    canonical,
    scientific_target_set,
    transport_cartesian_set,
)

RAW_BASE = ".local/source-raw/v2/spatial/cds_era5_hourly_spatial/taiex-tcc-stencil-preopen"
ACCEPTANCE_AUDIT = "data/audits/v2/spatial/taiex-spatial-interpolation-acceptance.json"
CONTRACT_AUDIT = "data/audits/v2/spatial/taiex-spatial-timestamp-contract.json"

EXTRAS_REASON = (
    "CDS date x time Cartesian product over 6 UTC dates (2026-03-01..2026-03-06) "
    "x 2 hours (00:00, 23:00) yields 12 transport timestamps. The 10 scientific "
    "pre-open targets are a strict subset; the 2 extras are the boundary cells: "
    "the first requested date's 00:00 (before the first trading day) and the last "
    "requested date's 23:00 (after the last trading day). They are deterministic, "
    "known before the request, and never enter bilinear/nearest/legacy analysis."
)


def _read_manifest(root: Path) -> dict[str, Any]:
    raw_dir = root / RAW_BASE
    manifests = sorted(raw_dir.glob("r*-*.manifest.json"))
    if not manifests:
        raise V2Error("no spatial stencil manifest found")
    return json.loads(manifests[-1].read_text(encoding="utf-8"))


def _read_observed(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Return the hour-granularity UTC timestamps present in the raw NetCDF."""
    raw_dir = root / RAW_BASE
    zips = sorted(raw_dir.glob("r*-*.zip"))
    if not zips:
        raise V2Error("no spatial stencil raw zip found")
    import xarray as xr

    with zipfile.ZipFile(zips[-1]) as archive:
        member = [n for n in archive.namelist() if n.endswith(".nc")][0]
        with tempfile.TemporaryDirectory(prefix="weather-stock-v2-contract-") as td:
            extracted = Path(td) / member
            extracted.write_bytes(archive.read(member))
            ds = xr.open_dataset(extracted)
            time_var = ds["valid_time"] if "valid_time" in ds else ds["time"]
            observed = [canonical(str(t)) for t in time_var.values]
            ds.close()
    return observed


def _local_map(timestamps: list[str]) -> list[dict[str, Any]]:
    tz = ZoneInfo(PILOT_TIMEZONE)
    out = []
    for ts in timestamps:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(ts + ":00+00:00")
        local = dt.astimezone(tz)
        out.append(
            {
                "timestamp_utc": ts,
                "taipei_local": local.strftime("%Y-%m-%d %H:%M"),
                "weekday": local.strftime("%a"),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the spatial timestamp-contract audit")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    manifest = _read_manifest(root)
    request = manifest["request"]

    scientific = scientific_target_set()
    transport = transport_cartesian_set(request)
    observed = _read_observed(root, manifest)

    acceptance = json.loads((root / ACCEPTANCE_AUDIT).read_text(encoding="utf-8"))
    analysis_consumed = {canonical(r["timestamp_utc"]) for r in acceptance["rows"]}

    contract = assess_contract(transport, set(observed), scientific, analysis_consumed)

    scientific_list = sorted(scientific)
    transport_list = sorted(transport)
    observed_list = sorted(observed)
    analysis_list = sorted(analysis_consumed)

    audit = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_spatial_timestamp_contract",
        "primary_anchor": EXCHANGE_ANCHOR,
        "pilot_timezone": PILOT_TIMEZONE,
        "pre_open_local_window": "07:00-08:59 Asia/Taipei",
        "raw_artifact_id": manifest["artifact_id"],
        "raw_sha256": manifest["sha256"],
        "raw_artifact_relative": f"{RAW_BASE}/{Path(manifest['artifact_id'].split(':')[-1])}.zip",
        "request_date_list": request["date"],
        "request_time_list": request["time"],
        "contract": {
            "scientific_target_count": contract["scientific_target_count"],
            "transport_requested_count": contract["transport_requested_count"],
            "raw_observed_count": contract["raw_observed_count"],
            "analysis_consumed_count": contract["analysis_consumed_count"],
            "scientific_target_timestamps": scientific_list,
            "transport_requested_timestamps": transport_list,
            "raw_observed_timestamps": observed_list,
            "analysis_consumed_timestamps": analysis_list,
            "transport_exact_match": contract["transport_exact_match"],
            "scientific_subset_match": contract["scientific_subset_match"],
            "analysis_exact_target_match": contract["analysis_exact_target_match"],
            "no_extra_leakage_into_analysis": contract["no_extra_leakage_into_analysis"],
            "extra_transport_timestamps": contract["extra_transport_timestamps"],
            "extra_transport_timestamp_reason": EXTRAS_REASON,
            "extra_transport_local_map": _local_map(contract["extra_transport_timestamps"]),
        },
        "gate_pass": (
            contract["transport_exact_match"]
            and contract["scientific_subset_match"]
            and contract["analysis_exact_target_match"]
            and contract["no_extra_leakage_into_analysis"]
        ),
        "validator_semantics_note": (
            "fetch_era5._timestamp_validation validates the TRANSPORT layer "
            "(expected_timestamps(request) = date x time cartesian product = 12), "
            "never the scientific target (10). Scientific selection to 10 happens "
            "downstream in build_taiex_spatial_acceptance via pre_open_timestamps(). "
            "The two layers were never conflated; no validator bug."
        ),
        "code_sha256": {
            "timestamp_contract.py": sha256_file(root / "scripts/v2/spatial/timestamp_contract.py"),
            "build_taiex_spatial_pilot.py": sha256_file(root / "scripts/v2/spatial/build_taiex_spatial_pilot.py"),
            "build_taiex_spatial_acceptance.py": sha256_file(root / "scripts/v2/spatial/build_taiex_spatial_acceptance.py"),
        },
        "historical_backfill_run": False,
    }
    write_json(root / CONTRACT_AUDIT, audit)
    print(json.dumps(
        {
            "contract": contract,
            "gate_pass": audit["gate_pass"],
            "extras": contract["extra_transport_timestamps"],
            "extras_local": audit["contract"]["extra_transport_local_map"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
