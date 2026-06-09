#!/usr/bin/env python3
"""Static safety gate for KIS trading automation changes.

The gate is intentionally local-only: it does not import project runtime modules,
read credential files, contact broker APIs, or require network access.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REQUIRED_AGENT_RULES = {
    "prod_confirmation": "실전(`prod`) 주문 전에",
    "reservation_confirmation": "실전(`prod`) 예약주문",
    "protective_confirmation": "실전(`prod`) 손익절",
    "signal_threshold": "신호 강도 `0.5` 미만",
    "masked_account": "계좌번호는 마스킹",
    "codex_manual_guard": "Codex는 훅을 지원하지 않는다",
}

SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(appkey|appsecret|access[_-]?token|approval[_-]?key)\b\s*[:=]\s*['\"][^'\"]{12,}['\"]"
)
ACCOUNT_LITERAL = re.compile(r"\b\d{8}-\d{2}\b")
PROD_ENV_WITHOUT_CONFIRMATION = re.compile(
    r"(?i)\b(KIS_ENV|KIS_MODE|KIS_LOCK_MODE)\b\s*[:=]\s*['\"]prod['\"]"
)

SOURCE_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".toml", ".json", ".sh"}
ALLOWLIST_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "node_modules",
    "uv.lock",
    "tests/fixtures",
}


def _tracked_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [p.relative_to(root) for p in root.rglob("*") if p.is_file()]
    return [Path(line) for line in result.stdout.splitlines() if line]


def _is_scannable(path: Path) -> bool:
    normalized = path.as_posix()
    return path.suffix in SOURCE_SUFFIXES and not any(part in normalized for part in ALLOWLIST_PARTS)


def check_agents(root: Path) -> list[str]:
    path = root / "AGENTS.md"
    if not path.exists():
        return ["AGENTS.md is required for Codex/ECC safety philosophy."]
    text = path.read_text(encoding="utf-8")
    return [
        f"AGENTS.md is missing safety rule marker: {name} ({marker})"
        for name, marker in REQUIRED_AGENT_RULES.items()
        if marker not in text
    ]


def check_import_gate(root: Path) -> list[str]:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return ["pyproject.toml is required for Python quality/import gates."]
    text = pyproject.read_text(encoding="utf-8")
    required = ["[tool.ruff]", "[tool.mypy]", "[tool.importlinter]", "Backtester must not depend"]
    return [f"pyproject.toml missing required quality/import gate marker: {marker}" for marker in required if marker not in text]


def check_source_patterns(root: Path) -> list[str]:
    failures: list[str] = []
    for rel_path in _tracked_files(root):
        if not _is_scannable(rel_path):
            continue
        path = root / rel_path
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        if SECRET_ASSIGNMENT.search(text):
            failures.append(f"{rel_path}: possible hardcoded KIS secret assignment")
        if ACCOUNT_LITERAL.search(text) and "mask" not in text.lower() and "마스킹" not in text:
            failures.append(f"{rel_path}: possible unmasked account number literal")
        if PROD_ENV_WITHOUT_CONFIRMATION.search(text) and "confirm" not in text.lower() and "확인" not in text:
            failures.append(f"{rel_path}: prod mode literal without nearby confirmation wording")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local-only KIS trading safety checks.")
    parser.add_argument("--root", default=".", help="Repository root to scan")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    failures = check_agents(root) + check_import_gate(root) + check_source_patterns(root)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    print("Trading safety gate passed: AGENTS rules, quality/import gate markers, and static secret/prod scans are clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
