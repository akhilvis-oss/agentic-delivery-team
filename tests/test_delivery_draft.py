"""Pre-publication review drafting uses target rules and fixed operator evidence."""

import base64
from copy import deepcopy
import hashlib
import json

import pytest

from test_runner_integration import (  # noqa: F401
    PUBLIC, STORY, advance, delivery, integration, runner, setup,
)

REAL_DRAFT = getattr(integration, "draft", None)
COMPLETE = (PUBLIC + "\n## Risk and scope\n"
            "- [x] SCOPE-1: Owner-authorized target.\n"
            "- [x] SCOPE-2: Synthetic public data only.\n"
            "- [x] SCOPE-3: Reversible scoped change.\n"
            "\n## Failure-mode verification\nFailure cases: rejected evidence.\n"
            "Recovery: persisted tier.\nConcurrency: target and reviewed head bound.\n")


@pytest.fixture
def drafting(delivery, monkeypatch):  # noqa: F811
    case = delivery
    config, path, job, client = case
    if REAL_DRAFT is not None:
        monkeypatch.setattr(integration, "draft", REAL_DRAFT)
    config["integration"]["public_evidence"] = COMPLETE
    rules = runner.read(__import__("pathlib").Path(__file__).resolve().parents[1]
                        / ".github/review-gate.json")
    rules["repo_url"] = "https://github.com/example/kit"
    state = {"rules": rules, "paths": ["lifecycle/critical.py"], "reads": [],
             "target": "c" * 40, "diffs": []}
    request = client.request
    git = runner.git

    def read(method, endpoint, data=None):
        if method == "GET" and endpoint.startswith("/branches/"):
            state["reads"].append(endpoint)
            return {"commit": {"sha": state["target"]}}
        if method == "GET" and endpoint.endswith("?ref=" + state["target"]):
            state["reads"].append(endpoint)
            if isinstance(state["rules"], Exception):
                raise state["rules"]
            return {"content": base64.b64encode(json.dumps(state["rules"]).encode()).decode()}
        return request(method, endpoint, data)

    def diff(settings, *args):
        if args[:4] == ("diff", "--no-renames", "--name-only", "-z"):
            state["diffs"].append(args)
            return "\0".join(state["paths"]) + "\0"
        return git(settings, *args)

    monkeypatch.setattr(client, "request", read)
    monkeypatch.setattr(runner, "git", diff)
    return case, state


def set_story(case, monkeypatch, section):
    config, path, job, client = case
    text = STORY + "\n\n" + section
    job["assignment"]["task_text"] = text
    job["assignment"]["task_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    job["assignment_sha256"] = runner.digest(job["assignment"])
    issue = client.issue
    monkeypatch.setattr(client, "issue", lambda number: {**issue(number), "body": text})
    runner.save_job(config, path, job)


def test_control_diff_is_drafted_before_publication_with_domain_evidence(drafting):
    case, state = drafting
    _, path, job, client = case
    state["paths"].append("runner/domain.py")

    advance(case)

    assert job["integration"]["reason"] == "owner_merge_required"
    assert job["integration"]["chosen_tier"] == "control-critical"
    assert "Tier: control-critical\n" in client.pr["body"]
    assert "## Failure-mode verification" in client.pr["body"]
    assert integration.load_gate().validate(client.pr, state["paths"], state["rules"]) == []
    assert state["reads"] == ["/branches/main",
                              "/contents/.github/review-gate.json?ref=" + state["target"]]
    assert state["diffs"] == [("diff", "--no-renames", "--name-only", "-z",
                               job["base_commit"], job["head_commit"])]
    assert runner.read(path)["integration"]["chosen_tier"] == "control-critical"


@pytest.mark.parametrize("section,expected,invalid", [
    ("## Review tier\ncontrol-critical", "control-critical", False),
    ("### rEvIeW tIeR\n\nDOMAIN-CRITICAL", "domain-critical", False),
    ("## Review tier\nlight", "standard", False),
    ("## Review tier\nnot-a-tier", "standard", True),
])
def test_story_tier_is_only_a_floor_and_invalid_values_are_not_echoed(
    drafting, monkeypatch, section, expected, invalid
):
    case, state = drafting
    state["paths"] = ["application.py"]
    config, _, job, client = case
    set_story(case, monkeypatch, section)

    advance(case)

    assert job["integration"]["chosen_tier"] == expected
    assert "Tier: " + expected + "\n" in client.pr["body"]
    assert ("An invalid story review tier was ignored." in client.pr["body"]) is invalid
    tier_section = integration.story_section(client.pr["body"], "Review tier")
    assert "not-a-tier" not in tier_section
    assert config["integration"]["declared_tier"] == "standard"


def test_story_cannot_lower_target_rules_and_review_base_is_used(drafting, monkeypatch):
    case, state = drafting
    _, _, job, _ = case
    job["review_base"] = job["head_commit"]
    job["assignment"]["review_base"] = job["review_base"]
    set_story(case, monkeypatch, "## Review tier\nlight")

    advance(case)

    assert job["integration"]["chosen_tier"] == "control-critical"
    assert state["diffs"][0][-2:] == (job["review_base"], job["head_commit"])
    assert job["integration"]["draft"]["target_sha"] == state["target"]
    assert state["target"] != job["review_base"]


@pytest.mark.parametrize("evidence", [
    COMPLETE.replace("## Security review", "## Unrelated security"),
    COMPLETE.replace("[x] SCOPE-2", "[ ] SCOPE-2"),
    COMPLETE + "\n## Security review\nRepeated.\n",
    COMPLETE + "\nPrivate: " + "/" + "Users" + "/private-person/repo\n",
])
def test_invalid_operator_evidence_stops_before_any_publication(drafting, evidence, capsys):
    case, _ = drafting
    config, path, job, client = case
    config["integration"]["public_evidence"] = evidence

    advance(case)

    assert job["status"] == "needs_owner"
    assert job["integration"]["reason"] == "pr_evidence_incomplete"
    assert not client.pushed and not client.writes and client.pr is None
    assert runner.read(path)["blocked_reason"]["reason_code"] == "pr_evidence_incomplete"
    assert capsys.readouterr().err.startswith("PR evidence: ")
    assert "private-person" not in json.dumps(job["blocked_reason"])


def test_requirement_ids_stop_without_publication(drafting):
    case, state = drafting
    _, _, job, client = case
    state["rules"]["requirement_ids"] = {"pattern": "REQ-[0-9]+", "allowed": None}

    advance(case)

    assert job["status"] == "needs_owner"
    assert job["integration"]["reason"] == "requirement_ids_unsupported"
    assert not client.pushed and not client.writes


@pytest.mark.parametrize("bad", ["unreadable", "foreign", "order", "invalid"])
def test_bad_target_rules_stop_as_publication_failed_not_stale_head(drafting, bad):
    case, state = drafting
    _, _, job, client = case
    if bad == "unreadable":
        state["rules"] = ValueError("synthetic unavailability")
    elif bad == "foreign":
        state["rules"]["repo_url"] = "https://github.com/foreign/repo"
    elif bad == "order":
        state["rules"]["tiers"]["order"] = list(reversed(integration.TIERS))
    else:
        state["rules"]["allowed_bases"] = []

    advance(case)

    assert job["integration"]["reason"] == "publication_failed"
    assert not client.pushed and not client.writes


def test_final_body_is_rechecked_before_create(drafting, monkeypatch):
    case, _ = drafting
    _, _, job, client = case
    body_for = integration.body_for
    calls = []

    def changed(*args, **kwargs):
        body = body_for(*args, **kwargs)
        calls.append(args[-1])
        return body + ("\n## Security review\nRepeated.\n" if len(calls) == 2 else "")

    monkeypatch.setattr(integration, "body_for", changed)
    advance(case)

    assert len(calls) == 2 and calls[0].endswith("#issuecomment-1")
    assert job["integration"]["reason"] == "pr_evidence_incomplete"
    assert client.pr is None and not client.reviews
    assert not any(endpoint == "/pulls" for _, endpoint, _ in client.writes)


def test_saved_chosen_tier_is_reused_without_new_target_reads(drafting):
    case, state = drafting
    config, path, job, client = case
    state["paths"] = ["application.py"]
    client.check_state = "pending"
    config["integration"]["declared_tier"] = "domain-critical"
    advance(case)
    assert job["integration"]["stage"] == "checking"
    first = deepcopy(state["reads"])
    stored = runner.read(path)
    job.clear()
    job.update(stored)
    client.job = job
    client.check_state = "success"

    advance(case)

    assert state["reads"] == first
    assert job["integration"]["chosen_tier"] == "domain-critical"
    assert job["integration"]["stage"] == "auto_merge_enabled"


def test_legacy_delivery_keeps_configured_tier_and_does_not_draft(drafting):
    case, state = drafting
    _, _, job, client = case
    job["integration"] = {"stage": "new"}

    advance(case)

    assert not state["reads"] and not state["diffs"]
    assert "chosen_tier" not in job["integration"]
    assert "Tier: standard\n" in client.pr["body"]
    assert job["integration"]["stage"] == "auto_merge_enabled"


def test_new_stop_texts_match_both_tables_and_default_example_disables_ids():
    from lifecycle import core
    from pathlib import Path

    for code in ("pr_evidence_incomplete", "requirement_ids_unsupported"):
        assert core.STOP_REASONS[code] == integration.REASONS[code]
        assert core.STOP_KINDS[code] == "Needs a fix"
        assert runner.block_reason(code)["reason_code"] == code
    root = Path(__file__).resolve().parents[1]
    assert json.loads((root / "templates/review-gate.example.json").read_text())["requirement_ids"] is None


def test_old_config_body_is_byte_compatible_at_its_tier(drafting):
    case, state = drafting
    config, _, job, client = case
    state["paths"] = ["application.py"]
    cfg = runner.lifecycle_api().load_config(config["lifecycle_config"])
    opts = integration.options(config)
    link = "https://github.com/example/kit/issues/1#issuecomment-1"

    _, body = integration.draft(runner.self_module(), config, job, cfg, opts, client, link)

    assert body == integration.body_for(runner.self_module(), config, job, cfg, opts, link)
    assert body.endswith("\n" + COMPLETE)


def test_selected_domain_tier_keeps_configured_second_reviewer_policy(drafting, monkeypatch):
    case, state = drafting
    config, _, job, client = case
    state["paths"] = ["application.py"]
    config["integration"].update(reviewer_provider="first",
        second_reviewer={"actor": "second-bot", "provider": "second"})
    set_story(case, monkeypatch, "## Review tier\ndomain-critical")

    advance(case)

    assert job["integration"]["chosen_tier"] == "domain-critical"
    assert job["integration"]["stage"] == "checking"
    assert not any(kind == "AUTO" for kind, _, _ in client.writes)


def test_target_rule_recheck_still_detects_later_category_increase(drafting):
    case, state = drafting
    _, _, job, client = case
    state["paths"] = ["application.py"]
    client.minimum = "control-critical"

    advance(case)

    assert job["integration"]["chosen_tier"] == "standard"
    assert job["integration"]["reason"] == "gate_category_higher"


def test_rename_out_of_control_path_keeps_old_path_in_real_diff(tmp_path):
    from pathlib import Path
    import re
    import subprocess
    from types import SimpleNamespace

    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).rstrip("\n")

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    source = tmp_path / "lifecycle/control.py"
    source.parent.mkdir()
    source.write_text("unchanged content\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("mv", "lifecycle/control.py", "ordinary.py")
    git("commit", "-qm", "rename")
    head = git("rev-parse", "HEAD")
    rules = json.loads((Path(__file__).resolve().parents[1] / ".github/review-gate.json").read_text())
    rules["repo_url"] = "https://github.com/example/kit"
    client = SimpleNamespace(request=lambda method, endpoint:
        {"commit": {"sha": head}} if endpoint.startswith("/branches/") else
        {"content": base64.b64encode(json.dumps(rules).encode()).decode()})
    tool = SimpleNamespace(git=lambda config, *args: git(*args), SHA=re.compile(r"[0-9a-f]{40}"),
                           read=lambda _: {}, public_receipt=lambda _: {})
    job = {"id": "rename", "assignment": {"task_text": STORY}, "base_commit": base,
           "review_base": base, "head_commit": head, "github_number": 1,
           "review_session": "independent", "verification_runs": [{"results": []}]}

    saved, _ = integration.draft(tool, {"state_dir": str(tmp_path)}, job,
        {"repository": "example/kit"}, {"declared_tier": "standard", "public_evidence": COMPLETE},
        client, "https://github.com/example/kit/issues/1#issuecomment-1")

    assert saved["changed_files"] == ["lifecycle/control.py", "ordinary.py"]
    assert saved["chosen_tier"] == "control-critical"
