from __future__ import annotations

import re
from pathlib import Path

from scripts.api_dependency_scan import detect_active_openai_dependencies
from scripts.v2.core import repo_root


def v2_text_files() -> list[Path]:
    root = repo_root()
    roots = [
        root / "config" / "v2",
        root / "docs" / "v2",
        root / "schemas" / "v2",
        root / "scripts" / "v2",
        root / "tests" / "v2",
    ]
    files = [root / ".env.example", root / "docs" / "V2_SOURCE_FEASIBILITY_AUDIT.md"]
    for base in roots:
        files.extend(
            path
            for path in base.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return files


def test_no_active_openai_api_dependency():
    assert detect_active_openai_dependencies(repo_root()) == []


def test_no_committed_secret_signature_in_v2():
    patterns = [
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{30,}\b"),
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        re.compile(r"(?im)^(?:CDSAPI_KEY|FRED_API_KEY)[ \t]*=[ \t]*[^\s#]+"),
    ]
    findings = []
    for path in v2_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(text) for pattern in patterns):
            findings.append(path.relative_to(repo_root()).as_posix())
    assert findings == []


def test_no_user_specific_absolute_path_in_v2():
    findings = []
    for path in v2_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if re.search(r"(?i)C:\\Users\\Lurek|D:\\CodexProjects\\weather-stock", text):
            findings.append(path.relative_to(repo_root()).as_posix())
    assert findings == []
