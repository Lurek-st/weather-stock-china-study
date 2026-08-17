from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from scripts.v2.build_weekly_audit import build_audit
from scripts.v2.core import (
    RawArtifactStore,
    assess_maturity,
    build_panel,
    build_weather_windows,
    fixture_frames,
    load_yaml,
    normalize_market_frame,
    repo_root,
    validate_registry_files,
    week_bounds,
    write_json,
    write_panel_bundle,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run one deterministic mature-week V2 pipeline.")
    result.add_argument("--week", required=True, help="ISO week YYYY-Www")
    result.add_argument("--root", type=Path, default=repo_root())
    result.add_argument("--offline", action="store_true")
    result.add_argument("--refresh", action="store_true")
    result.add_argument("--allow-provisional", action="store_true")
    result.add_argument("--freeze", action="store_true")
    result.add_argument("--dry-run", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    start, end = week_bounds(args.week)
    errors = validate_registry_files(args.root)
    if errors:
        print(json.dumps({"status": "configuration_invalid", "errors": errors}, indent=2))
        return 2
    plan = {
        "schema_version": "2.0.0",
        "week": args.week,
        "bounds": [start.isoformat(), end.isoformat()],
        "mode": "offline_fixture" if args.offline else "registered_live_sources",
        "refresh": args.refresh,
        "allow_provisional": args.allow_provisional,
        "freeze": args.freeze,
        "steps": [
            "readiness",
            "raw_acquisition",
            "hash_validation",
            "normalization",
            "weather_windows",
            "market_returns",
            "panel",
            "audit",
        ],
    }
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    canonical = args.root / "data" / "canonical" / "v2"
    if args.offline:
        if args.freeze:
            print(json.dumps({"status": "not_ready", "reason": "offline fixture is ERA5T provisional"}))
            return 4
        if not args.allow_provisional:
            print(json.dumps({"status": "provisional_ready", "reason": "pass --allow-provisional"}))
            return 4
        weather, raw_market, trading_dates = fixture_frames(args.root, args.week)
        fixture_path = args.root / "tests" / "v2" / "fixtures" / "mature_week_spec.yaml"
        RawArtifactStore(args.root / "data" / "source_raw" / "v2" / "fixtures").persist(
            source_id="versioned_fixture",
            provider="repository test fixture",
            logical_name=args.week,
            payload=fixture_path.read_bytes(),
            request={"offline": True, "week": args.week},
            status="provisional",
            licence="repository licence",
            suffix=".yaml",
        )
        market = normalize_market_frame(raw_market)
        weather_class = "provisional_reanalysis"
        calendar_status = "verified_fixture_calendar"
    else:
        weather_path = canonical / "weather" / f"{args.week}.parquet"
        market_path = canonical / "market" / f"{args.week}.parquet"
        calendar_path = canonical / "calendars" / f"{args.week}.json"
        missing = [
            str(path.relative_to(args.root))
            for path in (weather_path, market_path, calendar_path)
            if not path.exists()
        ]
        if missing:
            print(
                json.dumps(
                    {
                        "status": "not_ready",
                        "reason": "registered acquisition/canonical inputs are incomplete",
                        "missing": missing,
                        "next": "run registered fetch/normalize commands; controlled market and official calendar evidence may be required",
                    },
                    indent=2,
                )
            )
            return 4
        weather = pd.read_parquet(weather_path)
        market = pd.read_parquet(market_path)
        trading_dates = load_yaml(calendar_path)["trading_dates"] if calendar_path.suffix == ".yaml" else json.loads(calendar_path.read_text(encoding="utf-8"))["trading_dates"]
        weather_classes = set(weather["data_class"].dropna())
        if len(weather_classes) != 1:
            print(json.dumps({"status": "not_ready", "reason": "mixed weather data classes"}))
            return 4
        weather_class = next(iter(weather_classes))
        calendar_status = "verified_registered_calendar"
        if weather_class == "provisional_reanalysis" and not args.allow_provisional:
            print(json.dumps({"status": "provisional_ready", "reason": "pass --allow-provisional"}))
            return 4
        if args.freeze and weather_class != "final_reanalysis":
            print(json.dumps({"status": "not_ready", "reason": "freeze requires final ERA5"}))
            return 4

    markets = load_yaml(args.root / "config" / "v2" / "markets.yaml")["markets"]
    locations = load_yaml(args.root / "config" / "v2" / "locations.yaml")
    windows = pd.concat(
        [build_weather_windows(weather, market_config, trading_dates) for market_config in markets],
        ignore_index=True,
    )
    maturity = assess_maturity(
        weather_class=weather_class,
        market_statuses=market["value_status"],
        calendar_verified=True,
    )
    if maturity["status"] == "not_ready":
        print(json.dumps(maturity, indent=2))
        return 4
    if args.freeze and maturity["status"] != "frozen":
        print(json.dumps({"status": maturity["status"], "reason": "freeze gates are not satisfied"}))
        return 4
    tier = "frozen" if args.freeze else "provisional"
    maturity_manifest = {
        "schema_version": "2.0.0",
        "week": args.week,
        "status": maturity["status"],
        "weather_status": weather_class,
        "market_status": "registered_values",
        "calendar_status": calendar_status,
        "blocking_reasons": maturity["blocking_reasons"],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    (canonical / "weather").mkdir(parents=True, exist_ok=True)
    (canonical / "market").mkdir(parents=True, exist_ok=True)
    (canonical / "calendars").mkdir(parents=True, exist_ok=True)
    if args.offline:
        weather.to_parquet(canonical / "weather" / f"{args.week}.parquet", index=False)
        market.to_parquet(canonical / "market" / f"{args.week}.parquet", index=False)
        write_json(canonical / "calendars" / f"{args.week}.json", {"trading_dates": trading_dates})
    windows.to_parquet(canonical / "weather" / f"{args.week}-windows.parquet", index=False)
    write_json(canonical / f"{args.week}-maturity.json", maturity_manifest)
    panel = build_panel(market, windows, locations, tier)
    target = args.root / "data" / "panel" / "v2" / tier
    write_panel_bundle(panel, target, maturity_manifest)
    audit = build_audit(args.root, args.week, tier, maturity_manifest)
    audit_path = args.root / "data" / "audits" / "v2" / f"{args.week}-{tier}.json"
    write_json(audit_path, audit)
    print(
        json.dumps(
            {
                "status": maturity["status"],
                "week": args.week,
                "panel_rows": len(panel),
                "cities": panel["city_id"].nunique(),
                "markets": panel["market_id"].nunique(),
                "audit_passed": audit["validation"]["passed"],
                "panel": str(target.relative_to(args.root)),
            },
            indent=2,
        )
    )
    return 0 if audit["validation"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
