"""Fetch TWSE annual holiday schedules using the frontend-verified query mechanism.

Discovery result (bounded live probe, 2026-08-07):

- Official frontend page: https://www.twse.com.tw/zh/trading/holiday.html
- The page form is `<form id="form" data-api="/holidaySchedule/holidaySchedule">`
  with a hidden `date` field (`data-date="b:2021,e:1,f:Y"`), submitted by
  web-report.js `doQuery` as a GET to
  `https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=YYYY&response=json`
- `date` is a Gregorian year (YYYY); response `queryYear` echoes it and
  `title` uses the ROC year (e.g. 114 年 = 2025).
- Legacy `queryYear=109` on the old route returns the current year (2026):
  legacy_queryYear_route_status = ignored_or_fallback_current_year.
- The OpenAPI v1 endpoint `/holidaySchedule/holidaySchedule` takes no
  parameters and only returns the current-year snapshot.
- Verified data boundary: 2021..2026 return full schedules; 2019/2020 return
  the requested year's title/queryYear but an empty `data` array.

Raw responses are stored append-only under
`.local/source-raw/taiex-calendar/twse_official_holiday_schedule/<YEAR>/`.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import RawArtifactStore, V2Error, load_json, repo_root, sha256_file, write_json

FRONTEND_PAGE = "https://www.twse.com.tw/zh/trading/holiday.html"
ENDPOINT = "https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule"
RAW_BASE = ".local/source-raw/taiex-calendar"
SOURCE_ID = "twse_official_holiday_schedule"
LICENCE = "Open Government Data License v1.0"
VERIFIED_YEARS = [2020, 2021, 2022, 2023, 2024, 2025, 2026]


def fetch_year(year: int, session: requests.Session) -> dict[str, Any]:
    params = {"date": str(year), "response": "json"}
    response = session.get(ENDPOINT, params=params, timeout=45)
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.json()


def probe_year(year: int, root: Path, session: requests.Session | None = None) -> dict[str, Any]:
    """Fetch one year with one controlled retry, storing raw append-only."""
    session = session or requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (research probe; bounded)"})
    attempts: list[dict[str, Any]] = []
    last_error: str | None = None
    for attempt in range(2):
        try:
            payload = fetch_year(year, session)
            store = RawArtifactStore(root / RAW_BASE)
            result = store.persist(
                source_id=SOURCE_ID,
                provider="TWSE",
                logical_name=str(year),
                payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                request={"endpoint": ENDPOINT, "params": {"date": str(year), "response": "json"}},
                status="official_holiday_schedule",
                licence=LICENCE,
                suffix=".json",
                retries=attempt,
                retrieved_at=datetime.now(timezone.utc),
            )
            manifest = load_json(result.manifest_path)
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "retrieved_at": manifest["retrieved_at"],
                    "raw_sha256": manifest["sha256"],
                    "content_length": manifest["content_length"],
                    "revision": manifest["revision"],
                    "artifact_path": str(result.artifact_path),
                    "skipped_identical": result.skipped_identical,
                }
            )
            return {
                "requested_gregorian_year": year,
                "requested_roc_year": year - 1911,
                "payload": payload,
                "attempts": attempts,
                "retry_used": attempt > 0,
            }
        except Exception as exc:  # controlled single retry on network failure
            last_error = str(exc)
            attempts.append({"attempt": attempt + 1, "error": last_error})
    raise V2Error(f"twse holiday fetch failed for {year}: {last_error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch TWSE annual holiday schedules (frontend-verified date=YYYY mechanism)"
    )
    parser.add_argument("--years", type=int, nargs="*", default=VERIFIED_YEARS)
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = args.root
    if args.dry_run:
        print(json.dumps({"mode": "dry_run", "endpoint": ENDPOINT, "years": args.years}))
        return 0

    results = {}
    with requests.Session() as session:
        for year in args.years:
            record = probe_year(year, root, session)
            payload = record["payload"]
            results[str(year)] = {
                "requested_gregorian_year": year,
                "requested_roc_year": year - 1911,
                "stat": payload.get("stat"),
                "queryYear": payload.get("queryYear"),
                "title": payload.get("title"),
                "row_count": len(payload.get("data", [])),
                "first_date": payload["data"][0][0] if payload.get("data") else None,
                "last_date": payload["data"][-1][0] if payload.get("data") else None,
                "attempts": record["attempts"],
                "retry_used": record["retry_used"],
            }
    print(json.dumps({"status": "ok", "years": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
