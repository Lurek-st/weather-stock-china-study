from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import xarray as xr

from scripts.v2.core import normalize_weather_frame, repo_root


def read_input(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() not in {".nc", ".netcdf"}:
        raise SystemExit("weather input must be CSV or NetCDF")
    with path.open("rb") as handle:
        magic = handle.read(3)
    engine = "scipy" if magic == b"CDF" else "netcdf4"
    dataset = xr.open_dataset(path, engine=engine)
    if "latitude" in dataset.coords and "longitude" in dataset.coords:
        dataset = dataset.sel(
            latitude=float(dataset["latitude"].mean()),
            longitude=float(dataset["longitude"].mean()),
            method="nearest",
        )
    rename = {
        "t2m": "temperature_k",
        "d2m": "dewpoint_k",
        "tp": "precipitation_m",
        "tcc": "cloud_cover_fraction",
        "u10": "wind_u_mps",
        "v10": "wind_v_mps",
        "i10fg": "wind_gust_mps",
        "fg10": "wind_gust_mps",
        "ssrd": "solar_radiation_j_m2",
    }
    if "valid_time" in dataset.variables or "valid_time" in dataset.coords:
        rename["valid_time"] = "timestamp_utc"
    elif "time" in dataset.variables or "time" in dataset.coords:
        rename["time"] = "timestamp_utc"
    available = {key: value for key, value in rename.items() if key in dataset.variables or key in dataset.coords}
    frame = dataset.rename(available).to_dataframe().reset_index()
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize weather CSV to V2 canonical Parquet.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--data-class", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    output = args.output or args.root / "data" / "canonical" / "v2" / "weather" / f"{args.city}.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    normalize_weather_frame(
        read_input(args.input),
        city_id=args.city,
        source_id=args.source_id,
        data_class=args.data_class,
    ).to_parquet(output, index=False)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
