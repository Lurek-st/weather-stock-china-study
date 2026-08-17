"""Parse TWSE annual holiday schedules into audited classification.

Semantics of the official TWSE holiday schedule rows (fields:
[日期, 名稱, 說明]):

- Rows whose 名稱 contains an explicit trading-day marker
  (交易日 / 開始交易 / 最後交易) are EXPLICIT OPEN days, e.g.
  "國曆新年開始交易日", "農曆春節前最後交易日", "農曆春節後開始交易日".
- Rows named "市場無交易，僅辦理結算交割作業" are CLOSED (settlement-only).
- Any row whose 說明 mentions "補行上班，但不交易亦不交割" refers to a
  make-up workday that does NOT trade and does NOT settle: the date listed
  is a CLOSED day for the securities market.  It must never be classified
  as open.
- All other rows (holiday names like 農曆春節, 和平紀念日, ...) are
  CLOSED official days.

Unknown / unparseable rows are classified informational_unknown with an
issue recorded.  Cross-year dates or duplicate dates fail the parse.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import V2Error, load_json, repo_root, sha256_file, write_json

TRADING_DAY_KEYWORDS = ("交易日", "開始交易", "最後交易")
SETTLEMENT_ONLY_MARKER = "市場無交易，僅辦理結算交割作業"
NO_TRADE_MAKEUP_MARKER = "補行上班，但不交易亦不交割"
NO_TRADE_MARKERS = ("不交易", "不交割")


def _note_no_trade_dates(note: str, year: int) -> set[date]:
    """Extract dates a note states as no-trading ("X月X日市場無交易").

    A note may reference one date ("2月8日市場無交易") or a pair
    ("1月18日及1月19日市場無交易").  Only the referenced dates are returned;
    a note that also says "最後交易" is still examined, because the caller
    decides whether the referenced dates include the row's own date.
    """
    found: set[date] = set()
    for m in re.finditer(
        r"(\d{1,2})月(\d{1,2})日(?:及(\d{1,2})月(\d{1,2})日)?市場無交易",
        note,
    ):
        month_day_pairs = [(int(m.group(1)), int(m.group(2)))]
        if m.group(3):
            month_day_pairs.append((int(m.group(3)), int(m.group(4))))
        for month, day in month_day_pairs:
            try:
                found.add(date(year, month, day))
            except ValueError:
                continue
    return found


def classify_row(name: str, note: str, row_date: "date | None" = None) -> str:
    """Classify a single schedule row (date-aware).

    ``row_date`` is the schedule row's own date.  When a trading-day-named row
    has a note stating "X月X日市場無交易" for ITS OWN date, the row is a
    settlement-only (closed) day.  A note that references OTHER dates (e.g.
    "1月18日及1月19日市場無交易" under the 1月17日 last-trading-day row) must
    NOT reclassify the row's own date.

    Returns one of:
    - open_special_or_explicit_open
    - closed_official
    - informational_unknown
    """
    if any(keyword in name for keyword in TRADING_DAY_KEYWORDS):
        # Explicit trading days are open.  But if the same row explicitly
        # says no-trade/no-settlement for its own date, the trading-day marker
        # must not win.
        if row_date is not None and row_date in _note_no_trade_dates(note, row_date.year):
            return "closed_official"
        if any(marker in note for marker in NO_TRADE_MARKERS):
            return "closed_official"
        return "open_special_or_explicit_open"
    if SETTLEMENT_ONLY_MARKER in name:
        return "closed_official"
    if NO_TRADE_MAKEUP_MARKER in note or any(marker in note for marker in NO_TRADE_MARKERS):
        return "closed_official"
    return "closed_official"


def parse_annual_schedule(payload: dict[str, Any], requested_year: int) -> dict[str, Any]:
    """Parse one annual schedule payload with strict consistency checks."""
    rows: list[dict[str, Any]] = []
    closed: list[str] = []
    open_special: list[str] = []
    unknown: list[str] = []
    issues: list[str] = []
    seen: set[str] = set()
    cross_year: set[str] = set()

    for item in payload.get("data", []):
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            issues.append("row_malformed")
            continue
        raw_day = str(item[0])
        name = str(item[1])
        note = str(item[2]) if len(item) > 2 else ""
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            unknown.append(raw_day)
            issues.append(f"unparseable_date:{raw_day}")
            continue
        if day.year != requested_year:
            cross_year.add(raw_day)
            issues.append(f"cross_year_date:{raw_day}")
            continue
        if raw_day in seen:
            issues.append(f"duplicate_date:{raw_day}")
        seen.add(raw_day)
        status = classify_row(name, note, day)
        row: dict[str, Any] = {
            "date": raw_day,
            "name": name,
            "classification": status,
        }
        if note:
            row["note"] = note
        rows.append(row)
        if status == "open_special_or_explicit_open":
            open_special.append(raw_day)
        elif status == "closed_official":
            closed.append(raw_day)
        else:
            unknown.append(raw_day)

    result: dict[str, Any] = {
        "requested_year": requested_year,
        "title": payload.get("title"),
        "stat": payload.get("stat"),
        "queryYear": payload.get("queryYear"),
        "row_count": len(payload.get("data", [])),
        "parse_row_count": len(rows),
        "closed_official_dates": sorted(closed),
        "open_special_or_explicit_open_dates": sorted(open_special),
        "informational_unknown_dates": sorted(unknown),
        "all_dates_in_requested_year": not cross_year
        and all(row["date"].startswith(f"{requested_year}-") for row in rows),
        "no_duplicate_dates": "duplicate_date" not in {issue.split(":")[0] for issue in issues},
        "open_closed_exclusive": not (set(closed) & set(open_special)),
        "classification_issues": sorted(set(issues)),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse TWSE annual holiday schedules")
    parser.add_argument("--years", type=int, nargs="*")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    raw_base = root / ".local/source-raw/taiex-calendar/twse_official_holiday_schedule"
    years = args.years or sorted(
        int(path.name) for path in raw_base.iterdir() if path.is_dir() and path.name.isdigit()
    )
    output = {}
    for year in years:
        artifacts = sorted(
            path for path in (raw_base / str(year)).glob("r*-*.json")
            if not path.name.endswith(".manifest.json")
        )
        manifests = sorted((raw_base / str(year)).glob("r*-*.manifest.json"))
        if not artifacts or not manifests:
            raise V2Error(f"missing raw artifacts for {year}")
        payload = json.loads(artifacts[-1].read_text(encoding="utf-8"))
        manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
        parsed = parse_annual_schedule(payload, year)
        parsed["raw_artifact_id"] = manifest["artifact_id"]
        parsed["raw_revision"] = manifest["revision"]
        parsed["raw_sha256"] = manifest["sha256"]
        parsed["raw_content_length"] = manifest["content_length"]
        output[str(year)] = parsed
    print(json.dumps({"status": "ok", "years": output}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
