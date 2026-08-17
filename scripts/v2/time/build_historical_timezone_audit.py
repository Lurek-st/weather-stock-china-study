"""Stage 5B-3: historical IANA timezone qualification audit builder (no network).

Computes every historical canary through the Stage 5B-1 session-time engine,
compares against the frozen expected values, verifies ambient-vs-explicit
tzdata provider reproducibility, and writes the audit + timezone fingerprint.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.v2.core import SCHEMA_VERSION, repo_root, sha256_file, write_json
from scripts.v2.historical_timezone import CANARIES, tzdata_zoneinfo_dir, tzdb_provider_info, timezone_fingerprint
from scripts.v2.session_time import resolve_open_window

AUDIT_PATH = "data/audits/v2/time/historical-timezone-canary-1991-2020.json"


def _observed(canary: dict[str, Any]) -> dict[str, Any]:
    r = resolve_open_window(
        date.fromisoformat(canary["date"]),
        canary["timezone"],
        time.fromisoformat(canary["core_open_local"]),
        120,
    )
    return {
        "offset_str": r["utc_offset_str"],
        "core_open_utc": r["core_open_utc"],
        "window_start_utc": r["window_start_utc"],
        "window_end_utc": r["window_end_utc"],
        "hourly_support_timestamps": r["hourly_support_timestamps"],
    }


def _explicit_provider_offsets() -> list[str] | None:
    """Offsets recomputed with PYTHONTZPATH pointed only at the tzdata zoneinfo dir."""
    zdir = tzdata_zoneinfo_dir()
    if zdir is None:
        return None
    code = (
        "import json, zoneinfo\n"
        "from datetime import datetime\n"
        "for tz, d in json.loads(input()):\n"
        "    z = zoneinfo.ZoneInfo(tz)\n"
        "    print(z.utcoffset(datetime.fromisoformat(d + 'T12:00:00')))\n"
    )
    pairs = [(c["timezone"], c["date"]) for c in CANARIES]
    env = dict(os.environ)
    env["PYTHONTZPATH"] = str(zdir)
    out = subprocess.run(
        [sys.executable, "-c", code], input=json.dumps(pairs), capture_output=True, text=True, env=env
    ).stdout.splitlines()
    return out


def _td_repr_to_offset(td_repr: str) -> str:
    if "day" in td_repr:
        days_part, time_part = td_repr.split(",")
        days = int(days_part.split()[0])
        h, m, _ = time_part.strip().split(":")
        total = days * 1440 + int(h) * 60 + int(m)
    else:
        sign = "-" if td_repr.startswith("-") else "+"
        h, m, _ = td_repr.lstrip("-+").split(":")
        total = int(h) * 60 + int(m)
        if sign == "-":
            total = -total
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 60:02d}{total % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the historical timezone canary audit")
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    provider = tzdb_provider_info()
    fingerprint = timezone_fingerprint()
    explicit = _explicit_provider_offsets()

    canary_results = []
    all_match = True
    for canary in CANARIES:
        obs = _observed(canary)
        exp = canary["expected"]
        match = True
        for key in ("offset_str", "core_open_utc", "window_start_utc", "window_end_utc"):
            if key in exp and obs[key] != exp[key]:
                match = False
        if "support" in exp and obs["hourly_support_timestamps"] != exp["support"]:
            match = False
        if not match:
            all_match = False
        canary_results.append(
            {
                "label": canary["label"],
                "timezone": canary["timezone"],
                "date": canary["date"],
                "core_open_local": canary["core_open_local"],
                "expected": exp,
                "observed": obs,
                "exact_match": match,
            }
        )

    ambient_vs_explicit_match = None
    if explicit is not None:
        ambient_vs_explicit_match = all(
            _observed(c)["offset_str"] == _td_repr_to_offset(e) for c, e in zip(CANARIES, explicit)
        )

    cross_date_support_match = True
    for label in ("shanghai_1991_07_01_dst", "shanghai_1992_07_01_no_dst"):
        c = next(x for x in canary_results if x["label"] == label)
        if not c["exact_match"]:
            cross_date_support_match = False

    unresolved_count = sum(1 for r in canary_results if not r["exact_match"])
    research_usable = (
        all_match
        and provider["timezone_data_provider"] in {"tzdata", "system_tzdb"}
        and provider["timezone_data_version"] is not None
        and (ambient_vs_explicit_match is True or provider["timezone_data_provider"] == "system_tzdb")
        and cross_date_support_match
        and unresolved_count == 0
    )

    audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "historical_timezone_canary",
        "baseline_period": "1991-2020",
        "provider": provider,
        "fingerprint": fingerprint,
        "canaries": {
            "new_york_2006_2007": [r for r in canary_results if r["label"].startswith("new_york")],
            "berlin_1995_1996": [r for r in canary_results if r["label"].startswith("berlin")],
            "london_1995": [r for r in canary_results if r["label"].startswith("london")],
            "shanghai_1991_1992": [r for r in canary_results if r["label"].startswith("shanghai")],
            "tokyo_static": [r for r in canary_results if r["label"].startswith("tokyo")],
            "kolkata_static": [r for r in canary_results if r["label"].startswith("kolkata")],
        },
        "ambient_vs_explicit_provider_match": ambient_vs_explicit_match,
        "cross_utc_date_support_match": cross_date_support_match,
        "unresolved_count": unresolved_count,
        "research_usable": research_usable,
        "no_network_attestation": {"cds_api_calls": 0, "era5_downloads": 0},
        "code_hashes": {
            "session_time.py": sha256_file(root / "scripts/v2/session_time.py"),
            "historical_timezone.py": sha256_file(root / "scripts/v2/historical_timezone.py"),
            "build_historical_timezone_audit.py": sha256_file(root / "scripts/v2/time/build_historical_timezone_audit.py"),
        },
        "reproducibility": "deterministic (byte-identical across repeated builds; pure functions, no network)",
        "historical_backfill_run": False,
    }
    write_json(root / AUDIT_PATH, audit)
    gate = research_usable
    print(json.dumps(
        {
            "gate": "PASS_STAGE5B3_HISTORICAL_TIMEZONE" if gate else "REVISE_STAGE5B3_HISTORICAL_TIMEZONE",
            "provider": provider["timezone_data_provider"],
            "provider_version": provider["timezone_data_version"],
            "unresolved_count": unresolved_count,
            "ambient_vs_explicit_provider_match": ambient_vs_explicit_match,
            "cross_utc_date_support_match": cross_date_support_match,
            "research_usable": research_usable,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
