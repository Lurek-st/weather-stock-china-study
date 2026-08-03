import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_weekly_import_end_to_end(tmp_path):
    package = ROOT / "tests/fixtures/weekly/valid_weekly_package.md"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/import_weekly_package.py"), str(package), "--repo-root", str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    target = tmp_path / "data/raw/asia/2026-08-03.json"
    audit = tmp_path / "data/audits/weekly-import-2026-W32.json"
    processed = tmp_path / "inbox/processed/2026-W32.md"
    assert target.exists()
    assert audit.exists()
    assert processed.exists()
    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["revision"] == 1
    assert record["weather_scoring_version"] == "1.0.0"


def test_weekly_import_is_idempotent(tmp_path):
    package = ROOT / "tests/fixtures/weekly/valid_weekly_package.md"
    command = [sys.executable, str(ROOT / "scripts/import_weekly_package.py"), str(package), "--repo-root", str(tmp_path)]
    first = subprocess.run(command, text=True, capture_output=True)
    second = subprocess.run(command, text=True, capture_output=True)
    assert first.returncode == 0
    assert second.returncode == 0
    report = json.loads(second.stdout)
    assert report["skipped_identical"] == ["asia/2026-08-03"]
