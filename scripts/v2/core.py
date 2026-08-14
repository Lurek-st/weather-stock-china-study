from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jsonschema
import pandas as pd
import yaml

SCHEMA_VERSION = "2.0.0"
CORE_WEATHER_COLUMNS = [
    "air_temperature_c",
    "dew_point_c",
    "relative_humidity_pct",
    "apparent_temperature_c",
    "precipitation_mm",
    "cloud_cover_pct",
    "wind_speed_mps",
    "max_gust_mps",
    "solar_radiation_mj_m2",
]
VALUE_RANK = {
    "official_final": 50,
    "official_provisional": 40,
    "registered_primary_final": 30,
    "registered_backup": 20,
    "news_explanation_only": 0,
}


class V2Error(RuntimeError):
    """A deterministic, user-actionable pipeline failure."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise V2Error(f"{path.name} must contain a YAML mapping")
    return loaded


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise V2Error(f"{path.name} must contain a JSON object")
    return loaded


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_local_path(value: str) -> str:
    value = re.sub(r"(?i)[A-Z]:\\(?:[^\\\r\n]+\\)*", "<LOCAL_PATH>/", value)
    value = re.sub(r"(?<![A-Za-z0-9])/(?:home|Users)/[^/\s]+/", "<LOCAL_PATH>/", value)
    return value


def redact_structure(value: Any) -> Any:
    if isinstance(value, str):
        return redact_local_path(value)
    if isinstance(value, dict):
        return {key: redact_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    return value


def validate_registry_files(root: Path) -> list[str]:
    config_dir = root / "config" / "v2"
    schema_dir = root / "schemas" / "v2"
    pairs = [
        ("source-registry.yaml", "source-registry.schema.json"),
        ("locations.yaml", "location-registry.schema.json"),
        ("stations.yaml", "station-registry.schema.json"),
        ("markets.yaml", "market-registry.schema.json"),
        ("calendar-registry.yaml", "calendar-registry.schema.json"),
        ("units.yaml", "unit-registry.schema.json"),
    ]
    errors: list[str] = []
    loaded: dict[str, dict[str, Any]] = {}
    for config_name, schema_name in pairs:
        try:
            document = load_yaml(config_dir / config_name)
            schema = load_json(schema_dir / schema_name)
            jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(document)
            loaded[config_name] = document
        except Exception as exc:  # schema errors are aggregated for the CLI
            errors.append(f"{config_name}: {exc}")

    for name, key, id_key in [
        ("source-registry.yaml", "sources", "source_id"),
        ("locations.yaml", "locations", "city_id"),
        ("stations.yaml", "stations", "city_id"),
        ("markets.yaml", "markets", "market_id"),
        ("calendar-registry.yaml", "calendars", "calendar_source_id"),
    ]:
        if name not in loaded:
            continue
        ids = [row[id_key] for row in loaded[name][key]]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            errors.append(f"{name}: duplicate {id_key}: {duplicates}")

    locations = loaded.get("locations.yaml", {}).get("locations", [])
    for row in locations:
        try:
            ZoneInfo(row["timezone"])
        except (ZoneInfoNotFoundError, KeyError):
            errors.append(f"locations.yaml: invalid timezone for {row.get('city_id')}")
        if not -90 <= row.get("latitude", 999) <= 90:
            errors.append(f"locations.yaml: invalid latitude for {row.get('city_id')}")
        if not -180 <= row.get("longitude", 999) <= 180:
            errors.append(f"locations.yaml: invalid longitude for {row.get('city_id')}")

    units = load_yaml(config_dir / "units.yaml")
    required_units = set(CORE_WEATHER_COLUMNS)
    unknown = required_units - set(units.get("units", {}))
    if unknown:
        errors.append(f"units.yaml: undefined canonical units: {sorted(unknown)}")
    markets = loaded.get("markets.yaml", {}).get("markets", [])
    calendars = {
        row["calendar_source_id"]
        for row in loaded.get("calendar-registry.yaml", {}).get("calendars", [])
    }
    city_ids = {row["city_id"] for row in locations}
    for row in markets:
        try:
            ZoneInfo(row["timezone"])
        except (ZoneInfoNotFoundError, KeyError):
            errors.append(f"markets.yaml: invalid timezone for {row.get('market_id')}")
        if row.get("city_id") not in city_ids:
            errors.append(f"markets.yaml: undefined city for {row.get('market_id')}")
        if row.get("calendar_source_id") not in calendars:
            errors.append(f"markets.yaml: undefined calendar for {row.get('market_id')}")
    return errors


@dataclass(frozen=True)
class ArtifactResult:
    artifact_path: Path
    manifest_path: Path
    revision: int
    skipped_identical: bool


class RawArtifactStore:
    """Append-only raw artifact store with content-addressed idempotency."""

    def __init__(self, base: Path):
        self.base = base

    def persist(
        self,
        *,
        source_id: str,
        provider: str,
        logical_name: str,
        payload: bytes,
        request: dict[str, Any],
        status: str,
        licence: str,
        suffix: str,
        retries: int = 0,
        error: str | None = None,
        retrieved_at: datetime | None = None,
        validation_metadata: dict[str, Any] | None = None,
        final_request_id: str | None = None,
    ) -> ArtifactResult:
        digest = sha256_bytes(payload)
        target_dir = self.base / source_id / logical_name
        target_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(target_dir.glob("r*-*.manifest.json"))
        for manifest_path in existing:
            manifest = load_json(manifest_path)
            if manifest["sha256"] == digest:
                artifact_path = manifest_path.with_name(manifest_path.name.replace(".manifest.json", suffix))
                return ArtifactResult(artifact_path, manifest_path, manifest["revision"], True)

        revision = max([load_json(item)["revision"] for item in existing] or [0]) + 1
        stem = f"r{revision:04d}-{digest[:12]}"
        artifact_path = target_dir / f"{stem}{suffix}"
        manifest_path = target_dir / f"{stem}.manifest.json"
        if artifact_path.exists() or manifest_path.exists():
            raise V2Error("append-only collision; refusing to overwrite raw artifact")
        artifact_path.write_bytes(payload)
        retrieved = retrieved_at or datetime.now(timezone.utc)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "artifact_id": f"{source_id}:{logical_name}:{stem}",
            "source_id": source_id,
            "provider": provider,
            "retrieved_at": retrieved.isoformat(),
            "sha256": digest,
            "content_length": len(payload),
            "licence": licence,
            "status": status,
            "revision": revision,
            "retries": retries,
            "error": redact_local_path(error) if error else None,
            "request": redact_structure(request),
            "validation_metadata": redact_structure(validation_metadata) if validation_metadata else None,
            "final_request_id": final_request_id,
        }
        write_json(manifest_path, manifest)
        return ArtifactResult(artifact_path, manifest_path, revision, False)

    def lookup_by_final_request_id(self, final_request_id: str) -> ArtifactResult | None:
        """Find an accepted artifact whose manifest binds ``final_request_id``.

        Request-aware pre-network idempotency: a production runner calls this
        BEFORE constructing the CDS client.  ``None`` means no accepted
        artifact exists yet.  An artifact whose manifest mentions the id but
        fails the acceptance predicates (missing raw file, SHA mismatch,
        status != final, container validation not passed) is treated as a
        broken/partial acceptance and the caller MUST fail closed rather than
        silently re-download.
        """
        for manifest_path in sorted(self.base.glob("*/r*-*.manifest.json")) + sorted(self.base.glob("*/*/r*-*.manifest.json")):
            try:
                manifest = load_json(manifest_path)
            except Exception:
                continue
            if manifest.get("final_request_id") != final_request_id:
                continue
            artifact_path = manifest_path.with_name(
                manifest_path.name.replace(".manifest.json", self._suffix_from_manifest(manifest))
            )
            return ArtifactResult(artifact_path, manifest_path, manifest["revision"], False)
        return None

    @staticmethod
    def _suffix_from_manifest(manifest: dict[str, Any]) -> str:
        validation = manifest.get("validation_metadata") or {}
        return validation.get("raw_suffix", ".zip") if isinstance(validation, dict) else ".zip"


def relative_humidity(temp_c: pd.Series, dew_c: pd.Series) -> pd.Series:
    numerator = (17.625 * dew_c) / (243.04 + dew_c)
    denominator = (17.625 * temp_c) / (243.04 + temp_c)
    return (100.0 * (numerator - denominator).map(math.exp)).clip(0, 100)


def apparent_temperature(
    temp_c: pd.Series, relative_humidity_pct: pd.Series, wind_speed_mps: pd.Series
) -> pd.Series:
    """Steadman shaded apparent temperature, formula version steadman_v1."""
    vapour_pressure_hpa = (
        relative_humidity_pct
        / 100
        * 6.105
        * ((17.27 * temp_c) / (237.7 + temp_c)).map(math.exp)
    )
    return temp_c + 0.33 * vapour_pressure_hpa - 0.70 * wind_speed_mps - 4.0


def normalize_weather_frame(
    frame: pd.DataFrame, *, city_id: str, source_id: str, data_class: str
) -> pd.DataFrame:
    allowed = {"provisional_reanalysis", "final_reanalysis", "station_observation"}
    if data_class not in allowed:
        raise V2Error(f"invalid weather data_class: {data_class}")
    required = {
        "timestamp_utc",
        "temperature_k",
        "dewpoint_k",
        "precipitation_m",
        "cloud_cover_fraction",
        "wind_u_mps",
        "wind_v_mps",
        "wind_gust_mps",
        "solar_radiation_j_m2",
    }
    missing = required - set(frame.columns)
    if missing:
        raise V2Error(f"weather input missing fields: {sorted(missing)}")
    result = pd.DataFrame()
    result["timestamp_utc"] = pd.to_datetime(frame["timestamp_utc"], utc=True)
    duplicated = result["timestamp_utc"].duplicated(keep=False)
    if duplicated.any():
        raise V2Error("duplicate weather timestamp")
    # One-hour accumulation interval that the row timestamp ends (applies to
    # interval_accumulation variables such as precipitation and solar
    # radiation; instantaneous variables are valid at the timestamp itself).
    result["accumulation_interval_start_utc"] = result["timestamp_utc"] - pd.Timedelta(hours=1)
    result["accumulation_interval_end_utc"] = result["timestamp_utc"]
    result["city_id"] = city_id
    result["air_temperature_c"] = frame["temperature_k"].astype(float) - 273.15
    result["dew_point_c"] = frame["dewpoint_k"].astype(float) - 273.15
    result["relative_humidity_pct"] = relative_humidity(
        result["air_temperature_c"], result["dew_point_c"]
    )
    result["precipitation_mm"] = frame["precipitation_m"].astype(float) * 1000
    result["cloud_cover_pct"] = frame["cloud_cover_fraction"].astype(float) * 100
    result["wind_speed_mps"] = (
        frame["wind_u_mps"].astype(float) ** 2 + frame["wind_v_mps"].astype(float) ** 2
    ) ** 0.5
    result["max_gust_mps"] = frame["wind_gust_mps"].astype(float)
    result["solar_radiation_mj_m2"] = frame["solar_radiation_j_m2"].astype(float) / 1_000_000
    result["apparent_temperature_c"] = apparent_temperature(
        result["air_temperature_c"],
        result["relative_humidity_pct"],
        result["wind_speed_mps"],
    )
    result["source_id"] = source_id
    result["data_class"] = data_class
    result["revision"] = 1
    result["is_final"] = data_class == "final_reanalysis"
    result["quality_flags"] = [[] for _ in range(len(result))]
    return result


def _parse_clock(value: str) -> time:
    return time.fromisoformat(value)


def _inside_clock(current: time, start: time, end: time) -> bool:
    return start <= current < end


ACCUMULATION_CANONICALS = {"precipitation_mm", "solar_radiation_mj_m2"}
PARTIAL_INTERVAL_POLICY = "partial_accumulation_interval_excluded"
DERIVED_INSTANTANEOUS = {"relative_humidity_pct", "apparent_temperature_c"}


def load_variable_semantics(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Map canonical variable -> semantics entry from the registry."""
    root = root or repo_root()
    registry = load_yaml(root / "config" / "v2" / "weather-variable-semantics.yaml")
    mapping: dict[str, dict[str, Any]] = {}
    for entry in registry.get("variables", []):
        mapping.setdefault(entry["canonical_variable"], entry)
    return mapping


def _expected_instantaneous(start_local: datetime, end_local: datetime) -> int:
    """Count local whole-hour timestamps inside [start, end).

    Uses a tz-aware hourly range so DST transition days yield the actual
    number of local whole hours (23 or 25) instead of a naive UTC hour count.
    """
    return int(
        pd.date_range(start_local, end_local, freq="h", inclusive="left", tz=start_local.tzinfo).size
    )


def _expected_accumulation(start_local: datetime, end_local: datetime) -> int:
    """Count whole-hour accumulation intervals fully inside [start, end]."""
    count = 0
    current = start_local.replace(minute=0, second=0, microsecond=0)
    if current <= start_local:
        current += timedelta(hours=1)
    while current <= end_local:
        if current - timedelta(hours=1) >= start_local:
            count += 1
        current += timedelta(hours=1)
    return count


def _break_instantaneous_count(start_local: datetime, end_local: datetime, breaks: list[tuple[time, time]]) -> int:
    count = 0
    current = start_local.replace(minute=0, second=0, microsecond=0)
    if current < start_local:
        current += timedelta(hours=1)
    while current < end_local:
        clock = current.time()
        if any(_inside_clock(clock, a, b) for a, b in breaks):
            count += 1
        current += timedelta(hours=1)
    return count


def _break_accumulation_count(start_local: datetime, end_local: datetime, breaks: list[tuple[time, time]], day: date, tz) -> int:
    count = 0
    current = start_local.replace(minute=0, second=0, microsecond=0)
    if current <= start_local:
        current += timedelta(hours=1)
    while current <= end_local:
        interval_start = current - timedelta(hours=1)
        if interval_start >= start_local:
            for a, b in breaks:
                break_start = datetime.combine(day, a, tz)
                break_end = datetime.combine(day, b, tz)
                if interval_start < break_end and current > break_start:
                    count += 1
                    break
        current += timedelta(hours=1)
    return count


def build_weather_windows(
    hourly: pd.DataFrame,
    market_config: dict[str, Any],
    trading_dates: Iterable[str | date],
    calendar_overrides: dict[str, dict[str, Any]] | None = None,
) -> pd.DataFrame:
    """Aggregate pre-open, cash-session (break excluded), and full local day.

    Instantaneous variables (temperature, dew point, humidity, apparent
    temperature, cloud cover, wind, gust) are sampled by their valid
    timestamp; one-hour accumulation variables (precipitation, solar
    radiation) are aggregated only when their whole interval lies inside the
    window. Partial intervals are excluded and flagged.
    """
    city_id = market_config["city_id"]
    timezone_name = market_config["timezone"]
    tz = ZoneInfo(timezone_name)
    base_session = market_config["session"]
    calendar_overrides = calendar_overrides or {}
    semantics = load_variable_semantics()
    instant_columns = [c for c in CORE_WEATHER_COLUMNS if c not in ACCUMULATION_CANONICALS]
    acc_columns = [c for c in CORE_WEATHER_COLUMNS if c in ACCUMULATION_CANONICALS]
    missing_semantics = [
        c for c in CORE_WEATHER_COLUMNS
        if c not in semantics and c not in DERIVED_INSTANTANEOUS
    ]
    subset = hourly.loc[hourly["city_id"] == city_id].copy()
    subset["timestamp_utc"] = pd.to_datetime(subset["timestamp_utc"], utc=True)
    if subset["timestamp_utc"].duplicated().any():
        raise V2Error(f"duplicate hourly timestamps for {city_id}")
    subset["timestamp_local"] = subset["timestamp_utc"].dt.tz_convert(tz)
    if "accumulation_interval_start_utc" not in subset.columns:
        subset["accumulation_interval_start_utc"] = subset["timestamp_utc"] - pd.Timedelta(hours=1)
        subset["accumulation_interval_end_utc"] = subset["timestamp_utc"]
    subset["acc_start_local"] = subset["accumulation_interval_start_utc"].dt.tz_convert(tz)
    subset["acc_end_local"] = subset["accumulation_interval_end_utc"].dt.tz_convert(tz)
    rows: list[dict[str, Any]] = []
    for raw_day in trading_dates:
        day = date.fromisoformat(raw_day) if isinstance(raw_day, str) else raw_day
        override = calendar_overrides.get(day.isoformat(), {})
        session = {
            "open": override.get("open", base_session["open"]),
            "close": override.get("close", base_session["close"]),
            "breaks": override.get("breaks", base_session.get("breaks", [])),
        }
        open_clock = _parse_clock(session["open"])
        close_clock = _parse_clock(session["close"])
        breaks = [(_parse_clock(a), _parse_clock(b)) for a, b in session.get("breaks", [])]
        start_local = datetime.combine(day, time.min, tz)
        end_local = start_local + timedelta(days=1)
        open_local = datetime.combine(day, open_clock, tz)
        pre_open_start = open_local - timedelta(hours=2)
        close_local = datetime.combine(day, close_clock, tz)
        window_bounds = {
            "pre_open": (pre_open_start, open_local),
            "trading_session": (open_local, close_local),
            "full_day": (start_local, end_local),
        }
        for window, (w_start, w_end) in window_bounds.items():
            inst_mask = (subset["timestamp_local"] >= w_start) & (subset["timestamp_local"] < w_end)
            acc_mask = (subset["acc_start_local"] >= w_start) & (subset["acc_end_local"] <= w_end)
            overlap = (subset["acc_end_local"] > w_start) & (subset["acc_start_local"] < w_end)
            partial_mask = overlap & ~acc_mask
            if window == "trading_session" and breaks:
                in_break = pd.Series(False, index=subset.index)
                for break_start, break_end in breaks:
                    local_clock = subset["timestamp_local"].dt.time
                    in_break |= local_clock.map(lambda item: _inside_clock(item, break_start, break_end))
                inst_mask &= ~in_break
                for a, b in breaks:
                    break_start = datetime.combine(day, a, tz)
                    break_end = datetime.combine(day, b, tz)
                    acc_mask &= ~((subset["acc_start_local"] < break_end) & (subset["acc_end_local"] > break_start))
            inst_expected = _expected_instantaneous(w_start, w_end)
            acc_expected = _expected_accumulation(w_start, w_end)
            if window == "trading_session" and breaks:
                inst_expected -= _break_instantaneous_count(w_start, w_end, breaks)
                acc_expected -= _break_accumulation_count(w_start, w_end, breaks, day, tz)
            inst_selected = subset.loc[inst_mask]
            acc_selected = subset.loc[acc_mask]
            inst_observed = int(inst_selected.shape[0])
            acc_observed = int(acc_selected.shape[0])
            quality_flags: list[str] = []
            if missing_semantics:
                quality_flags.append("temporal_support_metadata_missing")
            if inst_observed < inst_expected:
                quality_flags.append("missing_hours")
                quality_flags.append("missing_instantaneous_hours")
            if acc_observed < acc_expected:
                quality_flags.append("missing_accumulation_intervals")
            if partial_mask.any():
                quality_flags.append("partial_accumulation_interval_excluded")
            row: dict[str, Any] = {
                "city_id": city_id,
                "market_id": market_config["market_id"],
                "trading_date": day.isoformat(),
                "window": window,
                "hour_count": inst_observed,
                # Compatibility field: equals the instantaneous expected count,
                # never int(session_duration_hours) (which gave TAIEX 4 vs 5).
                "expected_hour_count": inst_expected,
                "instantaneous_expected_count": inst_expected,
                "instantaneous_observed_count": inst_observed,
                "accumulation_expected_count": acc_expected,
                "accumulation_observed_count": acc_observed,
                "instantaneous_coverage_ratio": round(inst_observed / inst_expected, 4) if inst_expected else None,
                "accumulation_coverage_ratio": round(acc_observed / acc_expected, 4) if acc_expected else None,
                "partial_interval_policy": PARTIAL_INTERVAL_POLICY,
                "quality_flags": quality_flags,
                "calendar_status": override.get("calendar_status", "normal"),
            }
            for column in instant_columns:
                if column == "max_gust_mps":
                    row[column] = inst_selected[column].max()
                else:
                    row[column] = inst_selected[column].mean()
            for column in acc_columns:
                row[column] = acc_selected[column].sum(min_count=1)
            rows.append(row)
    return pd.DataFrame(rows)


def resolve_market_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in candidates if VALUE_RANK.get(row.get("authority"), -1) > 0]
    if not eligible:
        raise V2Error("no eligible structured market value; news cannot become production data")
    eligible.sort(key=lambda row: VALUE_RANK[row["authority"]], reverse=True)
    winner = dict(eligible[0])
    same_rank = [row for row in eligible if VALUE_RANK[row["authority"]] == VALUE_RANK[winner["authority"]]]
    values = {float(row["official_close"]) for row in same_rank}
    if len(values) > 1:
        winner["value_status"] = "conflicting"
        winner.setdefault("quality_flags", []).append("same_authority_conflict")
    return winner


def normalize_market_frame(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "market_id",
        "trading_date",
        "official_close",
        "source_timestamp",
        "value_status",
        "calendar_status",
        "calendar_source_id",
        "source_id",
        "revision",
        "is_final",
        "currency",
    }
    missing = required - set(frame.columns)
    if missing:
        raise V2Error(f"market input missing fields: {sorted(missing)}")
    result = frame.copy()
    result["trading_date"] = pd.to_datetime(result["trading_date"]).dt.date
    result["official_close"] = pd.to_numeric(result["official_close"], errors="raise")
    if (result["official_close"] <= 0).any():
        raise V2Error("official_close must be positive")
    result.sort_values(["market_id", "trading_date", "revision"], inplace=True)
    latest = result.drop_duplicates(["market_id", "trading_date"], keep="last").copy()
    latest["previous_official_close"] = latest.groupby("market_id")["official_close"].shift(1)
    previous_map = latest.set_index(["market_id", "trading_date"])["previous_official_close"]
    result["previous_official_close"] = [
        previous_map.loc[(market_id, trading_day)]
        for market_id, trading_day in zip(result["market_id"], result["trading_date"])
    ]
    result["close_to_close_return_pct"] = (
        (result["official_close"] / result["previous_official_close"] - 1) * 100
    )
    result["trading_date"] = result["trading_date"].astype(str)
    if "quality_flags" not in result:
        result["quality_flags"] = [[] for _ in range(len(result))]
    result["schema_version"] = SCHEMA_VERSION
    return result


def assess_maturity(
    *, weather_class: str, market_statuses: Iterable[str], calendar_verified: bool
) -> dict[str, Any]:
    statuses = list(market_statuses)
    blockers: list[str] = []
    if weather_class not in {"provisional_reanalysis", "final_reanalysis"}:
        blockers.append("core_weather_unavailable")
    if any(item in {"pending", "conflicting", "unavailable"} for item in statuses):
        blockers.append("market_values_not_mature")
    if not calendar_verified:
        blockers.append("calendar_not_verified")
    if blockers:
        state = "not_ready"
    elif weather_class == "final_reanalysis" and all(
        item in {"final", "corrected"} for item in statuses
    ):
        state = "frozen"
    elif all(item in {"final", "corrected"} for item in statuses):
        state = "research_ready"
    else:
        state = "provisional_ready"
    return {"status": state, "blocking_reasons": blockers}


def build_panel(
    market: pd.DataFrame,
    windows: pd.DataFrame,
    locations: dict[str, Any],
    tier: str,
    *,
    duplicate_policy: str = "error",
    include_sample_temperature_features: bool = True,
) -> pd.DataFrame:
    """Strict one-to-one weather-market panel builder.

    Research-grade default rejects duplicate market keys
    ``(market_id, trading_date)`` and duplicate weather window keys
    ``(city_id, market_id, trading_date, window)`` instead of silently
    dropping them.  Set ``duplicate_policy="keep_last"`` only for legacy
    compatibility paths; the real pilot must use the default.
    """
    if tier not in {"provisional", "frozen"}:
        raise V2Error("panel tier must be provisional or frozen")
    if duplicate_policy not in {"error", "keep_last"}:
        raise V2Error("duplicate_policy must be 'error' or 'keep_last'")
    index = ["city_id", "market_id", "trading_date"]

    # ---- strict weather window key check (research-grade) ----
    window_key = ["city_id", "market_id", "trading_date", "window"]
    required = set(window_key) | set(CORE_WEATHER_COLUMNS)
    missing = required - set(windows.columns)
    if missing:
        raise V2Error(f"weather windows missing fields: {sorted(missing)}")
    duplicated_windows = windows.duplicated(subset=window_key, keep=False)
    if duplicated_windows.any():
        dup = windows.loc[duplicated_windows, window_key].drop_duplicates()
        raise V2Error(
            f"duplicate weather window keys: {dup.to_dict('records')[:3]}"
        )

    pieces = []
    for window in ["pre_open", "trading_session", "full_day"]:
        selected = windows.loc[windows["window"] == window, index + CORE_WEATHER_COLUMNS].copy()
        dup_selected = selected.duplicated(subset=index, keep=False)
        if dup_selected.any():
            dup = selected.loc[dup_selected, index].drop_duplicates()
            raise V2Error(f"duplicate {window} rows for same key: {dup.to_dict('records')[:3]}")
        selected.rename(
            columns={column: f"{window}_{column}" for column in CORE_WEATHER_COLUMNS},
            inplace=True,
        )
        pieces.append(selected)
    weather_wide = pieces[0]
    for piece in pieces[1:]:
        weather_wide = weather_wide.merge(piece, on=index, how="outer", validate="one_to_one")
    weather_fields = [
        f"{window}_{column}"
        for window in ["pre_open", "trading_session", "full_day"]
        for column in CORE_WEATHER_COLUMNS
    ]

    # ---- market duplicate policy (research-grade rejects silently) ----
    market_key = ["market_id", "trading_date"]
    duplicated_market = market.duplicated(subset=market_key, keep=False)
    if duplicated_market.any():
        dup = market.loc[duplicated_market, market_key].drop_duplicates()
        if duplicate_policy == "error":
            raise V2Error(f"duplicate market keys: {dup.to_dict('records')[:3]}")
    if duplicate_policy == "keep_last":
        result = market.sort_values(["market_id", "trading_date", "revision"]).drop_duplicates(
            market_key, keep="last"
        ).copy()
    else:
        result = market.sort_values(["market_id", "trading_date", "revision"]).copy()

    location_map = {row["market_id"]: row["city_id"] for row in locations["locations"]}
    result["city_id"] = result["market_id"].map(location_map)

    # ---- strict coverage: every market key must be served by weather ----
    market_keys = set(zip(result["city_id"], result["market_id"], result["trading_date"]))
    weather_keys = set(zip(weather_wide["city_id"], weather_wide["market_id"], weather_wide["trading_date"]))
    missing_market_keys = market_keys - weather_keys
    if missing_market_keys:
        raise V2Error(
            "market keys missing from weather: "
            f"{sorted(missing_market_keys)[:3]}"
        )
    needed_mask = weather_wide[index].apply(tuple, axis=1).isin(market_keys)
    if weather_wide.loc[needed_mask, weather_fields].isna().any().any():
        incomplete = weather_wide.loc[needed_mask & weather_wide[weather_fields].isna().any(axis=1), index]
        raise V2Error(
            f"weather-wide missing window coverage: {incomplete.to_dict('records')[:3]}"
        )
    # keep only rows the market actually needs (excess weather keys dropped)
    weather_wide = weather_wide.loc[needed_mask].copy()

    # ---- strict one-to-one join: key sets must match exactly ----
    market_keys = set(zip(result["city_id"], result["market_id"], result["trading_date"]))
    weather_keys = set(zip(weather_wide["city_id"], weather_wide["market_id"], weather_wide["trading_date"]))
    if market_keys != weather_keys:
        raise V2Error(
            "market and weather key sets must match exactly; "
            f"only_market={sorted(market_keys - weather_keys)} "
            f"only_weather={sorted(weather_keys - market_keys)}"
        )
    result = result.merge(weather_wide, on=index, how="inner", validate="one_to_one")
    result["schema_version"] = SCHEMA_VERSION
    result["panel_tier"] = tier
    result["source_revision"] = result["revision"]

    if include_sample_temperature_features:
        monthly = result.groupby(["city_id", pd.to_datetime(result["trading_date"]).dt.month])[
            "pre_open_air_temperature_c"
        ]
        mean = monthly.transform("mean")
        std = monthly.transform("std").replace(0, float("nan"))
        result["pre_open_temperature_anomaly_c"] = result["pre_open_air_temperature_c"] - mean
        result["pre_open_temperature_z"] = (
            result["pre_open_air_temperature_c"] - mean
        ) / std
    return result


def week_bounds(week: str) -> tuple[date, date]:
    match = re.fullmatch(r"(\d{4})-W(\d{2})", week)
    if not match:
        raise V2Error("week must use ISO YYYY-Www")
    start = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    return start, start + timedelta(days=6)


def fixture_frames(root: Path, week: str) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    spec = load_yaml(root / "tests" / "v2" / "fixtures" / "mature_week_spec.yaml")
    if week != spec["week"]:
        raise V2Error(f"offline fixture only covers {spec['week']}")
    locations = load_yaml(root / "config" / "v2" / "locations.yaml")
    # The synthetic fixture covers the original eight-city composite only;
    # the pilot city (taipei) is excluded so legacy composite expectations
    # (8 markets x 5 days = 40 rows) remain stable.
    fixture_locations = [
        location for location in locations["locations"] if location["city_id"] != "taipei"
    ]
    start, end = week_bounds(week)
    timestamps = pd.date_range(
        datetime.combine(start - timedelta(days=1), time.min, timezone.utc),
        datetime.combine(end + timedelta(days=1), time.min, timezone.utc),
        freq="h",
        inclusive="left",
    )
    weather_rows = []
    for offset, location in enumerate(fixture_locations):
        for hour_index, timestamp in enumerate(timestamps):
            weather_rows.append(
                {
                    "timestamp_utc": timestamp,
                    "temperature_k": 283.15 + offset + 4 * math.sin(hour_index * math.pi / 12),
                    "dewpoint_k": 278.15 + offset,
                    "precipitation_m": 0.0002 if hour_index % 37 == 0 else 0.0,
                    "cloud_cover_fraction": (hour_index % 10) / 10,
                    "wind_u_mps": 2.0 + offset / 10,
                    "wind_v_mps": 1.0,
                    "wind_gust_mps": 5.0 + offset,
                    "solar_radiation_j_m2": 360000.0 if 7 <= timestamp.hour <= 17 else 0.0,
                    "city_id": location["city_id"],
                }
            )
    raw_weather = pd.DataFrame(weather_rows)
    normalized_parts = []
    for location in fixture_locations:
        normalized_parts.append(
            normalize_weather_frame(
                raw_weather.loc[raw_weather["city_id"] == location["city_id"]].drop(
                    columns="city_id"
                ),
                city_id=location["city_id"],
                source_id="fixture_era5t",
                data_class="provisional_reanalysis",
            )
        )
    weather = pd.concat(normalized_parts, ignore_index=True)
    trading_dates = [
        (start + timedelta(days=offset)).isoformat()
        for offset in range(7)
        if (start + timedelta(days=offset)).weekday() < 5
    ]
    market_rows = []
    for market_offset, location in enumerate(fixture_locations):
        for day_offset, trading_day in enumerate(trading_dates):
            market_rows.append(
                {
                    "market_id": location["market_id"],
                    "trading_date": trading_day,
                    "official_close": 1000 + market_offset * 100 + day_offset * 3,
                    "source_timestamp": f"{trading_day}T23:00:00Z",
                    "value_status": "final",
                    "calendar_status": "verified",
                    "calendar_source_id": "fixture_calendar",
                    "source_id": "fixture_official_market",
                    "revision": 1,
                    "is_final": True,
                    "currency": load_yaml(root / "config" / "v2" / "markets.yaml")["markets"][market_offset]["currency"],
                    "quality_flags": [],
                }
            )
    return weather, pd.DataFrame(market_rows), trading_dates


def write_panel_bundle(panel: pd.DataFrame, target: Path, maturity: dict[str, Any]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    csv_path = target / "city_market_daily.csv"
    parquet_path = target / "city_market_daily.parquet"
    panel.to_csv(csv_path, index=False, lineterminator="\n")
    panel.to_parquet(parquet_path, index=False)
    dictionary = {
        column: {
            "dtype": str(panel[column].dtype),
            "description": "V2 canonical or window-prefixed research variable",
        }
        for column in panel.columns
    }
    write_json(target / "data_dictionary.json", dictionary)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tier": panel["panel_tier"].iloc[0] if len(panel) else target.name,
        "row_count": len(panel),
        "csv_sha256": sha256_file(csv_path),
        "parquet_sha256": sha256_file(parquet_path),
        "maturity": maturity,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(target / "panel_manifest.json", manifest)


def backfill_estimate(root: Path, start: date, end: date) -> dict[str, Any]:
    if start > end:
        raise V2Error("backfill start must not be after end")
    days = (end - start).days + 1
    weekdays = sum(
        1 for offset in range(days) if (start + timedelta(days=offset)).weekday() < 5
    )
    years = sorted({(start + timedelta(days=offset)).year for offset in range(days)})
    sources = load_yaml(root / "config" / "v2" / "source-registry.yaml")["sources"]
    blocked = [
        item["source_id"]
        for item in sources
        if item.get("status") in {"manual_source_required", "official_manual_import"}
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "estimated_era5_requests": len(years) * 8,
        "estimated_ghcnh_requests": len(years) * 16,
        "estimated_market_records": weekdays * 8,
        "estimated_city_weather_hours": days * 24 * 8,
        "estimated_raw_size_gib": round(days * 24 * 8 * 8 * 8 / (1024**3) * 3.5, 2),
        "credentials": ["CDS account and local ~/.cdsapirc"],
        "missing_or_manual_sources": blocked,
        "licence_risks": ["FTSE 100", "DAX", "S&P 500", "unconfirmed automated exchange endpoints"],
    }
