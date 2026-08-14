"""Timezone provider contract + runtime guard (Stage 5B-3R1).

Single source of truth for the frozen IANA tzdb provider/version used by
historical timezone qualification and future climatology.  The runtime guard
fails closed: it refuses to proceed (raises V2Error) when the installed tzdata
version does not exactly match the frozen contract, so a rebuilt environment
cannot silently generate climatology against a different timezone database.
"""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Any

from scripts.v2.core import V2Error, load_yaml, repo_root

PROVIDER_CONTRACT_PATH = "config/v2/timezone-provider.yaml"
_USE_RUNTIME = object()


def load_provider_contract(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    return load_yaml(root / PROVIDER_CONTRACT_PATH)


def runtime_tzdata_version() -> str | None:
    """Installed tzdata version, or None if the package is absent."""
    try:
        return metadata.version("tzdata")
    except Exception:
        return None


def check_provider_runtime(
    root: Path | None = None,
    expected_version: str | None = None,
    actual_version: str | None | object = _USE_RUNTIME,
) -> dict[str, Any]:
    """Compare the frozen contract against the installed runtime (no raise).

    ``expected_version`` / ``actual_version`` overrides exist only for tests
    (e.g. a synthetic version-mismatch case); normally they come from the
    contract file and the installed tzdata package respectively.  ``actual_version
    = None`` means "provider explicitly absent"; the sentinel means "use runtime".
    """
    contract = load_provider_contract(root)
    expected_provider = contract["provider"]
    expected = expected_version if expected_version is not None else contract["version"]
    actual = runtime_tzdata_version() if actual_version is _USE_RUNTIME else actual_version
    provider_present = actual is not None
    return {
        "provider": expected_provider,
        "expected_version": expected,
        "actual_version": actual,
        "provider_installed": provider_present,
        "exact_match": provider_present and actual == expected,
        "qualification_audit": contract.get("qualification_audit"),
    }


def validate_provider_runtime(root: Path | None = None) -> dict[str, Any]:
    """Fail-closed runtime guard.

    Returns the check dict on success; raises V2Error on a version mismatch or
    missing provider, so research-grade climatology cannot proceed silently.
    """
    check = check_provider_runtime(root)
    if not check["provider_installed"]:
        raise V2Error(
            f"timezone provider {check['provider']} not installed; "
            f"expected version {check['expected_version']}"
        )
    if not check["exact_match"]:
        raise V2Error(
            f"timezone provider version mismatch: "
            f"expected {check['provider']}=={check['expected_version']} "
            f"but found {check['actual_version']}"
        )
    return check
