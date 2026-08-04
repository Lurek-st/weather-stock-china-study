from pathlib import Path

from scripts.v2.probes.common import classify


def test_missing_api_key_is_auditable_blocker_not_a_pass():
    values = {"download_allowed": "unclear", "automated_access_allowed": "unclear", "academic_research_allowed": "unclear", "transformation_allowed": "unclear", "public_raw_redistribution_allowed": "unclear", "public_derived_panel_allowed": "unclear"}
    assert classify(values, "unclear", False, False, False) == "technical_access_blocked"


def test_local_probe_cache_is_gitignored():
    root = Path(__file__).resolve().parents[3]
    assert ".local/" in (root / ".gitignore").read_text(encoding="utf-8")
