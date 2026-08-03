import json
from pathlib import Path

from scripts.weekly_package import (
    canonical_digest,
    convert_region_record,
    extract_daily_exports,
    parse_weekly_envelope,
    validate_transport,
)

ROOT = Path(__file__).resolve().parents[1]


def test_extract_and_validate_transport():
    text = (ROOT / "tests/fixtures/weekly/valid_weekly_package.md").read_text(encoding="utf-8")
    envelope = parse_weekly_envelope(text)
    assert envelope["week_id"] == "2026-W32"
    assert envelope["manifest"]["daily_export_count"] == 1
    exports = extract_daily_exports(text)
    assert len(exports) == 1
    assert validate_transport(exports[0]) == []


def test_convert_transport_to_canonical_scores():
    export = json.loads((ROOT / "tests/fixtures/daily_transport_asia.json").read_text(encoding="utf-8"))
    record = convert_region_record(export["records"][0], export)
    assert record["protocol_version"] == "1.0.2"
    assert len(record["cities"]) == 5
    assert len(record["markets"]) == 5
    assert record["cities"][0]["weather_windows"]["trading_session"]["weather_score"] is not None
    assert record["markets"][0]["market_score"] is not None


def test_canonical_digest_ignores_operational_metadata():
    export = json.loads((ROOT / "tests/fixtures/daily_transport_asia.json").read_text(encoding="utf-8"))
    a = convert_region_record(export["records"][0], export)
    b = json.loads(json.dumps(a))
    b["collected_at"] = "2026-08-05T00:00:00+00:00"
    b["revision"] = 8
    assert canonical_digest(a) == canonical_digest(b)


def test_transport_rejects_unknown_sources():
    export = json.loads((ROOT / "tests/fixtures/daily_transport_asia.json").read_text(encoding="utf-8"))
    export["records"][0]["cities"][0]["weather"]["pre_open"]["metrics"]["aqi"]["s"] = ["missing-source"]
    errors = validate_transport(export)
    assert any("unknown sources" in error for error in errors)


def test_transport_rejects_forecast_as_historical_observation():
    export = json.loads((ROOT / "tests/fixtures/daily_transport_asia.json").read_text(encoding="utf-8"))
    export["records"][0]["cities"][0]["weather"]["pre_open"]["metrics"]["aqi"]["t"] = "forecast"
    errors = validate_transport(export)
    assert any("forecast cannot be imported" in error for error in errors)
