"""Reconcile the TAIEX full-history calendar across three evidence layers.

Layers:
  A. Planned official calendar  -> TWSE annual schedules (closed + explicit open)
  B. Natural-disaster closure   -> DGPA Taipei status + frozen TWSE rule
  C. Observed market open       -> TWSE FMTQIK actual trading-date rows

Builds ``expected_open_dates`` and reconciles against
``observed_market_open_dates``, producing the four disagreement sets used by
the Calendar Gate.  Reads tracked audits only; writes the reconciliation
audit.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import SCHEMA_VERSION, load_json, repo_root, write_json

ANNUAL_AUDIT = "data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026.json"
DISASTER_AUDIT = "data/audits/v2/taiex-calendar/taiex-natural-disaster-closures-2020-2025.json"
MARKET_AUDIT = "data/audits/v2/taiex-calendar/taiex-market-open-observations-2020-2025.json"
SETTLEMENT_AUDIT = "data/audits/v2/taiex-calendar/taiex-settlement-only-corrections.json"
RECONCILIATION_AUDIT = "data/audits/v2/taiex-calendar/taiex-full-calendar-reconciliation-2020-2025.json"


def all_dates_in_range(start: str, end: str) -> list[str]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    result = []
    cur = s
    while cur <= e:
        result.append(cur.isoformat())
        cur += timedelta(days=1)
    return result


def build_expected_sets(
    annual: dict[str, Any],
    disaster_dates: set[str],
    start: str,
    end: str,
    settlement_corrections: set[str] | None = None,
) -> tuple[set[str], set[str]]:
    """Return (expected_open, expected_closed) for the target range."""
    settlement_corrections = settlement_corrections or set()
    planned_closed: set[str] = set()
    explicit_open: set[str] = set()
    for year, rec in annual["year_records"].items():
        planned_closed.update(rec.get("closed_official_dates", []))
        explicit_open.update(rec.get("explicit_open_dates", []))
    # Settlement-only dates were misclassified as explicit-open in the accepted
    # annual schedule; correct them to closed without altering that audit.
    explicit_open -= settlement_corrections
    planned_closed |= settlement_corrections
    expected_open: set[str] = set()
    expected_closed: set[str] = set()
    for iso in all_dates_in_range(start, end):
        d = date.fromisoformat(iso)
        if iso in disaster_dates:
            expected_closed.add(iso)
        elif iso in planned_closed:
            expected_closed.add(iso)
        elif iso in explicit_open:
            expected_open.add(iso)
        elif d.weekday() >= 5:
            expected_closed.add(iso)
        else:
            expected_open.add(iso)
    return expected_open, expected_closed


def reconcile(
    expected_open: set[str], expected_closed: set[str], observed_open: set[str]
) -> dict[str, Any]:
    all_dates = expected_open | expected_closed
    observed_closed = all_dates - observed_open
    return {
        "expected_open_observed_open": sorted(expected_open & observed_open),
        "expected_closed_observed_closed": sorted(expected_closed & observed_closed),
        "expected_open_but_no_market_record": sorted(expected_open - observed_open),
        "expected_closed_but_market_record": sorted(expected_closed & observed_open),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile TAIEX full-history calendar")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2025-12-31")
    args = parser.parse_args(argv)
    root = args.root

    annual = load_json(root / ANNUAL_AUDIT)
    disaster = load_json(root / DISASTER_AUDIT)
    market = load_json(root / MARKET_AUDIT)

    settlement_path = root / SETTLEMENT_AUDIT
    settlement = load_json(settlement_path) if settlement_path.exists() else {}
    settlement_dates = set(settlement.get("corrected_dates", []))

    disaster_dates = set(disaster.get("natural_disaster_closure_dates", []))
    observed_open = set(market.get("observed_market_open_dates", []))
    unresolved_disaster = disaster.get("ambiguous_missing_count", 0)
    months_failed = market.get("months_failed", [])

    expected_open, expected_closed = build_expected_sets(
        annual, disaster_dates, args.start, args.end, settlement_dates
    )
    sets = reconcile(expected_open, expected_closed, observed_open)

    unresolved_residual = (
        len(sets["expected_open_but_no_market_record"])
        + len(sets["expected_closed_but_market_record"])
    )
    unresolved_total = unresolved_residual + unresolved_disaster + len(months_failed)

    gate = (
        "PASS_FULL_HISTORY_CALENDAR_2020_2025"
        if unresolved_total == 0
        else "REVISE_FULL_HISTORY_CALENDAR_2020_2025"
    )

    audit = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_full_calendar_reconciliation_2020_2025",
        "target_period_start": args.start,
        "target_period_end": args.end,
        "expected_open_count": len(expected_open),
        "expected_closed_count": len(expected_closed),
        "observed_open_count": len(observed_open),
        "match_count": len(sets["expected_open_observed_open"]),
        "expected_open_but_no_market_record_count": len(sets["expected_open_but_no_market_record"]),
        "expected_closed_but_market_record_count": len(sets["expected_closed_but_market_record"]),
        "unresolved_disaster_count": unresolved_disaster,
        "unresolved_month_count": len(months_failed),
        "unresolved_residual_total": unresolved_residual,
        "unresolved_total": unresolved_total,
        "calendar_gate": gate,
        "full_history_calendar_verified": gate == "PASS_FULL_HISTORY_CALENDAR_2020_2025",
        "reconciliation_sets": sets,
        "months_failed": months_failed,
        "ambiguous_missing_disaster": disaster.get("ambiguous_missing", []),
    }
    write_json(root / RECONCILIATION_AUDIT, audit)
    import json

    print(
        json.dumps(
            {
                "calendar_gate": gate,
                "expected_open_count": audit["expected_open_count"],
                "observed_open_count": audit["observed_open_count"],
                "match_count": audit["match_count"],
                "expected_open_but_no_market_record_count": audit["expected_open_but_no_market_record_count"],
                "expected_closed_but_market_record_count": audit["expected_closed_but_market_record_count"],
                "unresolved_disaster_count": unresolved_disaster,
                "unresolved_month_count": len(months_failed),
                "unresolved_total": unresolved_total,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
