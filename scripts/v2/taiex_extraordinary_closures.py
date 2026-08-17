"""Fetch and store TWSE official announcements as extraordinary-closure evidence.

One controlled GET against the TWSE official news list. The raw response is
stored append-only (timestamped, never overwritten) under ignored
``.local/source-probes/taiex-extraordinary-closures/``. An ``evidence-*.json``
summary records the raw hash and the announcement rows so the acceptance audit
can derive its conclusion from stored evidence instead of a hard-coded string.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import repo_root
from scripts.v2.probes.common import semantic_hash

TWSE_NEWS_URL = "https://www.twse.com.tw/rwd/zh/news/newsList"
EXTRAORDINARY_KEYWORDS = ("颱風", "台风", "地震", "停市", "暫停", "暂停", "休市", "取消", "特殊", "災害", "灾害", "停止交易")


def _parse_announcements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map TWSE news rows [seq, title, ROC date, id1, id2] to ISO-dated records."""
    announcements: list[dict[str, Any]] = []
    for item in payload.get("data", []):
        if not isinstance(item, list) or len(item) < 4:
            continue
        seq = item[0]
        title = str(item[1])
        roc_date = str(item[2])
        identifier = str(item[3]) if len(item) > 3 else None
        try:
            parts = roc_date.replace("年", "-").replace("月", "-").replace("日", "").split("-")
            year = int(parts[0]) + 1911
            day = date_from_parts(year, int(parts[1]), int(parts[2]))
        except (ValueError, IndexError):
            continue
        announcements.append(
            {
                "date": day.isoformat(),
                "title": title,
                "url_or_identifier": identifier,
                "classification": (
                    "extraordinary"
                    if any(keyword in title for keyword in EXTRAORDINARY_KEYWORDS)
                    else "routine"
                ),
            }
        )
    return announcements


def date_from_parts(year: int, month: int, day: int) -> "Any":
    from datetime import date as _date

    return _date(year, month, day)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch TWSE announcement evidence into .local")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root
    evidence_dir = root / ".local/source-probes/taiex-extraordinary-closures"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    response = requests.get(
        TWSE_NEWS_URL,
        params={"response": "json"},
        timeout=(15, 30),
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    raw = response.content
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = evidence_dir / f"news-{stamp}.json"
    raw_path.write_bytes(raw)  # append-only; identical content re-fetches are new files
    payload = response.json()
    announcements = _parse_announcements(payload)
    evidence = {
        "source_url": TWSE_NEWS_URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "http_status": response.status_code,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "semantic_sha256": semantic_hash(announcements),
        "announcement_count": len(announcements),
        "raw_path": f".local/source-probes/taiex-extraordinary-closures/{raw_path.name}",
        "announcements": announcements,
    }
    evidence_path = evidence_dir / f"evidence-{stamp}.json"
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "http_status": response.status_code,
                "announcement_count": len(announcements),
                "raw_sha256": evidence["raw_sha256"],
                "evidence_path": str(evidence_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
