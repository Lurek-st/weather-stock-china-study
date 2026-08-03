#!/usr/bin/env python3
"""Import one ChatGPT weekly package with complete audit and rejection retention."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .validate_records import validate_record
    from .weekly_package import (
        ROOT,
        canonical_digest,
        convert_region_record,
        extract_daily_blocks,
        flatten_records,
        manifest_consistency_errors,
        parse_daily_exports_tolerant,
        parse_weekly_envelope,
        validate_transport,
        week_id_from_text,
    )
except ImportError:
    from validate_records import validate_record
    from weekly_package import (
        ROOT,
        canonical_digest,
        convert_region_record,
        extract_daily_blocks,
        flatten_records,
        manifest_consistency_errors,
        parse_daily_exports_tolerant,
        parse_weekly_envelope,
        validate_transport,
        week_id_from_text,
    )

CANONICAL_SCHEMA = ROOT / "schemas" / "regional-daily-record-v1.schema.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_source_label(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.name


def _slug(value: str | None, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-.")
    return cleaned or fallback


def preserve_text(path: Path, text: str) -> Path:
    """Never silently overwrite a materially different source package."""
    if not path.exists() or path.read_text(encoding="utf-8") == text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    alternate = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
    counter = 1
    while alternate.exists():
        alternate = path.with_name(f"{path.stem}-{stamp}-{counter}{path.suffix}")
        counter += 1
    alternate.write_text(text, encoding="utf-8")
    return alternate


class RejectionStore:
    def __init__(
        self,
        repo: Path,
        week_id: str,
        source_package: str,
        dry_run: bool,
    ) -> None:
        self.repo = repo
        self.week_id = week_id
        self.source_package = source_package
        self.dry_run = dry_run
        self.counter = 0

    def add(
        self,
        *,
        stage: str,
        codes: list[str],
        messages: list[str],
        region: str | None = None,
        target_date: str | None = None,
        raw: Any = None,
        retryable: bool = True,
        suggested_action: str = "Review the errors and submit a corrected backfill package.",
    ) -> dict[str, Any]:
        self.counter += 1
        name = (
            f"{self.counter:03d}-{_slug(stage, 'unknown')}-"
            f"{_slug(region, 'unknown')}-{_slug(target_date, 'unknown')}.json"
        )
        relative = Path("data") / "rejected" / self.week_id / name
        if not self.dry_run and (self.repo / relative).exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            relative = relative.with_name(f"{relative.stem}-{stamp}{relative.suffix}")
            suffix = 1
            while (self.repo / relative).exists():
                relative = relative.with_name(
                    f"{relative.stem}-{suffix}{relative.suffix}"
                )
                suffix += 1
        payload = {
            "rejection_stage": stage,
            "rejected_at": now_iso(),
            "week_id": self.week_id,
            "region": region,
            "target_date": target_date,
            "error_codes": codes,
            "error_messages": messages,
            "raw_daily_export": raw,
            "source_package": self.source_package,
            "retryable": retryable,
            "suggested_action": suggested_action,
        }
        if not self.dry_run:
            write_json(self.repo / relative, payload)
        return {
            "stage": stage,
            "record": f"{region or 'unknown'}/{target_date or 'unknown'}",
            "errors": messages,
            "rejection_path": relative.as_posix(),
        }


def _report_path(repo: Path, week_id: str) -> Path:
    return repo / "data" / "audits" / f"weekly-import-{week_id}.json"


def _save_fatal_package(
    repo: Path,
    package_path: Path,
    text: str,
    error: Exception,
    dry_run: bool,
) -> dict[str, Any]:
    week_id = week_id_from_text(text)
    raw_relative = Path("inbox") / "raw" / f"{week_id}-rejected.md"
    raw_path = repo / raw_relative
    if not dry_run:
        raw_path = preserve_text(raw_path, text)
        raw_relative = raw_path.relative_to(repo)
    source = raw_relative.as_posix() if not dry_run else safe_source_label(package_path, repo)
    store = RejectionStore(repo, week_id, source, dry_run)
    rejected = [
        store.add(
            stage="package_envelope",
            codes=["PACKAGE_ENVELOPE_INVALID"],
            messages=[str(error)],
            raw={"filename": package_path.name},
            suggested_action="Correct the weekly envelope and resubmit the complete package.",
        )
    ]
    report = {
        "week_id": week_id,
        "package_status": "invalid",
        "import_status": "rejected",
        "imported_at": now_iso(),
        "source_package": source,
        "accepted": [],
        "skipped_identical": [],
        "revised": [],
        "rejected": rejected,
        "warnings": [],
    }
    if not dry_run:
        write_json(_report_path(repo, week_id), report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", help="Markdown/TXT file containing a weekly package")
    parser.add_argument("--repo-root", default=str(ROOT), help="Repository root to write into")
    parser.add_argument(
        "--allow-revision",
        action="store_true",
        help="Create a new revision only after explicit human review",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo_root).resolve()
    package_path = Path(args.package).resolve()
    text = package_path.read_text(encoding="utf-8")
    try:
        envelope = parse_weekly_envelope(text)
    except Exception as exc:
        report = _save_fatal_package(repo, package_path, text, exc, args.dry_run)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    week_id = envelope["week_id"]
    raw_relative = Path("inbox") / "raw" / f"{week_id}.md"
    if not args.dry_run:
        raw_path = preserve_text(repo / raw_relative, text)
        raw_relative = raw_path.relative_to(repo)
    source_package = (
        raw_relative.as_posix()
        if not args.dry_run
        else safe_source_label(package_path, repo)
    )
    store = RejectionStore(repo, week_id, source_package, args.dry_run)
    exports, malformed = parse_daily_exports_tolerant(text)
    blocks = extract_daily_blocks(text)
    manifest_errors = manifest_consistency_errors(
        envelope,
        exports,
        block_count=len(blocks),
        malformed_count=len(malformed),
    )
    report: dict[str, Any] = {
        "week_id": week_id,
        "coverage_start": envelope["coverage_start"],
        "coverage_end": envelope["coverage_end"],
        "package_status": envelope["status"].lower(),
        "import_status": "pending",
        "manifest": envelope["manifest"],
        "imported_at": now_iso(),
        "source_package": source_package,
        "accepted": [],
        "skipped_identical": [],
        "revised": [],
        "rejected": [],
        "warnings": [],
    }

    for failure in malformed:
        report["rejected"].append(
            store.add(
                stage="daily_json_parse",
                codes=["DAILY_JSON_INVALID"],
                messages=[failure["error"]],
                raw={
                    "block_index": failure["block_index"],
                    "raw_block": failure["raw_block"],
                },
            )
        )
    for error in manifest_errors:
        report["rejected"].append(
            store.add(
                stage="manifest_consistency",
                codes=["MANIFEST_INCONSISTENT"],
                messages=[error],
                raw={"manifest": envelope["manifest"]},
            )
        )

    valid_exports: list[dict[str, Any]] = []
    for export in exports:
        errors = validate_transport(export)
        if errors:
            records = export.get("records") or [{}]
            for record in records:
                report["rejected"].append(
                    store.add(
                        stage="transport_validation",
                        codes=["TRANSPORT_VALIDATION_FAILED"],
                        messages=errors,
                        region=record.get("region"),
                        target_date=record.get("target_date"),
                        raw=export,
                    )
                )
        else:
            valid_exports.append(export)

    seen: set[tuple[str, str]] = set()
    for export, transport_record in flatten_records(valid_exports):
        key = (transport_record["region"], transport_record["target_date"])
        if not envelope["coverage_start"] <= key[1] <= envelope["coverage_end"]:
            report["rejected"].append(
                store.add(
                    stage="coverage_validation",
                    codes=["RECORD_OUTSIDE_COVERAGE"],
                    messages=[
                        f"record date is outside {envelope['coverage_start']}.."
                        f"{envelope['coverage_end']}"
                    ],
                    region=key[0],
                    target_date=key[1],
                    raw=transport_record,
                )
            )
            continue
        if key in seen:
            report["rejected"].append(
                store.add(
                    stage="weekly_duplicate",
                    codes=["DUPLICATE_REGION_DATE"],
                    messages=["duplicate region/date across weekly package"],
                    region=key[0],
                    target_date=key[1],
                    raw=transport_record,
                )
            )
            continue
        seen.add(key)
        target = repo / "data" / "raw" / key[0] / f"{key[1]}.json"
        existing: dict[str, Any] | None = None
        revision = 1
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                existing_errors = validate_record(existing, CANONICAL_SCHEMA)
                if existing_errors:
                    raise ValueError("; ".join(existing_errors))
                revision = int(existing.get("revision", 1))
            except Exception as exc:
                report["rejected"].append(
                    store.add(
                        stage="existing_record",
                        codes=["EXISTING_RECORD_INVALID"],
                        messages=[str(exc)],
                        region=key[0],
                        target_date=key[1],
                        raw=transport_record,
                        retryable=False,
                        suggested_action="Repair the existing canonical file before importing this record.",
                    )
                )
                continue
        try:
            candidate = convert_region_record(transport_record, export, revision=revision)
        except Exception as exc:
            report["rejected"].append(
                store.add(
                    stage="canonical_conversion",
                    codes=["CANONICAL_CONVERSION_FAILED"],
                    messages=[str(exc)],
                    region=key[0],
                    target_date=key[1],
                    raw=transport_record,
                )
            )
            continue
        validation_errors = validate_record(candidate, CANONICAL_SCHEMA)
        if validation_errors:
            report["rejected"].append(
                store.add(
                    stage="canonical_validation",
                    codes=["CANONICAL_VALIDATION_FAILED"],
                    messages=validation_errors,
                    region=key[0],
                    target_date=key[1],
                    raw=transport_record,
                )
            )
            continue
        if existing is not None:
            if canonical_digest(existing) == canonical_digest(candidate):
                report["skipped_identical"].append(f"{key[0]}/{key[1]}")
                continue
            if not args.allow_revision:
                report["rejected"].append(
                    store.add(
                        stage="canonical_conflict",
                        codes=["CANONICAL_CONFLICT"],
                        messages=[
                            "conflicts with existing canonical record; "
                            "use --allow-revision only after explicit review"
                        ],
                        region=key[0],
                        target_date=key[1],
                        raw=transport_record,
                        retryable=False,
                        suggested_action="Compare sources and request explicit approval for a revision.",
                    )
                )
                continue
            revision += 1
            candidate = convert_region_record(
                transport_record,
                export,
                revision=revision,
                supersedes_revision=revision - 1,
                revision_reason="weekly package supplied materially different source data",
            )
            validation_errors = validate_record(candidate, CANONICAL_SCHEMA)
            if validation_errors:
                report["rejected"].append(
                    store.add(
                        stage="revision_validation",
                        codes=["REVISION_VALIDATION_FAILED"],
                        messages=validation_errors,
                        region=key[0],
                        target_date=key[1],
                        raw=transport_record,
                    )
                )
                continue
            report["revised"].append(f"{key[0]}/{key[1]} revision {revision}")
        else:
            report["accepted"].append(f"{key[0]}/{key[1]}")
        if not args.dry_run:
            write_json(target, candidate)

    unresolved = envelope["manifest"]["unresolved_records"]
    partial = bool(
        envelope["status"] == "WEEKLY_AGGREGATION_INCOMPLETE"
        or unresolved
        or report["rejected"]
    )
    report["import_status"] = "partial" if partial else "complete"
    if not args.dry_run:
        destination = (
            repo / "inbox" / ("partial" if partial else "processed") / f"{week_id}.md"
        )
        destination = preserve_text(
            destination,
            (repo / raw_relative).read_text(encoding="utf-8"),
        )
        if partial:
            write_json(
                repo / "analysis" / "backfill_requests" / f"{week_id}.json",
                {
                    "week_id": week_id,
                    "created_at": now_iso(),
                    "source_package": source_package,
                    "unresolved_records": unresolved,
                    "rejected_records": [
                        {
                            "record": item["record"],
                            "stage": item["stage"],
                            "rejection_path": item["rejection_path"],
                        }
                        for item in report["rejected"]
                    ],
                },
            )
        write_json(_report_path(repo, week_id), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["rejected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
