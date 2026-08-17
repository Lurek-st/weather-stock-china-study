"""Build the audited five-row Taipei-TAIEX end-to-end pilot panel.

Pipeline integration acceptance (NOT a research-conclusion stage):

    weather windows (15 rows, tracked audit)
        -> pivot to 5 weather-wide rows (27 fields, no nulls)
    market canonical (5 rows, tracked audit)
        -> strict one-to-one join on (city_id, market_id, trading_date)
        -> 5-row provisional panel (local-only parquet)
        -> tracked panel acceptance audit

No statistics.  No sample-derived temperature anomaly/z features (the pilot
window is only five same-month days; a frozen climatology baseline is
required before any anomaly feature may be generated).  The independent
end-to-end oracle is rebuilt from the two already-tracked acceptance audits
(window + market) and never reuses build_panel() itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import (
    CORE_WEATHER_COLUMNS,
    SCHEMA_VERSION,
    V2Error,
    build_panel,
    load_json,
    load_yaml,
    repo_root,
    sha256_file,
    write_json,
)

MARKET_ID = "taiex"
CITY_ID = "taipei"
PILOT_DATES = [
    "2026-03-02",
    "2026-03-03",
    "2026-03-04",
    "2026-03-05",
    "2026-03-06",
]
WINDOWS = ("pre_open", "trading_session", "full_day")
WINDOW_PARQUET = (
    "data/canonical/v2/weather/taipei-era5-final-pilot-windows-20260302-20260306.parquet"
)
WINDOW_AUDIT = "data/audits/v2/weather-pilot/taipei-era5-window-acceptance.json"
MARKET_ACCEPTANCE_AUDIT = (
    "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json"
)
MARKET_CANONICAL_PATH = (
    "data/canonical/v2/market/taiex-final-pilot-20260302-20260306.parquet"
)
MARKET_CANONICAL_AUDIT = (
    "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-canonical.json"
)
CALENDAR_AUDIT = "data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json"
PANEL_OUTPUT = "data/panel/v2/provisional/taipei-taiex-pilot-20260302-20260306.parquet"
PANEL_AUDIT_OUTPUT = (
    "data/audits/v2/panel-pilot/taipei-taiex-2026-pilot-panel-acceptance.json"
)
ORACLE_RTOL = 1e-10
ORACLE_ATOL = 1e-12


def _weather_wide_from_oracle(window_audit: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Independent weather oracle: trading_date -> {window_variable: value}.

    Built exclusively from the tracked window acceptance audit rows; never
    reuses build_panel() or any pivot helper.
    """
    wide: dict[str, dict[str, float]] = {}
    for row in window_audit["rows"]:
        day = str(row["trading_date"])
        window = row["window"]
        for variable, value in row["weather_values"].items():
            wide.setdefault(day, {})[f"{window}_{variable}"] = float(value)
    return wide


def _market_oracle_from_acceptance(market_audit: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Independent market oracle: trading_date -> expected market fields.

    Built from the tracked market-only acceptance audit cross_validation
    daily closes plus previous_close_check and return_recalculation.
    """
    cross = market_audit["cross_validation"]
    daily_close = {
        item["date"]: float(item["close"]) for item in cross["daily"] if item["close"] is not None
    }
    previous = {
        item["date"]: float(item["expected_previous_close"])
        for item in cross["previous_close_check"]
    }
    returns = {
        item["date"]: float(item["recalculated"]) for item in cross["return_recalculation"]
    }
    oracle: dict[str, dict[str, float]] = {}
    for day in PILOT_DATES:
        oracle[day] = {
            "official_close": daily_close[day],
            "previous_official_close": previous[day],
            "close_to_close_return_pct": returns[day],
        }
    return oracle


def _compare_close(actual: float, expected: float, label: str, day: str) -> float:
    diff = abs(float(actual) - expected)
    tolerance = ORACLE_ATOL + ORACLE_RTOL * abs(expected)
    if diff > tolerance:
        raise V2Error(f"{label} oracle mismatch for {day}: {actual} vs {expected}")
    return diff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build audited five-row Taipei-TAIEX end-to-end pilot panel"
    )
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    # ---------- 1. weather-side evidence gate ----------
    window_path = root / WINDOW_PARQUET
    window_audit_path = root / WINDOW_AUDIT
    if not window_path.is_file():
        raise V2Error(f"window parquet missing: {window_path}")
    if not window_audit_path.is_file():
        raise V2Error(f"window audit missing: {window_audit_path}")
    window_audit = load_json(window_audit_path)
    actual_window_sha = sha256_file(window_path)
    if actual_window_sha != window_audit["window_parquet_sha256"]:
        raise V2Error("window parquet sha256 does not match tracked window audit")
    if window_audit.get("weather_window_status") != "accepted":
        raise V2Error("window audit status is not accepted")
    if window_audit.get("window_row_count") != 15 or window_audit.get("unique_key_count") != 15:
        raise V2Error("window audit row/unique counts must be 15")
    if not window_audit.get("all_count_contracts_match"):
        raise V2Error("window audit count contracts must match")
    if not window_audit.get("all_coverage_ratios_one"):
        raise V2Error("window audit coverage ratios must be one")
    if not window_audit.get("all_oracle_matches"):
        raise V2Error("window audit oracle matches must be true")
    if window_audit.get("panel_built") is not False:
        raise V2Error("window audit panel_built must be false")
    if window_audit.get("historical_backfill_run") is not False:
        raise V2Error("window audit historical_backfill_run must be false")
    if window_audit.get("selected_trading_dates") != PILOT_DATES:
        raise V2Error("window audit selected_trading_dates mismatch")

    windows = pd.read_parquet(window_path)
    if len(windows) != 15:
        raise V2Error(f"window parquet row_count {len(windows)} != 15")

    # ---------- 2. market-side tracked acceptance gate ----------
    market_acceptance_path = root / MARKET_ACCEPTANCE_AUDIT
    if not market_acceptance_path.is_file():
        raise V2Error(f"market acceptance audit missing: {market_acceptance_path}")
    market_acceptance = load_json(market_acceptance_path)
    if market_acceptance.get("adapter_status") != "market_only_pilot_accepted":
        raise V2Error("market acceptance adapter_status mismatch")
    if market_acceptance.get("pilot_ok") is not True:
        raise V2Error("market acceptance pilot_ok mismatch")
    if market_acceptance["cross_validation"]["pilot_week_record_count"] != 5:
        raise V2Error("market acceptance pilot_week_record_count != 5")
    if not market_acceptance["cross_validation"]["previous_close_match"]:
        raise V2Error("market acceptance previous_close_match mismatch")
    if not market_acceptance["cross_validation"]["return_recalculation_match"]:
        raise V2Error("market acceptance return_recalculation_match mismatch")
    if not market_acceptance["cross_validation"]["ohlc_validation_passed"]:
        raise V2Error("market acceptance ohlc_validation_passed mismatch")
    if market_acceptance["cross_validation"]["calendar_match_rate"] != 1.0:
        raise V2Error("market acceptance calendar_match_rate mismatch")
    for key in ("duplicate_dates", "missing_market_dates", "unexpected_market_dates", "unresolved_dates", "issues"):
        if market_acceptance["cross_validation"].get(key):
            raise V2Error(f"market acceptance {key} not empty")
    if market_acceptance.get("production_status") != "not_connected":
        raise V2Error("market acceptance production_status mismatch")

    # ---------- 3. market canonical gate ----------
    market_canonical_path = root / MARKET_CANONICAL_PATH
    market_canonical_audit_path = root / MARKET_CANONICAL_AUDIT
    if not market_canonical_path.is_file():
        raise V2Error(f"market canonical missing: {market_canonical_path}")
    if not market_canonical_audit_path.is_file():
        raise V2Error(f"market canonical audit missing: {market_canonical_audit_path}")
    market_canonical_audit = load_json(market_canonical_audit_path)
    actual_canonical_sha = sha256_file(market_canonical_path)
    if actual_canonical_sha != market_canonical_audit["market_canonical_sha256"]:
        raise V2Error("market canonical sha256 does not match tracked market canonical audit")
    if market_canonical_audit.get("market_canonical_status") != "accepted":
        raise V2Error("market canonical audit status mismatch")

    market = pd.read_parquet(market_canonical_path)
    if len(market) != 5:
        raise V2Error(f"market canonical row_count {len(market)} != 5")
    if market[["market_id", "trading_date"]].duplicated().any():
        raise V2Error("market canonical duplicate key")

    # ---------- 4. strict weather-wide pivot (15 -> 5 rows) ----------
    index = ["city_id", "market_id", "trading_date"]
    window_key = index + ["window"]
    if windows[window_key].duplicated().any():
        raise V2Error("duplicate weather window key in parquet")
    window_counts = windows.groupby(index)["window"].apply(lambda values: set(values)).to_dict()
    for day in PILOT_DATES:
        day_windows = window_counts.get((CITY_ID, MARKET_ID, day), set())
        if day_windows != set(WINDOWS):
            raise V2Error(f"missing/invalid windows for {day}: {sorted(day_windows)}")
    wide_pieces = []
    for window in WINDOWS:
        piece = windows.loc[windows["window"] == window, index + CORE_WEATHER_COLUMNS].copy()
        piece = piece.rename(
            columns={column: f"{window}_{column}" for column in CORE_WEATHER_COLUMNS}
        )
        wide_pieces.append(piece)
    weather_wide = wide_pieces[0]
    for piece in wide_pieces[1:]:
        weather_wide = weather_wide.merge(piece, on=index, how="outer", validate="one_to_one")
    weather_fields = [
        f"{window}_{column}" for window in WINDOWS for column in CORE_WEATHER_COLUMNS
    ]
    if len(weather_wide) != 5:
        raise V2Error(f"weather-wide row_count {len(weather_wide)} != 5")
    if weather_wide[index].duplicated().any():
        raise V2Error("weather-wide duplicate key")
    if weather_wide[weather_fields].isna().any().any():
        raise V2Error("weather-wide contains null weather fields")
    weather_wide = weather_wide.sort_values("trading_date").reset_index(drop=True)

    # ---------- 5. strict one-to-one panel join ----------
    market_keys = set(zip(market["market_id"], market["trading_date"]))
    weather_keys = set(zip(weather_wide["market_id"], weather_wide["trading_date"]))
    if market_keys != weather_keys:
        raise V2Error(
            "market and weather key sets must match exactly; "
            f"only_market={sorted(market_keys - weather_keys)} "
            f"only_weather={sorted(weather_keys - market_keys)}"
        )
    locations = load_yaml(root / "config" / "v2" / "locations.yaml")
    panel = build_panel(
        market,
        windows,
        locations,
        "provisional",
        duplicate_policy="error",
        include_sample_temperature_features=False,
    )
    panel = panel.sort_values("trading_date").reset_index(drop=True)
    if len(panel) != 5:
        raise V2Error(f"panel row_count {len(panel)} != 5")
    if panel[["city_id", "market_id", "trading_date"]].duplicated().any():
        raise V2Error("panel duplicate key")
    if "pre_open_temperature_anomaly_c" in panel.columns or "pre_open_temperature_z" in panel.columns:
        raise V2Error("sample temperature features must not be generated for the pilot")
    for field in weather_fields:
        if not pd.to_numeric(panel[field], errors="coerce").notna().all():
            raise V2Error(f"panel weather field {field} contains non-finite values")
    if not pd.to_numeric(panel["close_to_close_return_pct"], errors="coerce").apply(
        lambda value: value == value and abs(value) != float("inf")
    ).all():
        raise V2Error("panel market returns must be finite")

    # ---------- 6. independent end-to-end oracle ----------
    weather_oracle = _weather_wide_from_oracle(window_audit)
    market_oracle = _market_oracle_from_acceptance(market_acceptance)
    max_weather_abs_diff = 0.0
    max_market_abs_diff = 0.0
    for _, row in panel.iterrows():
        day = str(row["trading_date"])
        if day not in weather_oracle or day not in market_oracle:
            raise V2Error(f"panel row {day} missing from oracle")
        for variable, expected in weather_oracle[day].items():
            max_weather_abs_diff = max(
                max_weather_abs_diff,
                _compare_close(row[variable], expected, variable, day),
            )
        for field, expected in market_oracle[day].items():
            max_market_abs_diff = max(
                max_market_abs_diff,
                _compare_close(row[field], expected, field, day),
            )
    all_oracle_matches = True

    # ---------- 7. panel output (local-only) + re-read acceptance ----------
    panel_path = root / PANEL_OUTPUT
    panel_path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(panel_path, index=False)
    panel_sha = sha256_file(panel_path)
    reread = pd.read_parquet(panel_path)
    if len(reread) != 5:
        raise V2Error("re-read panel row_count != 5")
    if reread[["city_id", "market_id", "trading_date"]].duplicated().any():
        raise V2Error("re-read panel duplicate key")
    if list(reread["trading_date"]) != PILOT_DATES:
        raise V2Error(f"re-read panel dates not ascending pilot dates: {list(reread['trading_date'])}")

    # ---------- 8. reproducibility (rebuild once) ----------
    rebuild = build_panel(
        market,
        windows,
        locations,
        "provisional",
        duplicate_policy="error",
        include_sample_temperature_features=False,
    ).sort_values("trading_date").reset_index(drop=True)
    semantic_reproducibility = bool(panel.equals(rebuild))
    byte_reproducibility = False
    if semantic_reproducibility:
        probe_path = root / ".local" / "runs" / "panel-pilot" / "rebuild-probe.parquet"
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        rebuild.to_parquet(probe_path, index=False)
        byte_reproducibility = sha256_file(probe_path) == panel_sha

    # ---------- 9. tracked panel acceptance audit ----------
    # Builder code is tracked in the repository; hash against repo_root so
    # the audit remains valid even when --root points at a tmp/offline root.
    builder_files = {
        "scripts/v2/build_taiex_pilot_market.py": sha256_file(repo_root() / "scripts/v2/build_taiex_pilot_market.py"),
        "scripts/v2/build_taiex_pilot_panel.py": sha256_file(repo_root() / "scripts/v2/build_taiex_pilot_panel.py"),
        "scripts/v2/core.py": sha256_file(repo_root() / "scripts/v2/core.py"),
    }
    panel_rows = [
        {
            "trading_date": str(row["trading_date"]),
            "official_close": float(row["official_close"]),
            "previous_official_close": float(row["previous_official_close"]),
            "close_to_close_return_pct": float(row["close_to_close_return_pct"]),
        }
        for _, row in panel.iterrows()
    ]
    audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taipei_taiex_2026_pilot_panel_acceptance",
        "city_id": CITY_ID,
        "market_id": MARKET_ID,
        "pilot_dates": list(PILOT_DATES),
        "weather_window_audit_path": WINDOW_AUDIT,
        "weather_window_audit_sha256": sha256_file(window_audit_path),
        "window_parquet_path": WINDOW_PARQUET,
        "window_parquet_sha256": actual_window_sha,
        "market_acceptance_audit_path": MARKET_ACCEPTANCE_AUDIT,
        "market_acceptance_audit_sha256": sha256_file(market_acceptance_path),
        "market_canonical_audit_path": MARKET_CANONICAL_AUDIT,
        "market_canonical_audit_sha256": sha256_file(market_canonical_audit_path),
        "market_canonical_path": MARKET_CANONICAL_PATH,
        "market_canonical_sha256": actual_canonical_sha,
        "calendar_audit_path": CALENDAR_AUDIT,
        "calendar_audit_sha256": sha256_file(root / CALENDAR_AUDIT),
        "panel_builder_code_files": builder_files,
        "panel_builder_code_sha256": sha256_file(repo_root() / "scripts/v2/build_taiex_pilot_panel.py"),
        "market_row_count": len(market),
        "weather_wide_row_count": len(weather_wide),
        "panel_row_count": len(panel),
        "unique_key_count": len(panel),
        "weather_window_field_count": len(weather_fields),
        "weather_null_count": int(weather_wide[weather_fields].isna().sum().sum()),
        "one_to_one_join": True,
        "key_sets_exact_match": True,
        "all_market_returns_recalculated": True,
        "all_end_to_end_oracle_matches": all_oracle_matches,
        "max_weather_abs_diff": max_weather_abs_diff,
        "max_market_abs_diff": max_market_abs_diff,
        "sample_temperature_features_generated": False,
        "climatology_baseline_status": "not_available_historical_backfill_blocked",
        "panel_scope": "pilot_only",
        "panel_tier": "provisional",
        "input_weather_data_class": "final_reanalysis",
        "market_value_status": "final",
        "pilot_calendar_verified": True,
        "panel_parquet_path": PANEL_OUTPUT,
        "panel_parquet_sha256": panel_sha,
        "semantic_reproducibility": semantic_reproducibility,
        "byte_reproducibility": byte_reproducibility,
        "statistics_run": False,
        "historical_backfill_run": False,
        "panel_status": "accepted",
        "research_ready": False,
        "frozen": False,
        "rows": panel_rows,
    }
    audit_path = root / PANEL_AUDIT_OUTPUT
    write_json(audit_path, audit)

    print(
        json.dumps(
            {
                "status": "accepted",
                "panel_parquet_path": PANEL_OUTPUT,
                "panel_parquet_sha256": panel_sha,
                "panel_row_count": len(panel),
                "unique_key_count": len(panel),
                "weather_wide_row_count": len(weather_wide),
                "weather_window_field_count": len(weather_fields),
                "weather_null_count": int(weather_wide[weather_fields].isna().sum().sum()),
                "all_end_to_end_oracle_matches": all_oracle_matches,
                "max_weather_abs_diff": max_weather_abs_diff,
                "max_market_abs_diff": max_market_abs_diff,
                "semantic_reproducibility": semantic_reproducibility,
                "byte_reproducibility": byte_reproducibility,
                "audit_path": PANEL_AUDIT_OUTPUT,
                "audit_sha256": sha256_file(audit_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
