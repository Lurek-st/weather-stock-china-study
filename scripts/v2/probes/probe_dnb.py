"""Direct, isolated probe for the two registered DNB stock-index resources."""
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
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import repo_root, write_json
from scripts.v2.probes.common import semantic_hash

RESOURCES = {
    "post_2018": "https://statistiek.api.dnb.nl/api/dataset/resourcefile?id=64131a7e-3eaa-45f1-a915-b47dbf05b517",
    "historical": "https://statistiek.api.dnb.nl/api/dataset/resourcefile?id=9f3c874d-6407-44f6-881c-7f9de1dcf31e",
}


def _semantic(payload: bytes) -> str | None:
    try:
        loaded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return semantic_hash([loaded])


def fetch_twice(resource_id: str, url: str, cache: Path) -> dict[str, Any]:
    """Fetch only the registered URL twice; never infer an alternate endpoint."""
    attempts = []
    session = requests.Session()
    session.trust_env = False
    for attempt in range(2):
        retrieved_at = datetime.now(timezone.utc).isoformat()
        try:
            response = session.get(url, timeout=45, headers={"Accept": "application/json"})
            payload = response.content
            entry = {
                "retrieved_at": retrieved_at, "http_status": response.status_code,
                "content_length": len(payload), "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "raw_sha256": hashlib.sha256(payload).hexdigest(), "semantic_sha256": _semantic(payload),
            }
            if response.ok:
                cache.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                (cache / f"{resource_id}-{stamp}-{attempt + 1}.json").write_bytes(payload)
                entry["payload"] = response.json()
            else:
                entry["error"] = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            entry = {"retrieved_at": retrieved_at, "http_status": None, "content_length": None,
                     "etag": None, "last_modified": None, "raw_sha256": None,
                     "semantic_sha256": None, "error": type(exc).__name__}
        attempts.append(entry)
    usable = [entry for entry in attempts if "payload" in entry]
    if len(usable) != 2:
        status = "request_failed"
    elif usable[0]["raw_sha256"] == usable[1]["raw_sha256"]:
        status = "byte_identical"
    elif usable[0]["semantic_sha256"] == usable[1]["semantic_sha256"]:
        status = "semantic_identical"
    else:
        status = "revised"
    return {"resource_id": resource_id, "url": url, "status": status,
            "attempts": [{key: value for key, value in row.items() if key != "payload"} for row in attempts],
            "payloads": [row["payload"] for row in usable]}


def describe_json(payload: Any) -> dict[str, Any]:
    """Report structure only when an actual DNB JSON document was returned."""
    if isinstance(payload, dict):
        return {"top_level_type": "object", "top_level_keys": sorted(payload),
                "record_container_candidates": [key for key, value in payload.items() if isinstance(value, list)]}
    if isinstance(payload, list):
        return {"top_level_type": "array", "record_count": len(payload),
                "record_keys": sorted(payload[0]) if payload and isinstance(payload[0], dict) else []}
    return {"top_level_type": type(payload).__name__}


def stitch_series(historical: list[dict[str, Any]], current: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prefer the current resource on identical overlap; surface value conflicts."""
    by_date = {row["trading_date"]: dict(row) for row in historical}
    conflicts = []
    for row in current:
        existing = by_date.get(row["trading_date"])
        if existing and existing["close"] != row["close"]:
            conflicts.append({"trading_date": row["trading_date"], "historical_close": existing["close"], "current_close": row["close"]})
        by_date[row["trading_date"]] = dict(row)
    return [by_date[key] for key in sorted(by_date)], conflicts


def with_previous_close(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: row["trading_date"])
    result = []
    prior = None
    for row in ordered:
        item = dict(row)
        close = float(item["close"])
        item["previous_close"] = prior
        item["boundary_missing"] = prior is None
        item["return_pct"] = None if prior is None else round((close / prior - 1.0) * 100.0, 10)
        result.append(item)
        prior = close
    return result


def quality_gate(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    try:
        values = [float(row["close"]) for row in rows]
    except (KeyError, TypeError, ValueError):
        return False
    dates = [row["trading_date"] for row in rows]
    return all(value > 0 for value in values) and len(dates) == len(set(dates))


def run(root: Path) -> dict[str, Any]:
    cache = root / ".local" / "source-probes" / "aex-dnb"
    resource_results = {key: fetch_twice(key, url, cache) for key, url in RESOURCES.items()}
    successful = {key: value for key, value in resource_results.items() if value["payloads"]}
    return {
        "resources": {key: {name: value for name, value in result.items() if name != "payloads"}
                      for key, result in resource_results.items()},
        "json_structures": {key: describe_json(result["payloads"][0]) for key, result in successful.items()},
        "access_confirmed": len(successful) == len(RESOURCES),
        "local_cache": ".local/source-probes/aex-dnb (ignored)",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Direct DNB resourcefile probe; no production integration.")
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    result = run(args.root)
    write_json(args.root / "data" / "audits" / "v2" / "source-probes" / "aex_dnb" / "direct-resource-probe.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
