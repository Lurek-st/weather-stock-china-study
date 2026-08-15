"""Stage 5E-3C-R2 hermetic test helpers (ZERO network).

Shared fakes for controller orchestration tests that stub the engine
boundary:

- ``fake_execute_persisting`` : stands in for ``execute_unit_once`` AND
  persists a fake accepted raw artifact into the hermetic RawArtifactStore so
  the R2 derived stage can bind raw_sha256 (the real engine would persist;
  the stub must too, otherwise classify_units would not see accepted raw).
- ``fake_derived_extractor`` : stands in for the frozen scientific extractor
  (``production_unit.extract_unit_exposures``) using the SAME scientific
  helpers (common_daily_dates / unit_plan_counts) so the produced artifact
  passes the frozen derived acceptance predicate.  It never re-implements the
  science; it just shapes a deterministic hermetic output.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from scripts.v2.climatology.production_unit import (
    common_daily_dates,
    final_request_id_for,
    unit_plan_counts,
)
from scripts.v2.core import RawArtifactStore

FAKE_RAW_PAYLOAD = b"PK\x03\x04fakezip-r2"
FAKE_RAW_SHA = hashlib.sha256(FAKE_RAW_PAYLOAD).hexdigest()

# Production stores that hermetic tests MUST NEVER touch (Stage 5E-3C-R2
# isolation contract, post 2x pollution incidents).  Any resolution that
# lands on these is a wiring defect -> fail fast.
PRODUCTION_RAW_BASE = "data/source_raw/v2/climatology"
PRODUCTION_DERIVED_BASE = "data/canonical/v2/climatology/full-backfill"


def assert_isolation(raw_base: str | None, derived_base: str | None, root: Path | None = None) -> None:
    """Fail fast if a hermetic test base resolves onto a production store.

    Both test bases MUST be explicit (never the production default); and,
    when resolved, MUST NOT be inside the production raw/derived trees.
    """
    root = root or Path.cwd()
    raw = Path(raw_base) if raw_base else None
    derived = Path(derived_base) if derived_base else None
    prod_raw = (root / PRODUCTION_RAW_BASE).resolve()
    prod_derived = (root / PRODUCTION_DERIVED_BASE).resolve()
    if raw is None or raw == Path(""):
        raise AssertionError("hermetic raw_base must be explicit (production default forbidden)")
    raw_r = raw.resolve()
    if raw_r == prod_raw or prod_raw in raw_r.parents or raw_r in prod_raw.parents:
        raise AssertionError(f"hermetic raw_base resolves onto production store: {raw_r}")
    if derived is not None:
        derived_r = derived.resolve()
        if derived_r == prod_derived or prod_derived in derived_r.parents or derived_r in prod_derived.parents:
            raise AssertionError(f"hermetic derived_base resolves onto production store: {derived_r}")


def fake_execute_persisting(
    unit: dict[str, Any],
    root: Path | None = None,
    client_factory: Any = None,
    retrieve_calls_tracker: list[int] | None = None,
    raw_base: str | None = None,
) -> dict[str, Any]:
    """Stub execute_unit_once that ALSO persists a fake accepted raw.

    Mirrors what the real engine does (retrieve -> validate -> persist) using
    a fake payload, so the derived stage has an accepted raw to bind.

    ``raw_base`` defaults to the controller's CURRENT RAW_BASE attribute
    (which hermetic fixtures monkeypatch to a pytest tmp_path), so direct
    assignment ``ctl.execute_unit_once = fake_execute_persisting`` stays
    hermetic and NEVER writes the real repository raw store.  Isolation is
    enforced with an explicit fail-fast assertion.
    """
    import scripts.v2.climatology.full_backfill_controller as ctl

    from scripts.v2.climatology.production_unit import cds_request_for

    if retrieve_calls_tracker is not None:
        retrieve_calls_tracker.append("cds")
    if raw_base is None:
        raw_base = ctl.RAW_BASE
    # raw isolation enforced here; derived isolation is enforced by the fixture
    # (derived_base lives in the derived_store module, not the controller).
    assert_isolation(raw_base, None, root)
    base = Path(raw_base) if raw_base and Path(raw_base).is_absolute() else Path(root) / raw_base
    store = RawArtifactStore(base)
    request_id = final_request_id_for(unit)
    store.persist(
        source_id="cds_era5_hourly_climatology",
        provider="ECMWF",
        logical_name=f"{unit['market_id']}-{unit['period'].lower()}-prod-fake",
        payload=FAKE_RAW_PAYLOAD,
        request=cds_request_for(unit),
        status="final",
        licence="cc",
        suffix=".zip",
        validation_metadata={"container_validation_passed": True, "raw_suffix": ".zip"},
        final_request_id=request_id,
    )
    return {
        "skipped": False,
        "market_id": unit["market_id"],
        "period": unit["period"],
        "retrieve_calls": 1,
        "request_id": request_id,
        "artifact_id": f"fake:{unit['market_id']}:{unit['period']}",
        "sha256": FAKE_RAW_SHA,
        "bytes": len(FAKE_RAW_PAYLOAD),
    }


def fake_derived_extractor(unit: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Deterministic hermetic extractor shaped with the frozen science helpers.

    ``raw_sha256`` is always FAKE_RAW_SHA: every hermetic raw artifact
    (seeded skips + fake-execute new units) uses FAKE_RAW_PAYLOAD, so the
    accepted raw SHA is identical across the hermetic store.  The REAL
    repository raw store is never touched (it holds the 8 real accepted
    units whose SHAs differ).
    """
    root = root or Path.cwd()
    dates = [d.isoformat() for d in common_daily_dates(unit)]
    counts = unit_plan_counts(unit)
    rows = [{"date": d, "tcc_pct": 50.0, "missing": False} for d in dates]
    request_id = final_request_id_for(unit)
    # All hermetic raw artifacts (seeded skips + fake-execute new units) use
    # FAKE_RAW_PAYLOAD, so every accepted raw SHA is FAKE_RAW_SHA.  Do NOT
    # lookup the REAL repository raw store here (it would collide with the
    # 8 real accepted units and yield a mismatched SHA).
    raw_sha = FAKE_RAW_SHA
    return {
        "request_id": request_id,
        "market_id": unit["market_id"],
        "period": unit["period"],
        "row_count": len(rows),
        "missing_count": 0,
        "first_date": rows[0]["date"] if rows else None,
        "last_date": rows[-1]["date"] if rows else None,
        "rows": rows,
        "firewall": {
            "support_count": counts["support"],
            "transport_count": counts["transport"],
            "extras_count": counts["extras"],
            "consumed_support": counts["support"],
            "consumed_extras": 0,
        },
        "raw_sha256": raw_sha,
    }
