"""Stage 5E-3B cross-market production qualification wrapper (authorized units only).

Stage 5E-3C refactor: the AUTHORIZATION-FREE scientific engine now lives in
``production_unit.py``.  This module is the Stage 5E-3B stage-specific wrapper:

    AUTHORIZED_UNITS        = exact Stage 5E-3B seven units (unchanged)
    assert_stage5e3b_authorized(unit)
    run_unit_live(...)      = authorize THEN engine.execute_unit_once(...)

The 5E-3B fail-closed contract is preserved bit-for-bit: a market / year /
quarter outside the 7-unit registry (or the SP500 already qualified in 5E-3A)
FAILS CLOSED before any client is constructed.  No 960-request loop is
implemented here; the future full controller uses its OWN authorization gate
(``assert_full_backfill_authorized``) with this same engine.

Every unit is a FULL-BACKFILL-REUSABLE unit: it uses the exact scientific /
request contract of the future 1991-2020 quarterly backfill, so the future
full runner can look up the accepted request_id and PRE-NETWORK SKIP.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import RawArtifactStore, V2Error, repo_root
from scripts.v2.climatology.production_unit import (
    RAW_BASE,
    RAW_SOURCE_ID,
    cds_request_for,
    common_daily_dates,
    execute_unit_once,
    extract_unit_exposures,
    final_request_id_for,
    identity_payload,
    load_anchor,
    market_spec,
    quarter_daily_plan,
    quarter_transport,
    unit_plan_counts,
)

# Stage 5E-3B authorization registry: the EXACT 7 production units.
# (market_id, period_label, start, end)
AUTHORIZED_UNITS: list[dict[str, Any]] = [
    {"market_id": "sse_composite", "period": "1991Q3", "start": date(1991, 7, 1), "end": date(1991, 9, 30)},
    {"market_id": "szse_component", "period": "1992Q3", "start": date(1992, 7, 1), "end": date(1992, 9, 30)},
    {"market_id": "topix", "period": "2020Q1", "start": date(2020, 1, 1), "end": date(2020, 3, 31)},
    {"market_id": "nifty50", "period": "1991Q3", "start": date(1991, 7, 1), "end": date(1991, 9, 30)},
    {"market_id": "ftse100", "period": "1995Q4", "start": date(1995, 10, 1), "end": date(1995, 12, 31)},
    {"market_id": "dax", "period": "1996Q4", "start": date(1996, 10, 1), "end": date(1996, 12, 31)},
    {"market_id": "bse50", "period": "1991Q1", "start": date(1991, 1, 1), "end": date(1991, 3, 31)},
]
EXECUTION_ORDER = ["sse_composite", "ftse100", "dax", "nifty50", "topix", "szse_component", "bse50"]
EXCLUDED_ALREADY_QUALIFIED = {"sp500"}

# Aggregate expected counts (from the Stage 5E-3B spec).
EXPECTED_AGGREGATE = {"exposures": 640, "support": 2286, "transport": 2581, "extras": 295, "max_fields": 465}
EXPECTED_TOTAL_FORMAL_UNITS = 960
SP500_EXISTING_UNIT = {"market_id": "sp500", "period": "2007Q1"}


# ---------------------------------------------------------------------------
# Stage 5E-3B authorization gate
# ---------------------------------------------------------------------------


def aggregate_counts() -> dict[str, Any]:
    """5E-3B aggregate planning counts over the AUTHORIZED_UNITS registry."""
    totals = {"exposures": 0, "support": 0, "transport": 0, "extras": 0, "max_fields": 0}
    per_market = {}
    for unit in AUTHORIZED_UNITS:
        c = unit_plan_counts(unit)
        per_market[unit["market_id"]] = c
        totals["exposures"] += c["daily_exposures"]
        totals["support"] += c["support"]
        totals["transport"] += c["transport"]
        totals["extras"] += c["extras"]
        totals["max_fields"] = max(totals["max_fields"], c["fields"])
    return {"totals": totals, "per_market": per_market}


def assert_stage5e3b_authorized(unit: dict[str, Any]) -> None:
    """Fail closed unless the unit is in the Stage 5E-3B authorization registry."""
    if unit["market_id"] in EXCLUDED_ALREADY_QUALIFIED:
        raise V2Error(f"{unit['market_id']} already qualified in 5E-3A; not re-requestable")
    for authorized in AUTHORIZED_UNITS:
        if (
            authorized["market_id"] == unit["market_id"]
            and authorized["period"] == unit["period"]
            and authorized["start"] == unit["start"]
            and authorized["end"] == unit["end"]
        ):
            return
    raise V2Error(f"unauthorized unit: {unit['market_id']} {unit['period']}")


# Alias kept for the Stage 5E-3B test contract.
assert_authorized = assert_stage5e3b_authorized


def unit_from_args(market_id: str, year: int, quarter: int) -> dict[str, Any]:
    """Reconstruct the authorized unit dict from CLI args (fail closed)."""
    for authorized in AUTHORIZED_UNITS:
        start = authorized["start"]
        if market_id == authorized["market_id"] and start.year == year and (start.month - 1) // 3 + 1 == quarter:
            return dict(authorized)
    raise V2Error(f"unauthorized unit: {market_id} {year}Q{quarter}")


# ---------------------------------------------------------------------------
# Live runner (authorize THEN one bounded engine execution)
# ---------------------------------------------------------------------------


def run_unit_live(
    unit: dict[str, Any],
    root: Path | None = None,
    client_factory: Callable[[], Any] | None = None,
    retrieve_calls_tracker: list[int] | None = None,
) -> dict[str, Any]:
    """Execute ONE authorized production unit with at most ONE CDS retrieve.

    Stage 5E-3B semantics preserved: authorization is checked FIRST (fail
    closed), then the engine's request-aware pre-network idempotency lookup
    runs, then at most one bounded retrieve.
    """
    assert_stage5e3b_authorized(unit)
    return execute_unit_once(
        unit,
        root=root,
        client_factory=client_factory,
        retrieve_calls_tracker=retrieve_calls_tracker,
        raw_base=RAW_BASE,
    )


def extract_unit_exposures_wrapper(unit: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Stage-aware extraction wrapper (kept for CLI symmetry)."""
    return extract_unit_exposures(unit, root=root, raw_base=RAW_BASE)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 5E-3B quarterly production runner (authorized units only)")
    parser.add_argument("--market", type=str, help="market_id")
    parser.add_argument("--year", type=int, help="year of the authorized unit")
    parser.add_argument("--quarter", type=int, help="quarter (1-4) of the authorized unit")
    parser.add_argument("--plan", action="store_true", help="print pure planning counts (offline)")
    parser.add_argument("--live", action="store_true", help="run one authorized unit live (at most one retrieve)")
    parser.add_argument("--extract", action="store_true", help="extract daily exposures from accepted raw (offline)")
    args = parser.parse_args(argv)
    if args.plan:
        agg = aggregate_counts()
        print(json.dumps(agg, ensure_ascii=False, indent=2, default=str))
        return 0
    if not args.market or not args.year or not args.quarter:
        parser.error("--market/--year/--quarter required")
    unit = unit_from_args(args.market, args.year, args.quarter)
    if args.live:
        print(json.dumps(run_unit_live(unit), ensure_ascii=False, indent=2))
        return 0
    if args.extract:
        out = extract_unit_exposures_wrapper(unit)
        print(json.dumps({k: v for k, v in out.items() if k != "rows"}, ensure_ascii=False, indent=2))
        return 0
    parser.error("require --plan, --live or --extract")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
