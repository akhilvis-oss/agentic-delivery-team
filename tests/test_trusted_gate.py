"""Adversarial synthetic metadata only; no network or credentials required."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("trusted_gate", ROOT / "gate/review_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.fixture
def cfg():
    return gate.load_config(ROOT / ".github/review-gate.json")


def event_for(cfg, count=1):
    repository = cfg["repo_url"].removeprefix("https://github.com/")
    return {"repository": {"full_name": repository}, "pull_request": {
        "number": 7, "changed_files": count,
        "body": """## Review tier
Tier: light
## Maker summary
Maker context: synthetic-maker
Documentation clarification.
## Test evidence
Text inspected.
## Assumptions
None.
""",
        "base": {"ref": "main", "sha": "b" * 40, "repo": {"full_name": repository}},
        "head": {"sha": "a" * 40, "repo": {"full_name": "example/contributor"}},
    }}


class FakeAPI:
    def __init__(self, event, rows=None, before=None):
        self.pr = copy.deepcopy(event["pull_request"])
        self.rows = rows if rows is not None else [
            {"filename": f"docs/{i}.md", "status": "modified"}
            for i in range(self.pr["changed_files"])]
        self.calls = []
        self.before = before

    def __call__(self, path):
        self.calls.append(path)
        if self.before:
            self.before(self, len(self.calls))
        if "/files?" not in path:
            return copy.deepcopy(self.pr)
        page = int(parse_qs(urlparse(path).query)["page"][0])
        return copy.deepcopy(self.rows[(page - 1) * 100:page * 100])


@pytest.mark.parametrize("count", [1, 100, 101, 200, 3000])
def test_complete_pagination_and_receipt(cfg, count):
    event = event_for(cfg, count)
    api = FakeAPI(event)
    errors, receipt = gate.evaluate(event, api, cfg)
    assert errors == []
    assert receipt == {"head_sha": "a" * 40, "base_sha": "b" * 40,
                       "changed_files": count, "minimum_tier": "light"}
    assert len([path for path in api.calls if "/files?" in path]) == (count + 99) // 100
    assert all(path.startswith("/repos/" + cfg["repo_url"].split("github.com/")[1])
               for path in api.calls)


@pytest.mark.parametrize("count", [None, -1, True, "1", 3001])
def test_invalid_count_never_fetches_or_classifies_a_subset(cfg, count):
    event = event_for(cfg, count)
    with pytest.raises(ValueError):
        gate.evaluate(event, lambda _: pytest.fail("must fail before API access"), cfg)


def test_empty_diff_is_strictest(cfg):
    event = event_for(cfg, 0)
    errors, receipt = gate.evaluate(event, FakeAPI(event), cfg)
    assert receipt["minimum_tier"] == "control-critical"
    assert any("below required control-critical" in error for error in errors)


@pytest.mark.parametrize("field", ["head", "base", "body", "count", "base_ref", "head_repo"])
@pytest.mark.parametrize("when", [1, 3, 4])
def test_revision_drift_before_collection_after_collection_and_after_validation(cfg, field, when):
    event = event_for(cfg)

    def change(api, call):
        if call != when:
            return
        if field in {"head", "base"}:
            api.pr[field]["sha"] = "c" * 40
        elif field == "body":
            api.pr["body"] += "Edited."
        elif field == "count":
            api.pr["changed_files"] = 2
        elif field == "base_ref":
            api.pr["base"]["ref"] = "other"
        else:
            api.pr["head"]["repo"]["full_name"] = "other/contributor"

    with pytest.raises(ValueError):
        gate.evaluate(event, FakeAPI(event, before=change), cfg)


def test_mutation_inside_validation_is_detected(cfg, monkeypatch):
    event = event_for(cfg)
    api = FakeAPI(event)

    def validate(*_):
        api.pr["body"] += "Changed while validating."
        return []

    monkeypatch.setattr(gate, "validate", validate)
    with pytest.raises(ValueError):
        gate.evaluate(event, api, cfg)


@pytest.mark.parametrize("kind", ["short_first", "short_last", "duplicate_page", "duplicate_row",
                                 "extra_last", "not_list"])
def test_incomplete_paginated_data_fails(cfg, kind):
    event = event_for(cfg, 101)
    api = FakeAPI(event)
    original = api.__call__

    def malformed(path):
        rows = original(path)
        if "/files?" not in path:
            return rows
        page = int(parse_qs(urlparse(path).query)["page"][0])
        if kind == "short_first" and page == 1 or kind == "short_last" and page == 2:
            return rows[:-1]
        if kind == "duplicate_page" and page == 2:
            return api.rows[:1]
        if kind == "duplicate_row" and page == 1:
            return [rows[0]] * 100
        if kind == "extra_last" and page == 2:
            return rows * 2
        if kind == "not_list":
            return {"message": "sensitive response"}
        return rows

    with pytest.raises(ValueError):
        gate.evaluate(event, malformed, cfg)


@pytest.mark.parametrize("row", [None, [], {}, {"filename": "docs/a.md", "status": "unknown"},
    {"filename": "docs/a.md", "status": "renamed"},
    {"filename": "docs/a.md", "status": "renamed", "previous_filename": ""},
    {"filename": "docs/a.md", "status": "modified", "previous_filename": None},
    {"filename": None, "status": "modified"},
    {"filename": "docs/a.md\ngate/review_gate.py", "status": "modified"}])
def test_malformed_rows_fail_closed(cfg, row):
    event = event_for(cfg)
    with pytest.raises((ValueError, TypeError)):
        gate.evaluate(event, FakeAPI(event, [row]), cfg)


@pytest.mark.parametrize("path", [None, "", "/absolute", "../x", "a/../x", "a/./x", "./x",
    "a//x", "a/", "a\\x", "a\nx", "a\rx", "a\tx", "a\0x", "a\x7fx", "a\x85x", "C:/x"])
def test_ambiguous_paths_fail_closed(cfg, path):
    with pytest.raises(ValueError):
        gate.file_tier(path, cfg)


def test_names_are_preserved_and_never_normalized(cfg, tmp_path):
    path = "docs/ spaces ünicode.md "
    assert gate.checked_path(path) == path
    file = tmp_path / "paths.txt"
    file.write_text(path + "\n")
    assert gate.read_changed_files(file) == [path]
    file.write_bytes(b"docs/guide.md\r\n")
    with pytest.raises(ValueError):
        gate.read_changed_files(file)
    file.write_text("docs/guide.md\n\n")
    with pytest.raises(ValueError):
        gate.read_changed_files(file)


@pytest.mark.parametrize("original,tier", [("gate/review_gate.py", "control-critical"),
                                          ("runner/task.py", "domain-critical")])
def test_renames_preserve_original_risk(cfg, original, tier):
    event = event_for(cfg)
    errors, receipt = gate.evaluate(event, FakeAPI(event, [{
        "filename": "docs/moved.md", "previous_filename": original, "status": "renamed"}]), cfg)
    assert receipt["minimum_tier"] == tier
    assert any("below required " + tier in e for e in errors)


def test_deletion_preserves_risk(cfg):
    event = event_for(cfg)
    errors, receipt = gate.evaluate(event, FakeAPI(event, [{
        "filename": ".github/review-gate.json", "status": "removed"}]), cfg)
    assert receipt["minimum_tier"] == "control-critical"
    assert errors


@pytest.mark.parametrize("change", ["reason", "patterns", "empty_pattern", "code", "config",
                                    "workflows", "tests", "missing_group", "empty_group"])
def test_config_cannot_weaken_self_protection(cfg, tmp_path, change):
    data = copy.deepcopy(cfg)
    if change == "reason":
        data["tiers"]["rules"][0]["reason"] = " "
    elif change == "patterns":
        data["tiers"]["rules"][0]["patterns"] = []
    elif change == "empty_pattern":
        data["tiers"]["rules"][0]["patterns"].append("")
    elif change == "missing_group":
        del data["gate_paths"]["code"]
    elif change == "empty_group":
        data["gate_paths"]["code"] = []
    else:
        # The configured code/config/workflows/tests must each remain protected.
        data["gate_paths"][change] = ["new/unprotected.py"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        gate.load_config(path)


def test_adopter_paths_are_configurable(cfg, tmp_path):
    data = copy.deepcopy(cfg)
    data["gate_paths"] = {key: [f"custom/{key}.txt"] for key in gate.DEFAULT_GATE_PATHS}
    data["tiers"]["rules"][0]["patterns"].append("custom/*")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    assert gate.load_config(path)["gate_paths"] == data["gate_paths"]


@pytest.mark.parametrize("text,valid", [("Evidence: N/A", False), ("Evidence: n/a: --", False),
    ("Evidence: N/A: outside this scope", True), ("Evidence: a test", True),
    ("Evidence: one\nEvidence: two", False), ("Evidence: \nEvidence: one", False)])
def test_evidence_requires_exactly_one_line_and_na_reason(text, valid):
    assert gate.evidence_line(text, "Evidence") is valid


@pytest.mark.parametrize("failure", [OSError("SENSITIVE-TRANSPORT"), ValueError("SENSITIVE-API"),
                                    RuntimeError("SENSITIVE-TOKEN")])
def test_api_cli_sanitizes_all_failures(cfg, tmp_path, monkeypatch, capsys, failure):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(event_for(cfg)))

    def unavailable(_):
        raise failure

    monkeypatch.setattr(gate, "github_api", unavailable)
    assert gate.main([str(event), "--config", str(ROOT / ".github/review-gate.json"),
                      "--github-api"]) == 2
    captured = capsys.readouterr()
    assert "FAIL:" in captured.out
    assert "SENSITIVE" not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


def test_cli_success_rejection_and_no_body_echo(cfg, tmp_path, monkeypatch, capsys):
    data = event_for(cfg)
    data["pull_request"]["body"] += "\nSENSITIVE-PR-BODY\n"
    path = tmp_path / "event.json"
    args = [str(path), "--config", str(ROOT / ".github/review-gate.json"), "--github-api"]
    for tier, code in [("light", 0), ("unknown-SENSITIVE", 1)]:
        data["pull_request"]["body"] = data["pull_request"]["body"].replace("Tier: light", "Tier: " + tier)
        path.write_text(json.dumps(data))
        monkeypatch.setattr(gate, "github_api", FakeAPI(data))
        assert gate.main(args) == code
        assert "SENSITIVE" not in capsys.readouterr().out
    assert gate.main(args + ["--changed-files", str(path)]) == 2


def test_isolated_execution_ignores_working_directory_and_pythonpath(tmp_path):
    poison = tmp_path / "untrusted"
    poison.mkdir()
    payload = "from pathlib import Path\nPath('EXECUTED').touch()\nraise RuntimeError('PR code ran')\n"
    for name in ["json.py", "sitecustomize.py", "review_gate.py"]:
        (poison / name).write_text(payload)
    event = tmp_path / "event.json"
    event.write_text("{}")
    env = {**os.environ, "PYTHONPATH": str(poison)}
    result = subprocess.run([sys.executable, "-I", str(ROOT / "gate/review_gate.py"), str(event),
                             "--config", str(ROOT / ".github/review-gate.json"), "--github-api"],
                            cwd=poison, env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert not (poison / "EXECUTED").exists()
    assert "PR code ran" not in result.stderr


@pytest.mark.parametrize("name,script", [(".github/workflows/pr-review-gate.yml", "gate/review_gate.py"),
                                       ("templates/review-gate.yml", "scripts/review_gate.py")])
def test_trusted_workflows_use_only_base_code_and_read_permissions(name, script):
    text = (ROOT / name).read_text()
    assert re.search(r"^  pull_request_target:", text, re.M)
    assert text.count("actions/checkout@") == 1
    assert "ref: ${{ github.event.pull_request.base.sha }}" in text
    assert "repository: ${{ github.repository }}" in text
    assert "persist-credentials: false" in text
    assert "working-directory: .gate-base" in text
    assert f"python -I {script}" in text and "--github-api" in text
    assert "contents: read" in text and "pull-requests: read" in text
    for forbidden in ["pull_request.head", "refs/pull/", "secrets.", "pip ", "npm ",
                      "actions/cache", "artifact", "--jq", ": write", "write-all"]:
        # Comments may describe forbidden operations; inspect executable YAML only.
        executable = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert forbidden not in executable
    for action in re.findall(r"uses: (\S+)", text):
        assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40}", action)


def test_pr_ci_remains_build_only():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "not the trusted enforcement path" in text
    assert "pull_request:" in text and "pull_request_target:" not in text
    for command in ["python scripts/check_private_content.py", "python -m ruff", "python -m pytest"]:
        assert command in text
    assert "  review-gate:" not in text
