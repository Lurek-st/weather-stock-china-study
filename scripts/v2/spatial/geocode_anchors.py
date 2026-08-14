"""Stage 5C-G: official exchange anchor geocoding (Nominatim, policy-compliant).

One-time evidence-resolution step for the 8 research markets.  For each market
the OFFICIAL verified address (from exchange/operator sources) is forward
geocoded via Nominatim, the result is validated (city/country/street/POI), and
a reverse check is run on the selected coordinate.  Provenance is cached
locally so re-runs never re-query.

Nominatim policy honored: identifying User-Agent, <=1 request/second, cache,
no bulk scraping.  Only 8 locations (+8 reverse checks) are resolved.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import repo_root

USER_AGENT = "weather-stock-china-study/2.0 (research spatial anchor qualification; repository-owned, one-time 8-location resolution)"
CACHE_PATH = ".local/geocode/global-exchange-anchors-geocode-cache.json"
OUTPUT_PATH = "data/audits/v2/spatial/global-exchange-anchor-geocoding.json"

# Official verified addresses (evidence: exchange/operator official sites).
# queries: list tried in order (address first, then name+city / street fallback);
# the first query returning results is used, and both the queries tried and the
# selection rationale are recorded.
TARGETS: list[dict[str, Any]] = [
    {
        "market_id": "sse_composite",
        "venue_entity": "Shanghai Stock Exchange",
        "official_address": "388 Yanggao South Road, Pudong New Area, Shanghai, China",
        "official_address_zh": "上海市浦东新区杨高南路388号",
        "country": "China",
        "city": "Shanghai",
        "city_aliases": ["shanghai", "上海市", "上海"],
        "street_aliases": ["杨高南路", "yanggao", "金融交易广场"],
        "queries": [
            "上海金融交易广场 上海",
            "388 Yanggao South Road Shanghai",
            "Shanghai Stock Exchange Pudong Shanghai China",
        ],
    },
    {
        "market_id": "szse_component",
        "venue_entity": "Shenzhen Stock Exchange",
        "official_address": "2012 Shennan Boulevard, Futian District, Shenzhen, Guangdong, China",
        "official_address_zh": "深圳市福田区深南大道2012号",
        "country": "China",
        "city": "Shenzhen",
        "city_aliases": ["shenzhen", "深圳市", "深圳"],
        "street_aliases": ["深南大道", "shennan"],
        "queries": [
            "Shenzhen Stock Exchange 2012 Shennan Boulevard Futian District Shenzhen China",
            "深圳证券交易所 深南大道2012号 深圳",
        ],
    },
    {
        "market_id": "topix",
        "venue_entity": "Tokyo Stock Exchange, Inc.",
        "official_address": "2-1 Nihombashi Kabutocho, Chuo-ku, Tokyo 103-8220, Japan",
        "official_address_zh": "東京都中央区日本橋兜町2-1",
        "country": "Japan",
        "city": "Tokyo",
        "city_aliases": ["tokyo", "tokio", "東京都", "東京", "chuo", "中央区"],
        "street_aliases": ["兜町", "kabutocho"],
        "queries": [
            "Tokyo Stock Exchange 2-1 Nihombashi Kabutocho Chuo-ku Tokyo Japan",
            "Tokyo Stock Exchange Kabutocho Chuo Tokyo Japan",
            "東京証券取引所 日本橋兜町",
        ],
    },
    {
        "market_id": "nifty50",
        "venue_entity": "National Stock Exchange of India Ltd",
        "official_address": "Exchange Plaza, C-1, Block G, Bandra Kurla Complex, Bandra (East), Mumbai 400051, India",
        "country": "India",
        "city": "Mumbai",
        "city_aliases": ["mumbai", "bombay", "bandra"],
        "street_aliases": ["bandra", "kurla", "exchange plaza", "avenue 3", "g block"],
        "queries": [
            "National Stock Exchange of India Exchange Plaza Bandra Kurla Complex Mumbai India",
            "national stock exchange bandra kurla complex mumbai",
            "Exchange Plaza Bandra Kurla Complex Mumbai India",
        ],
    },
    {
        "market_id": "ftse100",
        "venue_entity": "London Stock Exchange plc",
        "official_address": "10 Paternoster Square, London EC4M 7LS, United Kingdom",
        "country": "United Kingdom",
        "city": "London",
        "city_aliases": ["london", "city of london"],
        "street_aliases": ["paternoster"],
        "queries": [
            "London Stock Exchange 10 Paternoster Square London EC4M 7LS United Kingdom",
            "10 Paternoster Square London United Kingdom",
        ],
    },
    {
        "market_id": "dax",
        "venue_entity": "Deutsche Börse AG (Xetra operator)",
        "official_address": "Mergenthalerallee 61, 65760 Eschborn, Germany",
        "country": "Germany",
        "city": "Eschborn",
        "city_aliases": ["eschborn", "frankfurt am main", "frankfurt"],
        "street_aliases": ["mergenthalerallee", "eschborn"],
        "queries": [
            "Deutsche Börse Mergenthalerallee 61 65760 Eschborn Germany",
            "Mergenthalerallee 61 65760 Eschborn Germany",
            "Deutsche Börse Eschborn Germany",
        ],
    },
    {
        "market_id": "sp500",
        "venue_entity": "New York Stock Exchange",
        "official_address": "11 Wall Street, New York, NY 10005, United States",
        "country": "United States",
        "city": "New York",
        "city_aliases": ["new york", "new york city", "manhattan"],
        "street_aliases": ["wall street"],
        "queries": [
            "New York Stock Exchange 11 Wall Street New York NY 10005 United States",
            "11 Wall Street New York United States",
        ],
    },
    {
        "market_id": "bse50",
        "venue_entity": "Beijing Stock Exchange",
        "official_address": "Jinyang Building, No.26-D Financial Street, Xicheng District, Beijing 100033, China",
        "official_address_zh": "北京市西城区金融大街丁26号金阳大厦",
        "country": "China",
        "city": "Beijing",
        "city_aliases": ["beijing", "北京市", "北京", "xicheng", "西城"],
        "street_aliases": ["金融大街", "financial street", "金阳大厦", "jinyang", "学院胡同"],
        "queries": [
            "北京证券交易所 金融大街丁26号 金阳大厦 北京",
            "金阳大厦 北京",
            "金融大街26号 北京",
            "北京证券交易所 北京",
        ],
    },
]


def _country_ok(target: dict[str, Any], hit: dict[str, Any]) -> bool:
    addr = hit.get("address", {})
    cc = addr.get("country_code", "").lower()
    country_map = {
        "China": "cn", "Japan": "jp", "India": "in", "United Kingdom": "gb",
        "Germany": "de", "United States": "us",
    }
    return cc == country_map.get(target["country"], "")


def _city_ok(target: dict[str, Any], hit: dict[str, Any]) -> bool:
    aliases = [a.lower() for a in target.get("city_aliases", [target["city"]])]
    addr = hit.get("address", {})
    addr_text = " ".join(str(v) for v in addr.values()).lower()
    display = (hit.get("display_name") or "").lower()
    for alias in aliases:
        if alias in addr_text or alias in display:
            return True
    return False


def _get(url: str) -> dict[str, Any] | list[Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def forward(query: str) -> list[dict[str, Any]]:
    q = urllib.parse.quote(query)
    url = f"https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&addressdetails=1&q={q}"
    return _get(url)


def reverse(lat: str, lon: str) -> dict[str, Any]:
    url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&addressdetails=1&lat={lat}&lon={lon}"
    return _get(url)


def _street_ok(target: dict[str, Any], hit: dict[str, Any]) -> bool:
    aliases = [a.lower() for a in target.get("street_aliases", [])]
    if not aliases:
        return True
    addr = hit.get("address", {})
    addr_text = " ".join(str(v) for v in addr.values()).lower()
    display = (hit.get("display_name") or "").lower()
    return any(a in addr_text or a in display for a in aliases)


POI_TYPES = {"stock_exchange", "exchange", "office", "company", "commercial", "building", "yes"}


def main(argv: list[str] | None = None) -> int:
    root = repo_root()
    cache_path = root / CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    results = []
    for target in TARGETS:
        mid = target["market_id"]
        if mid in cache and cache[mid].get("forward") and cache[mid].get("selection"):
            entry = cache[mid]
            entry["_from_cache"] = True
        else:
            # try queries in order; collect validated hits; prefer street-aligned
            # named POI/building; record queries tried + selection rationale.
            entry = {"queries_tried": [], "forward": [], "selection": None, "warning": None}
            candidates: list[dict[str, Any]] = []
            for qi, query in enumerate(target["queries"]):
                hits = forward(query)
                entry["queries_tried"].append(query)
                for h in hits:
                    if _country_ok(target, h) and _city_ok(target, h):
                        h["_query_index"] = qi
                        h["_street_aligned"] = _street_ok(target, h)
                        candidates.append(h)
                if candidates:
                    break
                time.sleep(1.1)
            if candidates:
                # prefer street-aligned named POI; then street-aligned non-POI;
                # then named POI without street match (warning); reject centroids.
                def score(h: dict[str, Any]) -> tuple:
                    poi = h.get("type") in POI_TYPES or h.get("class") in POI_TYPES
                    return (0 if h["_street_aligned"] else 1, 0 if poi else 1, -h["_query_index"])
                best = min(candidates, key=score)
                entry["forward"] = [best]
                entry["selection"] = {
                    "query_used": target["queries"][best["_query_index"]],
                    "query_index": best["_query_index"],
                    "street_aligned": best["_street_aligned"],
                    "poi_type": best.get("type"),
                    "rationale": "street-aligned named POI" if (best["_street_aligned"] and best.get("type") in POI_TYPES) else ("street-aligned result" if best["_street_aligned"] else "named POI without street-alias match (warning)"),
                }
                if not best["_street_aligned"]:
                    entry["warning"] = "selected hit is not street-aligned with the official address; reverse check MUST confirm"
            else:
                entry["warning"] = "no validated hit; market unresolved"
            cache[mid] = entry
        results.append({"market_id": mid, "target": target, **entry})

    cache_path.write_text(json.dumps({x["market_id"]: x for x in results}, ensure_ascii=False, indent=2), encoding="utf-8")

    # reverse checks (one per market)
    for r in results:
        mid = r["market_id"]
        if "_reverse" in r:
            continue
        hits = r.get("forward") or []
        if hits:
            r["_reverse"] = reverse(hits[0]["lat"], hits[0]["lon"])
            time.sleep(1.1)
    cache_path.write_text(json.dumps({x["market_id"]: x for x in results}, ensure_ascii=False, indent=2), encoding="utf-8")

    out = root / OUTPUT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        hits = r.get("forward") or []
        rev = r.get("_reverse") or {}
        sel = r.get("selection") or {}
        if hits:
            h = hits[0]
            print(f"{r['market_id']:15s} q={sel.get('query_index','?')} aligned={h.get('_street_aligned')} lat={h.get('lat')} lon={h.get('lon')} type={h.get('type')} | {h.get('display_name','')[:58]}")
        else:
            print(f"{r['market_id']:15s} NO VALIDATED RESULT (queries tried: {len(r.get('queries_tried', []))})")
        if rev:
            print(f"{'':15s} reverse={rev.get('display_name','')[:68]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
