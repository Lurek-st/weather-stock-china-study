"""Stage 5E-3C canonical full backfill plan builder (ZERO network).

Builds the deterministic 960-unit formal quarterly backfill plan for
1991-01-01 .. 2020-12-31 across the 8 frozen research markets, using ONLY the
existing Stage 5E-3B production planning logic (production_unit engine).  No
second scientific planner is introduced.

Canonical ordering (frozen): YEAR ascending -> QUARTER ascending ->
MARKET_ORDER.  Every unit carries ordinal / unit_key / market_id / year /
quarter / period / local_start / local_end / daily_exposure_count /
request_date_count / request_time_count / request_dates / request_times /
anchor_hash / final_request_id (all request identities under
REQUEST_IDENTITY_CONTRACT_VERSION 1.1.0).

The STATIC global_backfill_plan_hash binds ONLY the static scientific plan
(schema version, baseline, market order, ordering contract, identity version,
and the 960 ordered (ordinal, unit_key, final_request_id) entries).  It must
NOT bind accepted/missing status, download timestamps, raw SHAs, machine
paths, or progress -- otherwise the hash would change as units are retrieved.

Output: data/audits/v2/climatology/full-era5-backfill-plan-1991-2020.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.climatology.production_canary import EXPECTED_TIMEZONE_CANARY_HASH, REQUEST_IDENTITY_CONTRACT_VERSION
from scripts.v2.climatology.production_unit import final_request_id_for, load_anchor, quarter_daily_plan, unit_plan_counts
from scripts.v2.core import V2Error, load_json, repo_root

PLAN_SCHEMA_VERSION = "1.0.0"
BASELINE_START = date(1991, 1, 1)
BASELINE_END = date(2020, 12, 31)
MARKET_ORDER = [
    "sse_composite",
    "szse_component",
    "topix",
    "nifty50",
    "ftse100",
    "dax",
    "sp500",
    "bse50",
]
ORDERING_CONTRACT = "year_quarter_market"
PLAN_OUTPUT = "data/audits/v2/climatology/full-era5-backfill-plan-1991-2020.json"


def quarter_span(year: int, quarter: int) -> tuple[date, date]:
    starts = {1: date(year, 1, 1), 2: date(year, 4, 1), 3: date(year, 7, 1), 4: date(year, 10, 1)}
    ends = {1: date(year, 3, 31), 2: date(year, 6, 30), 3: date(year, 9, 30), 4: date(year, 12, 31)}
    return starts[quarter], ends[quarter]


def period_label(year: int, quarter: int) -> str:
    return f"{year}Q{quarter}"


def unit_key(market_id: str, year: int, quarter: int) -> str:
    return f"{market_id}:{period_label(year, quarter)}"


def generate_plan_units() -> list[dict[str, Any]]:
    """Programmatic 960-unit plan in frozen canonical order (no hand rows)."""
    units: list[dict[str, Any]] = []
    ordinal = 0
    for year in range(BASELINE_START.year, BASELINE_END.year + 1):
        for quarter in range(1, 5):
            for market_id in MARKET_ORDER:
                ordinal += 1
                start, end = quarter_span(year, quarter)
                unit = {"market_id": market_id, "period": period_label(year, quarter), "start": start, "end": end}
                rows = quarter_daily_plan(unit)
                counts = unit_plan_counts(unit, rows=rows)
                anchor = load_anchor(market_id)
                units.append(
                    {
                        "ordinal": ordinal,
                        "unit_key": unit_key(market_id, year, quarter),
                        "market_id": market_id,
                        "year": year,
                        "quarter": quarter,
                        "period": period_label(year, quarter),
                        "local_start": start.isoformat(),
                        "local_end": end.isoformat(),
                        "daily_exposure_count": counts["daily_exposures"],
                        "request_date_count": len(counts["request_dates"]),
                        "request_time_count": len(counts["request_times"]),
                        "request_dates": counts["request_dates"],
                        "request_times": counts["request_times"],
                        "anchor_hash": anchor["anchor_hash"],
                        "final_request_id": final_request_id_for(unit),
                    }
                )
    return units


def global_plan_hash(plan: dict[str, Any]) -> str:
    """Static scientific plan hash (does NOT bind progress/acceptance state)."""
    lines = [
        f"plan_schema_version={PLAN_SCHEMA_VERSION}",
        f"baseline={BASELINE_START.isoformat()}..{BASELINE_END.isoformat()}",
        f"market_order={','.join(MARKET_ORDER)}",
        f"ordering_contract={ORDERING_CONTRACT}",
        f"request_identity_contract_version={REQUEST_IDENTITY_CONTRACT_VERSION}",
    ]
    for unit in plan["units"]:
        lines.append(f"{unit['ordinal']}|{unit['unit_key']}|{unit['final_request_id']}")
    canonical = "\n".join(lines)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def exposure_invariants(plan: dict[str, Any]) -> dict[str, Any]:
    """Global exposure invariants: 960 units / 87,600 exposures etc."""
    units = plan["units"]
    total_exposures = sum(u["daily_exposure_count"] for u in units)
    per_market = {m: sum(u["daily_exposure_count"] for u in units if u["market_id"] == m) for m in MARKET_ORDER}
    per_year = {}
    for y in range(BASELINE_START.year, BASELINE_END.year + 1):
        per_year[str(y)] = sum(u["daily_exposure_count"] for u in units if u["year"] == y)
    per_market_year = {}
    for m in MARKET_ORDER:
        per_market_year[m] = {}
        for y in range(BASELINE_START.year, BASELINE_END.year + 1):
            per_market_year[m][str(y)] = sum(
                u["daily_exposure_count"] for u in units if u["market_id"] == m and u["year"] == y
            )
    return {
        "formal_unit_count": len(units),
        "total_daily_exposures": total_exposures,
        "per_market_daily_exposures": per_market,
        "per_year_daily_exposures": per_year,
        "per_market_year_daily_exposures": per_market_year,
        "invariants_hold": (
            len(units) == 960
            and total_exposures == 87_600
            and all(v == 10_950 for v in per_market.values())
            and all(v == 2_920 for v in per_year.values())
            and all(
                all(v == 365 for v in per_market_year[m].values())
                for m in MARKET_ORDER
            )
        ),
    }


def build_plan(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    units = generate_plan_units()
    if len(units) != 960:
        raise V2Error(f"generated {len(units)} units != 960")
    keys = [u["unit_key"] for u in units]
    ids = [u["final_request_id"] for u in units]
    if len(set(keys)) != 960:
        raise V2Error("unit_key collision in canonical plan")
    if len(set(ids)) != 960:
        raise V2Error("final_request_id collision in canonical plan")
    if any(not u["final_request_id"] for u in units):
        raise V2Error("null final_request_id in canonical plan")
    if any(len(u["final_request_id"]) != 64 for u in units):
        raise V2Error("malformed final_request_id in canonical plan")
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "audit_type": "full_era5_backfill_plan",
        "request_identity_contract_version": REQUEST_IDENTITY_CONTRACT_VERSION,
        "baseline": {"start": BASELINE_START.isoformat(), "end": BASELINE_END.isoformat()},
        "market_order": list(MARKET_ORDER),
        "ordering_contract": ORDERING_CONTRACT,
        "formal_unit_count": len(units),
        "units": units,
    }
    plan["exposure_invariants"] = exposure_invariants(plan)
    plan["global_backfill_plan_hash"] = global_plan_hash(plan)
    if not plan["exposure_invariants"]["invariants_hold"]:
        raise V2Error("global exposure invariants FAIL")
    return plan


def rebuild_verify(plan: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Recompute plan from code and verify byte-identical + hash-identical.

    Full 960 rebuild is intentionally done ONCE at build time (not in the
    fast test loop); tests re-derive a deterministic sample instead.
    """
    rebuilt = build_plan(root)
    same_units = json.dumps(rebuilt["units"], sort_keys=True, ensure_ascii=False) == json.dumps(
        plan["units"], sort_keys=True, ensure_ascii=False
    )
    return {
        "byte_identical": same_units,
        "hash_identical": rebuilt["global_backfill_plan_hash"] == plan["global_backfill_plan_hash"],
        "rebuilt_hash": rebuilt["global_backfill_plan_hash"],
        "stored_hash": plan["global_backfill_plan_hash"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 5E-3C canonical full backfill plan (ZERO network)")
    parser.add_argument("--write", action="store_true", help="write the canonical plan audit (tracked)")
    parser.add_argument("--verify", action="store_true", help="full rebuild + byte/hash verify (slow, ~90s)")
    args = parser.parse_args(argv)
    root = repo_root()
    plan = build_plan(root)
    print(json.dumps(
        {
            "formal_unit_count": plan["formal_unit_count"],
            "global_backfill_plan_hash": plan["global_backfill_plan_hash"],
            "invariants_hold": plan["exposure_invariants"]["invariants_hold"],
            "total_daily_exposures": plan["exposure_invariants"]["total_daily_exposures"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    if args.write:
        target = root / PLAN_OUTPUT
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"WROTE {PLAN_OUTPUT}")
    if args.verify:
        stored = load_json(root / PLAN_OUTPUT)
        result = rebuild_verify(stored, root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not (result["byte_identical"] and result["hash_identical"]):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
