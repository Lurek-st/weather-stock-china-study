"""Stage 5E-3C-R2 durable derived-exposure artifact store (runtime wiring).

Durable, gitignored, per-unit derived daily-exposure artifacts for the full
backfill.  This module is ORCHESTRATION / PERSISTENCE / VALIDATION only: the
scientific extraction itself stays in the frozen engine
``production_unit.extract_unit_exposures`` (no second scientific extractor).

Authority hierarchy (frozen):

    STATIC                  canonical 960-unit plan
    IMMUTABLE               accepted RawArtifactStore raw
    DETERMINISTIC DERIVED   per-unit derived exposure artifact (this store)
    APPEND-ONLY             progress journal
    DERIVED CONVENIENCE     progress snapshot

A unit is ``derived_complete`` ONLY when its derived artifact passes the full
acceptance predicate (request_id-bound, raw_sha-bound, row counts exact, 0
missing, exact common-calendar dates, finite tcc, 0 consumed extras, firewall
counts canonical, payload hash exact).  Journal events are never authority by
themselves: a corrupt/missing artifact FAILS CLOSED, and a valid artifact wins
over a stale journal entry.

Artifacts are written atomically (temp + replace).  Existing valid artifact =
idempotent skip; existing invalid artifact = FAIL CLOSED (no silent overwrite).

Stage 5E-3C-R2 performs ZERO network: raw artifacts come from the local
RawArtifactStore; extraction is only invoked through the frozen engine.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.production_unit import (
    common_daily_dates,
    unit_plan_counts,
)
from scripts.v2.core import V2Error, repo_root

DERIVED_BASE = "data/canonical/v2/climatology/full-backfill"
DERIVED_SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def derived_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / DERIVED_BASE


def derived_path_for(plan_unit: dict[str, Any], root: Path | None = None) -> Path:
    """Deterministic per-unit derived artifact path (gitignored)."""
    root = root or repo_root()
    request_id = plan_unit["final_request_id"]
    filename = f"{plan_unit['period'].lower()}-{request_id[:8]}.json"
    return derived_dir(root) / plan_unit["market_id"] / filename


# ---------------------------------------------------------------------------
# Artifact build / hash
# ---------------------------------------------------------------------------


def build_derived_artifact(
    plan_unit: dict[str, Any],
    extract_out: dict[str, Any],
) -> dict[str, Any]:
    """Construct the derived artifact from the frozen extractor output."""
    artifact = {
        "schema_version": DERIVED_SCHEMA_VERSION,
        "unit_key": plan_unit["unit_key"],
        "market_id": plan_unit["market_id"],
        "period": plan_unit["period"],
        "final_request_id": plan_unit["final_request_id"],
        "raw_sha256": extract_out["raw_sha256"],
        "row_count": extract_out["row_count"],
        "missing_count": extract_out["missing_count"],
        "first_date": extract_out["first_date"],
        "last_date": extract_out["last_date"],
        "firewall": dict(extract_out["firewall"]),
        "rows": [dict(r) for r in extract_out["rows"]],
    }
    artifact["derived_payload_sha256"] = derived_payload_sha256(artifact)
    return artifact


def derived_payload_sha256(artifact: dict[str, Any]) -> str:
    """Canonical SHA256 of the scientific payload (never self-referential).

    The payload excludes ``derived_payload_sha256`` itself from its hash
    input; the hash is computed over every OTHER field of the artifact.
    """
    payload = {k: v for k, v in artifact.items() if k != "derived_payload_sha256"}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Acceptance predicate (frozen spec 6)
# ---------------------------------------------------------------------------


def validate_derived_artifact(
    plan_unit: dict[str, Any],
    artifact: dict[str, Any],
    expected_raw_sha: str,
    root: Path | None = None,
) -> list[str]:
    """Full derived acceptance predicate; returns problem list (empty = valid).

    FAIL CLOSED on any problem: never treat a partially-valid artifact as
    derived_complete.
    """
    problems: list[str] = []
    if not isinstance(artifact, dict):
        return ["derived artifact is not a dict"]
    if artifact.get("schema_version") != DERIVED_SCHEMA_VERSION:
        problems.append(f"schema_version={artifact.get('schema_version')} != {DERIVED_SCHEMA_VERSION}")
    if artifact.get("unit_key") != plan_unit["unit_key"]:
        problems.append("unit_key mismatch")
    if artifact.get("final_request_id") != plan_unit["final_request_id"]:
        problems.append("final_request_id mismatch")
    if artifact.get("raw_sha256") != expected_raw_sha:
        problems.append("raw_sha256 mismatch")
    expected_rows = plan_unit["daily_exposure_count"]
    if artifact.get("row_count") != expected_rows:
        problems.append(f"row_count={artifact.get('row_count')} != expected {expected_rows}")
    if artifact.get("missing_count") != 0:
        problems.append(f"missing_count={artifact.get('missing_count')} != 0")

    # Exact common-calendar dates (Feb29 excluded) in canonical order.
    engine = _engine_unit(plan_unit)
    expected_dates = [d.isoformat() for d in common_daily_dates(engine)]
    rows = artifact.get("rows") or []
    actual_dates = [r.get("date") for r in rows]
    if actual_dates != expected_dates:
        problems.append("common-calendar dates mismatch (order or membership)")

    # All tcc_pct finite / non-None for non-missing rows.
    for r in rows:
        if r.get("missing") is False:
            val = r.get("tcc_pct")
            if val is None or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                problems.append(f"non-finite tcc_pct at {r.get('date')}")

    # Firewall must equal canonical plan (support/transport/extras/consumed).
    firewall = artifact.get("firewall") or {}
    canonical_counts = unit_plan_counts(_engine_unit(plan_unit))
    if firewall.get("support_count") != canonical_counts["support"]:
        problems.append(f"firewall.support_count={firewall.get('support_count')} != {canonical_counts['support']}")
    if firewall.get("transport_count") != canonical_counts["transport"]:
        problems.append(f"firewall.transport_count={firewall.get('transport_count')} != {canonical_counts['transport']}")
    if firewall.get("extras_count") != canonical_counts["extras"]:
        problems.append(f"firewall.extras_count={firewall.get('extras_count')} != {canonical_counts['extras']}")
    if firewall.get("consumed_extras") != 0:
        problems.append(f"firewall.consumed_extras={firewall.get('consumed_extras')} != 0")
    if firewall.get("consumed_support") != canonical_counts["support"]:
        problems.append(f"firewall.consumed_support={firewall.get('consumed_support')} != {canonical_counts['support']}")

    # Payload hash exact (recompute; never trust a stored hash alone).
    if artifact.get("derived_payload_sha256") != derived_payload_sha256(artifact):
        problems.append("derived_payload_sha256 mismatch")
    return problems


def _engine_unit(plan_unit: dict[str, Any]) -> dict[str, Any]:
    """Map canonical plan row to the engine unit dict (start/end as date)."""
    from datetime import date as _date

    return {
        "market_id": plan_unit["market_id"],
        "period": plan_unit["period"],
        "start": _date.fromisoformat(plan_unit["local_start"]),
        "end": _date.fromisoformat(plan_unit["local_end"]),
    }


# ---------------------------------------------------------------------------
# Persistence (atomic; idempotent on valid; FAIL CLOSED on corrupt)
# ---------------------------------------------------------------------------


def persist_derived_atomic(
    plan_unit: dict[str, Any],
    artifact: dict[str, Any],
    expected_raw_sha: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Write the derived artifact atomically.

    - target missing  -> atomic create
    - target exists & valid -> idempotent skip (return already_valid)
    - target exists & invalid -> FAIL CLOSED (no silent overwrite)
    """
    root = root or repo_root()
    path = derived_path_for(plan_unit, root)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise V2Error(
                f"corrupt existing derived artifact (unreadable) for {plan_unit['unit_key']}; "
                "control-layer review required; no silent overwrite"
            ) from exc
        problems = validate_derived_artifact(plan_unit, existing, expected_raw_sha, root)
        if not problems:
            return {"written": False, "already_valid": True, "path": str(path)}
        raise V2Error(
            f"corrupt existing derived artifact for {plan_unit['unit_key']} "
            f"(problems: {problems[:3]}); control-layer review required; no silent overwrite"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {"written": True, "already_valid": False, "path": str(path)}


def load_derived_artifact(plan_unit: dict[str, Any], root: Path | None = None) -> dict[str, Any] | None:
    """Load the derived artifact if present; None when missing."""
    root = root or repo_root()
    path = derived_path_for(plan_unit, root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise V2Error(f"corrupt derived artifact (unreadable) for {plan_unit['unit_key']}") from exc


# ---------------------------------------------------------------------------
# Derived inventory (spec 17): complete / missing / invalid
# ---------------------------------------------------------------------------


def classify_derived_units(
    plan: dict[str, Any],
    root: Path | None = None,
    raw_classification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify every formal unit by derived-artifact validity.

    Reuses the raw acceptance scan to obtain the expected raw SHA per accepted
    unit (derived artifact must be raw_sha-bound).  A unit without accepted raw
    is neither complete nor invalid in the derived sense: it is ``missing``
    (derived requires raw first).  A present-but-corrupt artifact is ``invalid``
    and FAILS CLOSED.
    """
    root = root or repo_root()
    raw_cls = raw_classification or classify_raw_units(plan, root)
    raw_by_key = {u["unit_key"]: u for u in raw_cls["accepted"]}
    complete: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for unit in plan["units"]:
        raw = raw_by_key.get(unit["unit_key"])
        if raw is None:
            missing.append({"unit_key": unit["unit_key"], "final_request_id": unit["final_request_id"]})
            continue
        try:
            artifact = load_derived_artifact(unit, root)
        except V2Error as exc:
            invalid.append({"unit_key": unit["unit_key"], "final_request_id": unit["final_request_id"], "reason": str(exc)})
            continue
        if artifact is None:
            missing.append(
                {
                    "unit_key": unit["unit_key"],
                    "final_request_id": unit["final_request_id"],
                    "note": "raw_accepted_derived_missing",
                }
            )
            continue
        problems = validate_derived_artifact(unit, artifact, raw["sha256"], root)
        if problems:
            invalid.append(
                {"unit_key": unit["unit_key"], "final_request_id": unit["final_request_id"], "problems": problems[:5]}
            )
        else:
            complete.append(
                {"unit_key": unit["unit_key"], "final_request_id": unit["final_request_id"], "raw_sha256": raw["sha256"]}
            )
    return {"complete": complete, "missing": missing, "invalid": invalid}


def classify_raw_units(plan: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Re-export of raw classification (imported lazily to avoid cycles)."""
    from scripts.v2.climatology.full_backfill_controller import classify_units

    return classify_units(plan, root)
