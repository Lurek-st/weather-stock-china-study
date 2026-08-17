from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import RawArtifactStore, load_yaml, repo_root


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch NOAA hourly station validation data.")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--city")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stations = load_yaml(args.root / "config" / "v2" / "stations.yaml")["stations"]
    if args.city:
        stations = [row for row in stations if row["city_id"] == args.city]
    requests = []
    for row in stations:
        for role in ("primary", "backup"):
            station_id = row[f"{role}_id"]
            url = f"https://www.ncei.noaa.gov/data/global-hourly/access/{args.year}/{station_id}.csv"
            requests.append({"city_id": row["city_id"], "role": role, "station_id": station_id, "url": url})
    if args.dry_run:
        print(json.dumps(requests, indent=2))
        return 0
    store = RawArtifactStore(args.root / "data" / "source_raw" / "v2" / "weather")
    failures = 0
    for request in requests:
        try:
            with urllib.request.urlopen(request["url"], timeout=60) as response:
                payload = response.read()
            store.persist(
                source_id="noaa_ghcnh",
                provider="NOAA NCEI",
                logical_name=f"{request['city_id']}-{request['role']}-{args.year}",
                payload=payload,
                request=request,
                status="station_observation",
                licence="US government data; cite NOAA NCEI",
                suffix=".csv",
            )
        except Exception as exc:
            failures += 1
            print(f"validation-only station unavailable: {request['station_id']}: {exc}")
    return 0 if failures < len(requests) else 3


if __name__ == "__main__":
    raise SystemExit(main())
