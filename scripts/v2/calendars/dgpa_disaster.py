"""DGPA natural-disaster work-suspension evidence: parse + fetch + audit.

Layer B of the TAIEX full-history calendar qualification.

The authoritative natural-disaster source is the Directorate-General of
Personnel Administration (DGPA) "歷次天然災害停止上班上課訊息":

    https://www.dgpa.gov.tw/informationlist?uid=374

Each entry (``information?uid=374&pid=<N>``) carries an official ``nds.html``
attachment (Chinese; ``ndsE.html`` is the English twin) whose table lists, per
county/city, whether work/school is suspended.

Three attachment semantics verified against official raw evidence:

1. The attachment lists **only** counties/cities (or townships/schools) that
   have a suspension.  A fully normal county/city is absent; a whole-island
   "no suspension" day shows "無停班停課訊息".
2. The Taipei cell uses "今天 ..." / "明天 ..." clauses relative to the
   announcement date.  "今天停止上班" closes *that* day; "明天停止上班" closes
   the *next* day, while today remains normal.  A cell with only "明天停止上班"
   therefore means today was normal and tomorrow is closed.
3. "尚未宣布消息" / "尚未列入警戒區" mean no suspension (normal).

TWSE rule (frozen): a Taipei full-day or morning suspension closes the TWSE
cash market all day; an afternoon-only suspension does not; township/school
level suspension is not a whole-market closure.

This module only *parses and records* Taipei status and derives closure dates;
it never infers a closure from a typhoon name or from news keywords.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
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

LIST_URL = "https://www.dgpa.gov.tw/informationlist?uid=374"
DETAIL_URL = "https://www.dgpa.gov.tw/information?uid=374&pid={pid}"
FILE_CONVERSION_URL = "https://www.dgpa.gov.tw/FileConversion"
LICENCE = "Open Government Data License v1.0"
SOURCE_ID = "dgpa_natural_disaster_suspension"
RAW_BASE = ".local/source-raw/taiex-calendar/dgpa_natural_disaster"

TAIPEI_NAMES = ("臺北市", "台北市", "Taipei City", "Taipei")
EMPTY_MARKER = "無停班停課訊息"

FULL_DAY_MARKERS = ("停止上班", "停止上課")
NORMAL_MARKERS = ("照常上班", "照常上課", "尚未列入警戒區", "正常上班", "正常上課", "尚未宣布消息")
AFTERNOON_MARKERS = ("下午",)
MORNING_MARKERS = ("上午",)
LOCAL_MARKERS = ("鄉", "鎮", "區", "里", "村", "學校", "國小", "國中", "分校", "幼兒園", "小學", "中學", "高中", "大學")

_LOCAL_WORDS = (
    "里", "區", "鄉", "鎮", "村",
    "學校", "國小", "國中", "分校", "幼兒園", "小學", "中學", "高中", "大學",
)

HARD_PAGE_CAP = 60


# ---------------------------------------------------------------------------
# Pure parsing (zero-network, unit-testable)
# ---------------------------------------------------------------------------

def roc_to_iso(roc_year: int, month: int, day: int) -> str:
    return date(roc_year + 1911, month, day).isoformat()


def parse_event_date(title_text: str) -> str | None:
    m = re.search(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", title_text)
    if not m:
        return None
    return roc_to_iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def parse_list_entries(html: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for match in re.finditer(
        r'<a href="information\?uid=374&amp;pid=(\d+)"[^>]*>(.*?)</a>', html, re.S
    ):
        pid = match.group(1)
        body = re.sub(r"<[^>]+>", " ", match.group(2))
        body = re.sub(r"\s+", " ", body).strip()
        event_date = parse_event_date(body)
        ann_match = re.search(r"(\d{3})\.(\d{2})\.(\d{2})", body)
        announce_date = (
            roc_to_iso(int(ann_match.group(1)), int(ann_match.group(2)), int(ann_match.group(3)))
            if ann_match
            else None
        )
        entries.append(
            {"pid": pid, "title": body, "event_date": event_date, "announce_date": announce_date}
        )
    return entries


def extract_city_rows(html: str) -> list[tuple[str, str]]:
    rows = re.findall(r"<TR[^>]*>(.*?)</TR>", html, re.S | re.I)
    out: list[tuple[str, str]] = []
    for tr in rows:
        cells = re.findall(r"<T[DH][^>]*>(.*?)</T[DH]>", tr, re.S | re.I)
        cells = [re.sub(r"<[^>]+>", " ", c) for c in cells]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        if len(cells) >= 2 and cells[0] and "縣市名稱" not in cells[0]:
            out.append((cells[0], cells[1]))
    return out


def extract_taipei_cell(html: str) -> str | None:
    for name, status in extract_city_rows(html):
        if any(tp in name for tp in TAIPEI_NAMES):
            return status
    return None


def split_today_tomorrow(text: str) -> tuple[str | None, str | None]:
    """Split a Taipei cell into today and tomorrow clauses.

    The today clause may be empty (cell starts with '明天...'), which means
    today has no suspension announcement.
    """
    text = text.strip()
    if "明天" in text:
        idx = text.find("明天")
        today_part = text[:idx].strip()
        tomorrow_part = text[idx:].strip()
        return (today_part or None, tomorrow_part)
    return (text or None, None)


def _is_local_sentence(sentence: str) -> bool:
    """True when a sentence is a local (township/district/school) note.

    A local note has an administrative-unit word followed by ':'.  A phrase
    like '尚未列入警戒區' has no ':', so it is NOT local.
    """
    return ":" in sentence and any(w in sentence for w in _LOCAL_WORDS)


def _split_local_city(clause: str) -> tuple[list[str], list[str]]:
    sentences = [s.strip() for s in re.split(r"[。；;]", clause) if s.strip()]
    local = [s for s in sentences if _is_local_sentence(s)]
    city = [s for s in sentences if not _is_local_sentence(s)]
    return local, city


def classify_clause(clause: str | None) -> str:
    """Classify a single today/tomorrow clause into a work status.

    Returns full_day_stop / morning_stop / afternoon_stop / normal /
    partial_or_local / ambiguous.  A None clause means 'not announced', which
    is normal (no suspension).
    """
    if not clause or not clause.strip():
        return "normal"
    local, city = _split_local_city(clause)
    city_part = " ".join(city)

    has_suspension = any(k in city_part for k in FULL_DAY_MARKERS)
    has_normal = any(k in city_part for k in NORMAL_MARKERS)
    has_afternoon = any(k in city_part for k in AFTERNOON_MARKERS)
    has_morning = any(k in city_part for k in MORNING_MARKERS)

    if has_suspension and not has_normal:
        if has_afternoon and not has_morning:
            return "afternoon_stop"
        if has_morning and not has_afternoon:
            return "morning_stop"
        return "full_day_stop"

    if has_normal and not has_suspension:
        return "normal"

    # Only local (township/district/school) notes, no whole-city statement.
    if local and not city:
        return "partial_or_local"

    return "ambiguous"


def derive_taipei_statuses(cell_text: str | None) -> dict[str, Any]:
    """Return today/tomorrow work statuses for a Taipei cell.

    ``cell_text`` None means Taipei is absent from the attachment (normal).
    Local (township/district/school) notes are split off and never alter the
    whole-city status.  When the cell contains only local notes, today is
    ``partial_or_local`` (no whole-city suspension).
    """
    if cell_text is None:
        return {"today_status": "normal", "tomorrow_status": None, "exact_text": ""}
    local, city = _split_local_city(cell_text)
    if city:
        today, tomorrow = split_today_tomorrow(" ".join(city))
        return {
            "today_status": classify_clause(today),
            "tomorrow_status": classify_clause(tomorrow),
            "exact_text": cell_text,
        }
    if local:
        return {
            "today_status": "partial_or_local",
            "tomorrow_status": None,
            "exact_text": cell_text,
        }
    return {"today_status": "ambiguous", "tomorrow_status": None, "exact_text": cell_text}


def closure_dates_for_event(event_date: str, statuses: dict[str, Any]) -> list[str]:
    """Derive the market-closure dates for one DGPA event.

    ``event_date`` is the announcement date (list-title date).  A full-day or
    morning suspension on 'today' closes event_date; on 'tomorrow' closes
    event_date + 1.
    """
    closes = []
    today = statuses["today_status"]
    tomorrow = statuses["tomorrow_status"]
    d = date.fromisoformat(event_date)
    if today in {"full_day_stop", "morning_stop"}:
        closes.append(d.isoformat())
    if tomorrow in {"full_day_stop", "morning_stop"}:
        closes.append((d + timedelta(days=1)).isoformat())
    return closes


def derive_market_closure(status: str) -> str:
    mapping = {
        "full_day_stop": "market_closed_full_day",
        "morning_stop": "market_closed_full_day",
        "afternoon_stop": "regular_cash_market_open",
        "normal": "no_natural_disaster_closure",
        "partial_or_local": "no_automatic_full_market_closure",
        "ambiguous": "unresolved",
        "missing": "unresolved",
    }
    return mapping.get(status, "unresolved")


# ---------------------------------------------------------------------------
# Fetching (bounded, append-only)
# ---------------------------------------------------------------------------

def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status is not None and 500 <= status < 600
    return False


def _get(session: requests.Session, url: str) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=(15, 30))
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if not _is_retryable(exc):
                raise V2Error(f"non-retryable DGPA fetch failure: {exc}") from exc
            time.sleep(1.0 * (attempt + 1))
    raise V2Error(f"DGPA fetch failed after retry: {last_error}")


def fetch_list_pages(
    root: Path, *, start_page: int = 1, stop_before: str = "2020-01-01", delay: float = 0.3
) -> dict[str, Any]:
    store = RawArtifactStore(root / RAW_BASE)
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (research probe; bounded)"})
    pages: list[dict[str, Any]] = []
    all_entries: list[dict[str, Any]] = []
    terminated_reason = None
    for page in range(start_page, HARD_PAGE_CAP + 1):
        url = LIST_URL + (f"&page={page}" if page > 1 else "")
        resp = _get(session, url)
        resp.encoding = "utf-8"
        raw = resp.content
        result = store.persist(
            source_id=SOURCE_ID,
            provider="DGPA (行政院人事行政總處)",
            logical_name=f"list-page-{page:03d}",
            payload=raw,
            request={"url": url, "page": page},
            status="natural_disaster_list",
            licence=LICENCE,
            suffix=".html",
            retrieved_at=datetime.now(timezone.utc),
        )
        entries = parse_list_entries(resp.text)
        pages.append(
            {
                "page": page,
                "url": url,
                "http_status": resp.status_code,
                "raw_sha256": sha256_bytes(raw),
                "artifact_id": f"{SOURCE_ID}:list-page-{page:03d}:{result.manifest_path.stem}",
                "entry_count": len(entries),
            }
        )
        if not entries:
            terminated_reason = "empty_page"
            break
        all_entries.extend(entries)
        oldest_event = max((e["event_date"] for e in entries if e["event_date"]), default=None)
        if oldest_event and oldest_event < stop_before:
            terminated_reason = f"oldest_event_{oldest_event}_before_{stop_before}"
            break
        time.sleep(delay)
    return {
        "pages": pages,
        "entries": all_entries,
        "terminated_reason": terminated_reason,
        "hard_page_cap": HARD_PAGE_CAP,
    }


def _chinese_attachment_urls(detail_text: str) -> list[str]:
    urls = []
    for m in re.finditer(
        r'FileConversion\?filename=([^&"]+)&amp;nfix=&amp;name=(nds(?:_\d+)?\.html)',
        detail_text,
    ):
        fname, name = m.group(1), m.group(2)
        urls.append(f"{FILE_CONVERSION_URL}?filename={fname}&nfix=&name={name}")
    return urls


def fetch_event_detail(root: Path, pid: str, session: requests.Session) -> dict[str, Any]:
    store = RawArtifactStore(root / RAW_BASE)
    detail_url = DETAIL_URL.format(pid=pid)
    resp = _get(session, detail_url)
    resp.encoding = "utf-8"
    detail_raw = resp.content
    store.persist(
        source_id=SOURCE_ID,
        provider="DGPA (行政院人事行政總處)",
        logical_name=f"detail-{pid}",
        payload=detail_raw,
        request={"url": detail_url, "pid": pid},
        status="natural_disaster_detail",
        licence=LICENCE,
        suffix=".html",
        retrieved_at=datetime.now(timezone.utc),
    )
    attachments: list[dict[str, Any]] = []
    html_parts: list[str] = []
    for att_url in _chinese_attachment_urls(resp.text):
        att_resp = _get(session, att_url)
        att_resp.encoding = "utf-8"
        att_raw = att_resp.content
        att_result = store.persist(
            source_id=SOURCE_ID,
            provider="DGPA (行政院人事行政總處)",
            logical_name=f"attachment-{pid}",
            payload=att_raw,
            request={"url": att_url, "pid": pid},
            status="natural_disaster_attachment",
            licence=LICENCE,
            suffix=".html",
            retrieved_at=datetime.now(timezone.utc),
        )
        attachments.append(
            {
                "url": att_url,
                "raw_sha256": sha256_bytes(att_raw),
                "artifact_id": f"{SOURCE_ID}:attachment-{pid}:{att_result.manifest_path.stem}",
            }
        )
        html_parts.append(att_resp.text)
    combined_html = "\n".join(html_parts)
    return {
        "pid": pid,
        "detail_url": detail_url,
        "detail_raw_sha256": sha256_bytes(detail_raw),
        "attachments": attachments,
        "attachment_fetched": bool(attachments),
        "attachment_html": combined_html,
    }


def build_coverage_audit(root: Path, pages: dict[str, Any]) -> dict[str, Any]:
    entries = pages["entries"]
    target = [
        e for e in entries if e["event_date"] and "2020-01-01" <= e["event_date"] <= "2025-12-31"
    ]
    pids = [e["pid"] for e in entries]
    dup_pids = sorted({p for p in pids if pids.count(p) > 1})
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_dgpa_source_coverage_2020_2025",
        "source_url": LIST_URL,
        "target_period_start": "2020-01-01",
        "target_period_end": "2025-12-31",
        "pages_traversed": len(pages["pages"]),
        "terminated_reason": pages["terminated_reason"],
        "hard_page_cap": HARD_PAGE_CAP,
        "total_entries_seen": len(entries),
        "target_period_entries": len(target),
        "duplicate_pids": dup_pids,
        "coverage_complete": bool(pages["terminated_reason"] and not dup_pids),
        "unresolved_count": len(dup_pids),
        "pages": pages["pages"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and audit DGPA natural-disaster evidence")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--stop-before", default="2020-01-01")
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args(argv)
    root = args.root

    pages = fetch_list_pages(
        root, start_page=args.start_page, stop_before=args.stop_before, delay=args.delay
    )
    coverage = build_coverage_audit(root, pages)
    write_json(
        root / "data/audits/v2/taiex-calendar/taiex-dgpa-source-coverage-2020-2025.json",
        coverage,
    )
    print(
        json.dumps(
            {
                "pages_traversed": coverage["pages_traversed"],
                "terminated_reason": coverage["terminated_reason"],
                "total_entries": coverage["total_entries_seen"],
                "target_period_entries": coverage["target_period_entries"],
                "coverage_complete": coverage["coverage_complete"],
                "duplicate_pids": coverage["duplicate_pids"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.list_only:
        return 0

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (research probe; bounded)"})
    target_entries = [
        e
        for e in pages["entries"]
        if e["event_date"] and "2020-01-01" <= e["event_date"] <= "2025-12-31"
    ]
    records: list[dict[str, Any]] = []
    closure_set: set[str] = set()
    for i, entry in enumerate(target_entries, 1):
        try:
            detail = fetch_event_detail(root, entry["pid"], session)
        except V2Error as exc:
            records.append(
                {
                    "event_date": entry["event_date"],
                    "announce_date": entry["announce_date"],
                    "pid": entry["pid"],
                    "detail_raw_sha256": None,
                    "attachment_sha256s": [],
                    "attachment_fetched": False,
                    "other_city_count": 0,
                    "exact_taipei_text": f"(fetch_failed: {exc})",
                    "today_status": "missing",
                    "tomorrow_status": None,
                    "derived_market_closure": "unresolved",
                    "closure_dates": [],
                }
            )
            print(f"[{i}/{len(target_entries)}] {entry['event_date']} pid={entry['pid']} -> FETCH_FAILED")
            time.sleep(args.delay)
            continue
        html = detail["attachment_html"]
        city_rows = extract_city_rows(html) if html else []
        taipei_cell = extract_taipei_cell(html) if html else None
        empty_marker = bool(html and EMPTY_MARKER in html)

        if not detail["attachment_fetched"]:
            statuses = {"today_status": "missing", "tomorrow_status": None}
            exact = ""
        elif taipei_cell is not None:
            statuses = derive_taipei_statuses(taipei_cell)
            exact = taipei_cell
        elif empty_marker or len(city_rows) >= 1:
            statuses = {"today_status": "normal", "tomorrow_status": None}
            exact = "(臺北市未列入停班停課名單)"
        else:
            statuses = {"today_status": "ambiguous", "tomorrow_status": None}
            exact = "(attachment unparseable)"

        closure_dates = closure_dates_for_event(entry["event_date"], statuses)
        closure_set.update(closure_dates)
        # manual review when either clause is ambiguous and would matter for a
        # whole-market closure decision
        unresolved_clauses = [
            s for s in (statuses["today_status"], statuses["tomorrow_status"]) if s == "ambiguous"
        ]
        records.append(
            {
                "event_date": entry["event_date"],
                "announce_date": entry["announce_date"],
                "pid": entry["pid"],
                "detail_raw_sha256": detail["detail_raw_sha256"],
                "attachment_sha256s": [a["raw_sha256"] for a in detail["attachments"]],
                "attachment_fetched": detail["attachment_fetched"],
                "other_city_count": len(city_rows),
                "exact_taipei_text": exact,
                "today_status": statuses["today_status"],
                "tomorrow_status": statuses["tomorrow_status"],
                "manual_review_required": bool(unresolved_clauses),
                "derived_market_closure": derive_market_closure(statuses["today_status"]),
                "closure_dates": closure_dates,
            }
        )
        print(
            f"[{i}/{len(target_entries)}] {entry['event_date']} pid={entry['pid']} "
            f"today={statuses['today_status']} tomorrow={statuses['tomorrow_status']}"
        )
        time.sleep(args.delay)

    closure_dates = sorted(closure_set)
    ambiguous = [r for r in records if r["manual_review_required"] or r["today_status"] == "missing"]
    audit = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_natural_disaster_closures_2020_2025",
        "source_url": LIST_URL,
        "target_period_start": "2020-01-01",
        "target_period_end": "2025-12-31",
        "event_count": len(records),
        "closure_date_count": len(closure_dates),
        "natural_disaster_closure_dates": closure_dates,
        "ambiguous_missing_count": len(ambiguous),
        "ambiguous_missing": ambiguous,
        "records": records,
    }
    write_json(
        root / "data/audits/v2/taiex-calendar/taiex-natural-disaster-closures-2020-2025.json",
        audit,
    )
    print(
        json.dumps(
            {
                "event_count": len(records),
                "closure_date_count": len(closure_dates),
                "ambiguous_missing_count": len(ambiguous),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
