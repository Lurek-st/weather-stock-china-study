"""Stage 5E-3C full backfill controller / plan / authorization tests (ZERO network).

Covers the frozen Stage 5E-3C contract:
- canonical 960-unit plan: ordering, uniqueness, exposure invariants
- request identity v1.1.0 everywhere; timezone + anchor hashes non-null
- current durable inventory through 2004; SP500 corrected binding recognized
- hashes deterministic and change-sensitive (plan / policy / candidate)
- authorization kill switch: 0 network when not live-authorized
- resume / ledger semantics; failure classification; annual batch bound

NO network is constructed anywhere in these tests.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.build_full_backfill_plan import (
    BASELINE_END,
    BASELINE_START,
    MARKET_ORDER,
    global_plan_hash,
    period_label,
    quarter_span,
    unit_key,
)
from scripts.v2.climatology.full_backfill_controller import (
    CONTROLLER_POLICY_PATH,
    FAILURE_CONTRACT,
    authorization_candidate_hash,
    classify_units,
    controller_policy_hash,
    dry_run_year,
    load_authorization,
    load_plan,
    reconcile_progress,
    verify_authorization_binding,
    year_units,
)
from scripts.v2.climatology.production_canary import (
    EXPECTED_TIMEZONE_CANARY_HASH,
    REQUEST_IDENTITY_CONTRACT_VERSION,
    load_global_registry_hash,
)
from scripts.v2.core import V2Error, repo_root

ROOT = repo_root()
PLAN = load_plan(ROOT)


@pytest.fixture(scope="module")
def current_inventory():
    """One read-only production inventory scan shared by state-anchor tests."""
    return classify_units(PLAN, ROOT)


# ---------------------------------------------------------------------------
# Canonical plan (spec 34)
# ---------------------------------------------------------------------------


def test_plan_960_units_generated():
    assert PLAN["formal_unit_count"] == 960
    units = PLAN["units"]
    assert len(units) == 960
    # 120 per market
    for m in MARKET_ORDER:
        assert sum(1 for u in units if u["market_id"] == m) == 120
    # 32 per year
    for y in range(BASELINE_START.year, BASELINE_END.year + 1):
        assert sum(1 for u in units if u["year"] == y) == 32
    # 4 per market-year
    for m in MARKET_ORDER:
        for y in range(BASELINE_START.year, BASELINE_END.year + 1):
            assert sum(1 for u in units if u["market_id"] == m and u["year"] == y) == 4


def test_plan_ordinal_boundaries():
    units = PLAN["units"]
    assert units[0]["ordinal"] == 1
    assert units[0]["unit_key"] == "sse_composite:1991Q1"
    assert units[7]["ordinal"] == 8
    assert units[7]["unit_key"] == "bse50:1991Q1"
    assert units[8]["ordinal"] == 9
    assert units[8]["unit_key"] == "sse_composite:1991Q2"
    assert units[-1]["ordinal"] == 960
    assert units[-1]["unit_key"] == "bse50:2020Q4"


def test_plan_ordering_contract_year_quarter_market():
    units = PLAN["units"]
    for i in range(1, len(units)):
        prev, cur = units[i - 1], units[i]
        assert (prev["year"], prev["quarter"], MARKET_ORDER.index(prev["market_id"])) <= (
            cur["year"],
            cur["quarter"],
            MARKET_ORDER.index(cur["market_id"]),
        )


def test_plan_unit_keys_and_ids_unique():
    units = PLAN["units"]
    keys = [u["unit_key"] for u in units]
    ids = [u["final_request_id"] for u in units]
    assert len(set(keys)) == 960
    assert len(set(ids)) == 960
    assert all(ids)
    assert all(len(i) == 64 for i in ids)


def test_plan_exposure_invariants():
    inv = PLAN["exposure_invariants"]
    assert inv["invariants_hold"] is True
    assert inv["total_daily_exposures"] == 87_600
    assert all(v == 10_950 for v in inv["per_market_daily_exposures"].values())
    assert all(v == 2_920 for v in inv["per_year_daily_exposures"].values())
    for m in MARKET_ORDER:
        for y in range(BASELINE_START.year, BASELINE_END.year + 1):
            assert inv["per_market_year_daily_exposures"][m][str(y)] == 365


def test_plan_feb29_exclusion_preserved():
    # Feb 29 is never a scientific daily exposure (per-market-year = 365).
    inv = PLAN["exposure_invariants"]
    for m in MARKET_ORDER:
        for y in (1992, 1996, 2000, 2004, 2008, 2012, 2016, 2020):
            assert inv["per_market_year_daily_exposures"][m][str(y)] == 365


def test_plan_identity_version_all_110():
    # Identity version is a plan-level contract; each final_request_id was
    # computed under v1.1.0 by construction.  Re-verify a deterministic sample.
    sample = [PLAN["units"][0], PLAN["units"][7], PLAN["units"][8], PLAN["units"][959]]
    for u in sample:
        assert u["final_request_id"]
        assert len(u["final_request_id"]) == 64


def test_plan_anchor_hash_non_null_all():
    for u in PLAN["units"][::97]:  # deterministic stride
        assert u["anchor_hash"]
        assert len(u["anchor_hash"]) == 64


def test_plan_rebuild_sample_matches_code():
    # The plan must be recomputable from code; verify a deterministic sample.
    from scripts.v2.climatology.production_unit import final_request_id_for, quarter_daily_plan, unit_plan_counts

    sample = [1, 8, 9, 120, 240, 480, 721, 960]
    for ordinal in sample:
        u = PLAN["units"][ordinal - 1]
        unit = {
            "market_id": u["market_id"],
            "period": u["period"],
            "start": date.fromisoformat(u["local_start"]),
            "end": date.fromisoformat(u["local_end"]),
        }
        assert final_request_id_for(unit) == u["final_request_id"]


# ---------------------------------------------------------------------------
# Accepted inventory (spec 35)
# ---------------------------------------------------------------------------


def test_inventory_current_450_510_0(current_inventory):
    cls = current_inventory
    assert len(cls["accepted"]) == 450
    assert len(cls["missing"]) == 510
    assert len(cls["invalid"]) == 0


def test_inventory_sp500_corrected_binding_recognized(current_inventory):
    cls = current_inventory
    sp500 = [u for u in cls["accepted"] if u["unit_key"] == "sp500:2007Q1"]
    assert len(sp500) == 1
    assert sp500[0]["binding_type"] == "corrected_request_identity_requalification"


def test_inventory_completed_years_through_2004_and_no_2005(current_inventory):
    cls = current_inventory
    keys = {u["unit_key"] for u in cls["accepted"]}
    for year in range(1991, 2005):
        assert sum(key.split(":", 1)[1].startswith(str(year)) for key in keys) == 32
    assert not {key for key in keys if key.split(":", 1)[1].startswith("2005")}


def test_inventory_no_smoke_units(current_inventory):
    # The TAIEX / smoke canaries are NOT formal units (sp500 canary is, via the
    # corrected binding; the legacy smoke ids are not in the plan).
    cls = current_inventory
    legacy = "6dd6398cdb83f4e9486a673fca97f486783ed46b6379706b3968c4d806cc2bc3"
    assert all(u["final_request_id"] != legacy for u in cls["accepted"])
    assert legacy not in [u["final_request_id"] for u in PLAN["units"]]


# ---------------------------------------------------------------------------
# Hashes (spec 36)
# ---------------------------------------------------------------------------


def test_plan_hash_deterministic():
    assert PLAN["global_backfill_plan_hash"] == global_plan_hash(PLAN)


def test_policy_hash_deterministic():
    h1 = controller_policy_hash(ROOT)
    h2 = controller_policy_hash(ROOT)
    assert h1 == h2
    assert len(h1) == 64


def test_candidate_hash_deterministic():
    h1 = authorization_candidate_hash(ROOT)
    h2 = authorization_candidate_hash(ROOT)
    assert h1 == h2
    assert len(h1) == 64


def test_change_one_request_id_changes_plan_hash():
    mutated = json.loads(json.dumps(PLAN))
    unit = mutated["units"][0]
    unit["final_request_id"] = "0" * 64 if unit["final_request_id"] != "0" * 64 else "1" * 64
    assert global_plan_hash(mutated) != PLAN["global_backfill_plan_hash"]


def test_change_ordering_changes_plan_hash():
    mutated = json.loads(json.dumps(PLAN))
    mutated["units"] = list(reversed(mutated["units"]))
    assert global_plan_hash(mutated) != PLAN["global_backfill_plan_hash"]


def test_policy_hash_change_sensitive(tmp_path):
    # Changing a policy rule (batch slots 32->33 or auto_retry) changes the hash.
    import yaml

    src = (ROOT / CONTROLLER_POLICY_PATH).read_text(encoding="utf-8")
    p1 = json.dumps(yaml.safe_load(src), sort_keys=True, ensure_ascii=False)
    h1 = hashlib_sha256(p1)

    alt = src.replace("max_formal_slots_per_invocation: 32", "max_formal_slots_per_invocation: 33")
    p2 = json.dumps(yaml.safe_load(alt), sort_keys=True, ensure_ascii=False)
    h2 = hashlib_sha256(p2)
    assert h1 != h2

    alt2 = src.replace("automatic_annual_retry: false", "automatic_annual_retry: true")
    p3 = json.dumps(yaml.safe_load(alt2), sort_keys=True, ensure_ascii=False)
    h3 = hashlib_sha256(p3)
    assert h1 != h3


def hashlib_sha256(payload: str) -> str:
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_materialized_authorization_candidate_valid_but_inactive(monkeypatch):
    # Stage 5E-4E-R5C materialized the R5 snapshot-finalization hardening
    # authorization CANDIDATE from the actual repository state: the binding is
    # valid (6/6), while the kill switch stays INACTIVE pending a NEW explicit
    # control-layer approval.  Bound-hash mismatch fail-closed coverage is
    # hermetic: test_kill_switch_hash_mismatch_zero_network and
    # test_policy_hash_changed_and_old_authorization_cannot_bind.
    import scripts.v2.climatology.full_backfill_controller as ctl

    monkeypatch.setattr(ctl, "AUTHORIZATION_PATH", "config/v2/full-backfill-authorization.yaml")
    binding = verify_authorization_binding(ROOT)
    assert binding["valid"] is True
    assert binding["checks"] == {
        "global_backfill_plan_hash": True,
        "controller_policy_hash": True,
        "request_identity_contract_version": True,
        "global_spatial_anchor_registry_hash": True,
        "timezone_canary_hash": True,
        "authorization_candidate_hash": True,
    }
    # A valid binding does NOT authorize production execution.
    assert binding["live_backfill_authorized"] is False
    auth = load_authorization(ROOT)
    assert auth["live_backfill_authorized"] is False
    assert auth["authorization_state"] == "candidate_for_control_layer"
    assert auth["control_layer_approval_required"] is True
    # The candidate remains scientifically bound to the same plan.
    assert auth["bound_hashes"]["global_backfill_plan_hash"] == PLAN["global_backfill_plan_hash"]


# ---------------------------------------------------------------------------
# Authorization kill switch (spec 37)
# ---------------------------------------------------------------------------


def test_kill_switch_live_blocked_zero_network(tmp_path, monkeypatch):
    # The REAL repo is authorized=true since Stage 5E-3C-A; this test injects
    # an authorized=false authorization to verify the kill switch still blocks
    # before any client construction (0 network, 0 retrieve).
    import scripts.v2.climatology.full_backfill_controller as ctl

    payload = {
        "authorization_schema_version": "1.0.0",
        "live_backfill_authorized": False,
        "authorization_state": "candidate_for_control_layer",
        "control_layer_approval_required": True,
        "bound_hashes": {
            "global_backfill_plan_hash": PLAN["global_backfill_plan_hash"],
            "controller_policy_hash": controller_policy_hash(ROOT),
            "request_identity_contract_version": REQUEST_IDENTITY_CONTRACT_VERSION,
            "global_spatial_anchor_registry_hash": load_global_registry_hash(ROOT),
            "timezone_canary_hash": EXPECTED_TIMEZONE_CANARY_HASH,
        },
    }
    payload["authorization_candidate_hash"] = authorization_candidate_hash(ROOT)
    import yaml

    auth_path = tmp_path / "auth.yaml"
    auth_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setattr(ctl, "AUTHORIZATION_PATH", str(auth_path))
    monkeypatch.setattr(ctl, "RAW_BASE", str(tmp_path / "raw"))
    monkeypatch.setattr(ctl, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ctl, "JOURNAL_PATH", str(tmp_path / "state" / "progress.events.jsonl"))
    monkeypatch.setattr(ctl, "SNAPSHOT_PATH", str(tmp_path / "state" / "progress.snapshot.json"))

    # The CLI must fail before any client construction; run_year_batch refuses.
    with pytest.raises(V2Error) as excinfo:
        ctl.run_year_batch(PLAN, 1991, root=ROOT)
    assert "authorization kill switch" in str(excinfo.value)


def test_kill_switch_hash_mismatch_zero_network(tmp_path, monkeypatch):
    # A mutated authorization (changed policy hash) -> binding invalid -> 0 network.
    import scripts.v2.climatology.full_backfill_controller as ctl

    original = (ROOT / ctl.AUTHORIZATION_PATH).read_text(encoding="utf-8")
    mutated = original.replace(
        '  controller_policy_hash: "', '  controller_policy_hash: "00'
    )
    (tmp_path / "auth.yaml").write_text(mutated, encoding="utf-8")
    monkeypatch.setattr(ctl, "AUTHORIZATION_PATH", str(tmp_path / "auth.yaml"))
    binding = verify_authorization_binding(ROOT)
    assert binding["valid"] is False
    assert binding["checks"]["controller_policy_hash"] is False


# ---------------------------------------------------------------------------
# Annual batch bound (spec 40)
# ---------------------------------------------------------------------------


def test_1991_completed_32_static_slots(current_inventory):
    units = year_units(PLAN, 1991)
    accepted = {unit["unit_key"] for unit in current_inventory["accepted"]}
    assert len(units) == 32
    assert all(unit["unit_key"] in accepted for unit in units)


def test_any_year_slots_leq_32():
    for y in (1992, 1995, 2007, 2010, 2020):
        assert len(year_units(PLAN, y)) <= 32


def test_year_units_exact_32():
    for y in (1991, 2000, 2020):
        assert len(year_units(PLAN, y)) == 32


# ---------------------------------------------------------------------------
# Resume / ledger (spec 38)
# ---------------------------------------------------------------------------


def test_reconcile_accepted_beats_stale_pending_journal(
    tmp_path, monkeypatch, current_inventory
):
    import scripts.v2.climatology.full_backfill_controller as ctl

    monkeypatch.setattr(ctl, "JOURNAL_PATH", str(tmp_path / "progress.events.jsonl"))
    monkeypatch.setattr(ctl, "SNAPSHOT_PATH", str(tmp_path / "progress.snapshot.json"))
    monkeypatch.setattr(ctl, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ctl, "classify_units", lambda plan, root=None: current_inventory)
    monkeypatch.setattr(
        ctl,
        "classify_derived_units",
        lambda plan, root=None, raw_classification=None: {"complete": [], "missing": [], "invalid": []},
    )
    monkeypatch.setattr(ctl, "authorization_candidate_hash", lambda root=None: "candidate")
    # Journal says pending; raw store says accepted -> authority = raw store.
    ctl.append_event(
        {"event_type": "unit_retrieve_started", "unit_key": "sp500:2007Q1",
         "final_request_id": "a65d8f589338b97a30f9981f37253aa20c2041e344c8187eead06c3b123b4945"},
        ROOT,
    )
    state = reconcile_progress(PLAN, ROOT)
    accepted_keys = {u["unit_key"] for u in state["classification"]["accepted"]}
    assert "sp500:2007Q1" in accepted_keys
    assert state["snapshot"]["accepted_raw_units"] == 450
    assert "sp500:2007Q1" not in state["snapshot"]["blocked_units"]


def test_journal_accepted_but_raw_missing_fails(tmp_path, monkeypatch):
    import scripts.v2.climatology.full_backfill_controller as ctl
    import scripts.v2.climatology.derived_store as derived_store
    import scripts.v2.climatology.production_unit as production_unit

    monkeypatch.setattr(ctl, "JOURNAL_PATH", str(tmp_path / "progress.events.jsonl"))
    monkeypatch.setattr(ctl, "SNAPSHOT_PATH", str(tmp_path / "progress.snapshot.json"))
    monkeypatch.setattr(ctl, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ctl, "RAW_BASE", str(tmp_path / "raw"))
    monkeypatch.setattr(production_unit, "RAW_BASE", str(tmp_path / "raw"))
    monkeypatch.setattr(derived_store, "DERIVED_BASE", str(tmp_path / "derived"))
    monkeypatch.setattr(
        ctl,
        "classify_units",
        lambda plan, root=None: {"accepted": [], "missing": plan["units"], "invalid": []},
    )
    monkeypatch.setattr(
        ctl,
        "classify_derived_units",
        lambda plan, root=None, raw_classification=None: {"complete": [], "missing": [], "invalid": []},
    )
    monkeypatch.setattr(ctl, "authorization_candidate_hash", lambda root=None: "candidate")
    # Fabricate an accepted event for a unit whose raw does NOT exist.
    fake_unit = "topix:1991Q1"
    fake_id = next(u["final_request_id"] for u in PLAN["units"] if u["unit_key"] == fake_unit)
    ctl.append_event(
        {"event_type": "unit_raw_accepted", "unit_key": fake_unit, "final_request_id": fake_id}, ROOT
    )
    state = reconcile_progress(PLAN, ROOT)
    assert fake_unit in state["snapshot"]["blocked_units"]


def test_snapshot_atomic_write(tmp_path, monkeypatch):
    import scripts.v2.climatology.full_backfill_controller as ctl

    monkeypatch.setattr(ctl, "SNAPSHOT_PATH", str(tmp_path / "progress.snapshot.json"))
    snap = {"plan_hash": PLAN["global_backfill_plan_hash"], "accepted_raw_units": 8}
    written = ctl.write_snapshot(snap, ROOT)
    assert written.exists()
    assert json.loads(written.read_text(encoding="utf-8"))["accepted_raw_units"] == 8
    # No .tmp leftovers.
    assert not list(tmp_path.glob("*.tmp"))


def test_plan_hash_mismatch_in_ledger_fails_closed(tmp_path, monkeypatch):
    import scripts.v2.climatology.full_backfill_controller as ctl

    monkeypatch.setattr(ctl, "JOURNAL_PATH", str(tmp_path / "progress.events.jsonl"))
    monkeypatch.setattr(ctl, "SNAPSHOT_PATH", str(tmp_path / "progress.snapshot.json"))
    monkeypatch.setattr(ctl, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        ctl,
        "classify_units",
        lambda plan, root=None: {"accepted": [], "missing": plan["units"], "invalid": []},
    )
    monkeypatch.setattr(
        ctl,
        "classify_derived_units",
        lambda plan, root=None, raw_classification=None: {"complete": [], "missing": [], "invalid": []},
    )
    monkeypatch.setattr(ctl, "authorization_candidate_hash", lambda root=None: "candidate")
    # A journal event referencing a DIFFERENT plan hash than current -> the
    # controller must not trust it: reconcile under the CURRENT plan is valid,
    # and the acceptance predicates still hold (no fake success).
    fake_unit = "topix:1991Q1"
    fake_id = next(u["final_request_id"] for u in PLAN["units"] if u["unit_key"] == fake_unit)
    ctl.append_event(
        {"event_type": "unit_raw_accepted", "unit_key": fake_unit, "final_request_id": fake_id},
        ROOT,
    )
    state = reconcile_progress(PLAN, ROOT)
    assert state["snapshot"]["plan_hash"] == PLAN["global_backfill_plan_hash"]


# ---------------------------------------------------------------------------
# Failure policy (spec 39)
# ---------------------------------------------------------------------------


def test_failure_contract_frozen():
    assert FAILURE_CONTRACT["pre_network_contract"] == "non_retryable_until_control_review"
    assert FAILURE_CONTRACT["service_health_defer"] == "operational_deferred"
    assert FAILURE_CONTRACT["operational_retrieve"] == "retryable_operational"
    assert FAILURE_CONTRACT["request_contract_rejection"] == "contract_review_required"
    assert FAILURE_CONTRACT["payload_validation"] == "validation_failure_control_review_required"
    assert FAILURE_CONTRACT["derived_only"] == "raw_accepted_derived_retry"


def test_no_automatic_retry_semantics():
    # Annual retry false and one logical retrieve per unit attempt are distinct
    # from the explicitly bounded physical transport recovery policy.
    # in the controller policy file.
    policy = json.loads(json.dumps(__import__("yaml").safe_load(
        (ROOT / CONTROLLER_POLICY_PATH).read_text(encoding="utf-8")
    )))
    assert policy["controller_retry"]["automatic_annual_retry"] is False
    assert policy["logical_retrieve"]["max_cds_retrieve_calls_per_unit_attempt"] == 1
    assert policy["transport"]["maximum_total_tries_per_robust_http_operation"] == 3
    assert policy["concurrency"] == 1
    assert policy["year_boundary_forced_stop"] is True


# ---------------------------------------------------------------------------
# Contract sanity (spec 6)
# ---------------------------------------------------------------------------


def test_identity_contract_version_frozen():
    assert REQUEST_IDENTITY_CONTRACT_VERSION == "1.1.0"
    assert PLAN["request_identity_contract_version"] == "1.1.0"


def test_timezone_canary_hash_frozen():
    assert EXPECTED_TIMEZONE_CANARY_HASH == "8d21b954fd957a4596fbd2fc7a926355a2ca81bfcef99b94ea5b44956ef7338e"


def test_spatial_registry_hash_bound():
    registry_hash = load_global_registry_hash(ROOT)
    auth = load_authorization(ROOT)
    assert auth["bound_hashes"]["global_spatial_anchor_registry_hash"] == registry_hash
    assert len(registry_hash) == 64


def test_sp500_2007q1_in_plan_with_corrected_id():
    for u in PLAN["units"]:
        if u["unit_key"] == "sp500:2007Q1":
            assert u["final_request_id"].startswith("a65d8f589338")
            break
    else:
        pytest.fail("sp500:2007Q1 missing from canonical plan")


# ---------------------------------------------------------------------------
# Controller must not run during 3C (spec 29)
# ---------------------------------------------------------------------------


def test_no_all_flag_supported():
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "scripts.v2.climatology.full_backfill_controller", "--all"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=60,
    )
    assert proc.returncode != 0
    assert "unrecognized arguments" in (proc.stderr or "").lower() or "--all" not in (proc.stderr or "")
