"""Credential-gated, non-production probe for the FSC KOSPI OpenAPI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import repo_root, write_json
from scripts.v2.probes.common import quality_check, semantic_hash

ENDPOINT = "https://apis.data.go.kr/1160100/service/GetMarketIndexInfoService/getStockMarketIndex"
KEY_ENV = "KOREA_DATA_GO_KR_SERVICE_KEY"
WINDOWS = (
    {"window_id": "recent_30", "request_rule": "previous 60 calendar days; select latest 30 valid records"},
    {"window_id": "2023_06", "start": "2023-06-01", "end": "2023-06-30"},
    {"window_id": "2020_10", "start": "2020-10-01", "end": "2020-10-31"},
)


def service_key_status(environ: dict[str, str] | None = None) -> str:
    return "set" if (environ or os.environ).get(KEY_ENV, "").strip() else "unset"


def redact_text(value: str) -> str:
    """Do not permit a Service Key to appear in requests, errors, or audits."""
    key = os.environ.get(KEY_ENV, "")
    return value.replace(key, "[REDACTED]") if key else value


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("response", {}).get("body", {}).get("items", {}).get("item", [])
    if isinstance(items, dict):
        return [items]
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _number(value: Any) -> float | None:
    if value in (None, "", "-", "null"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def normalize_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map only documented/observed FSC response fields; absent fields stay null."""
    rows = []
    for item in _items(payload):
        rows.append({
            "trading_date": item.get("basDt"), "index_name": item.get("idxNm"),
            "open": _number(item.get("mkp")), "high": _number(item.get("hipr")),
            "low": _number(item.get("lopr")), "close": _number(item.get("clpr")),
            "change": _number(item.get("vs")), "source_return_pct": _number(item.get("fltRt")),
            "volume": _number(item.get("trqu")), "turnover": _number(item.get("trPrc")),
            "market_cap": _number(item.get("lstgMrktTotAmt")), "constituent_count": _number(item.get("lstgStCnt")),
        })
    return rows


def with_previous_close(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: str(row.get("trading_date", "")))
    previous: float | None = None
    result = []
    for row in ordered:
        item = dict(row)
        item["previous_close"] = previous
        item["boundary_missing"] = previous is None
        close = item.get("close")
        item["return_pct_computed"] = None if previous is None or close is None else (close / previous - 1.0) * 100.0
        if close is not None:
            previous = close
        result.append(item)
    return result


def precheck(session: requests.Session, service_key: str) -> dict[str, Any]:
    params = {"serviceKey": service_key, "resultType": "json", "pageNo": 1, "numOfRows": 10, "likeIdxNm": "KOSPI"}
    try:
        response = session.get(ENDPOINT, params=params, timeout=(10, 30), headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        return {"status": "network_error", "detail": type(exc).__name__}
    if response.status_code in {401, 403}:
        return {"status": "credential_invalid", "http_status": response.status_code}
    if response.status_code == 429:
        return {"status": "quota_exceeded", "http_status": 429}
    if response.status_code >= 500:
        return {"status": "provider_error", "http_status": response.status_code}
    try:
        payload = response.json()
    except ValueError:
        return {"status": "unexpected_response", "http_status": response.status_code}
    header = payload.get("response", {}).get("header", {})
    code = str(header.get("resultCode", ""))
    if response.status_code == 200 and code in {"00", "0"}:
        return {"status": "credential_valid", "http_status": 200, "result_code": code, "observed_item_count": len(_items(payload))}
    message = redact_text(str(header.get("resultMsg", "")))
    return {"status": "credential_pending_activation" if "SERVICE" in message.upper() else "credential_invalid", "http_status": response.status_code, "result_code": code, "message": message}


def build_result(root: Path, live: bool = False) -> dict[str, Any]:
    status = service_key_status()
    preflight: dict[str, Any] = {"status": "credential_required"}
    if live and status == "set":
        preflight = precheck(requests.Session(), os.environ[KEY_ENV])
    return {
        "probe_version": "1.0.0", "market_id": "kospi", "city_id": "seoul",
        "source_id": "korea_public_data_fsc_index_api", "executed_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live" if live else "dry_run", "windows": list(WINDOWS),
        "service_key_status": status, "credential_precheck": preflight,
        "licence_assessment": {"licence_name": "Portal metadata: use-permission scope unrestricted; third-party KRX rights not explicitly resolved", "evidence_urls": ["https://www.data.go.kr/catalog/15094807/openapi.json", "https://www.data.go.kr/en/data/15094807/openapi.do"], "values": {"download_allowed": "yes", "automated_access_allowed": "yes", "long_term_storage_allowed": "unclear", "academic_research_allowed": "yes", "transformation_allowed": "unclear", "public_raw_redistribution_allowed": "unclear", "public_canonical_redistribution_allowed": "unclear", "public_derived_panel_allowed": "unclear", "commercial_reuse_allowed": "unclear", "attribution_required": "unclear", "share_alike_required": "unclear"}, "third_party_rights_status": "unclear"},
        "third_party_rights": {"status": "third_party_rights_unresolved", "reason": "FSC portal identifies Korea Exchange index data but the consulted public contract does not expressly settle public redistribution of KRX-index payloads."},
        "technical_access": {"account_required": "yes", "api_key_required": "yes", "live_access_confirmed": False, "actual_resource_url": ENDPOINT, "access_notes": "Service Key is required; no key is stored in or requested by the repository."},
        "data_quality": {"quality_gate_confirmed": False, "observed_fields": [], "findings": ["No credentialed response inspected; no window, sequence, field, or historical-coverage claim is inferred."]},
        "historical_coverage_confirmed": False, "calendar_assessment": {"status": "calendar_verification_partial", "official_evidence_url": "https://global.krx.co.kr/"},
        "repeatability": {"status": "request_not_run", "reason": "Service Key is unavailable; repeated requests were not attempted."},
        "blocking_issues": ["KOREA_DATA_GO_KR_SERVICE_KEY is required for official API payload validation.", "KRX third-party redistribution rights remain unresolved at the public-contract level."],
        "final_probe_status": "credential_required" if status == "unset" else "technical_access_blocked",
        "raw_response_policy": "local_only:.local/source-probes/kospi; no raw response is versioned",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the isolated FSC KOSPI source probe.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    if args.live and service_key_status() == "unset":
        result = build_result(args.root, live=False)
    else:
        result = build_result(args.root, live=args.live)
    write_json(args.root / "data" / "audits" / "v2" / "source-probes" / "kospi" / "probe-result.json", result)
    print(json.dumps({"market_id": "kospi", "status": result["final_probe_status"], "service_key_status": result["service_key_status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
