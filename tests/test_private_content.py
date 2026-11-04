"""Synthetic strings only; real-looking values are assembled at runtime."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check", ROOT / "scripts/check_private_content.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

HOME = "/" + "Users/alice/project"
KEY_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
OPENAI = "sk-" + "proj-" + "A" * 30
GITHUB = "gh" + "p_" + "b" * 36
AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
ASSIGNED = "NEWS" + "API_KEY=" + "c" * 24


@pytest.mark.parametrize("text, name", [
    (f"state_dir = '{HOME}/state'", "home-directory path"),
    ("/" + "home/bob/.config", "home-directory path"),
    (KEY_HEADER, "private key"),
    (f"key = {OPENAI}", "OpenAI-style key"),
    (f"token: {GITHUB}", "GitHub token"),
    (AWS, "AWS access key"),
    (ASSIGNED, "assigned secret"),
])
def test_findings(text, name):
    assert [n for _, n in check.scan_text(text)] == [name]


@pytest.mark.parametrize("text", [
    "/" + "Users/you/project",
    "/" + "home/runner/work",
    "OPENAI_API_KEY=REPLACE_ME",
    "API_KEY=${OPENAI_API_KEY}",
    "TOKEN = <your token>",
    "see https://example.com/users/alice",
    f"{HOME} # " + check.ALLOW_MARKER,
])
def test_allowed(text):
    assert list(check.scan_text(text)) == []


def test_reports_line_numbers_not_values(tmp_path, capsys):
    target = tmp_path / "config.py"
    target.write_text("ok = 1\n" + ASSIGNED + "\n")
    assert check.main([str(target)]) == 1
    out = capsys.readouterr().out
    assert ":2: possible assigned secret" in out and "c" * 24 not in out


def test_clean_file_passes(tmp_path):
    target = tmp_path / "clean.md"
    target.write_text("Nothing private here.\n")
    assert check.main([str(target)]) == 0
