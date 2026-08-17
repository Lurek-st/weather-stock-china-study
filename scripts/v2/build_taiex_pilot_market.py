"""Build an audited local-only five-row TAIEX pilot market canonical.

Evidence chain (append-only, no network):

    .local/source-raw/taiex/twse_taiex_official/{YYYYMMDD}/rNNNN-*.json
        -> parser (_rows) with an explicit pre-dedup duplicate check
        -> previous-close enrichment from actual prior trading records
        -> canonical five pilot rows
        -> data/canonical/v2/market/taiex-final-pilot-20260302-20260306.parquet
           (local-only; never committed)
        -> tracked audit data/audits/v2/taiex-adapter-acceptance/
           taiex-2026-market-canonical.json

No TWSE network call, no CSV, no refresh.  The five official closes and the
close-to-close returns are independently recalculated and cross-checked
against the already-tracked market-only acceptance audit (oracle).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import (
    SCHEMA_VERSION,
    V2Error,
    load_json,
    repo_root,
    sha256_file,
    write_json,
)
from scripts.v2.probes.probe_taiex import _rows

MARKET_ID = "taiex"
CITY_ID = "taipei"
CURRENCY = "TWD"
SOURCE_ID = "twse_taiex_official"
CALENDAR_SOURCE_ID = "twse_official_holiday_schedule"
PILOT_DATES = [
    "2026-03-02",
    "2026-03-03",
    "2026-03-04",
    "2026-03-05",
    "2026-03-06",
]
RAW_DIR = ".local/source-raw/taiex/twse_taiex_official"
MARKET_ACCEPTANCE_AUDIT = (
    "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json"
)
CALENDAR_AUDIT = "data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json"
CANONICAL_OUTPUT = (
    "data/canonical/v2/market/taiex-final-pilot-20260302-20260306.parquet"
)
CANONICAL_AUDIT_OUTPUT = (
    "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-canonical.json"
)
VALID_SOURCE_MONTHS = ("20260201", "20260301")
RTOL = 1e-12
ATOL = 1e-12


def _load_manifests(raw_dir: Path) -> list[dict[str, Any]]:
    """Load raw manifests for the two validated 2026 pilot request months."""
    manifests: list[dict[str, Any]] = []
    for month in VALID_SOURCE_MONTHS:
        month_dir = raw_dir / month
        if not month_dir.is_dir():
            raise V2Error(f"raw artifact directory missing: {month_dir}")
        found = sorted(month_dir.glob("*.manifest.json"))
        if not found:
            raise V2Error(f"raw manifest missing for month {month}")
        for manifest_path in found:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["request"]["params"]["date"] != month:
                raise V2Error(
                    f"manifest request month {manifest['request']['params']['date']} "
                    f"does not match directory {month}"
                )
            manifests.append(manifest)
    return manifests


def _verify_artifact(raw_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate a frozen raw artifact: existence, sha256, length, revision."""
    artifact_id = manifest["artifact_id"]
    month = manifest["request"]["params"]["date"]
    file_name = artifact_id.split(":")[-1] + ".json"
    path = raw_dir / month / file_name
    if not path.is_file():
        raise V2Error(f"raw artifact file missing: {path}")
    actual_sha = sha256_file(path)
    if actual_sha != manifest["sha256"]:
        raise V2Error(f"raw artifact sha256 mismatch for {artifact_id}")
    actual_length = path.stat().st_size
    if actual_length != manifest["content_length"]:
        raise V2Error(
            f"raw artifact content_length mismatch for {artifact_id}: "
            f"{actual_length} != {manifest['content_length']}"
        )
    revision = manifest.get("revision")
    if not isinstance(revision, int) or revision < 1:
        raise V2Error(f"raw artifact revision invalid for {artifact_id}: {revision}")
    return {
        "artifact_id": artifact_id,
        "revision": revision,
        "sha256": manifest["sha256"],
        "content_length": manifest["content_length"],
        "source_month": month,
        "retrieved_at": manifest.get("retrieved_at"),
    }


def _parse_rows_with_duplicate_check(
    raw_dir: Path, manifests: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Parse every raw JSON with _rows() and reject duplicate trading dates.

    Returns (all_rows, {trading_date: artifact_meta}).  Duplicates are
    rejected BEFORE any de-duplication: no {date: row} silent collapsing.
    """
    all_rows: list[dict[str, Any]] = []
    date_artifact: dict[str, dict[str, Any]] = {}
    for manifest in sorted(manifests, key=lambda item: item["artifact_id"]):
        meta = _verify_artifact(raw_dir, manifest)
        month = meta["source_month"]
        path = raw_dir / month / (manifest["artifact_id"].split(":")[-1] + ".json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = _rows(payload)
        for row in rows:
            trading_date = str(row["trading_date"])
            if trading_date in date_artifact:
                raise V2Error(
                    f"duplicate raw trading_date {trading_date} across artifacts"
                )
            date_artifact[trading_date] = meta
            all_rows.append(row)
    counts = Counter(str(row["trading_date"]) for row in all_rows)
    duplicates = {key: value for key, value in counts.items() if value > 1}
    if duplicates:
        raise V2Error(f"duplicate trading_date found in raw rows: {duplicates}")
    return all_rows, date_artifact


def _enrich_previous_close(
    rows: list[dict[str, Any]], date_artifact: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Derive previous-close fields from actual prior trading records only."""
    ordered = sorted(rows, key=lambda row: str(row["trading_date"]))
    enriched: list[dict[str, Any]] = []
    previous_close: float | None = None
    previous_date: str | None = None
    for row in ordered:
        item = dict(row)
        close = float(item["close"])
        item["official_close"] = close
        item["previous_official_close"] = previous_close
        item["previous_close_source_date"] = previous_date
        item["previous_close_raw_artifact_id"] = (
            date_artifact[previous_date]["artifact_id"] if previous_date else None
        )
        item["raw_artifact_id"] = date_artifact[str(row["trading_date"])]["artifact_id"]
        item["raw_revision"] = date_artifact[str(row["trading_date"])]["revision"]
        item["source_timestamp"] = date_artifact[str(row["trading_date"])]["retrieved_at"]
        item["return_pct"] = (
            None if previous_close is None else round((close / previous_close - 1.0) * 100.0, 10)
        )
        enriched.append(item)
        previous_close = close
        previous_date = str(row["trading_date"])
    return enriched


def _build_canonical_frame(enriched: list[dict[str, Any]]) -> pd.DataFrame:
    selected = [row for row in enriched if str(row["trading_date"]) in PILOT_DATES]
    if len(selected) != len(PILOT_DATES):
        raise V2Error(
            f"pilot dates missing from enriched rows: "
            f"{sorted(set(PILOT_DATES) - {str(row['trading_date']) for row in selected})}"
        )
    rows = []
    for row in selected:
        rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "market_id": MARKET_ID,
                "trading_date": str(row["trading_date"]),
                "official_close": float(row["close"]),
                "previous_official_close": row["previous_official_close"],
                "close_to_close_return_pct": row["return_pct"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "source_timestamp": row["source_timestamp"],
                "value_status": "final",
                "calendar_status": "verified",
                "calendar_source_id": CALENDAR_SOURCE_ID,
                "source_id": SOURCE_ID,
                "revision": row["raw_revision"],
                "is_final": True,
                "currency": CURRENCY,
                "quality_flags": [],
                "raw_artifact_id": row["raw_artifact_id"],
                "raw_revision": row["raw_revision"],
                "previous_close_source_date": row["previous_close_source_date"],
                "previous_close_raw_artifact_id": row["previous_close_raw_artifact_id"],
            }
        )
    frame = pd.DataFrame(rows).sort_values("trading_date").reset_index(drop=True)
    return frame


def _recalculate_returns(frame: pd.DataFrame) -> pd.DataFrame:
    recomputed = (
        frame["official_close"] / frame["previous_official_close"] - 1.0
    ) * 100.0
    recomputed = recomputed.round(10)
    expected = frame["close_to_close_return_pct"].astype(float)
    if not recomputed.equals(expected):
        raise V2Error("canonical returns do not match independent recalculation")
    return frame


def _cross_check_acceptance(
    frame: pd.DataFrame, acceptance: dict[str, Any]
) -> dict[str, Any]:
    cross = acceptance["cross_validation"]
    expected_close = {
        item["date"]: float(item["close"]) for item in cross["daily"] if item["close"] is not None
    }
    prev_check = {
        item["date"]: item["expected_previous_close"] for item in cross["previous_close_check"]
    }
    return_check = {
        item["date"]: item["recalculated"] for item in cross["return_recalculation"]
    }
    previous_match = True
    return_match = True
    ohlc_match = True
    daily_rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        day = str(row["trading_date"])
        close_ok = abs(float(row["official_close"]) - expected_close[day]) <= ATOL
        prev_ok = abs(float(row["previous_official_close"]) - float(prev_check[day])) <= ATOL
        ret_ok = abs(float(row["close_to_close_return_pct"]) - float(return_check[day])) <= ATOL
        ohlc_ok = (
            float(row["low"]) <= float(row["open"]) <= float(row["high"])
            and float(row["low"]) <= float(row["official_close"]) <= float(row["high"])
        )
        previous_match = previous_match and prev_ok
        return_match = return_match and ret_ok
        ohlc_match = ohlc_match and ohlc_ok
        if not (close_ok and prev_ok and ret_ok and ohlc_ok):
            raise V2Error(f"acceptance cross-check failed for {day}")
        daily_rows.append(
            {
                "trading_date": day,
                "official_close": float(row["official_close"]),
                "previous_official_close": float(row["previous_official_close"]),
                "close_to_close_return_pct": float(row["close_to_close_return_pct"]),
                "previous_close_source_date": row["previous_close_source_date"],
                "raw_artifact_id": row["raw_artifact_id"],
                "previous_close_raw_artifact_id": row["previous_close_raw_artifact_id"],
            }
        )
    return {
        "previous_close_match": previous_match,
        "return_recalculation_match": return_match,
        "ohlc_validation_passed": ohlc_match,
        "daily": daily_rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build audited local-only five-row TAIEX pilot market canonical"
    )
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args(argv)
    root = args.root

    raw_dir = root / RAW_DIR
    acceptance_path = root / MARKET_ACCEPTANCE_AUDIT
    calendar_path = root / CALENDAR_AUDIT
    if not acceptance_path.is_file():
        raise V2Error(f"market acceptance audit missing: {acceptance_path}")
    if not calendar_path.is_file():
        raise V2Error(f"calendar audit missing: {calendar_path}")

    acceptance = load_json(acceptance_path)
    if acceptance.get("adapter_status") != "market_only_pilot_accepted":
        raise V2Error("market acceptance audit not accepted")
    if acceptance.get("pilot_ok") is not True:
        raise V2Error("market acceptance pilot_ok is not true")
    if acceptance["cross_validation"]["pilot_week_record_count"] != 5:
        raise V2Error("market acceptance pilot_week_record_count != 5")
    if acceptance["cross_validation"]["duplicate_dates"]:
        raise V2Error(f"market acceptance has duplicate_dates: {acceptance['cross_validation']['duplicate_dates']}")
    if acceptance.get("production_status") != "not_connected":
        raise V2Error("market acceptance production_status != not_connected")
    if acceptance.get("repeatability", {}).get("classification") not in {
        "byte_identical",
        "semantic_identical",
    }:
        raise V2Error("market acceptance repeatability classification invalid")

    manifests = _load_manifests(raw_dir)
    raw_rows, date_artifact = _parse_rows_with_duplicate_check(raw_dir, manifests)
    enriched = _enrich_previous_close(raw_rows, date_artifact)
    frame = _build_canonical_frame(enriched)
    frame = _recalculate_returns(frame)

    # five official-close/return acceptance values are treated strictly as
    # acceptance oracles; they are NOT data inputs.  The canonical frame is
    # derived only from local raw; the tracked market-only acceptance audit
    # (independent of this builder) provides the authoritative expected map.
    cross = _cross_check_acceptance(frame, acceptance)

    # row-level acceptance
    if len(frame) != 5:
        raise V2Error(f"canonical row_count {len(frame)} != 5")
    if frame.duplicated(subset=["market_id", "trading_date"]).any():
        raise V2Error("canonical duplicate (market_id, trading_date)")
    if (frame["official_close"] <= 0).any():
        raise V2Error("canonical official_close must be positive")
    if frame["previous_official_close"].isna().any():
        raise V2Error("canonical previous_official_close must be non-null")
    if not pd.to_numeric(frame["close_to_close_return_pct"]).apply(lambda value: value == value and abs(value) != float("inf")).all():
        raise V2Error("canonical returns must all be finite")

    canonical_path = root / CANONICAL_OUTPUT
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(canonical_path, index=False)
    canonical_sha = sha256_file(canonical_path)

    raw_artifacts = [
        {
            "artifact_id": meta["artifact_id"],
            "revision": meta["revision"],
            "sha256": meta["sha256"],
            "content_length": meta["content_length"],
            "source_month": meta["source_month"],
        }
        for meta in sorted(
            {item["artifact_id"]: item for item in date_artifact.values()}.values(),
            key=lambda item: item["artifact_id"],
        )
    ]

    audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "audit_type": "taiex_2026_market_canonical",
        "market_id": MARKET_ID,
        "city_id": CITY_ID,
        "pilot_dates": list(PILOT_DATES),
        "market_acceptance_audit_path": MARKET_ACCEPTANCE_AUDIT,
        "market_acceptance_audit_sha256": sha256_file(acceptance_path),
        "calendar_audit_path": CALENDAR_AUDIT,
        "calendar_audit_sha256": sha256_file(calendar_path),
        "raw_artifacts": raw_artifacts,
        "market_canonical_path": CANONICAL_OUTPUT,
        "market_canonical_sha256": canonical_sha,
        "market_row_count": len(frame),
        "unique_key_count": int(frame[["market_id", "trading_date"]].drop_duplicates().shape[0]),
        "rows": cross["daily"],
        "return_recalculation_match": True,
        "previous_close_match": cross["previous_close_match"],
        "ohlc_validation_passed": cross["ohlc_validation_passed"],
        "source_timestamp_semantics": "raw_retrieved_at",
        "market_canonical_status": "accepted",
        "production_status": "not_connected",
        "historical_backfill_run": False,
    }
    audit_path = root / CANONICAL_AUDIT_OUTPUT
    write_json(audit_path, audit)

    print(
        json.dumps(
            {
                "status": "accepted",
                "market_canonical_path": CANONICAL_OUTPUT,
                "market_canonical_sha256": canonical_sha,
                "market_row_count": len(frame),
                "unique_key_count": len(frame),
                "audit_path": CANONICAL_AUDIT_OUTPUT,
                "audit_sha256": sha256_file(audit_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
