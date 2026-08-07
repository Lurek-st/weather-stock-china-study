"""Recover the official TWSE 2020 annual holiday schedule from archival evidence.

Recovery chain (2026-08-07 bounded probe):

1. TWSE Data E-Shop official announcement, 2019-12-01:
   "Holiday Schedule of 2020" /
   "中華民國109年有價證券集中交易市場開（休）市日期表"
   https://eshop.twse.com.tw/en/news/detail/000000006e0bbe8d016ef4453cb6030c
   (zh: .../zh/news/detail/000000006e0bbe8d016ef442f1e2030b)
   whose body links the official schedule page:
   https://www.twse.com.tw/zh/holidaySchedule/holidaySchedule

2. That legacy route now 302-redirects to the generic current-year page
   (https://www.twse.com.tw/zh/trading/holiday.html), and the current RWD
   route (date=YYYY&response=json) returns an EMPTY data array for 2020
   (requested_year_acknowledged_no_data).

3. Internet Archive captured the legacy route on 2020-06-04 (and later):
   https://web.archive.org/web/20200604104858id_/https://www.twse.com.tw/zh/holidaySchedule/holidaySchedule
   with title "中華民國109年有價證券集中交易市場開（休）市日期表" and the
   full 2020 schedule table.  Original authority is TWSE; Internet Archive
   is used strictly as archival transport evidence.

The archival HTML is stored append-only under
`.local/source-raw/taiex-calendar/twse_official_holiday_schedule_archival/2020/`.
This module parses the recovered HTML into schedule rows and validates the
same closed/open/unknown classification rules as the live years.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import (
    SCHEMA_VERSION,
    V2Error,
    RawArtifactStore,
    load_json,
    repo_root,
    sha256_file,
    write_json,
)
from scripts.v2.parse_twse_holiday_schedule import (
    classify_row,
    parse_annual_schedule,
)

ARCHIVAL_BASE = ".local/source-raw/taiex-calendar/twse_official_holiday_schedule_archival"
RWD_2020_DIR = ".local/source-raw/taiex-calendar/twse_official_holiday_schedule/2020"
RECOVERY_AUDIT = "data/audits/v2/taiex-calendar/taiex-2020-annual-schedule-recovery.json"
ANNUAL_AUDIT_2020_2026 = "data/audits/v2/taiex-calendar/taiex-annual-schedules-2020-2026.json"
ANNUAL_AUDIT_2021_2026 = "data/audits/v2/taiex-calendar/taiex-annual-schedules-2021-2026.json"

ESHOP_ANNOUNCEMENT_EN = (
    "https://eshop.twse.com.tw/en/news/detail/000000006e0bbe8d016ef4453cb6030c"
)
ESHOP_ANNOUNCEMENT_ZH = (
    "https://eshop.twse.com.tw/zh/news/detail/000000006e0bbe8d016ef442f1e2030b"
)
ORIGINAL_SCHEDULE_URL = "https://www.twse.com.tw/zh/holidaySchedule/holidaySchedule"
ARCHIVED_URL = "https://web.archive.org/web/20200604104858id_/https://www.twse.com.tw/zh/holidaySchedule/holidaySchedule"


def parse_archival_html(html: str) -> list[dict[str, Any]]:
    """Parse the 2020 legacy-route HTML schedule table into date rows.

    The table has columns [名稱, 日期, 星期, 說明].  Date cells may contain
    multiple dates ("1月25日1月26日...").  Returns rows with ISO dates.
    """
    rows: list[dict[str, Any]] = []
    table_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    for raw_tr in table_rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", raw_tr, re.S)
        cells = [re.sub(r"<[^>]+>", "", cell).strip() for cell in cells]
        if len(cells) < 2 or not cells[0]:
            continue
        name = cells[0]
        raw_dates = cells[1] if len(cells) > 1 else ""
        note = cells[3] if len(cells) > 3 else (cells[2] if len(cells) > 2 else "")
        # parse each embedded "M月D日" (may be "1月25日1月26日" or with year suffix)
        month_day_matches = re.findall(r"(\d{1,2})月(\d{1,2})日", raw_dates)
        if not month_day_matches:
            continue
        for month_str, day_str in month_day_matches:
            month = int(month_str)
            day = int(day_str)
            rows.append(
                {
                    "name": name,
                    "month": month,
                    "day": day,
                    "note": note,
                }
            )
    return rows


def rows_to_dates(rows: list[dict[str, Any]], year: int) -> list[dict[str, Any]]:
    """Attach ISO dates for the given year (e.g. 2020).

    The 2020 legacy HTML places settlement-only days inside the note of the
    prior trading day (e.g. "1月21日及1月22日市場無交易，僅辦理結算交割作業。"),
    whereas 2021+ emit them as standalone rows.  Extract those referenced
    dates as closed_official settlement rows so the 2020 parse matches the
    modern format.
    """
    dated = []
    for row in rows:
        try:
            iso = date(year, row["month"], row["day"]).isoformat()
        except ValueError:
            continue
        dated.append({**row, "date": iso})
        # settlement-only days referenced in the note
        for m1, d1, m2, d2 in re.findall(
            r"(\d{1,2})月(\d{1,2})日及(\d{1,2})月(\d{1,2})日市場無交易", row.get("note", "")
        ):
            for m, d in ((m1, d1), (m2, d2)):
                try:
                    dated.append(
                        {
                            "name": "市場無交易，僅辦理結算交割作業",
                            "month": int(m),
                            "day": int(d),
                            "note": "",
                            "date": date(year, int(m), int(d)).isoformat(),
                        }
                    )
                except ValueError:
                    continue
    return dated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recover and audit the official TWSE 2020 holiday schedule")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--archival-html", type=Path)
    args = parser.parse_args(argv)
    root = args.root

    # ---- load the recovered archival HTML (explicit path or stored copy) ----
    if args.archival_html:
        html = args.archival_html.read_text(encoding="utf-8", errors="replace")
        source_label = str(args.archival_html)
    else:
        stored = root / ARCHIVAL_BASE / "2020"
        artifacts = sorted(
            p for p in stored.glob("r*-*.json") if not p.name.endswith(".manifest.json")
        )
        if not artifacts:
            raise V2Error("no archival artifact stored for 2020; run with --archival-html first")
        html = json.loads(artifacts[-1].read_text(encoding="utf-8"))["html"]
        source_label = str(artifacts[-1])

    # ---- persist archival evidence append-only (idempotent) ----
    store = RawArtifactStore(root / ARCHIVAL_BASE)
    store_result = store.persist(
        source_id="twse_official_holiday_schedule_archival",
        provider="TWSE via Internet Archive transport",
        logical_name="2020",
        payload=json.dumps(
            {
                "original_authority": "TWSE",
                "retrieval_transport": "web_archive",
                "original_url": ORIGINAL_SCHEDULE_URL,
                "archived_url": ARCHIVED_URL,
                "publication_date": "2019-12-01",
                "html": html,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        request={
            "source_id": "twse_official_holiday_schedule_archival",
            "original_url": ORIGINAL_SCHEDULE_URL,
            "archived_url": ARCHIVED_URL,
        },
        status="archival_official_schedule",
        licence="Open Government Data License v1.0",
        suffix=".json",
    )
    manifest = load_json(store_result.manifest_path)
    html_sha = sha256_file(store_result.artifact_path)

    # ---- parse + validate ----
    rows = rows_to_dates(parse_archival_html(html), 2020)
    if not rows:
        raise V2Error("recovered 2020 schedule is empty")
    payload = {
        "stat": "ok",
        "date": "20200101",
        "title": "109 年市場開休市日期",
        "fields": ["日期", "名稱", "說明"],
        "data": [[row["date"], row["name"], row.get("note", "")] for row in rows],
        "queryYear": 2020,
        "total": len(rows),
    }
    parsed = parse_annual_schedule(payload, 2020)

    # ---- verification gates ----
    if parsed["classification_issues"]:
        raise V2Error(f"2020 schedule classification issues: {parsed['classification_issues']}")
    if not parsed["all_dates_in_requested_year"]:
        raise V2Error("2020 schedule contains dates outside 2020")
    if not parsed["no_duplicate_dates"]:
        raise V2Error("2020 schedule contains duplicate dates")
    if not parsed["open_closed_exclusive"]:
        raise V2Error("2020 schedule has open/closed conflict")
    if len(rows) < 15:
        raise V2Error(f"2020 schedule suspiciously small: {len(rows)} rows")

    # ---- rwd empty evidence preserved check ----
    rwd_dir = root / RWD_2020_DIR
    rwd_artifacts = sorted(
        p for p in rwd_dir.glob("r*-*.json") if not p.name.endswith(".manifest.json")
    )
    rwd_manifest = None
    if rwd_artifacts:
        rwd_manifests = sorted(rwd_dir.glob("r*-*.manifest.json"))
        if rwd_manifests:
            rwd_manifest = load_json(rwd_manifests[-1])
            rwd_payload = json.loads(rwd_artifacts[-1].read_text(encoding="utf-8"))
            if rwd_payload.get("data") != []:
                raise V2Error("current RWD empty evidence must remain untouched")

    semantic_hash = hashlib.sha256(
        json.dumps(
            sorted(
                ({"date": row["date"], "name": row["name"]} for row in rows),
                key=lambda item: (item["date"], item["name"]),
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_2020_annual_schedule_recovery",
        "current_rwd_status": "requested_year_acknowledged_no_data",
        "official_eshop_announcement": {
            "title": "Holiday Schedule of 2020 / 中華民國109年有價證券集中交易市場開（休）市日期表",
            "publication_date": "2019-12-01",
            "url": ESHOP_ANNOUNCEMENT_EN,
            "url_zh": ESHOP_ANNOUNCEMENT_ZH,
            "html_sha256": "321aa3ef3be8dae8eebd4c5d7f786f828ce443004337a3453c80e05e1cc7e298",
        },
        "recovery_source": {
            "authority": "TWSE",
            "original_url": ORIGINAL_SCHEDULE_URL,
            "retrieval_transport": "web_archive",
            "retrieved_url": ARCHIVED_URL,
            "content_type": "text/html",
            "raw_artifact_id": manifest["artifact_id"],
            "raw_sha256": manifest["sha256"],
        },
        "current_rwd_empty_evidence": {
            "preserved": True,
            "path": RWD_2020_DIR,
            "artifact_id": rwd_manifest["artifact_id"] if rwd_manifest else None,
        },
        "calendar_year": 2020,
        "roc_year": 109,
        "row_count": len(rows),
        "closed_official_dates": parsed["closed_official_dates"],
        "explicit_open_dates": parsed["open_special_or_explicit_open_dates"],
        "informational_unknown_dates": parsed["informational_unknown_dates"],
        "all_dates_in_year": parsed["all_dates_in_requested_year"],
        "no_duplicate_dates": parsed["no_duplicate_dates"],
        "open_closed_exclusive": parsed["open_closed_exclusive"],
        "classification_issues": parsed["classification_issues"],
        "semantic_sha256": semantic_hash,
        "recovery_status": "accepted",
        "full_history_calendar_verified": False,
        "historical_backfill_run": False,
    }
    audit_path = root / RECOVERY_AUDIT
    write_json(audit_path, audit)

    print(
        json.dumps(
            {
                "status": "accepted",
                "row_count": len(rows),
                "closed": len(parsed["closed_official_dates"]),
                "explicit_open": len(parsed["open_special_or_explicit_open_dates"]),
                "semantic_sha256": semantic_hash,
                "archival_artifact": manifest["artifact_id"],
                "recovery_audit_path": RECOVERY_AUDIT,
                "recovery_audit_sha256": sha256_file(audit_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
