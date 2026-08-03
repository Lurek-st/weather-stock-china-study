from pathlib import Path

from scripts.validate_records import validate_file

ROOT = Path(__file__).resolve().parents[1]


def test_valid_fixture_passes():
    errors = validate_file(
        ROOT / "tests" / "fixtures" / "valid_asia.json",
        ROOT / "schemas" / "regional-daily-record-v1.schema.json",
    )
    assert errors == []
