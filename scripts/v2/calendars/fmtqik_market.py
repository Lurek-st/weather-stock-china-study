"""TWSE FMTQIK daily market-turnover reports: fetch + audit.

Layer C of the TAIEX full-history calendar qualification.

The TWSE daily market-turnover information (每日市場成交資訊, FMTQIK) is the
official, 1990-01-04 onward, monthly report of actual trading days:

    https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date=YYYYMMDD&response=json

The endpoint returns the whole month for any ``date`` inside that month; each
row is ``[ROC_date, 成交股數, 成交金額, 成交筆數, 發行量加權股價指數, 漲跌點數]``.
Only days that actually traded appear, so a natural-disaster closure (or any
non-trading day) is absent from the returned row set.

This module builds ``observed_market_open_dates`` for 2020-01..2025-12 (72
months).  It never treats HTTP 200 as evidence of a specific date trading: it
parses the actual returned date rows.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import (
    SCHEMA_VERSION,
    V2Error,
    RawArtifactStore,
    repo_root,
    sha256_bytes,
    write_json,
)

ENDPOINT = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
LICENCE = "Open Government Data License v1.0"
SOURCE_ID = "twse_fmtqik_daily_market"
RAW_BASE = ".local/source-raw/taiex-calendar/twse_fmtqik_daily_market"


# ---------------------------------------------------------------------------
# Pure parsing (zero-network, unit-testable)
# ---------------------------------------------------------------------------

def parse_trading_dates(payload: dict[str, Any], expected_year: int, expected_month: int) -> dict[str, Any]:
    """Extract ISO trading dates from a FMTQIK monthly payload.

    Returns ``{dates, issues}``.  Each returned ROC date must fall inside the
    requested Gregorian year/month; duplicates and out-of-month rows are issues.
    """
    dates: list[str] = []
    issues: list[str] = []
    seen: set[str] = set()
    for row in payload.get("data", []):
        if not isinstance(row, (list, tuple)) or not row:
            issues.append("row_malformed")
            continue
        raw = str(row[0])
        m = re_match_roc(raw)
        if not m:
            issues.append(f"unparseable_date:{raw}")
            continue
        roc_year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        iso = date(roc_year + 1911, month, day).isoformat()
        if roc_year + 1911 != expected_year or month != expected_month:
            issues.append(f"out_of_month_date:{raw}")
            continue
        if iso in seen:
            issues.append(f"duplicate_date:{iso}")
            continue
        seen.add(iso)
        dates.append(iso)
    return {"dates": sorted(dates), "issues": sorted(set(issues))}


def re_match_roc(raw: str) -> Any:
    import re

    return re.fullmatch(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", raw.strip())


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status is not None and 500 <= status < 600
    return False


def months_in_range(start: str, end: str) -> list[tuple[int, int]]:
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    result = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return result


def fetch_month(
    root: Path, year: int, month: int, session: requests.Session
) -> dict[str, Any]:
    """Fetch one FMTQIK month and return parsed trading dates."""
    store = RawArtifactStore(root / RAW_BASE)
    date_param = f"{year:04d}{month:02d}01"
    params = {"date": date_param, "response": "json"}
    last_error: Exception | None = None
    payload: dict[str, Any] | None = None
    raw: bytes | None = None
    for attempt in range(2):
        try:
            resp = session.get(ENDPOINT, params=params, timeout=(15, 30))
            resp.raise_for_status()
            resp.encoding = "utf-8"
            raw = resp.content
            payload = resp.json()
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if not _is_retryable(exc):
                raise V2Error(f"non-retryable FMTQIK failure for {year}-{month:02d}: {exc}") from exc
    if payload is None:
        raise V2Error(f"FMTQIK fetch failed for {year}-{month:02d}: {last_error}")

    result = store.persist(
        source_id=SOURCE_ID,
        provider="TWSE",
        logical_name=f"{year:04d}-{month:02d}",
        payload=raw,
        request={"endpoint": ENDPOINT, "params": params},
        status="daily_market_turnover",
        licence=LICENCE,
        suffix=".json",
        retrieved_at=datetime.now(timezone.utc),
    )
    parsed = parse_trading_dates(payload, year, month)
    return {
        "year": year,
        "month": month,
        "stat": payload.get("stat"),
        "title": payload.get("title"),
        "row_count": len(payload.get("data", [])),
        "raw_sha256": sha256_bytes(raw),
        "artifact_id": f"{SOURCE_ID}:{year:04d}-{month:02d}:{result.manifest_path.stem}",
        "dates": parsed["dates"],
        "issues": parsed["issues"],
    }


def build_audit(root: Path, months: list[dict[str, Any]]) -> dict[str, Any]:
    all_dates: set[str] = set()
    failed: list[dict[str, Any]] = []
    for m in months:
        if m.get("issues"):
            failed.append(
                {
                    "year": m["year"],
                    "month": m["month"],
                    "issues": m["issues"],
                }
            )
        all_dates.update(m["dates"])
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_market_open_observations_2020_2025",
        "source_url": ENDPOINT,
        "target_period_start": "2020-01-01",
        "target_period_end": "2025-12-31",
        "months_expected": len(months),
        "months_accepted": len(months) - len(failed),
        "months_failed": failed,
        "observed_market_open_dates": sorted(all_dates),
        "observed_market_open_count": len(all_dates),
        "unresolved_count": len(failed),
        "months": [
            {
                "year": m["year"],
                "month": m["month"],
                "row_count": m["row_count"],
                "date_count": len(m["dates"]),
                "raw_sha256": m["raw_sha256"],
                "artifact_id": m["artifact_id"],
                "issues": m["issues"],
            }
            for m in months
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and audit TWSE FMTQIK monthly market-turnover reports")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--start", default="2020-01")
    parser.add_argument("--end", default="2025-12")
    parser.add_argument("--delay", type=float, default=0.3)
    args = parser.parse_args(argv)
    root = args.root

    month_pairs = months_in_range(args.start, args.end)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (research probe; bounded)"})
    months = []
    for i, (year, month) in enumerate(month_pairs, 1):
        rec = fetch_month(root, year, month, session)
        months.append(rec)
        print(
            f"[{i}/{len(month_pairs)}] {year}-{month:02d} rows={rec['row_count']} "
            f"dates={len(rec['dates'])} issues={rec['issues']}"
        )
        time.sleep(args.delay)

    audit = build_audit(root, months)
    write_json(
        root / "data/audits/v2/taiex-calendar/taiex-market-open-observations-2020-2025.json",
        audit,
    )
    print(
        json.dumps(
            {
                "months_expected": audit["months_expected"],
                "months_accepted": audit["months_accepted"],
                "months_failed": audit["months_failed"],
                "observed_market_open_count": audit["observed_market_open_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
