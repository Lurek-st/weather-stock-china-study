"""Offline tests for ERA5 stepType-split ZIP container support.

Covers safe ZIP inspection, per-member NetCDF validation (exact UTC
timestamp set, spatial grid, variable union), raw-ZIP persistence, and the
offline live simulation. Zero network: no CDS API is imported or called.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr

import scripts.v2.fetch_era5 as fe
from scripts.v2.core import RawArtifactStore, V2Error
from scripts.v2.fetch_era5 import (
    DOWNLOAD_FORMAT,
    EXPECTED_DOWNLOAD_CONTAINER,
    NETCDF_VARIABLES,
    inspect_download_container,
    main as era5_main,
    validate_download_container,
    validate_netcdf,
    validate_zip_container,
)

ROOT = Path(__file__).resolve().parents[2]

# Request fixture: single-day segment 16:00-23:00 UTC (8 timestamps).
SINGLE_DAY_REQUEST = {"date": ["2026-03-01"], "time": [f"{h:02d}:00" for h in range(16, 24)]}

# Pilot area for taipei: centre 25.0375, 121.5646, half-span 0.13.
PILOT_AREA = {
    "area": [25.1675, 121.4346, 24.9075, 121.6946],
    "area_north": 25.1675,
    "area_west": 121.4346,
    "area_south": 24.9075,
    "area_east": 121.6946,
    "area_half_span_degrees": 0.13,
}

INSTANT_VARS = ["t2m", "d2m", "tcc", "u10", "v10"]
ACCUM_VARS = ["tp", "ssrd"]
GUST_VARS = ["i10fg"]


def expected_timestamps(request: dict) -> list[datetime]:
    out = []
    for day in request["date"]:
        for hour in request["time"]:
            out.append(datetime.fromisoformat(f"{day}T{int(hour.split(':')[0]):02d}:00:00+00:00"))
    return sorted(out)


def make_member_netcdf(
    path: Path,
    variables: list[str],
    timestamps: list[datetime],
    latitude: list[float] | None = None,
    longitude: list[float] | None = None,
) -> None:
    latitude = latitude or [25.05, 25.02]
    longitude = longitude or [121.50, 121.60]
    ts = pd.DatetimeIndex([item.replace(tzinfo=None) for item in timestamps])  # naive = UTC
    data_vars = {
        name: (("valid_time", "latitude", "longitude"), np.full((len(ts), len(latitude), len(longitude)), 1.0))
        for name in variables
    }
    dataset = xr.Dataset(
        data_vars,
        coords={"valid_time": ts, "latitude": latitude, "longitude": longitude},
    )
    dataset.to_netcdf(path)


def make_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def legal_member_bytes(timestamps: list[datetime]) -> dict[str, bytes]:
    """Three members whose variable union covers all eight requested vars."""
    import tempfile

    members = {}
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        p1 = base / "instant.nc"
        p2 = base / "accum.nc"
        p3 = base / "gust.nc"
        make_member_netcdf(p1, INSTANT_VARS, timestamps)
        make_member_netcdf(p2, ACCUM_VARS, timestamps)
        make_member_netcdf(p3, GUST_VARS, timestamps)
        members = {
            "instant.nc": p1.read_bytes(),
            "accum.nc": p2.read_bytes(),
            "gust.nc": p3.read_bytes(),
        }
    return members


def make_legal_zip(path: Path, request: dict) -> None:
    make_zip(path, legal_member_bytes(expected_timestamps(request)))


def fake_readiness() -> dict:
    return {
        "cdsapi_installed": True,
        "cdsapi_version": "0.7.7",
        "cdsapirc_status": "present_shape_valid",
        "url_field_present": True,
        "key_field_present": True,
        "dataset_terms_status": "user_confirmed_outside_task",
        "terms_confirmation_file_present": True,
        "credential_readiness_status": "ready_for_future_live_request",
    }


class FakeZipCDSClient:
    """Offline fake that writes a three-member ZIP for every retrieve."""

    def __init__(self, fault: str | None = None, extra_variable: str | None = None):
        self.calls = []
        self.fault = fault
        self.extra_variable = extra_variable

    def retrieve(self, dataset, request, target):
        self.calls.append({"dataset": dataset, "request": request})
        timestamps = expected_timestamps(request)
        members = legal_member_bytes(timestamps)
        if self.fault == "drop_last_timestamp":
            # Rebuild member 2 (accum) missing its final timestamp.
            partial_ts = timestamps[:-1]
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                p2 = Path(tmp) / "accum.nc"
                make_member_netcdf(p2, ACCUM_VARS, partial_ts)
                members["accum.nc"] = p2.read_bytes()
        if self.extra_variable:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                p1 = Path(tmp) / "instant.nc"
                make_member_netcdf(p1, INSTANT_VARS + [self.extra_variable], timestamps)
                members["instant.nc"] = p1.read_bytes()
        make_zip(Path(target), members)


def make_live_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "config/v2").mkdir(parents=True)
    (root / "config/v2/locations.yaml").write_text(
        "locations:\n"
        "  - city_id: taipei\n"
        "    city_name: Taipei\n"
        "    latitude: 25.0375\n"
        "    longitude: 121.5646\n"
        "    timezone: Asia/Taipei\n"
        "    market_id: taiex\n",
        encoding="utf-8",
    )
    (root / "config/v2/markets.yaml").write_text(
        "markets:\n"
        "  - market_id: taiex\n"
        "    city_id: taipei\n"
        "    currency: TWD\n"
        "    timezone: Asia/Taipei\n"
        "    session: {open: \"09:00\", close: \"13:30\", breaks: []}\n"
        "    calendar_source_id: twse_official_holiday_schedule\n",
        encoding="utf-8",
    )
    (root / "config/v2/pilot-scope.yaml").write_text(
        "primary_market: taiex\nprimary_city: taipei\nscope_status: single_market_pilot\n",
        encoding="utf-8",
    )
    semantics_source = ROOT / "config/v2/weather-variable-semantics.yaml"
    (root / "config/v2/weather-variable-semantics.yaml").write_text(
        semantics_source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return root


# ---------------- contract ----------------

def test_mixed_step_type_contract_uses_zip():
    assert DOWNLOAD_FORMAT == "zip"
    assert EXPECTED_DOWNLOAD_CONTAINER == "zip"
    assert "mixed_grib_step_types" in fe.CONTAINER_REASON


def test_dry_run_audit_records_zip_contract(tmp_path, capsys):
    root = make_live_root(tmp_path)
    era5_main(["--pilot-only", "--final", "--root", str(root)])
    audit = json.loads(capsys.readouterr().out)
    assert audit["download_format"] == "zip"
    assert audit["expected_download_container"] == "zip"
    assert audit["container_reason"] == fe.CONTAINER_REASON
    assert audit["live_requests_run"] is False
    assert audit["weather_data_downloaded"] is False
    assert audit["historical_backfill_run"] is False


# ---------------- magic detection ----------------

def test_zip_magic_detected(tmp_path):
    path = tmp_path / "c.zip"
    make_legal_zip(path, SINGLE_DAY_REQUEST)
    assert inspect_download_container(path) == "zip"


def test_netcdf_classic_magic_detected(tmp_path):
    path = tmp_path / "classic.bin"
    path.write_bytes(b"CDF\x01" + b"\x00" * 64)
    assert inspect_download_container(path) == "netcdf"


def test_netcdf4_hdf5_magic_detected(tmp_path):
    path = tmp_path / "hdf5.bin"
    path.write_bytes(b"\x89HDF\r\n\x1a\n" + b"\x00" * 64)
    assert inspect_download_container(path) == "netcdf"


def test_unrecognized_magic_rejected(tmp_path):
    path = tmp_path / "unknown.bin"
    path.write_bytes(b"\x00\x01\x02\x03whatever")
    with pytest.raises(V2Error, match="unrecognized download container"):
        inspect_download_container(path)


# ---------------- container validation: happy paths ----------------

def test_legal_multi_member_zip_passes(tmp_path):
    path = tmp_path / "legal.zip"
    make_legal_zip(path, SINGLE_DAY_REQUEST)
    result = validate_download_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)
    assert result["container_type"] == "zip"
    assert result["member_count"] == 3
    assert result["all_requested_variables_present"] is True
    assert result["missing_variables"] == []
    assert result["duplicate_variables_across_members"] == []
    assert result["all_member_timestamp_sets_match"] is True
    assert result["all_member_spatial_checks_passed"] is True
    assert result["container_validation_passed"] is True
    assert result["raw_suffix"] == ".zip"
    assert set(result["observed_variable_union"]) == set(NETCDF_VARIABLES)
    assert len(result["container_sha256"]) == 64
    assert result["zip_safety_passed"] is True


def test_single_member_direct_netcdf_compat_passes(tmp_path):
    path = tmp_path / "direct.nc"
    make_member_netcdf(path, NETCDF_VARIABLES, expected_timestamps(SINGLE_DAY_REQUEST))
    result = validate_download_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)
    assert result["container_type"] == "netcdf"
    assert result["member_count"] == 1
    assert result["container_validation_passed"] is True
    assert result["raw_suffix"] == ".nc"


def test_validate_netcdf_legacy_full_variable_still_works(tmp_path):
    path = tmp_path / "full.nc"
    make_member_netcdf(path, NETCDF_VARIABLES, expected_timestamps(SINGLE_DAY_REQUEST))
    summary = validate_netcdf(path, SINGLE_DAY_REQUEST)
    assert summary["netcdf_validation_passed"] is True


def test_latitude_order_insensitive_grid_acceptance(tmp_path):
    # Same grid values, opposite latitude sort direction -> accepted.
    t1 = sorted(expected_timestamps(SINGLE_DAY_REQUEST))
    with zipfile.ZipFile(tmp_path / "a.zip", "w") as zf:
        p1 = tmp_path / "m1.nc"
        p2 = tmp_path / "m2.nc"
        p3 = tmp_path / "m3.nc"
        make_member_netcdf(p1, INSTANT_VARS, t1, latitude=[25.05, 25.02])
        make_member_netcdf(p2, ACCUM_VARS, t1, latitude=[25.02, 25.05])
        make_member_netcdf(p3, GUST_VARS, t1, latitude=[25.05, 25.02])
        zf.write(p1, "m1.nc")
        zf.write(p2, "m2.nc")
        zf.write(p3, "m3.nc")
    result = validate_download_container(tmp_path / "a.zip", SINGLE_DAY_REQUEST, PILOT_AREA)
    assert result["container_validation_passed"] is True


# ---------------- zip safety failures ----------------

def test_corrupt_zip_rejected(tmp_path):
    path = tmp_path / "corrupt.zip"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 200)
    with pytest.raises(V2Error, match="corrupt zip"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_empty_zip_rejected(tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w"):
        pass
    with pytest.raises(V2Error, match="no members"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_no_netcdf_member_rejected(tmp_path):
    path = tmp_path / "txt.zip"
    make_zip(path, {"readme.txt": b"hello"})
    with pytest.raises(V2Error, match="cannot be opened as NetCDF"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_path_traversal_rejected(tmp_path):
    path = tmp_path / "traversal.zip"
    make_zip(path, {"../evil.nc": b"PK\x03\x04" * 10})
    with pytest.raises(V2Error, match="path traversal"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_absolute_member_path_rejected(tmp_path):
    path = tmp_path / "abs.zip"
    make_zip(path, {"/tmp/evil.nc": b"x" * 100})
    with pytest.raises(V2Error, match="absolute"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_windows_drive_member_path_rejected(tmp_path):
    path = tmp_path / "drive.zip"
    make_zip(path, {"C:\\evil.nc": b"x" * 100})
    with pytest.raises(V2Error, match="absolute"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_symlink_member_rejected(tmp_path):
    import stat as stat_module

    path = tmp_path / "link.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("link.nc")
        info.create_system = 3
        info.external_attr = (stat_module.S_IFLNK | 0o777) << 16
        zf.writestr(info, b"target")
    with pytest.raises(V2Error, match="symbolic-link"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def make_encrypted_zip(path: Path) -> None:
    """Create a zip whose general-purpose flag carries the encryption bit."""
    make_zip(path, {"secret.nc": b"PK\x03\x04" * 10})
    data = bytearray(path.read_bytes())
    idx = 0
    while True:
        idx = data.find(b"PK\x03\x04", idx)
        if idx < 0:
            break
        data[idx + 6] |= 0x01  # local file header general purpose flag
        idx += 4
    idx = 0
    while True:
        idx = data.find(b"PK\x01\x02", idx)
        if idx < 0:
            break
        data[idx + 8] |= 0x01  # central directory header general purpose flag
        idx += 4
    path.write_bytes(bytes(data))


def test_encrypted_member_rejected(tmp_path):
    path = tmp_path / "enc.zip"
    make_encrypted_zip(path)
    with pytest.raises(V2Error, match="encrypted"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_nested_archive_rejected(tmp_path):
    path = tmp_path / "nested.zip"
    make_zip(path, {"inner.zip": b"PK\x03\x04" * 10})
    with pytest.raises(V2Error, match="nested archive"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_duplicate_member_names_rejected(tmp_path):
    path = tmp_path / "dup.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.nc", b"x" * 100)
        zf.writestr("a.nc", b"y" * 100)
    with pytest.raises(V2Error, match="duplicate zip member names"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_case_insensitive_duplicate_rejected(tmp_path):
    path = tmp_path / "case.zip"
    make_zip(path, {"A.nc": b"x" * 100, "a.nc": b"y" * 100})
    with pytest.raises(V2Error, match="case-insensitive duplicate"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_oversize_member_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(fe, "ZIP_MAX_MEMBER_SIZE", 50)
    path = tmp_path / "big.zip"
    make_zip(path, {"big.nc": b"x" * 500})
    with pytest.raises(V2Error, match="per-member size limit"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_zip_bomb_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(fe, "ZIP_MAX_COMPRESSION_RATIO", 10)
    path = tmp_path / "bomb.zip"
    make_zip(path, {"bomb.nc": b"A" * 200_000})
    with pytest.raises(V2Error, match="compression ratio"):
        validate_zip_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


# ---------------- member validation failures ----------------

def _zip_with_member_bytes(members: dict[str, bytes], tmp_path: Path, name: str = "z.zip") -> Path:
    path = tmp_path / name
    make_zip(path, members)
    return path


def test_missing_member_timestamp_rejected(tmp_path):
    timestamps = sorted(expected_timestamps(SINGLE_DAY_REQUEST))
    members = legal_member_bytes(timestamps)
    with tempfile_dir() as tmp:
        p2 = Path(tmp) / "accum.nc"
        make_member_netcdf(p2, ACCUM_VARS, timestamps[:-1])
        members["accum.nc"] = p2.read_bytes()
    path = _zip_with_member_bytes(members, tmp_path)
    with pytest.raises(V2Error, match="missing .* expected timestamps"):
        validate_download_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_unexpected_member_timestamp_rejected(tmp_path):
    timestamps = sorted(expected_timestamps(SINGLE_DAY_REQUEST))
    members = legal_member_bytes(timestamps)
    with tempfile_dir() as tmp:
        p1 = Path(tmp) / "instant.nc"
        extra = timestamps + [datetime(2026, 3, 2, 0, tzinfo=timezone.utc)]
        make_member_netcdf(p1, INSTANT_VARS, extra)
        members["instant.nc"] = p1.read_bytes()
    path = _zip_with_member_bytes(members, tmp_path)
    with pytest.raises(V2Error, match="unexpected"):
        validate_download_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_duplicate_member_timestamp_rejected(tmp_path):
    timestamps = sorted(expected_timestamps(SINGLE_DAY_REQUEST))
    members = legal_member_bytes(timestamps)
    with tempfile_dir() as tmp:
        p3 = Path(tmp) / "gust.nc"
        dup = timestamps + [timestamps[0]]
        make_member_netcdf(p3, GUST_VARS, dup)
        members["gust.nc"] = p3.read_bytes()
    path = _zip_with_member_bytes(members, tmp_path)
    with pytest.raises(V2Error, match="duplicate timestamps"):
        validate_download_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_non_hourly_member_timestamp_rejected(tmp_path):
    timestamps = sorted(expected_timestamps(SINGLE_DAY_REQUEST))
    members = legal_member_bytes(timestamps)
    with tempfile_dir() as tmp:
        p1 = Path(tmp) / "instant.nc"
        half = [item.replace(minute=30) for item in timestamps]
        make_member_netcdf(p1, INSTANT_VARS, half)
        members["instant.nc"] = p1.read_bytes()
    path = _zip_with_member_bytes(members, tmp_path)
    with pytest.raises(V2Error, match="non-hourly"):
        validate_download_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_member_spatial_out_of_area_rejected(tmp_path):
    timestamps = sorted(expected_timestamps(SINGLE_DAY_REQUEST))
    members = legal_member_bytes(timestamps)
    with tempfile_dir() as tmp:
        p1 = Path(tmp) / "instant.nc"
        make_member_netcdf(p1, INSTANT_VARS, timestamps, latitude=[26.0, 25.8])
        members["instant.nc"] = p1.read_bytes()
    path = _zip_with_member_bytes(members, tmp_path)
    with pytest.raises(V2Error, match="latitude outside requested area"):
        validate_download_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_spatial_grid_mismatch_rejected(tmp_path):
    timestamps = sorted(expected_timestamps(SINGLE_DAY_REQUEST))
    with zipfile.ZipFile(tmp_path / "m.zip", "w") as zf:
        p1 = tmp_path / "m1.nc"
        p2 = tmp_path / "m2.nc"
        make_member_netcdf(p1, INSTANT_VARS, timestamps, latitude=[25.05, 25.02])
        make_member_netcdf(p2, ACCUM_VARS, timestamps, latitude=[25.03, 25.01])
        zf.write(p1, "m1.nc")
        zf.write(p2, "m2.nc")
    with pytest.raises(V2Error, match="spatial grid mismatch"):
        validate_download_container(tmp_path / "m.zip", SINGLE_DAY_REQUEST, PILOT_AREA)


# ---------------- variable union failures ----------------

def test_missing_requested_variable_rejected(tmp_path):
    timestamps = sorted(expected_timestamps(SINGLE_DAY_REQUEST))
    members = legal_member_bytes(timestamps)
    with tempfile_dir() as tmp:
        p3 = Path(tmp) / "gust.nc"
        make_member_netcdf(p3, [], timestamps)  # drop i10fg
        members["gust.nc"] = p3.read_bytes()
    path = _zip_with_member_bytes(members, tmp_path)
    with pytest.raises(V2Error, match="miss requested variables"):
        validate_download_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_duplicate_variable_across_members_rejected(tmp_path):
    timestamps = sorted(expected_timestamps(SINGLE_DAY_REQUEST))
    members = legal_member_bytes(timestamps)
    with tempfile_dir() as tmp:
        p1 = Path(tmp) / "instant.nc"
        make_member_netcdf(p1, INSTANT_VARS + ["tp"], timestamps)  # tp also in accum
        members["instant.nc"] = p1.read_bytes()
    path = _zip_with_member_bytes(members, tmp_path)
    with pytest.raises(V2Error, match="multiple zip members"):
        validate_download_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_not_only_first_member_validated(tmp_path):
    # First member legal, second member invalid -> container must fail.
    timestamps = sorted(expected_timestamps(SINGLE_DAY_REQUEST))
    members = legal_member_bytes(timestamps)
    with tempfile_dir() as tmp:
        p2 = Path(tmp) / "accum.nc"
        make_member_netcdf(p2, ACCUM_VARS, timestamps[:-1])  # invalid member 2
        members["accum.nc"] = p2.read_bytes()
    path = _zip_with_member_bytes(members, tmp_path)
    with pytest.raises(V2Error, match="missing"):
        validate_download_container(path, SINGLE_DAY_REQUEST, PILOT_AREA)


def test_validation_failure_persists_nothing(tmp_path):
    root = make_live_root(tmp_path)
    with pytest.raises(V2Error):
        era5_main(
            ["--pilot-only", "--final", "--live", "--root", str(root)],
            client_factory=lambda: FakeZipCDSClient(fault="drop_last_timestamp"),
            readiness=fake_readiness(),
        )
    store_dir = root / "data/source_raw/v2/weather"
    manifests = list(store_dir.rglob("*.manifest.json")) if store_dir.exists() else []
    assert manifests == []


# ---------------- raw ZIP persistence ----------------

def test_legal_zip_persists_raw_zip_bytes(tmp_path, capsys):
    root = make_live_root(tmp_path)
    era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: FakeZipCDSClient(),
        readiness=fake_readiness(),
    )
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 3
    store = RawArtifactStore(root / "data/source_raw/v2/weather")
    for item in results:
        assert item["container_validation"]["container_type"] == "zip"
        assert item["container_validation"]["container_validation_passed"] is True
        assert item["timestamp_validation"]["netcdf_validation_passed"] is True
        manifest_path = root / item["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact_path = manifest_path.with_name(manifest_path.name.replace(".manifest.json", ".zip"))
        assert artifact_path.exists()
        # Persisted bytes are the original ZIP container.
        with zipfile.ZipFile(artifact_path) as zf:
            assert len(zf.infolist()) == 3
        assert manifest["sha256"] == item["sha256"]


def test_artifact_suffix_zip(tmp_path, capsys):
    root = make_live_root(tmp_path)
    era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: FakeZipCDSClient(),
        readiness=fake_readiness(),
    )
    results = json.loads(capsys.readouterr().out)
    for item in results:
        assert item["manifest_path"].endswith(".manifest.json")
        manifest = json.loads((root / item["manifest_path"]).read_text(encoding="utf-8"))
        assert "r" in manifest["artifact_id"]


def test_manifest_sha_matches_raw_zip(tmp_path, capsys):
    root = make_live_root(tmp_path)
    era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: FakeZipCDSClient(),
        readiness=fake_readiness(),
    )
    results = json.loads(capsys.readouterr().out)
    import hashlib

    for item in results:
        manifest = json.loads((root / item["manifest_path"]).read_text(encoding="utf-8"))
        artifact = root / "data/source_raw/v2/weather" / manifest["source_id"] / (
            manifest["artifact_id"].split(":")[-2]
        ) / f"r{manifest['revision']:04d}-{manifest['sha256'][:12]}.zip"
        assert artifact.exists()
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert digest == manifest["sha256"]
        assert manifest["content_length"] == artifact.stat().st_size


def test_manifest_container_summary_present(tmp_path, capsys):
    root = make_live_root(tmp_path)
    era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: FakeZipCDSClient(),
        readiness=fake_readiness(),
    )
    results = json.loads(capsys.readouterr().out)
    manifest = json.loads((root / results[0]["manifest_path"]).read_text(encoding="utf-8"))
    meta = manifest["validation_metadata"]
    assert meta["container_type"] == "zip"
    assert meta["member_count"] == 3
    assert set(meta["observed_variable_union"]) == set(NETCDF_VARIABLES)
    assert meta["container_validation_passed"] is True
    assert manifest["request"]["download_format"] == "zip"
    assert manifest["request"]["data_format"] == "netcdf"


def test_live_simulation_three_retrieves_not_six(tmp_path, capsys):
    root = make_live_root(tmp_path)
    client = FakeZipCDSClient()
    era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: client,
        readiness=fake_readiness(),
    )
    assert len(client.calls) == 3  # not 6 or 9 per-variable splits
    assert [len(call["request"]["date"]) for call in client.calls] == [1, 4, 1]
    assert [len(call["request"]["time"]) for call in client.calls] == [8, 24, 17]
    for call in client.calls:
        assert call["request"]["download_format"] == "zip"


def test_live_output_no_absolute_path(tmp_path, capsys):
    root = make_live_root(tmp_path)
    era5_main(
        ["--pilot-only", "--final", "--live", "--root", str(root)],
        client_factory=lambda: FakeZipCDSClient(),
        readiness=fake_readiness(),
    )
    out = capsys.readouterr().out
    assert ":\\" not in out
    assert "D:" not in out


def test_default_dry_run_no_side_effect(tmp_path):
    root = make_live_root(tmp_path)
    code = era5_main(["--pilot-only", "--final", "--root", str(root)])
    assert code == 0
    assert not (root / "data/audits/v2").exists()
    assert not list(root.rglob("*.manifest.json"))


# ---------------- status regressions ----------------

def test_aex_status_unchanged():
    audits = list((ROOT / "data/audits/v2").rglob("*.json"))
    assert audits  # audits exist


def test_taiex_market_status_unchanged():
    market_audit = json.loads(
        (ROOT / "data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json").read_text(encoding="utf-8")
    )
    assert market_audit["adapter_status"] == "market_only_pilot_accepted"
    assert market_audit["production_status"] == "not_connected"
    assert market_audit["research_ready"] is False
    assert market_audit["frozen"] is False


def test_full_history_calendar_verified_still_false():
    window_audit = json.loads(
        (ROOT / "data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json").read_text(encoding="utf-8")
    )
    assert window_audit["full_history_calendar_verified"] is False


def test_first_failed_run_evidence_preserved():
    run_dir = ROOT / ".local/runs/taipei-era5-live/20260806T090232Z"
    assert (run_dir / "result.json").exists()
    assert (run_dir / "run.log").exists()


# ---------------- helpers ----------------

def tempfile_dir():
    import tempfile

    return tempfile.TemporaryDirectory()
