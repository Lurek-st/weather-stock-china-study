"""Stage 5B-3R1: timezone provider contract + runtime guard tests."""
from __future__ import annotations

import pytest

from scripts.v2.core import V2Error
from scripts.v2.timezone_provider import (
    check_provider_runtime,
    load_provider_contract,
    runtime_tzdata_version,
    validate_provider_runtime,
)


def test_contract_loads_provider_and_version():
    contract = load_provider_contract()
    assert contract["provider"] == "tzdata"
    assert contract["version"] == "2026.3"
    assert contract["qualification_audit"].endswith("historical-timezone-canary-1991-2020.json")


def test_runtime_matches_frozen_version():
    check = check_provider_runtime()
    assert check["provider_installed"] is True
    assert check["actual_version"] == "2026.3"
    assert check["exact_match"] is True


def test_validate_passes_when_matching():
    assert validate_provider_runtime()["exact_match"] is True


def test_synthetic_version_mismatch_fails():
    check = check_provider_runtime(expected_version="2026.3", actual_version="9999.1")
    assert check["exact_match"] is False


def test_missing_provider_detected():
    check = check_provider_runtime(expected_version="2026.3", actual_version=None)
    assert check["provider_installed"] is False
    assert check["exact_match"] is False


def test_validate_fails_closed_on_mismatch(monkeypatch):
    monkeypatch.setattr("scripts.v2.timezone_provider.runtime_tzdata_version", lambda: "9999.1")
    with pytest.raises(V2Error) as exc:
        validate_provider_runtime()
    assert "version mismatch" in str(exc.value)


def test_validate_fails_closed_on_missing(monkeypatch):
    monkeypatch.setattr("scripts.v2.timezone_provider.runtime_tzdata_version", lambda: None)
    with pytest.raises(V2Error) as exc:
        validate_provider_runtime()
    assert "not installed" in str(exc.value)
