"""Fail if tracked files contain personal paths, private keys or token-like strings.

This repository is public. Project-specific and personal details (home folders,
account names, keys, tokens) belong in local config that is never committed;
the repository holds only generic code and *.example files.

A line can be exempted by adding the marker `private-content: allow` to it,
for example in documentation that must show a placeholder path. Use sparingly;
reviewers see every exemption in the diff.

Usage: python scripts/check_private_content.py [PATH ...]   (default: git ls-files)
"""

import re
import subprocess
import sys
from pathlib import Path

ALLOW_MARKER = "private-content: " + "allow"
PLACEHOLDER_USERS = {"you", "user", "username", "name", "runner", "example", "me"}

CHECKS = (
    ("home-directory path", re.compile(r"(?<![\w.])/(?:Users|home)/([A-Za-z0-9._-]+)")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("assigned secret", re.compile(
        r"(?i)\b[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)\s*[=:]\s*['\"]?(?!REPLACE|CHANGE|<|\$|\{)[A-Za-z0-9/+_-]{16,}")),
)
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz"}


def scan_text(text):
    """Yield (line number, check name) for each finding; never yield the matched value."""
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        finding = next((name for name, pattern in CHECKS for match in pattern.finditer(line)
                        if not (name == "home-directory path"
                                and match.group(1).lower() in PLACEHOLDER_USERS)), None)
        if finding:
            yield number, finding  # one finding per line is enough to act on


def tracked_files():
    output = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [Path(item) for item in output.decode().split("\0") if item]


def main(argv=None):
    paths = [Path(p) for p in (argv if argv is not None else sys.argv[1:])] or tracked_files()
    findings = 0
    for path in paths:
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, name in scan_text(text):
            print(f"FAIL: {path}:{number}: possible {name}")
            findings += 1
    if findings:
        print(f"{findings} possible private-content finding(s). Move the value to local, "
              f"uncommitted config, or mark a reviewed placeholder line with '{ALLOW_MARKER}'.")
        return 1
    print(f"PASS: no private content found in {len(paths)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
