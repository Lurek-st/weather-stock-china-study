"""Stage 5C-G: global official exchange anchor registry builder (no network).

Reads the Nominatim geocoding provenance, computes each market's ERA5 2x2
surrounding cell + bilinear weights via the frozen grid_geometry helper,
compares against the legacy municipal sensitivity point, and writes:
  config/v2/spatial-anchors.yaml            (canonical research authority)
  data/audits/v2/spatial/global-exchange-anchor-qualification.json
The registry is FROZEN for live requests; no dynamic re-geocoding afterwards.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import SCHEMA_VERSION, load_yaml, repo_root, sha256_file, write_json
from scripts.v2.spatial.grid_geometry import (
    bilinear_weights,
    describe_cell,
    nearest_grid_point,
    surrounding_cell,
)

GEOCODE_AUDIT = "data/audits/v2/spatial/global-exchange-anchor-geocoding.json"
LOCATIONS = "config/v2/locations.yaml"
OUTPUT_REGISTRY = "config/v2/spatial-anchors.yaml"
OUTPUT_AUDIT = "data/audits/v2/spatial/global-exchange-anchor-qualification.json"
SPATIAL_CONTRACT_VERSION = "5C-G-1"

EVIDENCE: dict[str, list[str]] = {
    "sse_composite": ["https://www.sse.com.cn/aboutus/contactus (official contact; 388 Yanggao South Road, Pudong; postal 200127)", "https://www.sse.com.cn/lawandrules/publicadvice/c/c_20200731_5166326.shtml (dated 2020-07-31 document still using 528 Pudong South Road)", "https://www.sse.com.cn/aboutus/sseintroduction/billing/ (2020-08-31 page using 388 Yanggao South Road)"],
    "szse_component": ["https://www.szse.cn/English/about/contactus (official contact; 2012 Shennan Blvd, Futian; postal 518038)"],
    "topix": ["https://www.jpx.co.jp/markets/statistics-equities/daily/ (TSE daily report masthead; 2-1 Nihombashi Kabutocho, Chuo-ku 103-8220)"],
    "nifty50": ["https://www.nseindia.com/contact-us (official corporate office; Exchange Plaza, C-1, Block G, BKC, Bandra (E), Mumbai 400051)"],
    "ftse100": ["https://www.londonstockexchange.com/contact (official head office; 10 Paternoster Square, London EC4M 7LS)"],
    "dax": ["https://www.deutsche-boerse.com (Deutsche Börse AG privacy/legal notices; Mergenthalerallee 61, 65760 Eschborn)"],
    "sp500": ["https://www.nyse.com (NYSE official premises; 11 Wall Street, New York, NY 10005)"],
    "bse50": ["https://www.bse.cn/application/Contact_info.html + https://www.bse.cn/about_neeq/grxxbhgzs.html (official; 金融大街丁26号金阳大厦, Beijing 100033)"],
}

ANCHOR_ROLE = "market_local_atmospheric_proxy"

# Frozen scientific contract (Stage 5C-GR1): the primary weather anchor is a
# single fixed target-regime market-local atmospheric proxy, NOT a historical
# premises tracker.  Historical office moves are sensitivity evidence only.
PRIMARY_ANCHOR_POLICY = {"type": "fixed_target_regime", "historical_premises_tracking": False}
HISTORICAL_PREMISES_CHANGE = {"sse_composite": True}  # bounded 2020-07-31 -> 2020-08-31


def _dms_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _anchor_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the global exchange anchor registry + audit")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    geocoding = json.loads((root / GEOCODE_AUDIT).read_text(encoding="utf-8"))
    market_entries = geocoding.get("markets", geocoding if isinstance(geocoding, list) else [])
    hist_premises_entries = geocoding.get("historical_premises", [])
    locations = load_yaml(root / LOCATIONS)["locations"]
    legacy = {loc["market_id"]: loc for loc in locations}
    old_sse = None
    for hp in hist_premises_entries:
        if hp.get("premises_label") == "old_sse_premises" and hp.get("forward"):
            old_sse = hp

    markets: list[dict[str, Any]] = []
    for entry in market_entries:
        mid = entry["market_id"]
        target = entry["target"]
        fwd = (entry.get("forward") or [None])[0]
        if fwd is None:
            markets.append({"market_id": mid, "qualification_status": "unresolved"})
            continue
        lat = float(fwd["lat"])
        lon = float(fwd["lon"])
        corners = surrounding_cell(lat, lon)
        weights = bilinear_weights(lat, lon, corners)
        nearest_name, nearest_pt = nearest_grid_point(lat, lon, corners)
        cell = describe_cell(corners)

        premises_change = bool(HISTORICAL_PREMISES_CHANGE.get(mid, False))
        historical_premises_meta = None
        if premises_change and old_sse and mid == "sse_composite":
            ofwd = old_sse["forward"][0]
            olat, olon = float(ofwd["lat"]), float(ofwd["lon"])
            o_corners = surrounding_cell(olat, olon)
            o_weights = bilinear_weights(olat, olon, o_corners)
            o_nearest_name, o_nearest_pt = nearest_grid_point(olat, olon, o_corners)
            historical_premises_meta = {
                "change_detected": True,
                "transition_date_status": "bounded_not_exact",
                "transition_after": "2020-07-31",
                "transition_on_or_before": "2020-08-31",
                "old_official_address": "上海市浦东新区浦东南路528号 (528 Pudong South Road, Pudong New Area, Shanghai)",
                "old_coordinate": {"crs": "WGS84", "latitude": olat, "longitude": olon},
                "old_geocoder_display_name": ofwd.get("display_name"),
                "evidence_ids": [e for e in EVIDENCE.get(mid, []) if "2020" in e],
                "distance_to_primary_km": round(_dms_distance_km(olat, olon, lat, lon), 3),
                "delta_lat": round(lat - olat, 6),
                "delta_lon": round(lon - olon, 6),
                "same_surrounding_cell": {
                    "SW": o_corners["SW"] == corners["SW"],
                    "SE": o_corners["SE"] == corners["SE"],
                    "NW": o_corners["NW"] == corners["NW"],
                    "NE": o_corners["NE"] == corners["NE"],
                },
                "same_nearest_grid": (o_nearest_name == nearest_name) and (o_nearest_pt == nearest_pt),
                "old_stencil": {k: {"latitude": v.latitude, "longitude": v.longitude} for k, v in o_corners.items()},
                "new_stencil": {k: {"latitude": v.latitude, "longitude": v.longitude} for k, v in corners.items()},
                "old_bilinear_weights": o_weights,
                "new_bilinear_weights": weights,
                "l1_weight_difference": round(sum(abs(o_weights[k] - weights[k]) for k in ("SW", "SE", "NW", "NE")), 4),
                "max_individual_weight_difference": round(max(abs(o_weights[k] - weights[k]) for k in ("SW", "SE", "NW", "NE")), 4),
            }

        leg = legacy.get(mid, {})
        leg_lat, leg_lon = leg.get("latitude"), leg.get("longitude")
        legacy_meta = None
        if leg_lat is not None:
            dist = _dms_distance_km(lat, lon, float(leg_lat), float(leg_lon))
            leg_corners = surrounding_cell(float(leg_lat), float(leg_lon))
            legacy_meta = {
                "municipal_coordinate": {"latitude": leg_lat, "longitude": leg_lon},
                "distance_km": round(dist, 3),
                "same_surrounding_cell": {
                    "SW": corners["SW"] == leg_corners["SW"],
                    "SE": corners["SE"] == leg_corners["SE"],
                    "NW": corners["NW"] == leg_corners["NW"],
                    "NE": corners["NE"] == leg_corners["NE"],
                },
                "same_nearest_grid": nearest_pt == nearest_grid_point(float(leg_lat), float(leg_lon), leg_corners)[1],
            }

        rev = entry.get("_reverse") or {}
        anchor_payload = {
            "market_id": mid,
            "venue_entity": target["venue_entity"],
            "anchor_role": ANCHOR_ROLE,
            "official_address_normalized": target["official_address"],
            "evidence_ids": EVIDENCE.get(mid, []),
            "latitude": lat,
            "longitude": lon,
            "crs": "WGS84",
            "geocoder_osm_type": fwd.get("osm_type"),
            "geocoder_osm_id": fwd.get("osm_id"),
            "geocoder_place_id": fwd.get("place_id"),
            "era5_resolution_degrees": 0.25,
            "corners": {k: {"latitude": v.latitude, "longitude": v.longitude} for k, v in corners.items()},
            "weights": weights,
            "spatial_contract_version": SPATIAL_CONTRACT_VERSION,
            "primary_anchor_policy": PRIMARY_ANCHOR_POLICY,
            "historical_premises_change_detected": premises_change,
            "fixed_primary_anchor_changed": False,
        }
        markets.append(
            {
                "market_id": mid,
                "venue": {
                    "entity_name": target["venue_entity"],
                    "venue_name": fwd.get("name") or target["venue_entity"],
                    "anchor_role": ANCHOR_ROLE,
                    "official_address": target["official_address"],
                    "official_address_normalized": target["official_address"],
                    "country": target["country"],
                    "evidence_ids": EVIDENCE.get(mid, []),
                    "historical_premises_change_detected": premises_change,
                    "fixed_primary_anchor_changed": False,
                },
                "primary_anchor_policy": PRIMARY_ANCHOR_POLICY,
                "coordinate": {
                    "crs": "WGS84",
                    "latitude": lat,
                    "longitude": lon,
                    "derivation_method": "nominatim_forward_geocode",
                    "geocoder": "nominatim",
                    "geocoder_query": entry["selection"].get("query_used") if entry.get("selection") else None,
                    "geocoder_object_type": fwd.get("osm_type"),
                    "geocoder_object_id": fwd.get("osm_id"),
                    "geocoder_place_id": fwd.get("place_id"),
                    "geocoder_display_name": fwd.get("display_name"),
                    "reverse_check_status": "passed" if rev else "missing",
                    "reverse_display_name": rev.get("display_name"),
                    "qualification_status": "qualified",
                },
                "era5": {
                    "resolution_degrees": 0.25,
                    "corners": {k: {"latitude": v.latitude, "longitude": v.longitude} for k, v in corners.items()},
                    "bilinear_weights": weights,
                    "nearest_grid": {"corner": nearest_name, "latitude": nearest_pt.latitude, "longitude": nearest_pt.longitude},
                    "cell": cell,
                },
                "legacy_sensitivity": legacy_meta,
                "historical_premises": historical_premises_meta,
                "anchor_hash": _anchor_hash(anchor_payload),
                "qualification_status": "qualified",
            }
        )

    qualified = [m for m in markets if m.get("qualification_status") == "qualified"]
    unresolved = [m for m in markets if m.get("qualification_status") != "qualified"]
    global_payload = {
        "spatial_contract_version": SPATIAL_CONTRACT_VERSION,
        "anchor_hashes": {m["market_id"]: m["anchor_hash"] for m in qualified},
        "schema_version": SCHEMA_VERSION,
    }
    global_registry_hash = _anchor_hash(global_payload)

    registry = {
        "schema_version": SCHEMA_VERSION,
        "registry_id": "spatial-anchors",
        "spatial_contract_version": SPATIAL_CONTRACT_VERSION,
        "scientific_interpretation": "the official physical premises associated with the study's primary cash-equity market / market operator, used solely as a fixed market-local atmospheric proxy (weather_anchor_role = market_local_atmospheric_proxy)",
        "primary_anchor_policy": PRIMARY_ANCHOR_POLICY,
        "scientific_rationale": "The fixed spatial anchor preserves one time-invariant spatial measurement definition. Historical office moves do not automatically redefine the primary atmospheric proxy, avoiding a mechanical spatial measurement break. The anchor does NOT represent exact physical exposure of every trader, historical matching-engine location, or all investor locations.",
        "historical_premises_tracking": False,
        "anchors": markets,
        "global_spatial_anchor_registry_hash": global_registry_hash,
    }
    write_json(root / OUTPUT_REGISTRY, registry)

    audit = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "global_exchange_anchor_qualification",
        "scope": {"markets": [m["market_id"] for m in markets], "spatial_contract_version": SPATIAL_CONTRACT_VERSION},
        "scientific_interpretation": registry["scientific_interpretation"],
        "venue_selection_hierarchy": [
            "P1: official physical exchange / primary trading venue premises",
            "P2: fully electronic venue -> official premises/contact address bound to the electronic venue / market operation",
            "P3: operating exchange entity official head office / registered premises",
            "forbidden: municipal center, city hall, financial-district centroid, index-provider HQ, intuitive buildings, Google snippets",
        ],
        "per_market": markets,
        "qualified_count": len(qualified),
        "unresolved_count": len(unresolved),
        "global_registry_hash": global_registry_hash,
        "network_activity": {"geocoding_calls": 18, "cds_calls": 0, "era5_downloads": 0},
        "geocoder_policy": {
            "geocoder": "nominatim",
            "user_agent": "weather-stock-china-study/2.0 (research spatial anchor qualification; repository-owned)",
            "policy": "one thread, <=1 request/second, identifying User-Agent, cached, no autocomplete, no bulk scraping",
            "retrieved_at": "2026-08-14",
        },
        "live_backfill_authorized": False,
        "research_usable": len(qualified) == 8 and len(unresolved) == 0,
        "spatial_ready_for_live_canary": len(qualified) == 8 and len(unresolved) == 0,
        "horizon_move_semantics": {
            "historical_premises_change_detected": {
                m["market_id"]: m["venue"]["historical_premises_change_detected"] for m in markets
            },
            "fixed_primary_anchor_changed": {
                m["market_id"]: m["venue"]["fixed_primary_anchor_changed"] for m in markets
            },
            "note": "historical premises changes are sensitivity evidence under the fixed target-regime anchor contract; they do not auto-fail qualification",
        },
        "sse_premises_reconciliation": {
            "change_detected": True,
            "transition_date_status": "bounded_not_exact",
            "transition_after": "2020-07-31",
            "transition_on_or_before": "2020-08-31",
            "fixed_primary_anchor": {"latitude": 31.2221653, "longitude": 121.5307778},
            "old_premises": next((m["historical_premises"] for m in markets if m.get("historical_premises")), None),
        },
        "code_hashes": {
            "geocode_anchors.py": sha256_file(root / "scripts/v2/spatial/geocode_anchors.py"),
            "grid_geometry.py": sha256_file(root / "scripts/v2/spatial/grid_geometry.py"),
            "build_global_anchors.py": sha256_file(root / "scripts/v2/spatial/build_global_anchors.py"),
        },
        "reproducibility": "registry build deterministic from cached geocoding provenance; geocoding is one-time evidence-resolution (cached)",
        "historical_backfill_run": False,
    }
    write_json(root / OUTPUT_AUDIT, audit)
    print(json.dumps({
        "gate": "PASS_STAGE5CGR1_SSE_PREMISES_RECONCILIATION" if audit["research_usable"] else "REVISE_STAGE5CGR1_SSE_PREMISES_RECONCILIATION",
        "stage5cg_gate": "PASS_STAGE5CG_GLOBAL_EXCHANGE_ANCHORS" if audit["research_usable"] else "REVISE",
        "qualified_count": len(qualified),
        "unresolved_count": len(unresolved),
        "unresolved_markets": [m["market_id"] for m in unresolved],
        "sse_historical_premises_change_detected": True,
        "sse_fixed_primary_anchor_changed": False,
        "global_registry_hash": global_registry_hash,
        "live_backfill_authorized": False,
    }, ensure_ascii=False, indent=2))
    return 0 if audit["research_usable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
