"""Stage 5B-1: New York session / DST canary audit builder (no network).

Builds the spring (2024-03-08/11) and fall (2024-11-01/04) DST canaries using
the generic session-time helper, runs a synthetic temporal-integration oracle,
and writes the evidence + canary audit.  Zero network: the official evidence
is recorded from the retrieved sources (retrieval date 2026-08-13); no ERA5 or
weather data is requested or downloaded.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import SCHEMA_VERSION, V2Error, repo_root, sha256_file, write_json
from scripts.v2.climatology.temporal_integration import piecewise_linear_mean
from scripts.v2.session_time import resolve_open_window

AUDIT_PATH = "data/audits/v2/time/new-york-dst-canary-2024.json"

MARKET_ID = "sp500"
CORE_OPEN_CLOCK = time(9, 30)
CORE_CLOSE_CLOCK = time(16, 0)
WINDOW_MINUTES = 120

# Official / historical evidence (retrieved 2026-08-13).
EVIDENCE = {
    "session_evidence": {
        "source_authority": "New York Stock Exchange (NYSE) official website",
        "source_url": "https://www.nyse.com/trade/hours-calendars",
        "retrieval_date": "2026-08-13",
        "core_open": "9:30 a.m. ET (Core Trading Session)",
        "core_close": "4:00 p.m. ET",
        "pre_opening_session": "6:30 a.m. ET (orders queued until 9:30 Core Open Auction)",
        "early_trading_session": "7:00 a.m. to 9:30 a.m. ET (Tapes B & C; NOT the core open)",
        "note": "Pre-Opening and Early Trading Session are pre-market sessions, not the primary core open.",
    },
    "historical_evidence": [
        {
            "source_authority": "U.S. SEC — Cboe Exchange proposed rule change (Release No. 34-93819)",
            "source_url": "https://www.sec.gov/rules/sro/cboe/2021/34-93819.pdf",
            "retrieval_date": "2026-08-13",
            "issue_year": 2021,
            "quote": "NYSE Rule 1.1 defines 'Core Trading Hours' as the hours between 9:30 a.m. through 4:00 p.m. ET and NYSE Rule 7.34 provides ... (2) the Core Trading Session, which ... begins at 9:30 a.m. and concludes at 4:00 p.m. ET.",
        },
        {
            "source_authority": "Federal Register / govinfo.gov — SEC Sunshine Act Meeting (2022-06383)",
            "source_url": "https://www.govinfo.gov/content/pkg/FR-2022-03-28/pdf/2022-06383.pdf",
            "retrieval_date": "2026-08-13",
            "issue_date": "2022-03-28",
            "quote": "NYSE Rule 1.1 defines 'Core Trading Hours' as the hours between 9:30 a.m. through 4:00 p.m. ET.",
        },
    ],
    "canary_trading_date_evidence": {
        "source_authority": "NYSE official FTP short-volume data",
        "source_url": "https://ftp.nyse.com/ShortData/NYSEshvol/NYSEshvol2024/NYSEshvol202403/",
        "retrieval_date": "2026-08-13",
        "2024_03_08_present": True,
        "2024_03_11_present": True,
        "2024_03_09_saturday_absent": True,
        "2024_03_10_sunday_absent": True,
        "note": "2024-03-08 and 2024-03-11 both have official NYSE short-volume files; the weekend (Mar 9/10) has none.",
    },
    "historical_scope_statement": "2020-2025 target horizon: no evidence found of a change from the 09:30 local core open; bounded check only (NOT a full 30-year session-history reconstruction).",
}


def _canary(day: str) -> dict[str, Any]:
    return resolve_open_window(date.fromisoformat(day), "America/New_York", CORE_OPEN_CLOCK, WINDOW_MINUTES, CORE_CLOSE_CLOCK)


def _oracle_constant():
    canary = _canary("2024-03-11")
    ts = [datetime.fromisoformat(t) for t in canary["hourly_support_timestamps"]]
    start = datetime.fromisoformat(canary["window_start_utc"])
    end = datetime.fromisoformat(canary["window_end_utc"])
    values = [0.4] * len(ts)
    return piecewise_linear_mean(ts, values, start, end), 0.4


def _oracle_linear():
    canary = _canary("2024-03-11")
    ts = [datetime.fromisoformat(t) for t in canary["hourly_support_timestamps"]]
    start = datetime.fromisoformat(canary["window_start_utc"])
    end = datetime.fromisoformat(canary["window_end_utc"])
    values = [0.0, 0.1, 0.2, 0.3]
    # independent hand computation: mean over [11:30,13:30) = 0.15
    return piecewise_linear_mean(ts, values, start, end), 0.15


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the New York DST canary audit")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    spring_before = _canary("2024-03-08")
    spring_after = _canary("2024-03-11")
    fall_before = _canary("2024-11-01")
    fall_after = _canary("2024-11-04")

    const_prod, const_oracle = _oracle_constant()
    lin_prod, lin_oracle = _oracle_linear()

    # invariants
    local_wall_clock_invariant = (
        spring_before["window_start_local"][11:19] == spring_after["window_start_local"][11:19]
        and spring_before["core_open_local_clock"] == spring_after["core_open_local_clock"]
    )
    utc_one_hour_shift = (
        spring_before["window_start_utc"].endswith("12:30:00+00:00")
        and spring_after["window_start_utc"].endswith("11:30:00+00:00")
    )
    oracle_pass = (
        abs(const_prod - const_oracle) < 1e-12
        and abs(lin_prod - lin_oracle) < 1e-12
    )

    audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "new_york_dst_canary",
        "market_id": MARKET_ID,
        "temporal_reference_exchange": "NYSE / U.S. core cash-equity trading session",
        "timezone": "America/New_York",
        "session_regime": {"core_open_local": "09:30", "core_close_local": "16:00"},
        "timezone_regime": "America/New_York (IANA); UTC offset derived per date by the engine",
        "session_regime_note": "DST is a timezone-regime property, never a new session regime",
        "primary_exposure_window": "[core_open - 120 minutes, core_open) = 07:30-09:30 local",
        "evidence": EVIDENCE,
        "historical_core_open_status": "nyse_2020_2025_core_open_evidence = provisionally_stable_09_30",
        "spring_canary": {
            "before_dst_2024_03_08": spring_before,
            "after_dst_2024_03_11": spring_after,
            "local_wall_clock_invariant": local_wall_clock_invariant,
            "utc_one_hour_shift": utc_one_hour_shift,
        },
        "fall_pure_function_regression": {
            "before_fallback_2024_11_01": fall_before,
            "after_fallback_2024_11_04": fall_after,
        },
        "temporal_integration_oracle": {
            "constant_field": {"production": const_prod, "oracle": const_oracle, "match": abs(const_prod - const_oracle) < 1e-12},
            "linear_field": {"production": lin_prod, "oracle": lin_oracle, "match": abs(lin_prod - lin_oracle) < 1e-12},
            "oracle_pass": oracle_pass,
        },
        "no_network_attestation": {
            "cds_requests": 0,
            "weather_raw_downloaded": 0,
            "era5_fetched": False,
            "note": "only required_hourly_support_timestamps constructed; no weather data fetched",
        },
        "gate": (
            spring_before["utc_offset_str"] == "-0500"
            and spring_after["utc_offset_str"] == "-0400"
            and fall_before["utc_offset_str"] == "-0400"
            and fall_after["utc_offset_str"] == "-0500"
            and local_wall_clock_invariant
            and utc_one_hour_shift
            and spring_before["hourly_support_count"] == 4
            and spring_after["hourly_support_count"] == 4
            and oracle_pass
        ),
        "code_sha256": {
            "session_time.py": sha256_file(root / "scripts/v2/session_time.py"),
            "temporal_integration.py": sha256_file(root / "scripts/v2/climatology/temporal_integration.py"),
            "build_new_york_dst_canary.py": sha256_file(root / "scripts/v2/time/build_new_york_dst_canary.py"),
        },
        "reproducibility": "deterministic (byte-identical across repeated builds; pure functions, no network)",
        "historical_backfill_run": False,
    }
    write_json(root / AUDIT_PATH, audit)
    print(json.dumps(
        {
            "gate": "PASS_STAGE5B1_NEW_YORK_DST_CANARY" if audit["gate"] else "REVISE_STAGE5B1_NEW_YORK_DST_CANARY",
            "spring_before_offset": spring_before["utc_offset_str"],
            "spring_after_offset": spring_after["utc_offset_str"],
            "spring_before_window_utc": [spring_before["window_start_utc"], spring_before["window_end_utc"]],
            "spring_after_window_utc": [spring_after["window_start_utc"], spring_after["window_end_utc"]],
            "support_before": [t[11:16] for t in spring_before["hourly_support_timestamps"]],
            "support_after": [t[11:16] for t in spring_after["hourly_support_timestamps"]],
            "oracle_pass": oracle_pass,
            "no_network": True,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if audit["gate"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
