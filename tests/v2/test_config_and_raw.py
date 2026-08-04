from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from scripts.v2.core import (
    RawArtifactStore,
    redact_local_path,
    repo_root,
    sha256_bytes,
    validate_registry_files,
)


def copy_registry(tmp_path: Path) -> Path:
    root = repo_root()
    shutil.copytree(root / "config" / "v2", tmp_path / "config" / "v2")
    shutil.copytree(root / "schemas" / "v2", tmp_path / "schemas" / "v2")
    return tmp_path


def mutate_yaml(path: Path, mutate) -> None:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(value)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def test_registries_are_valid():
    assert validate_registry_files(repo_root()) == []


@pytest.mark.parametrize(
    ("filename", "mutate", "needle"),
    [
        (
            "source-registry.yaml",
            lambda d: d["sources"].append(dict(d["sources"][0])),
            "duplicate source_id",
        ),
        (
            "locations.yaml",
            lambda d: d["locations"].append(dict(d["locations"][0])),
            "duplicate city_id",
        ),
        (
            "locations.yaml",
            lambda d: d["locations"][0].update(timezone="Mars/Olympus"),
            "invalid timezone",
        ),
        (
            "locations.yaml",
            lambda d: d["locations"][0].update(latitude=91),
            "locations.yaml",
        ),
        (
            "locations.yaml",
            lambda d: d["locations"][0].update(longitude=181),
            "locations.yaml",
        ),
    ],
)
def test_invalid_registry_fails(tmp_path, filename, mutate, needle):
    root = copy_registry(tmp_path)
    mutate_yaml(root / "config" / "v2" / filename, mutate)
    assert needle in "\n".join(validate_registry_files(root))


def test_undefined_unit_fails(tmp_path):
    root = copy_registry(tmp_path)
    mutate_yaml(
        root / "config" / "v2" / "units.yaml",
        lambda d: d["units"].pop("air_temperature_c"),
    )
    assert "undefined canonical units" in "\n".join(validate_registry_files(root))


def test_undefined_calendar_fails(tmp_path):
    root = copy_registry(tmp_path)
    mutate_yaml(
        root / "config" / "v2" / "markets.yaml",
        lambda d: d["markets"][0].update(calendar_source_id="missing_calendar"),
    )
    assert "undefined calendar" in "\n".join(validate_registry_files(root))


def test_raw_artifact_sha_and_idempotency(tmp_path):
    store = RawArtifactStore(tmp_path)
    kwargs = dict(
        source_id="test_source",
        provider="provider",
        logical_name="2026-W28",
        payload=b"immutable",
        request={"path": "D:\\private\\input.csv"},
        status="final",
        licence="test",
        suffix=".csv",
        retrieved_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    first = store.persist(**kwargs)
    second = store.persist(**kwargs)
    assert first.revision == 1
    assert second.skipped_identical
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["sha256"] == sha256_bytes(b"immutable")
    assert "D:\\" not in json.dumps(manifest)


def test_changed_raw_creates_revision_without_overwrite(tmp_path):
    store = RawArtifactStore(tmp_path)
    base = dict(
        source_id="test_source",
        provider="provider",
        logical_name="logical",
        request={},
        status="provisional",
        licence="test",
        suffix=".bin",
    )
    first = store.persist(payload=b"one", **base)
    second = store.persist(payload=b"two", **base)
    assert (first.revision, second.revision) == (1, 2)
    assert first.artifact_path.read_bytes() == b"one"
    assert second.artifact_path.read_bytes() == b"two"


@pytest.mark.parametrize(
    "value",
    [
        r"C:\Users\Alice\secret.csv",
        r"D:\CodexProjects\private\data.json",
        "/home/alice/secret.csv",
        "/Users/alice/secret.csv",
    ],
)
def test_local_paths_are_redacted(value):
    redacted = redact_local_path(value)
    assert "Alice" not in redacted and "alice" not in redacted
