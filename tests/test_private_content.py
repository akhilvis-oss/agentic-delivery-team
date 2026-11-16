"""Synthetic strings only; real-looking values are assembled at runtime."""

import importlib.util
from pathlib import Path
import subprocess
import sys

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


def test_cli_and_guard_use_the_same_rules():
    from lifecycle import core, private_content

    assert check.CHECKS is private_content.CHECKS
    assert check.scan_text is private_content.scan_text
    assert core.check is private_content


@pytest.mark.parametrize("text, finding", [
    (HOME, "home-directory path"),
    (KEY_HEADER, "private key"),
    (OPENAI, "OpenAI-style key"),
    # Preserve the existing first-match precedence for overlapping key patterns.
    ("sk-" + "ant-" + "z" * 30, "OpenAI-style key"),
    (GITHUB, "GitHub token"),
    (AWS, "AWS access key"),
    ("xox" + "b-" + "z" * 20, "Slack token"),
    (ASSIGNED, "assigned secret"),
    ("/" + "Users/you/project", None),
    ("TOKEN = <your token>", None),
    (HOME + " # " + check.ALLOW_MARKER, None),
])
def test_direct_cli_preserves_output_and_exit_codes(tmp_path, text, finding):
    target = tmp_path / "input.txt"
    target.write_text(text + "\n")
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts/check_private_content.py"), str(target)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.stderr == ""
    if finding:
        assert result.returncode == 1
        assert result.stdout == (
            f"FAIL: {target}:1: possible {finding}\n"
            "1 possible private-content finding(s). Move the value to local, "
            "uncommitted config, or mark a reviewed placeholder line with '"
            + check.ALLOW_MARKER + "'.\n"
        )
    else:
        assert result.returncode == 0
        assert result.stdout == "PASS: no private content found in 1 file(s)\n"
