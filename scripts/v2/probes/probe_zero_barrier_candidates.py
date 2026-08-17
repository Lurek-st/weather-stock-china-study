"""Bounded non-production screen for official, no-credential market sources."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import repo_root, write_json
from scripts.v2.probes.common import semantic_hash

PROBE_VERSION = "1.0.0"

# These are source-screen candidates, never production adapters.
CANDIDATES: list[dict[str, Any]] = [
    {
        "market_id": "canada_tsx_composite_boc", "country": "Canada", "city": "Toronto",
        "index": "S&P/TSX Composite Index", "provider": "Bank of Canada",
        "dataset": "Valet API observations", "dataset_url": "https://www.bankofcanada.ca/valet/",
        "resource_url": "https://www.bankofcanada.ca/valet/observations/V122620/json?start_date=2020-01-02&end_date=2020-01-10",
        "licence_url": "https://www.bankofcanada.ca/terms/",
        "technical_probe": "boc_json", "eod_definition": "index level; end-of-day status requires series-level confirmation",
        "rights": "unclear", "third_party": "unclear", "coverage": "2020_claimed",
    },
    {
        "market_id": "brazil_ibovespa_bcb", "country": "Brazil", "city": "São Paulo",
        "index": "Ibovespa", "provider": "Banco Central do Brasil",
        "dataset": "BCB SGS series 7", "dataset_url": "https://dadosabertos.bcb.gov.br/",
        "resource_url": "https://api.bcb.gov.br/dados/serie/bcdata.sgs.7/dados?formato=json&dataInicial=01/01/2020&dataFinal=10/01/2020",
        "licence_url": "https://dadosabertos.bcb.gov.br/",
        "technical_probe": "bcb_json", "eod_definition": "daily closing index level requires source-series confirmation",
        "rights": "unclear", "third_party": "unclear", "coverage": "2020_claimed",
    },
    {
        "market_id": "australia_all_ordinaries_rba", "country": "Australia", "city": "Sydney",
        "index": "No qualifying stock-index series confirmed", "provider": "Reserve Bank of Australia",
        "dataset": "RBA Table F1 historical workbook", "dataset_url": "https://www.rba.gov.au/statistics/tables/",
        "resource_url": "https://www.rba.gov.au/statistics/tables/xls/f01hist.xlsx",
        "licence_url": "https://www.rba.gov.au/copyright/",
        "technical_probe": "xlsx", "eod_definition": "",
        "rights": "no", "third_party": "no", "coverage": "unknown",
    },
]

REJECTIONS = [
    {"market": "Japan—TOPIX", "official_source": "Japan Exchange Group", "reason": "bounded official review did not identify an open-licence, no-contract machine resource for public derived data", "hard_condition": "public_derived_rights_unclear", "evidence": "https://www.jpx.co.jp/english/markets/indices/", "reconsider": "yes, if JPX publishes a resource-level open licence"},
    {"market": "Singapore—STI", "official_source": "Singapore Exchange", "reason": "bounded official review did not identify a no-account, open-licence daily machine resource", "hard_condition": "public_derived_rights_unclear", "evidence": "https://www.sgx.com/securities/market-data", "reconsider": "yes, if SGX provides an open resource contract"},
    {"market": "Norway—OSEBX", "official_source": "Norges Bank", "reason": "official API access documentation requires an API key", "hard_condition": "api_key_required", "evidence": "https://developer.norges-bank.no/", "reconsider": "no for zero-barrier scope"},
    {"market": "Chile—IPSA", "official_source": "Banco Central de Chile", "reason": "bounded official review did not identify a no-credential, resource-level open-rights contract for the branded index", "hard_condition": "public_derived_rights_unclear", "evidence": "https://www.bcentral.cl/", "reconsider": "yes, with an explicit resource licence"},
    {"market": "South Africa—JSE/FTSE", "official_source": "JSE", "reason": "index branding and rights are third-party controlled in the bounded official review", "hard_condition": "third_party_rights_unclear", "evidence": "https://www.jse.co.za/", "reconsider": "yes, with a public redistribution grant"},
    {"market": "Malaysia—FTSE Bursa", "official_source": "Bursa Malaysia", "reason": "FTSE-branded index rights are not resolved for public derived distribution", "hard_condition": "third_party_rights_unclear", "evidence": "https://www.bursamalaysia.com/market_information/equities", "reconsider": "yes, with an explicit grant"},
    {"market": "Indonesia—IDX Composite", "official_source": "Indonesia Stock Exchange", "reason": "bounded official review did not identify an open-licence stable machine resource", "hard_condition": "public_derived_rights_unclear", "evidence": "https://www.idx.co.id/en/market-data/", "reconsider": "yes, with resource-level terms"},
    {"market": "Australia—All Ordinaries via RBA F1", "official_source": "Reserve Bank of Australia", "reason": "the stable F1 workbook is a monthly interest-rates table, not a qualifying daily stock-index resource; its notes identify ASX end-of-day benchmark data as proprietary with all rights reserved", "hard_condition": "no_daily_end_of_day_value; third_party_rights_blocking", "evidence": "https://www.rba.gov.au/statistics/tables/xls/f01hist.xlsx", "reconsider": "no for this resource"},
]


def classify(candidate: dict[str, Any], *, technical_ok: bool) -> str:
    if any(candidate.get(key) for key in ("requires_login", "requires_key", "requires_phone", "requires_payment")):
        return "rejected"
    if candidate.get("coverage") != "2020_claimed" or not candidate.get("eod_definition"):
        return "rejected"
    if not technical_ok:
        return "tier_3_technical_risk"
    if candidate.get("rights") != "yes" or candidate.get("third_party") != "yes":
        return "tier_2_rights_review_needed"
    return "tier_1_ready_for_full_probe"


def score(candidate: dict[str, Any], tier: str) -> int:
    if tier not in {"tier_1_ready_for_full_probe", "tier_2_rights_review_needed"}:
        return 0
    access = 25
    stability = 20 if candidate.get("technical_probe") else 0
    licence = 20 if candidate.get("rights") == "yes" else 5
    third_party = 15 if candidate.get("third_party") == "yes" else 0
    history = 10 if candidate.get("coverage") == "2020_claimed" else 0
    definition = 5 if candidate.get("eod_definition") else 0
    calendar = 5 if candidate.get("calendar_source") else 0
    return access + stability + licence + third_party + history + definition + calendar


def _parse(candidate: dict[str, Any], raw: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    if candidate["technical_probe"] == "bcb_json":
        rows = json.loads(raw.decode("utf-8"))
        return rows if isinstance(rows, list) else [], ["data", "valor"]
    if candidate["technical_probe"] == "boc_json":
        payload = json.loads(raw.decode("utf-8"))
        observations = payload.get("observations", []) if isinstance(payload, dict) else []
        return observations if isinstance(observations, list) else [], ["date", "V122620"]
    return [], ["workbook_binary"]


def _probe_resource(session: requests.Session, candidate: dict[str, Any], cache: Path) -> dict[str, Any]:
    attempts = []
    cache.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            response = session.get(candidate["resource_url"], timeout=(10, 30), headers={"Accept": "application/json,*/*"})
            raw = response.content
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (cache / f"{candidate['market_id']}-{stamp}-{attempt + 1}.bin").write_bytes(raw)
            rows, fields = _parse(candidate, raw) if response.ok else ([], [])
            attempts.append({"http_status": response.status_code, "content_type": response.headers.get("Content-Type"), "content_length": len(raw), "etag": response.headers.get("ETag"), "last_modified": response.headers.get("Last-Modified"), "raw_sha256": hashlib.sha256(raw).hexdigest(), "semantic_sha256": semantic_hash(rows), "row_count": len(rows), "fields": fields})
        except requests.RequestException as exc:
            attempts.append({"http_status": None, "error": type(exc).__name__, "row_count": 0, "fields": []})
    first, second = attempts
    if first.get("raw_sha256") and first["raw_sha256"] == second.get("raw_sha256"):
        repeatability = "byte_identical"
    elif first.get("semantic_sha256") and first["semantic_sha256"] == second.get("semantic_sha256"):
        repeatability = "semantic_identical"
    elif first.get("http_status") and second.get("http_status"):
        repeatability = "revised"
    else:
        repeatability = "request_failed"
    return {"attempts": attempts, "repeatability": repeatability, "technical_ok": all(row.get("http_status") == 200 for row in attempts)}


def run(root: Path, live: bool) -> dict[str, Any]:
    cache = root / ".local" / "source-probes" / "zero-barrier-shortlist"
    session = requests.Session()
    results = []
    for candidate in CANDIDATES:
        probe = _probe_resource(session, candidate, cache) if live else {"repeatability": "request_not_run", "technical_ok": False, "attempts": []}
        tier = classify(candidate, technical_ok=probe["technical_ok"])
        results.append({**candidate, "tier": tier, "score": score(candidate, tier), "probe": probe})
    return {"probe_version": PROBE_VERSION, "executed_at": datetime.now(timezone.utc).isoformat(), "mode": "live" if live else "dry_run", "candidates": results, "rejections": REJECTIONS, "no_tier_1_candidate_found": not any(row["tier"] == "tier_1_ready_for_full_probe" for row in results), "raw_response_policy": "local_only:.local/source-probes/zero-barrier-shortlist; no raw response is versioned"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded official zero-barrier source screen.")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    result = run(args.root, args.live)
    write_json(args.root / "data" / "audits" / "v2" / "source-probes" / "zero-barrier-shortlist.json", result)
    print(json.dumps({"mode": result["mode"], "candidate_count": len(result["candidates"]), "no_tier_1_candidate_found": result["no_tier_1_candidate_found"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
