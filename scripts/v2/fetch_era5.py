from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import RawArtifactStore, load_yaml, repo_root, week_bounds


VARIABLES = [
    "2m_temperature",
    "2m_dewpoint_temperature",
    "total_precipitation",
    "total_cloud_cover",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "instantaneous_10m_wind_gust",
    "surface_solar_radiation_downwards",
]


def requests_for_week(root: Path, week: str) -> list[dict]:
    start, end = week_bounds(week)
    locations = load_yaml(root / "config" / "v2" / "locations.yaml")["locations"]
    days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    requests = []
    for location in locations:
        request = {
            "product_type": ["reanalysis"],
            "variable": VARIABLES,
            "date": days,
            "time": [f"{hour:02d}:00" for hour in range(24)],
            "data_format": "netcdf",
            "download_format": "unarchived",
            "area": [
                location["latitude"] + 0.13,
                location["longitude"] - 0.13,
                location["latitude"] - 0.13,
                location["longitude"] + 0.13,
            ],
        }
        requests.append({"city_id": location["city_id"], "request": request})
    return requests


def main() -> int:
    parser = argparse.ArgumentParser(description="Download immutable ERA5/ERA5T hourly raw data.")
    parser.add_argument("--week", required=True)
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    requests = requests_for_week(args.root, args.week)
    if args.dry_run:
        print(json.dumps({"dataset": "reanalysis-era5-single-levels", "requests": requests}, indent=2))
        return 0
    try:
        import cdsapi
    except ImportError as exc:
        raise SystemExit("cdsapi is required; install requirements.txt") from exc
    results = []
    client = cdsapi.Client()
    with tempfile.TemporaryDirectory(prefix="weather-stock-v2-") as tmp:
        for item in requests:
            target = Path(tmp) / f"{item['city_id']}.nc"
            client.retrieve("reanalysis-era5-single-levels", item["request"], str(target))
            source_id = "cds_era5_hourly" if args.final else "cds_era5t_hourly"
            result = RawArtifactStore(args.root / "data" / "source_raw" / "v2" / "weather").persist(
                source_id=source_id,
                provider="ECMWF Copernicus Climate Change Service",
                logical_name=f"{item['city_id']}-{args.week}",
                payload=target.read_bytes(),
                request=item,
                status="final" if args.final else "provisional",
                licence="CC-BY-4.0 catalogue terms and attribution",
                suffix=".nc",
            )
            results.append(str(result.manifest_path))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
