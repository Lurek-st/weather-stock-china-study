"""Narrow TWSE exploratory adapter; it never feeds the V2 production pipeline."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from scripts.v2.probes.common import quality_check, semantic_hash

URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
MONTHS = {"recent_30": ["20260601", "20260701", "20260803"], "2023_06": ["20230601"], "2020_10": ["20201002"]}


def _request(date_token: str, cache: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    params = {"date": date_token, "response": "json"}
    attempts = []
    for _ in range(2):
        response = requests.get(URL, params=params, timeout=45, headers={"Accept": "application/json"})
        response.raise_for_status()
        response.encoding = "utf-8"
        raw = response.content
        cache.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (cache / f"{date_token}-{stamp}-{len(attempts) + 1}.json").write_bytes(raw)
        attempts.append({"status": response.status_code, "etag": response.headers.get("ETag"),
                         "last_modified": response.headers.get("Last-Modified"), "content_length": len(raw),
                         "raw_sha256": hashlib.sha256(raw).hexdigest(), "payload": response.json()})
    first, second = attempts
    first_semantic = semantic_hash(_rows(first["payload"]))
    second_semantic = semantic_hash(_rows(second["payload"]))
    return first["payload"], {"request_params": params, "first": {key: first[key] for key in first if key != "payload"},
                               "second": {key: second[key] for key in second if key != "payload"},
                               "byte_status": "byte_identical" if first["raw_sha256"] == second["raw_sha256"] else "revised",
                               "semantic_sha256_first": first_semantic, "semantic_sha256_second": second_semantic,
                               "semantic_status": "semantic_identical" if first_semantic == second_semantic else "revised"}


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    fields = payload.get("fields", [])
    records = []
    for item in payload.get("data", []):
        values = dict(zip(fields, item))
        raw_date = str(values.get("日期", ""))
        try:
            roc_year, month, day = raw_date.split("/")
            iso = f"{int(roc_year) + 1911:04d}-{int(month):02d}-{int(day):02d}"
        except (ValueError, IndexError):
            iso = raw_date
        records.append({"trading_date": iso, "open": values.get("開盤指數"), "high": values.get("最高指數"),
                        "low": values.get("最低指數"), "close": values.get("收盤指數")})
        for key in ("open", "high", "low", "close"):
            if records[-1][key] is not None:
                records[-1][key] = str(records[-1][key]).replace(",", "")
    return records


def run(root: Path) -> dict[str, Any]:
    cache = root / ".local" / "source-probes" / "taiex"
    windows = []
    repeatability = {}
    for window_id, months in MONTHS.items():
        rows: list[dict[str, Any]] = []
        for month in months:
            payload, repeat = _request(month, cache)
            month_rows = _rows(payload)
            rows.extend(month_rows)
            repeatability[f"{window_id}:{month}"] = repeat
        rows = sorted({row["trading_date"]: row for row in rows}.values(), key=lambda row: row["trading_date"])
        if window_id == "recent_30":
            rows = rows[-30:]
        quality = quality_check(rows)
        windows.append({"window_id": window_id, "record_count": len(rows), "quality": quality,
                        "normalized_sha256": semantic_hash(rows)})
    return {"windows": windows, "repeatability": repeatability,
            "local_cache": ".local/source-probes/taiex (ignored)"}
