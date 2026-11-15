"""Shared public-content scanning rules for lifecycle records and the CLI."""

import re

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
