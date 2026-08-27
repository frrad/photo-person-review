#!/usr/bin/env python3
"""Reject private values and binary media accidentally tracked by Git."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

DEFAULT_PATTERNS = (
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"(?i)\b(?:ghp|github_pat|sk-or-v1|sk)-[A-Za-z0-9_-]{20,}",
    r"(?i)\b(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd)"
    r"\s*[:=]\s*['\"][^'\"]{16,}['\"]",
    r"(?i)\b[A-Z0-9._%+-]+@(?:gmail|outlook|hotmail|icloud|yahoo)\.[A-Z]{2,}\b",
    r"(?i)https://app\.vidigami\.com/[^\s'\"]+/(?:pages|users)/[^\s'\"]+",
    r"(?i)\b(?:page|space|user)\|(?!example|synthetic)[0-9]{3,}\b",
)

FORBIDDEN_SUFFIXES = {
    ".avif",
    ".cr2",
    ".cr3",
    ".db",
    ".dng",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp4",
    ".nef",
    ".png",
    ".raw",
    ".sqlite",
    ".sqlite3",
    ".tif",
    ".tiff",
    ".webp",
}


def _git_files(root: Path, *args: str) -> list[Path]:
    try:
        result = subprocess.run(["git", *args], check=True, capture_output=True, text=True, cwd=root)
    except (OSError, subprocess.CalledProcessError):
        return []
    return [Path(name) for name in result.stdout.splitlines() if name]


def candidate_files(root: Path) -> list[Path]:
    """Return unique tracked, staged, and unignored untracked files below *root*."""

    tracked = _git_files(root, "ls-files")
    staged = _git_files(root, "diff", "--cached", "--name-only", "--diff-filter=ACMRT")
    untracked = _git_files(root, "ls-files", "--others", "--exclude-standard")
    relative = {path for path in (*tracked, *staged, *untracked) if path.parts and not path.is_absolute()}
    return sorted(path for path in relative if (root / path).is_file())


def load_patterns(root: Path) -> list[re.Pattern[str]]:
    pattern_file = root / ".privacy-patterns"
    expressions = list(DEFAULT_PATTERNS)
    if pattern_file.exists():
        expressions.extend(
            line.strip()
            for line in pattern_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    try:
        return [re.compile(expression) for expression in expressions]
    except re.error as error:
        raise ValueError(f"invalid regular expression in .privacy-patterns: {error}") from error


def scan(root: Path) -> list[str]:
    """Return privacy findings without printing matched private content."""

    patterns = load_patterns(root)
    findings: list[str] = []
    for relative_path in candidate_files(root):
        path = root / relative_path
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"{relative_path}: forbidden tracked binary/state file")
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            findings.append(f"{relative_path}: unreadable tracked binary file")
            continue
        for line_number, line in enumerate(lines, start=1):
            if any(pattern.search(line) for pattern in patterns):
                findings.append(f"{relative_path}:{line_number}: private pattern")
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        findings = scan(root)
    except ValueError as error:
        print(f"privacy check error: {error}", file=sys.stderr)
        return 2
    if findings:
        print("Potential private material found:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("Privacy check passed: Git-visible files contain no configured private material.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
