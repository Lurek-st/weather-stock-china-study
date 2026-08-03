import copy
import json
import subprocess
import sys
from pathlib import Path

from scripts.api_dependency_scan import detect_active_openai_dependencies
from scripts.import_weekly_package import safe_source_label
from scripts.weekly_package import (
    PackageError,
    manifest_consistency_errors,
    parse_weekly_envelope,
    validate_transport,
)

ROOT = Path(__file__).resolve().parents[1]
IMPORTER = ROOT / "scripts" / "import_weekly_package.py"


def daily_export():
    return json.loads(
        (ROOT / "tests" / "fixtures" / "daily_transport_asia.json").read_text(
            encoding="utf-8"
        )
    )


def refs_from(exports):
    values = [
        {"region": record["region"], "target_date": record["target_date"]}
        for export in exports
        for record in export.get("records", [])
    ]
    return list({(item["region"], item["target_date"]): item for item in values}.values())


def weekly_package(
    exports,
    *,
    expected=None,
    status="COMPLETE",
    raw_blocks=None,
    week_id="2026-W32",
    coverage_start="2026-08-03",
    coverage_end="2026-08-09",
    found_override=None,
):
    raw_blocks = raw_blocks or []
    found = found_override if found_override is not None else refs_from(exports)
    expected = expected if expected is not None else list(found)
    unresolved = [item for item in expected if item not in found]
    backfilled = [
        {"region": record["region"], "target_date": record["target_date"]}
        for export in exports
        for record in export.get("records", [])
        if record.get("collection_mode") == "backfill"
    ]

    def counts(values):
        result = {}
        for item in values:
            result[item["region"]] = result.get(item["region"], 0) + 1
        return result

    manifest = {
        "expected": counts(expected),
        "found": counts(found),
        "expected_records": expected,
        "found_records": found,
        "backfilled_records": backfilled,
        "replaced_damaged_records": [],
        "unresolved_records": unresolved,
        "duplicates_removed": [],
        "source_conflicts": [],
        "daily_export_count": len(exports) + len(raw_blocks),
    }
    blocks = [
        "BEGIN_DAILY_WEATHER_MARKET_EXPORT\n"
        + json.dumps(export, ensure_ascii=False, separators=(",", ":"))
        + "\nEND_DAILY_WEATHER_MARKET_EXPORT"
        for export in exports
    ]
    blocks.extend(
        "BEGIN_DAILY_WEATHER_MARKET_EXPORT\n"
        + raw
        + "\nEND_DAILY_WEATHER_MARKET_EXPORT"
        for raw in raw_blocks
    )
    return (
        "BEGIN_CODEX_WEEKLY_IMPORT_PACKAGE\n"
        "PACKAGE_VERSION: 1.0.2\n"
        f"WEEK_ID: {week_id}\n"
        f"COVERAGE_START: {coverage_start}\n"
        f"COVERAGE_END: {coverage_end}\n"
        "GENERATED_AT: 2026-08-10T07:00:00+08:00\n"
        f"STATUS: {status}\n\n"
        "WEEKLY_MANIFEST_JSON:\n"
        + json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
        + "\n\n".join(blocks)
        + "\nEND_CODEX_WEEKLY_IMPORT_PACKAGE\n"
    )


def run_import(tmp_path, text, *extra):
    repo = tmp_path / "repo"
    package = tmp_path / "outside" / "weekly.md"
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_text(text, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(IMPORTER),
            str(package),
            "--repo-root",
            str(repo),
            *extra,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return repo, result, json.loads(result.stdout)


def test_api_detector_ignores_documentation_and_itself(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "detector.py").write_text(
        'WORDS = ["OPENAI_API_KEY", "api.openai.com", "OpenAI"]\n',
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "Do not use OPENAI_API_KEY or the OpenAI Responses API.", encoding="utf-8"
    )
    assert detect_active_openai_dependencies(tmp_path) == []
    assert detect_active_openai_dependencies(ROOT) == []


def test_api_detector_finds_environment_read(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "collector.py").write_text(
        'import os\nkey = os.getenv("OPENAI_API_KEY")\n', encoding="utf-8"
    )
    findings = detect_active_openai_dependencies(tmp_path)
    assert findings == [
        {
            "path": "scripts/collector.py",
            "reason": "reads OPENAI_API_KEY from environment",
        }
    ]


def test_api_detector_finds_workflow_secret(tmp_path):
    workflow = tmp_path / ".github" / "workflows" / "collect.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  collect:\n    steps:\n      - run: python collect.py\n"
        "        env:\n          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}\n",
        encoding="utf-8",
    )
    findings = detect_active_openai_dependencies(tmp_path)
    assert any("secrets.OPENAI_API_KEY" in item["reason"] for item in findings)


def test_source_label_redacts_windows_and_unix_absolute_paths(tmp_path):
    repo = tmp_path / "repo"
    assert (
        safe_source_label(Path(r"C:\Users\Example\Downloads\weekly.md"), repo)
        == "weekly.md"
    )
    assert safe_source_label(Path("/home/example/weekly.md"), repo) == "weekly.md"


def test_week_id_must_match_complete_natural_week():
    text = weekly_package([daily_export()], week_id="2026-W31")
    try:
        parse_weekly_envelope(text)
    except PackageError as exc:
        assert "does not match coverage week" in str(exc)
    else:
        raise AssertionError("mismatched WEEK_ID was accepted")


def test_manifest_found_must_match_actual_records():
    export = daily_export()
    wrong = [{"region": "europe", "target_date": "2026-08-03"}]
    text = weekly_package([export], expected=wrong, found_override=wrong)
    envelope = parse_weekly_envelope(text)
    errors = manifest_consistency_errors(envelope, [export], 1)
    assert any("found_records does not match" in error for error in errors)


def test_checked_calendar_requires_known_source():
    export = daily_export()
    export["records"][0]["markets"][0]["session"]["calendar_source_ids"] = []
    errors = validate_transport(export)
    assert any("requires calendar_source_ids" in error for error in errors)


def test_complete_weekly_import_and_safe_source_path(tmp_path):
    repo, result, report = run_import(tmp_path, weekly_package([daily_export()]))
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["import_status"] == "complete"
    assert report["source_package"] == "inbox/raw/2026-W32.md"
    assert "Users" not in json.dumps(report)
    assert (repo / "inbox/processed/2026-W32.md").exists()
    assert (repo / "data/raw/asia/2026-08-03.json").exists()


def test_incomplete_package_partially_imports_and_builds_backfill(tmp_path):
    expected = [
        {"region": "asia", "target_date": "2026-08-03"},
        {"region": "europe", "target_date": "2026-08-03"},
    ]
    repo, result, report = run_import(
        tmp_path,
        weekly_package(
            [daily_export()],
            expected=expected,
            status="WEEKLY_AGGREGATION_INCOMPLETE",
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["import_status"] == "partial"
    assert (repo / "inbox/partial/2026-W32.md").exists()
    assert not (repo / "inbox/processed/2026-W32.md").exists()
    assert (repo / "analysis/backfill_requests/2026-W32.json").exists()
    assert (repo / "data/raw/asia/2026-08-03.json").exists()


def test_damaged_json_does_not_discard_valid_block(tmp_path):
    expected = [
        {"region": "asia", "target_date": "2026-08-03"},
        {"region": "europe", "target_date": "2026-08-03"},
    ]
    repo, result, report = run_import(
        tmp_path,
        weekly_package(
            [daily_export()],
            expected=expected,
            status="WEEKLY_AGGREGATION_INCOMPLETE",
            raw_blocks=['{"broken":'],
        ),
    )
    assert result.returncode == 1
    assert report["import_status"] == "partial"
    assert (repo / "data/raw/asia/2026-08-03.json").exists()
    assert any(item["stage"] == "daily_json_parse" for item in report["rejected"])
    for item in report["rejected"]:
        assert (repo / item["rejection_path"]).exists()


def test_transport_failure_is_retained(tmp_path):
    export = daily_export()
    export["records"][0]["cities"][0]["weather"]["pre_open"]["metrics"]["aqi"][
        "s"
    ] = ["missing-source"]
    repo, result, report = run_import(tmp_path, weekly_package([export]))
    assert result.returncode == 1
    assert not (repo / "data/raw/asia/2026-08-03.json").exists()
    assert any(item["stage"] == "transport_validation" for item in report["rejected"])
    assert all((repo / item["rejection_path"]).exists() for item in report["rejected"])


def test_repeated_rejection_does_not_overwrite_prior_evidence(tmp_path):
    export = daily_export()
    export["records"][0]["markets"][0]["metrics"]["close"]["s"] = ["missing-source"]
    text = weekly_package([export])
    repo, first, first_report = run_import(tmp_path, text)
    assert first.returncode == 1
    package = tmp_path / "outside" / "weekly.md"
    second = subprocess.run(
        [
            sys.executable,
            str(IMPORTER),
            str(package),
            "--repo-root",
            str(repo),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    second_report = json.loads(second.stdout)
    assert second.returncode == 1
    first_paths = {item["rejection_path"] for item in first_report["rejected"]}
    second_paths = {item["rejection_path"] for item in second_report["rejected"]}
    assert first_paths.isdisjoint(second_paths)
    assert all((repo / path).exists() for path in first_paths | second_paths)


def test_duplicate_region_date_is_rejected_and_retained(tmp_path):
    first = daily_export()
    second = copy.deepcopy(first)
    second["run_id"] = "fixture-global-2026-08-04-duplicate"
    repo, result, report = run_import(tmp_path, weekly_package([first, second]))
    assert result.returncode == 1
    assert (repo / "data/raw/asia/2026-08-03.json").exists()
    assert any(item["stage"] == "weekly_duplicate" for item in report["rejected"])


def test_identical_reimport_is_idempotent(tmp_path):
    text = weekly_package([daily_export()])
    repo, first, _ = run_import(tmp_path, text)
    assert first.returncode == 0
    package = tmp_path / "outside" / "weekly.md"
    second = subprocess.run(
        [
            sys.executable,
            str(IMPORTER),
            str(package),
            "--repo-root",
            str(repo),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    report = json.loads(second.stdout)
    assert second.returncode == 0
    assert report["skipped_identical"] == ["asia/2026-08-03"]


def test_conflict_rejected_then_explicit_revision_allowed(tmp_path):
    original = weekly_package([daily_export()])
    repo, first, _ = run_import(tmp_path, original)
    assert first.returncode == 0
    changed = daily_export()
    metric = changed["records"][0]["markets"][0]["metrics"]["close"]
    metric["v"] += 1
    package = tmp_path / "outside" / "changed.md"
    package.write_text(weekly_package([changed]), encoding="utf-8")
    command = [
        sys.executable,
        str(IMPORTER),
        str(package),
        "--repo-root",
        str(repo),
    ]
    conflict = subprocess.run(command, text=True, capture_output=True, check=False)
    assert conflict.returncode == 1
    assert any(
        item["stage"] == "canonical_conflict"
        for item in json.loads(conflict.stdout)["rejected"]
    )
    revised = subprocess.run(
        [*command, "--allow-revision"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert revised.returncode == 0, revised.stdout + revised.stderr
    record = json.loads(
        (repo / "data/raw/asia/2026-08-03.json").read_text(encoding="utf-8")
    )
    assert record["revision"] == 2
    assert record["supersedes_revision"] == 1


def test_damaged_existing_record_is_retained_as_rejection(tmp_path):
    text = weekly_package([daily_export()])
    repo, first, _ = run_import(tmp_path, text)
    assert first.returncode == 0
    (repo / "data/raw/asia/2026-08-03.json").write_text(
        "{broken", encoding="utf-8"
    )
    package = tmp_path / "outside" / "weekly.md"
    result = subprocess.run(
        [
            sys.executable,
            str(IMPORTER),
            str(package),
            "--repo-root",
            str(repo),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert any(item["stage"] == "existing_record" for item in report["rejected"])


def test_out_of_range_record_is_not_imported(tmp_path):
    export = daily_export()
    export["records"][0]["target_date"] = "2026-08-10"
    expected = [{"region": "asia", "target_date": "2026-08-10"}]
    repo, result, report = run_import(
        tmp_path, weekly_package([export], expected=expected)
    )
    assert result.returncode == 1
    assert not (repo / "data/raw/asia/2026-08-10.json").exists()
    assert any(item["stage"] == "coverage_validation" for item in report["rejected"])


def test_five_day_import_panel_and_acceptance_report(tmp_path):
    exports = []
    expected = []
    for offset, target_date in enumerate(
        ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
    ):
        export = daily_export()
        export["run_id"] = f"fixture-global-{target_date}-{offset}"
        export["generated_at"] = f"{target_date}T23:00:00+08:00"
        export["records"][0]["target_date"] = target_date
        exports.append(export)
        expected.append({"region": "asia", "target_date": target_date})
    repo, result, report = run_import(
        tmp_path, weekly_package(exports, expected=expected)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(report["accepted"]) == 5

    panel = repo / "data" / "derived" / "daily_panel.csv"
    panel_result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_daily_panel.py"),
            "--input",
            str(repo / "data" / "raw"),
            "--output",
            str(panel),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert panel_result.returncode == 0, panel_result.stdout + panel_result.stderr
    assert panel.exists()

    expected_path = tmp_path / "expected.json"
    expected_path.write_text(
        json.dumps(
            {"asia": [item["target_date"] for item in expected], "europe": [], "us": []}
        ),
        encoding="utf-8",
    )
    acceptance = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "acceptance_report.py"),
            "--start",
            "2026-08-03",
            "--end",
            "2026-08-07",
            "--repo-root",
            str(repo),
            "--expected-json",
            str(expected_path),
            "--output",
            str(repo / "analysis" / "results" / "acceptance.json"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    result_json = json.loads(acceptance.stdout)
    assert acceptance.returncode == 0, acceptance.stdout + acceptance.stderr
    assert result_json["v1_engineering_acceptance_pass"] is True
    assert result_json["metrics"]["active_openai_api_references"] == []
