"""Detect active OpenAI API dependencies without flagging documentation or detectors."""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import yaml

API_KEY = "OPENAI_API_KEY"
OPENAI_HOST = "api.openai.com"


def _constant_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    current: ast.AST = node.func
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _python_reasons(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []

    reasons: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "openai" or alias.name.startswith("openai.") for alias in node.names):
                reasons.add("imports OpenAI SDK")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "openai" or (node.module and node.module.startswith("openai.")):
                reasons.add("imports OpenAI SDK")
        elif isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "os"
                and value.attr == "environ"
                and _constant_string(node.slice) == API_KEY
            ):
                reasons.add("reads OPENAI_API_KEY from environment")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            first_arg = _constant_string(node.args[0]) if node.args else None
            if (
                name in {"os.getenv", "os.environ.get", "getenv"}
                or name.endswith(".environ.get")
            ) and first_arg == API_KEY:
                reasons.add("reads OPENAI_API_KEY from environment")
            if name in {"OpenAI", "AsyncOpenAI", "openai.OpenAI", "openai.AsyncOpenAI"}:
                reasons.add("instantiates OpenAI SDK client")
            call_strings = [
                value
                for value in (
                    *(_constant_string(arg) for arg in node.args),
                    *(_constant_string(keyword.value) for keyword in node.keywords),
                )
                if value
            ]
            network_call = (
                name in {"requests.get", "requests.post", "requests.request", "httpx.get", "httpx.post", "httpx.request"}
                or name.endswith(".request")
                or name in {"urlopen", "urllib.request.urlopen"}
            )
            if network_call and any(OPENAI_HOST in value for value in call_strings):
                reasons.add("calls api.openai.com")
    return sorted(reasons)


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _iter_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_strings(child)


def _workflow_reasons(path: Path) -> list[str]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    reasons: set[str] = set()
    for value in _iter_strings(parsed):
        compact = re.sub(r"\s+", " ", value)
        if re.search(r"secrets\s*\.\s*OPENAI_API_KEY", compact, re.IGNORECASE):
            reasons.add("workflow references secrets.OPENAI_API_KEY")
        if OPENAI_HOST in compact:
            reasons.add("workflow calls api.openai.com")
        if re.search(r"\bOPENAI_API_KEY\s*=", compact):
            reasons.add("workflow configures OPENAI_API_KEY")
        if re.search(r"(?:python\S*\s+\S*openai\S*|\bopenai\s+)", compact, re.IGNORECASE):
            reasons.add("workflow executes OpenAI-related command")
    return sorted(reasons)


def detect_active_openai_dependencies(root: Path) -> list[dict[str, str]]:
    """Return active code/workflow findings; prose-only mentions are ignored."""
    findings: list[dict[str, str]] = []
    scripts = root / "scripts"
    if scripts.exists():
        for path in sorted(scripts.rglob("*.py")):
            for reason in _python_reasons(path):
                findings.append({"path": path.relative_to(root).as_posix(), "reason": reason})
    workflows = root / ".github" / "workflows"
    if workflows.exists():
        for path in sorted((*workflows.rglob("*.yml"), *workflows.rglob("*.yaml"))):
            for reason in _workflow_reasons(path):
                findings.append({"path": path.relative_to(root).as_posix(), "reason": reason})
    return findings
