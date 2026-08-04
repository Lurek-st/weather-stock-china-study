"""Build a bounded, source-attributed TAIEX calendar; never infer it from prices."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import load_yaml, repo_root, write_json


def parse_twse_dates(payload: Any) -> set[date]:
    """Extract ISO or ROC dates from an official holiday-schedule payload."""
    text = json.dumps(payload, ensure_ascii=False)
    matches = re.findall(r"(?<!\d)(\d{3,4})[/-](\d{1,2})[/-](\d{1,2})(?!\d)", text)
    dates: set[date] = set()
    for raw_year, raw_month, raw_day in matches:
        year = int(raw_year)
        if year < 1000:
            year += 1911
        try:
            dates.add(date(year, int(raw_month), int(raw_day)))
        except ValueError:
            continue
    return dates


def build_year(year: int, official_closed: set[date], source_url: str, revision: str | None, verified_at: str) -> list[dict[str, Any]]:
    current = date(year, 1, 1)
    result = []
    while current.year == year:
        if current in official_closed:
            status, calendar_status, reason = "closed_official_holiday", "official", "TWSE annual holiday schedule"
        elif current.weekday() >= 5:
            status, calendar_status, reason = "closed_weekend", "derived_weekend", "Saturday/Sunday weekend rule"
        else:
            status, calendar_status, reason = "unresolved", "unresolved", "official open-session evidence not yet parsed"
        result.append({"market_id": "taiex", "date": current.isoformat(), "calendar_status": calendar_status, "session_status": status, "source_url": source_url, "source_year": year, "source_revision": revision, "verified_at": verified_at, "reason": reason, "early_close": False, "special_session": False, "quality_flags": [] if status != "unresolved" else ["official_open_status_unresolved"]})
        current += timedelta(days=1)
    return result


def previous_open_date(records: list[dict[str, Any]], target: str) -> str | None:
    opens = [row["date"] for row in records if row["session_status"] in {"open", "open_special", "early_close"} and row["date"] < target]
    return max(opens) if opens else None


def fetch_year(session: requests.Session, source: dict[str, Any], year: int, cache: Path) -> tuple[set[date], str | None, str | None]:
    url = source["resource_url_template"].format(year=year)
    try:
        response = session.get(url, timeout=(10, 30), headers={"Accept": "application/json"})
        response.raise_for_status()
        raw = response.content
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"taiex-calendar-{year}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json").write_bytes(raw)
        payload = response.json()
        return parse_twse_dates(payload), hashlib.sha256(raw).hexdigest(), None
    except (requests.RequestException, ValueError) as exc:
        return set(), None, type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", nargs="*", type=int)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    source = load_yaml(args.root / "config/v2/calendars/taiex-calendar-sources.yaml")
    years = args.years or source["years"]
    cache = args.root / ".local/source-probes/taiex-calendar"
    session = requests.Session()
    summaries, records = [], []
    for year in years:
        verified_at = datetime.now(timezone.utc).isoformat()
        closed, revision, error = fetch_year(session, source, year, cache) if args.live else (set(), None, "dry_run")
        closed = {item for item in closed if item.year == year}
        if not error and not closed:
            error = "year_parameter_not_confirmed"
        records.extend(build_year(year, closed, source["resource_url_template"].format(year=year), revision, verified_at))
        summaries.append({"year": year, "official_closed_dates": len(closed), "status": "calendar_year_unresolved" if error or not closed else "official_holiday_evidence_loaded", "error": error})
    cache.mkdir(parents=True, exist_ok=True)
    local_records = cache / "derived-calendar-records.json"
    write_json(local_records, records)
    audit = {"calendar_source_id": source["calendar_source_id"], "mode": "live" if args.live else "dry_run", "years": summaries, "pilot_windows_calendar_verified": False, "full_history_calendar_verified": False, "record_count": len(records), "canonical_records_sha256": hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(), "cross_validation": {"2020_10": "not_run_official_calendar_unresolved", "2023_06": "not_run_official_calendar_unresolved", "recent_30": "not_run_official_calendar_unresolved"}, "raw_response_policy": "local_only:.local/source-probes/taiex-calendar"}
    write_json(args.root / "data/audits/v2/taiex-calendar/calendar-audit.json", audit)
    print(json.dumps({"mode": audit["mode"], "years": summaries}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
