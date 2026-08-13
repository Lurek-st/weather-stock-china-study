"""Build a settlement-only correction audit from TWSE raw annual schedules.

Some TWSE annual-schedule rows carry name "農曆春節前最後交易日" with a note
"X月X日市場無交易，僅辦理結算交割作業" (settlement-only, no trading).  The
accepted annual-schedule parser (``parse_twse_holiday_schedule.py``) classifies
such rows as explicit-open because it only checks the note for "不交易/不交割",
not "市場無交易".  Those dates therefore appear in ``explicit_open_dates`` even
though the market did not trade.

This module emits a tracked correction audit (``settlement_only``) so the
reconciliation layer can treat them as closed, WITHOUT altering the accepted
annual-schedule hashes (frozen by the control plane).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import SCHEMA_VERSION, repo_root, write_json

RAW_BASE = ".local/source-raw/taiex-calendar/twse_official_holiday_schedule"
CORRECTION_AUDIT = "data/audits/v2/taiex-calendar/taiex-settlement-only-corrections.json"

SETTLEMENT_NOTE_MARKERS = ("市場無交易", "無交易")
TRADING_DAY_NAME_MARKERS = ("交易日",)
# A true last-trading-day note states the day traded ("最後交易"); a
# settlement-only note states "X月X日市場無交易" for the row's own date.
LAST_TRADE_MARKER = "最後交易"


def scan_settlement_only(root: Path, start_year: int, end_year: int) -> list[dict[str, Any]]:
    """Scan raw TWSE annual schedules for settlement-only misclassified rows."""
    corrections: list[dict[str, Any]] = []
    for year in range(start_year, end_year + 1):
        year_dir = root / RAW_BASE / str(year)
        if not year_dir.exists():
            continue
        artifacts = sorted(
            p for p in year_dir.glob("r*-*.json") if not p.name.endswith(".manifest.json")
        )
        if not artifacts:
            continue
        payload = json.loads(artifacts[-1].read_text(encoding="utf-8"))
        for row in payload.get("data", []):
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            raw_day, name, note = str(row[0]), str(row[1]), str(row[2])
            is_trading_day_name = any(k in name for k in TRADING_DAY_NAME_MARKERS)
            is_settlement_note = any(k in note for k in SETTLEMENT_NOTE_MARKERS)
            is_last_trade = LAST_TRADE_MARKER in note
            # The row's own date is settlement-only only when its note states no
            # trading for that date and does NOT state it was a last-trading day.
            if is_trading_day_name and is_settlement_note and not is_last_trade:
                try:
                    iso = date.fromisoformat(raw_day).isoformat()
                except ValueError:
                    iso = raw_day
                corrections.append(
                    {
                        "date": iso,
                        "name": name,
                        "note": note,
                        "annual_schedule_status": "explicit_open",
                        "corrected_status": "closed_settlement_only",
                        "evidence": "TWSE annual-schedule note states no trading (settlement only)",
                        "raw_year": year,
                    }
                )
    return corrections


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build settlement-only correction audit")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--start-year", type=int, default=2020)
    parser.add_argument("--end-year", type=int, default=2025)
    args = parser.parse_args(argv)
    root = args.root
    corrections = scan_settlement_only(root, args.start_year, args.end_year)
    audit = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_settlement_only_corrections",
        "correction_count": len(corrections),
        "corrected_dates": sorted(c["date"] for c in corrections),
        "corrections": corrections,
    }
    write_json(root / CORRECTION_AUDIT, audit)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
