"""Parse ChatGPT weekly packages and convert compact daily exports to canonical records."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker

try:
    from .scoring import market_score, weather_score
except ImportError:
    from scoring import market_score, weather_score

ROOT = Path(__file__).resolve().parents[1]
TRANSPORT_SCHEMA_PATH = ROOT / "schemas" / "daily-transport-v1.0.2.schema.json"
WEEKLY_MANIFEST_SCHEMA_PATH = ROOT / "schemas" / "weekly-manifest-v1.0.2.schema.json"
MARKETS_PATH = ROOT / "config" / "markets.json"

DAILY_BEGIN = "BEGIN_DAILY_WEATHER_MARKET_EXPORT"
DAILY_END = "END_DAILY_WEATHER_MARKET_EXPORT"
WEEKLY_BEGIN = "BEGIN_CODEX_WEEKLY_IMPORT_PACKAGE"
WEEKLY_END = "END_CODEX_WEEKLY_IMPORT_PACKAGE"


class PackageError(ValueError):
    pass


def extract_between(text: str, begin: str, end: str) -> list[str]:
    pattern = re.compile(re.escape(begin) + r"\s*(.*?)\s*" + re.escape(end), re.DOTALL)
    return [match.group(1).strip() for match in pattern.finditer(text)]


def extract_daily_blocks(text: str) -> list[str]:
    return extract_between(text, DAILY_BEGIN, DAILY_END)


def parse_daily_exports_tolerant(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse blocks independently so one damaged JSON block does not hide valid siblings."""
    exports: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for index, raw_block in enumerate(extract_daily_blocks(text), start=1):
        block = re.sub(r"^```(?:json)?\s*", "", raw_block, flags=re.IGNORECASE)
        block = re.sub(r"\s*```$", "", block)
        try:
            value = json.loads(block)
            if not isinstance(value, dict):
                raise PackageError("daily export must be a JSON object")
        except (json.JSONDecodeError, PackageError) as exc:
            failures.append({
                "block_index": index,
                "error": str(exc),
                "raw_block": raw_block,
            })
            continue
        exports.append(value)
    return exports, failures



def parse_weekly_envelope(text: str) -> dict[str, Any]:
    blocks = extract_between(text, WEEKLY_BEGIN, WEEKLY_END)
    if len(blocks) != 1:
        raise PackageError(f"expected exactly one weekly package envelope, found {len(blocks)}")
    body = blocks[0]
    def field(name: str) -> str:
        match = re.search(rf"^{re.escape(name)}:\s*(.+?)\s*$", body, re.MULTILINE)
        if not match:
            raise PackageError(f"missing weekly field {name}")
        return match.group(1).strip()
    version = field("PACKAGE_VERSION")
    if version != "1.0.2":
        raise PackageError(f"unsupported PACKAGE_VERSION {version}")
    status = field("STATUS")
    if status not in {"COMPLETE", "WEEKLY_AGGREGATION_INCOMPLETE"}:
        raise PackageError(f"invalid STATUS {status}")
    week_id = field("WEEK_ID")
    if not re.fullmatch(r"[0-9]{4}-W[0-9]{2}", week_id):
        raise PackageError(f"invalid WEEK_ID {week_id}")
    coverage_start = field("COVERAGE_START")
    coverage_end = field("COVERAGE_END")
    start = parse_date(coverage_start)
    end = parse_date(coverage_end)
    if start.weekday() != 0 or end != start + timedelta(days=6):
        raise PackageError("coverage must be one complete Monday-Sunday natural week")
    iso = start.isocalendar()
    expected_week_id = f"{iso.year}-W{iso.week:02d}"
    if week_id != expected_week_id or end.isocalendar()[:2] != iso[:2]:
        raise PackageError(
            f"WEEK_ID {week_id} does not match coverage week {expected_week_id}"
        )
    generated_at = field("GENERATED_AT")
    try:
        parsed_generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PackageError(f"invalid GENERATED_AT {generated_at}") from exc
    if parsed_generated.tzinfo is None:
        raise PackageError("GENERATED_AT must include a timezone")
    manifest_match = re.search(r"WEEKLY_MANIFEST_JSON:\s*\n(\{[^\n]*\})", body)
    if not manifest_match:
        raise PackageError("missing single-line WEEKLY_MANIFEST_JSON")
    try:
        manifest = json.loads(manifest_match.group(1))
    except json.JSONDecodeError as exc:
        raise PackageError(f"invalid WEEKLY_MANIFEST_JSON: {exc}") from exc
    schema = json.loads(WEEKLY_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [f"manifest {e.json_path}: {e.message}" for e in sorted(validator.iter_errors(manifest), key=lambda e: list(e.path))]
    if errors:
        raise PackageError("; ".join(errors))
    return {
        "package_version": version, "week_id": week_id,
        "coverage_start": coverage_start, "coverage_end": coverage_end,
        "generated_at": generated_at, "status": status, "manifest": manifest,
    }


def extract_daily_exports(text: str) -> list[dict[str, Any]]:
    blocks = extract_daily_blocks(text)
    exports, failures = parse_daily_exports_tolerant(text)
    if failures:
        failure = failures[0]
        raise PackageError(
            f"daily export {failure['block_index']} is invalid JSON: {failure['error']}"
        )
    if not blocks:
        raise PackageError("no daily export blocks found")
    return exports


def _record_ref_set(values: Iterable[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(str(value["region"]), str(value["target_date"])) for value in values}


def manifest_consistency_errors(
    envelope: dict[str, Any],
    exports: list[dict[str, Any]],
    block_count: int,
    malformed_count: int = 0,
) -> list[str]:
    """Validate manifest claims against the records that were actually parsed."""
    manifest = envelope["manifest"]
    errors: list[str] = []
    if block_count == 0:
        errors.append("weekly package contains no daily export machine blocks")
    if manifest["daily_export_count"] != block_count:
        errors.append(
            f"manifest daily_export_count={manifest['daily_export_count']} "
            f"but found {block_count} machine blocks"
        )

    rows = flatten_records(exports)
    actual_list = [
        (str(record.get("region")), str(record.get("target_date")))
        for _, record in rows
    ]
    actual = set(actual_list)
    duplicates = sorted(key for key in actual if actual_list.count(key) > 1)
    if duplicates:
        errors.append(f"duplicate region/date records in package: {duplicates}")

    expected = _record_ref_set(manifest["expected_records"])
    declared_found = _record_ref_set(manifest["found_records"])
    unresolved = _record_ref_set(manifest["unresolved_records"])
    backfilled = _record_ref_set(manifest["backfilled_records"])
    replaced = _record_ref_set(manifest["replaced_damaged_records"])
    duplicates_removed = _record_ref_set(manifest["duplicates_removed"])
    actual_backfilled = {
        (str(record.get("region")), str(record.get("target_date")))
        for _, record in rows
        if record.get("collection_mode") == "backfill"
    }

    if declared_found != actual:
        errors.append(
            "manifest found_records does not match parsed records: "
            f"declared={sorted(declared_found)} actual={sorted(actual)}"
        )
    if unresolved != expected - declared_found:
        errors.append(
            "manifest unresolved_records must equal expected_records - found_records"
        )
    if not declared_found <= expected:
        errors.append("manifest found_records contains records not present in expected_records")
    if backfilled != actual_backfilled:
        errors.append(
            "manifest backfilled_records does not match records with collection_mode=backfill"
        )
    if not replaced <= declared_found:
        errors.append("manifest replaced_damaged_records must be a subset of found_records")
    if not duplicates_removed <= expected:
        errors.append("manifest duplicates_removed must be a subset of expected_records")

    start = parse_date(envelope["coverage_start"])
    end = parse_date(envelope["coverage_end"])
    for region, target_date in sorted(expected | actual):
        try:
            parsed = parse_date(target_date)
        except ValueError:
            continue
        if not start <= parsed <= end:
            errors.append(
                f"{region}/{target_date} falls outside coverage "
                f"{envelope['coverage_start']}..{envelope['coverage_end']}"
            )

    expected_counts = {
        region: sum(1 for value in expected if value[0] == region)
        for region in sorted({*manifest["expected"], *(r for r, _ in expected)})
    }
    found_counts = {
        region: sum(1 for value in declared_found if value[0] == region)
        for region in sorted({*manifest["found"], *(r for r, _ in declared_found)})
    }
    if manifest["expected"] != expected_counts:
        errors.append("manifest expected counts do not match expected_records")
    if manifest["found"] != found_counts:
        errors.append("manifest found counts do not match found_records")
    if envelope["status"] == "COMPLETE" and (unresolved or malformed_count):
        errors.append("STATUS=COMPLETE cannot contain unresolved or malformed records")
    return errors


def validate_transport(export: dict[str, Any], schema_path: Path = TRANSPORT_SCHEMA_PATH) -> list[str]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [f"schema {e.json_path}: {e.message}" for e in sorted(validator.iter_errors(export), key=lambda e: list(e.path))]
    errors.extend(transport_semantic_errors(export))
    return errors


def _market_config() -> dict[str, Any]:
    return json.loads(MARKETS_PATH.read_text(encoding="utf-8"))


def transport_semantic_errors(export: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    config = _market_config()["regions"]
    seen_region_dates: set[tuple[str, str]] = set()

    for record in export.get("records", []):
        region = record.get("region")
        target_date = record.get("target_date")
        key = (str(region), str(target_date))
        if key in seen_region_dates:
            errors.append(f"duplicate region/date in export: {region}/{target_date}")
        seen_region_dates.add(key)
        if region not in config:
            continue

        expected_cities = {c["city_id"] for c in config[region]["cities"]}
        expected_markets = {c["primary_index_id"] for c in config[region]["cities"]}
        found_cities = {c.get("city_id") for c in record.get("cities", [])}
        primary_markets = {m.get("market_id") for m in record.get("markets", []) if m.get("primary")}
        missing_cities = expected_cities - found_cities
        missing_markets = expected_markets - primary_markets
        if missing_cities:
            errors.append(f"{region}/{target_date}: missing cities {sorted(missing_cities)}")
        if missing_markets:
            errors.append(f"{region}/{target_date}: missing primary markets {sorted(missing_markets)}")

        source_ids = [s.get("id") for s in record.get("sources", [])]
        if len(source_ids) != len(set(source_ids)):
            errors.append(f"{region}/{target_date}: duplicate source ids")
        known = set(source_ids)

        for city in record.get("cities", []):
            for window_name, window in city.get("weather", {}).items():
                for metric_name, metric in window.get("metrics", {}).items():
                    value = metric.get("v")
                    refs = set(metric.get("s", []))
                    if value is not None and not refs:
                        errors.append(f"{region}/{target_date}/{city.get('city_id')}/{window_name}/{metric_name}: non-null value without source")
                    unknown = refs - known
                    if unknown:
                        errors.append(f"{region}/{target_date}/{city.get('city_id')}/{window_name}/{metric_name}: unknown sources {sorted(unknown)}")
                    if metric.get("t") == "forecast":
                        errors.append(f"{region}/{target_date}/{city.get('city_id')}/{window_name}/{metric_name}: forecast cannot be imported as completed historical observation")

        mandatory_market = {"previous_close", "open", "high", "low", "close", "close_to_close_return_pct", "intraday_range_pct"}
        for market in record.get("markets", []):
            status = market.get("session", {}).get("status")
            session = market.get("session", {})
            metrics = market.get("metrics", {})
            calendar_refs = set(session.get("calendar_source_ids", []))
            unknown_calendar = calendar_refs - known
            if unknown_calendar:
                errors.append(
                    f"{region}/{target_date}/{market.get('market_id')}: "
                    f"unknown calendar sources {sorted(unknown_calendar)}"
                )
            if session.get("calendar_checked") and not calendar_refs:
                errors.append(
                    f"{region}/{target_date}/{market.get('market_id')}: "
                    "calendar_checked=true requires calendar_source_ids"
                )
            for metric_name, metric in metrics.items():
                value = metric.get("v")
                refs = set(metric.get("s", []))
                if value is not None and not refs:
                    errors.append(f"{region}/{target_date}/{market.get('market_id')}/{metric_name}: non-null value without source")
                unknown = refs - known
                if unknown:
                    errors.append(f"{region}/{target_date}/{market.get('market_id')}/{metric_name}: unknown sources {sorted(unknown)}")
            if status in {"open", "early_close", "special_session"}:
                missing = sorted(k for k in mandatory_market if metrics.get(k, {}).get("v") is None)
                if missing:
                    errors.append(f"{region}/{target_date}/{market.get('market_id')}: open session missing mandatory metrics {missing}")
            elif status in {"closed_holiday", "closed_weekend", "unexpected_closure"}:
                non_null = sorted(k for k, metric in metrics.items() if metric.get("v") is not None)
                if non_null:
                    errors.append(f"{region}/{target_date}/{market.get('market_id')}: closed session has non-null market metrics {non_null}")

        if record.get("collection_mode") == "backfill" and not record.get("backfill_reason"):
            errors.append(f"{region}/{target_date}: backfill_reason required")
    return errors


def _canonical_metric(metric: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": metric.get("v"),
        "unit": metric.get("u"),
        "data_type": metric.get("t"),
        "source_ids": metric.get("s", []),
        "notes": metric.get("n"),
    }


def _window_hours(start: str, end: str) -> float:
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    minutes = (eh * 60 + em) - (sh * 60 + sm)
    if minutes <= 0:
        minutes += 24 * 60
    return minutes / 60.0


def convert_region_record(record: dict[str, Any], export: dict[str, Any], revision: int = 1, supersedes_revision: int | None = None, revision_reason: str | None = None) -> dict[str, Any]:
    config = _market_config()["regions"][record["region"]]
    city_cfg = {c["city_id"]: c for c in config["cities"]}

    cities = []
    for city in record["cities"]:
        cfg = city_cfg[city["city_id"]]
        windows = {}
        for window_name, window in city["weather"].items():
            metrics = {k: _canonical_metric(v) for k, v in window["metrics"].items()}
            scored = weather_score(metrics, _window_hours(window["start"], window["end"]), window.get("alert"))
            windows[window_name] = {
                "start_local": window["start"],
                "end_local": window["end"],
                "metrics": metrics,
                "alert_severity": window.get("alert", "unknown"),
                "score_components": scored["components"],
                "weather_index": scored["index"],
                "weather_score": scored["score"],
                "score_status": scored["status"],
                "explanation_zh": window.get("notes_zh") or "由固定V1.0评分函数根据原始天气指标计算。",
            }
        cities.append({
            "city_id": cfg["city_id"], "city_name": cfg["city_name"], "country": cfg["country"],
            "timezone": cfg["timezone"], "primary_exchange": cfg["exchange"], "weather_windows": windows,
        })

    markets = []
    for market in record["markets"]:
        metrics = {k: _canonical_metric(v) for k, v in market["metrics"].items()}
        status = market["session"]["status"]
        if status in {"closed_holiday", "closed_weekend", "unexpected_closure"}:
            scored = {"components": {"return": None, "breadth": None, "close_position": None, "stability": None}, "index": None, "score": None, "status": "score_unavailable"}
        else:
            scored = market_score(metrics)
        markets.append({
            "market_id": market["market_id"], "index_name": market["index_name"], "city_id": market["city_id"],
            "exchange": market["exchange"], "currency": market["currency"], "primary": market["primary"],
            "session": {
                "trading_status": status, "session_type": market["session"]["type"],
                "official_calendar_checked": market["session"]["calendar_checked"],
                "calendar_source_ids": market["session"].get("calendar_source_ids", []),
                "closure_reason": market["session"].get("reason"), "open_local": market["session"].get("open"),
                "close_local": market["session"].get("close"),
            },
            "metrics": metrics, "score_components": scored["components"], "market_index": scored["index"],
            "market_score": scored["score"], "score_status": scored["status"],
            "explanation_zh": market.get("notes_zh") or "由固定V1.0评分函数根据市场原始指标计算。",
        })

    sources = [{
        "source_id": s["id"], "title": s.get("title") or s["publisher"], "url": s["url"],
        "publisher": s["publisher"], "accessed_at": s["accessed_at"], "official": s["official"],
        "notes": s.get("notes"),
    } for s in record["sources"]]
    status = record["quality"]["status"]
    confidence = {"complete": 0.9, "partial": 0.65, "unrecoverable": 0.25}[status]
    generated = export["generated_at"]
    expected = generated
    return {
        "schema_version": "1.0.0", "protocol_version": "1.0.2", "region": record["region"],
        "target_date": record["target_date"], "collection_mode": record["collection_mode"],
        "expected_run_at": expected, "collected_at": generated, "lag_days": record["lag_days"],
        "backfill_reason": record.get("backfill_reason"), "prompt_version": "global-scheduled-collector-1.0.2",
        "weather_scoring_version": "1.0.0", "market_scoring_version": "1.0.0",
        "model_name": "ChatGPT scheduled task", "task_run_id": export["run_id"],
        "revision": revision, "supersedes_revision": supersedes_revision, "revision_reason": revision_reason,
        "cities": cities, "markets": markets, "sources": sources,
        "data_quality": {"status": status, "missing_fields": [], "warnings": record["quality"]["issues"], "confidence": confidence},
        "summary_zh": record.get("summary_zh"),
    }


def canonical_digest(record: dict[str, Any]) -> str:
    copy = deepcopy(record)
    for key in ["collected_at", "expected_run_at", "task_run_id", "revision", "supersedes_revision", "revision_reason"]:
        copy.pop(key, None)
    return hashlib.sha256(json.dumps(copy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def week_id_from_text(text: str, fallback_date: str | None = None) -> str:
    match = re.search(r"^WEEK_ID:\s*([0-9]{4}-W[0-9]{2})\s*$", text, re.MULTILINE)
    if match:
        return match.group(1)
    value = parse_date(fallback_date or datetime.now().date().isoformat())
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def flatten_records(exports: Iterable[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for export in exports:
        records = export.get("records", [])
        if isinstance(records, list):
            for record in records:
                if isinstance(record, dict):
                    rows.append((export, record))
    return rows
