"""Pilot-hardened weather-window builder for the Taipei ERA5 pilot week.

The core aggregation lives in ``scripts.v2.core.build_weather_windows``
(instantaneous variables sampled by valid timestamp; one-hour accumulation
variables aggregated only when their whole interval lies inside the window;
partial intervals excluded and flagged). This CLI hardens the *execution and
audit chain*: it binds the canonical Parquet to the normalization audit, the
normalization audit to the raw acceptance audit, takes trading dates only
from the verified 2026 TAIEX pilot calendar audit, runs a single market
(taiex), writes a local-only 15-row window Parquet, and produces a tracked
non-sensitive window acceptance audit plus an independent oracle
cross-check.

Zero network: no CDS client is imported or instantiated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from scripts.v2.core import CORE_WEATHER_COLUMNS, build_weather_windows, load_yaml, repo_root, write_json

WINDOW_ORDER = ["pre_open", "trading_session", "full_day"]
PARTIAL_INTERVAL_POLICY = "partial_accumulation_interval_excluded"
EXPECTED_COUNT_CONTRACT = {
    "pre_open": {"instantaneous": 2, "accumulation": 2},
    "trading_session": {"instantaneous": 5, "accumulation": 4},
    "full_day": {"instantaneous": 24, "accumulation": 24},
}
WINDOW_BUILDER_CODE_FILES = [
    "scripts/v2/build_weather_windows.py",
    "scripts/v2/core.py",
    "scripts/v2/normalize_weather.py",
    "scripts/v2/fetch_era5.py",
]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_evidence_chain(
    weather: Path,
    normalization_audit_path: Path,
    raw_audit_path: Path,
    calendar_audit_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    """Verify all audit bindings; returns the loaded audits and trading dates."""
    canonical_sha = sha256_file(weather)
    normalization = load_json(normalization_audit_path)
    raw_audit = load_json(raw_audit_path)
    calendar = load_json(calendar_audit_path)

    if canonical_sha != normalization.get("canonical_parquet_sha256"):
        raise SystemExit(
            f"canonical sha mismatch: parquet {canonical_sha[:16]} != audit {str(normalization.get('canonical_parquet_sha256'))[:16]}"
        )
    if normalization.get("canonical_row_count") != 121:
        raise SystemExit(f"canonical row_count != 121: {normalization.get('canonical_row_count')}")
    if normalization.get("canonical_status") != "accepted":
        raise SystemExit(f"canonical_status != accepted: {normalization.get('canonical_status')}")
    bound_raw_sha = normalization.get("input_raw_acceptance_audit_sha256")
    actual_raw_sha = sha256_file(raw_audit_path)
    if bound_raw_sha != actual_raw_sha:
        raise SystemExit("normalization audit raw-audit SHA binding broken")
    if raw_audit.get("raw_acceptance_status") != "accepted":
        raise SystemExit(f"raw_acceptance_status != accepted: {raw_audit.get('raw_acceptance_status')}")
    if calendar.get("pilot_calendar_status") != "calendar_verified_for_2026_pilot_window":
        raise SystemExit(
            f"calendar pilot status != calendar_verified_for_2026_pilot_window: {calendar.get('pilot_calendar_status')}"
        )
    trading_dates = (calendar.get("selection") or {}).get("selected_week_dates")
    if trading_dates != [
        "2026-03-02",
        "2026-03-03",
        "2026-03-04",
        "2026-03-05",
        "2026-03-06",
    ]:
        raise SystemExit(f"calendar selected_week_dates not the exact 5-day pilot week: {trading_dates}")
    return normalization, raw_audit, calendar, canonical_sha, trading_dates


def calendar_overrides_for(trading_dates: list[str]) -> dict[str, dict[str, Any]]:
    return {
        day: {"calendar_status": "official_verified_open"}
        for day in trading_dates
    }


def validate_window_frame(frame: pd.DataFrame, trading_dates: list[str]) -> dict[str, Any]:
    if len(frame) != 15:
        raise SystemExit(f"window row count {len(frame)} != 15")
    keys = list(zip(frame["city_id"], frame["market_id"], frame["trading_date"], frame["window"]))
    if len(set(keys)) != 15:
        raise SystemExit("window unique key not 15/15")
    expected_dates = set(trading_dates)
    if set(frame["trading_date"]) != expected_dates:
        raise SystemExit("window trading dates do not match calendar")
    flags_ok = True
    trading_partial = True
    for _, row in frame.iterrows():
        window = row["window"]
        flags = list(row["quality_flags"])
        contract = EXPECTED_COUNT_CONTRACT[window]
        if int(row["instantaneous_observed_count"]) != contract["instantaneous"]:
            flags_ok = False
        if int(row["accumulation_observed_count"]) != contract["accumulation"]:
            flags_ok = False
        if row["instantaneous_coverage_ratio"] != 1.0 or row["accumulation_coverage_ratio"] != 1.0:
            flags_ok = False
        if window == "pre_open" and "partial_accumulation_interval_excluded" in flags:
            flags_ok = False
        if window == "full_day" and "partial_accumulation_interval_excluded" in flags:
            flags_ok = False
        if window == "trading_session" and "partial_accumulation_interval_excluded" not in flags:
            trading_partial = False
        for banned in ("missing_hours", "missing_instantaneous_hours", "missing_accumulation_intervals", "temporal_support_metadata_missing"):
            if banned in flags:
                flags_ok = False
    if not flags_ok:
        raise SystemExit("window count/coverage/flag contract violated")
    if not trading_partial:
        raise SystemExit("trading_session must carry partial_accumulation_interval_excluded")
    for column in CORE_WEATHER_COLUMNS:
        if frame[column].isna().any():
            raise SystemExit(f"null weather value in {column}")
        if not np.isfinite(frame[column].astype(float)).all():
            raise SystemExit(f"non-finite weather value in {column}")
    if (frame["relative_humidity_pct"] < 0).any() or (frame["relative_humidity_pct"] > 100).any():
        raise SystemExit("relative_humidity_pct out of range")
    if (frame["cloud_cover_pct"] < 0).any() or (frame["cloud_cover_pct"] > 100).any():
        raise SystemExit("cloud_cover_pct out of range")
    for column in ("precipitation_mm", "wind_speed_mps", "max_gust_mps", "solar_radiation_mj_m2"):
        if (frame[column] < 0).any():
            raise SystemExit(f"{column} negative")
    return {"row_count": len(frame), "unique_key_count": len(set(keys))}


def oracle_windows(hourly: pd.DataFrame, trading_dates: list[str]) -> pd.DataFrame:
    """Independent pilot oracle: recompute the 15 rows from the canonical
    hourly frame using direct Asia/Taipei time masks. Does not call
    build_weather_windows."""
    tz = ZoneInfo("Asia/Taipei")
    subset = hourly.copy()
    subset["timestamp_utc"] = pd.to_datetime(subset["timestamp_utc"], utc=True)
    subset["timestamp_local"] = subset["timestamp_utc"].dt.tz_convert(tz)
    subset["acc_start_local"] = pd.to_datetime(subset["accumulation_interval_start_utc"], utc=True).dt.tz_convert(tz)
    subset["acc_end_local"] = pd.to_datetime(subset["accumulation_interval_end_utc"], utc=True).dt.tz_convert(tz)
    instant_cols = [c for c in CORE_WEATHER_COLUMNS if c not in ("precipitation_mm", "solar_radiation_mj_m2")]
    rows = []
    for day_text in trading_dates:
        day = date.fromisoformat(day_text)
        day_start = datetime.combine(day, time.min, tz)
        day_end = day_start + timedelta(days=1)
        open_at = day_start.replace(hour=9)
        close_at = day_start.replace(hour=13, minute=30)
        bounds = {
            "pre_open": (day_start.replace(hour=7), open_at),
            "trading_session": (open_at, close_at),
            "full_day": (day_start, day_end),
        }
        for window, (w_start, w_end) in bounds.items():
            inst_mask = (subset["timestamp_local"] >= w_start) & (subset["timestamp_local"] < w_end)
            acc_mask = (subset["acc_start_local"] >= w_start) & (subset["acc_end_local"] <= w_end)
            inst = subset.loc[inst_mask]
            acc = subset.loc[acc_mask]
            row: dict[str, Any] = {
                "city_id": "taipei",
                "market_id": "taiex",
                "trading_date": day.isoformat(),
                "window": window,
            }
            for column in instant_cols:
                row[column] = inst[column].max() if column == "max_gust_mps" else inst[column].mean()
            row["precipitation_mm"] = acc["precipitation_mm"].sum(min_count=1)
            row["solar_radiation_mj_m2"] = acc["solar_radiation_mj_m2"].sum(min_count=1)
            rows.append(row)
    return pd.DataFrame(rows)


def compare_oracle(window_frame: pd.DataFrame, oracle: pd.DataFrame) -> tuple[bool, float]:
    merged = window_frame.merge(
        oracle,
        on=["city_id", "market_id", "trading_date", "window"],
        suffixes=("_builder", "_oracle"),
    )
    max_abs_diff = 0.0
    for column in CORE_WEATHER_COLUMNS:
        left = merged[f"{column}_builder"].astype(float).to_numpy()
        right = merged[f"{column}_oracle"].astype(float).to_numpy()
        if not np.allclose(left, right, rtol=1e-10, atol=1e-12):
            return False, float(np.max(np.abs(left - right)))
        if len(left):
            max_abs_diff = max(max_abs_diff, float(np.max(np.abs(left - right))))
    return True, max_abs_diff


def _code_hashes(root: Path) -> dict[str, Any]:
    files: dict[str, str] = {}
    for relative in WINDOW_BUILDER_CODE_FILES:
        path = root / relative
        files[relative] = sha256_file(path) if path.exists() else ""
    combined_payload = json.dumps({key: files[key] for key in WINDOW_BUILDER_CODE_FILES}, sort_keys=True)
    combined = sha256_bytes(combined_payload.encode("utf-8"))
    return {"combined": combined, "files": files}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build pilot weather windows with full audit binding.")
    parser.add_argument("--weather", type=Path, help="canonical hourly weather Parquet")
    parser.add_argument("--normalization-audit", type=Path, help="tracked normalization audit JSON")
    parser.add_argument("--calendar-audit", type=Path, help="tracked 2026 pilot calendar audit JSON")
    parser.add_argument("--market-id", default="taiex", help="market to build windows for")
    parser.add_argument("--output", type=Path, help="window Parquet output (local-only)")
    parser.add_argument("--audit-output", type=Path, help="window acceptance audit output (tracked)")
    parser.add_argument("--root", type=Path, default=repo_root())
    parser.add_argument("--calendar", type=Path, help="legacy CSV calendar entry (kept for compatibility)")
    args = parser.parse_args(argv)

    if args.weather is None and args.calendar is None:
        parser.error("require --weather (pilot) or --calendar (legacy)")
    root = args.root

    if args.calendar is not None:
        # Legacy path: CSV calendar, all registered markets (unchanged).
        calendar = pd.read_csv(args.calendar)
        markets = load_yaml(root / "config" / "v2" / "markets.yaml")["markets"]
        frames = []
        for market in markets:
            dates = calendar.loc[calendar["market_id"] == market["market_id"], "trading_date"]
            frames.append(build_weather_windows(pd.read_parquet(args.weather), market, dates))
        output = args.output or root / "data" / "canonical" / "v2" / "weather" / "windows-legacy.parquet"
        output.parent.mkdir(parents=True, exist_ok=True)
        pd.concat(frames, ignore_index=True).to_parquet(output, index=False)
        print(output)
        return 0

    # Pilot path with full evidence-chain binding.
    weather = (root / args.weather) if not args.weather.is_absolute() else args.weather
    normalization_path = (root / args.normalization_audit) if not args.normalization_audit.is_absolute() else args.normalization_audit
    calendar_audit_path = (root / args.calendar_audit) if not args.calendar_audit.is_absolute() else args.calendar_audit
    normalization = load_json(normalization_path)
    raw_rel = normalization.get("raw_acceptance_audit_path")
    if not raw_rel:
        raise SystemExit("normalization audit missing raw_acceptance_audit_path")
    raw_audit_path = root / raw_rel
    normalization, raw_audit, calendar, canonical_sha, trading_dates = verify_evidence_chain(
        weather, normalization_path, raw_audit_path, calendar_audit_path
    )

    markets = {row["market_id"]: row for row in load_yaml(root / "config" / "v2" / "markets.yaml")["markets"]}
    if args.market_id not in markets:
        raise SystemExit(f"market {args.market_id} not registered")
    market = markets[args.market_id]
    if market["city_id"] != "taipei":
        raise SystemExit(f"market {args.market_id} not linked to taipei")

    hourly = pd.read_parquet(weather)
    overrides = calendar_overrides_for(trading_dates)
    window_frame = build_weather_windows(hourly, market, trading_dates, calendar_overrides=overrides)
    validation = validate_window_frame(window_frame, trading_dates)
    oracle = oracle_windows(hourly, trading_dates)
    oracle_match, oracle_max_diff = compare_oracle(window_frame, oracle)
    if not oracle_match:
        raise SystemExit(f"independent oracle mismatch (max abs diff {oracle_max_diff})")

    output = args.output
    if not output.is_absolute():
        output = (root / output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    window_frame.to_parquet(output, index=False)
    window_sha = sha256_file(output)
    # Reproducibility: rebuild once and compare.
    rebuilt = build_weather_windows(hourly, market, trading_dates, calendar_overrides=overrides)
    rebuilt.to_parquet(output, index=False)
    rebuilt_sha = sha256_file(output)
    semantic_match = window_frame.sort_values(["trading_date", "window"]).reset_index(drop=True).equals(
        rebuilt.sort_values(["trading_date", "window"]).reset_index(drop=True)
    )
    byte_match = window_sha == rebuilt_sha
    final_sha = rebuilt_sha

    if args.audit_output is not None:
        semantics_path = root / "config" / "v2" / "weather-variable-semantics.yaml"
        code = _code_hashes(root)
        per_row = []
        for _, row in window_frame.sort_values(["trading_date", "window"]).iterrows():
            per_row.append(
                {
                    "trading_date": row["trading_date"],
                    "window": row["window"],
                    "instantaneous_expected_count": int(row["instantaneous_expected_count"]),
                    "instantaneous_observed_count": int(row["instantaneous_observed_count"]),
                    "accumulation_expected_count": int(row["accumulation_expected_count"]),
                    "accumulation_observed_count": int(row["accumulation_observed_count"]),
                    "instantaneous_coverage_ratio": row["instantaneous_coverage_ratio"],
                    "accumulation_coverage_ratio": row["accumulation_coverage_ratio"],
                    "quality_flags": list(row["quality_flags"]),
                    "weather_values": {column: float(row[column]) for column in CORE_WEATHER_COLUMNS},
                }
            )
        audit = {
            "schema_version": "2.0.0",
            "audit_type": "taipei_era5_window_acceptance",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "city_id": "taipei",
            "market_id": "taiex",
            "timezone": "Asia/Taipei",
            "input_canonical_path": weather.relative_to(root).as_posix(),
            "input_canonical_sha256": canonical_sha,
            "input_normalization_audit_path": normalization_path.relative_to(root).as_posix(),
            "input_normalization_audit_sha256": sha256_file(normalization_path),
            "input_raw_acceptance_audit_path": raw_audit_path.relative_to(root).as_posix(),
            "input_raw_acceptance_audit_sha256": sha256_file(raw_audit_path),
            "calendar_audit_path": calendar_audit_path.relative_to(root).as_posix(),
            "calendar_audit_sha256": sha256_file(calendar_audit_path),
            "calendar_pilot_status": calendar.get("pilot_calendar_status"),
            "full_history_calendar_verified": False,
            "weather_semantics_path": "config/v2/weather-variable-semantics.yaml",
            "weather_semantics_sha256": sha256_file(semantics_path),
            "window_builder_code_files": code["files"],
            "window_builder_code_sha256": code["combined"],
            "selected_trading_dates": trading_dates,
            "window_row_count": validation["row_count"],
            "unique_key_count": validation["unique_key_count"],
            "expected_count_contract": EXPECTED_COUNT_CONTRACT,
            "all_count_contracts_match": True,
            "all_coverage_ratios_one": True,
            "all_oracle_matches": oracle_match,
            "oracle_numeric_max_abs_diff": oracle_max_diff,
            "partial_interval_policy": PARTIAL_INTERVAL_POLICY,
            "rows": per_row,
            "window_parquet_path": output.relative_to(root).as_posix(),
            "window_parquet_sha256": final_sha,
            "semantic_reproducibility": semantic_match,
            "byte_reproducibility": byte_match,
            "weather_window_status": "accepted",
            "panel_built": False,
            "historical_backfill_run": False,
        }
        write_json(root / args.audit_output, audit)
    print(
        json.dumps(
            {
                "window_output": output.relative_to(root).as_posix(),
                "window_parquet_sha256": final_sha,
                "row_count": validation["row_count"],
                "all_oracle_matches": oracle_match,
                "semantic_reproducibility": semantic_match,
                "byte_reproducibility": byte_match,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
