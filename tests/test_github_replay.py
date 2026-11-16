"""Real GitHub recordings through production transport, parsing and decisions."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lifecycle import core
from recorded_github import FIXTURES, RecordedGitHub
import test_runner as runner_fixtures
from test_lifecycle import policy

integration = runner_fixtures.runner.integration_api()
SMOKE = "akhilvis-oss/adt-smoke"


def recording(name):
    return json.loads((FIXTURES / name).read_bytes())


def client_for(cfg, routes):
    client = core.GitHub(cfg, token="fake")
    transport = RecordedGitHub(client, {
        (method, "/repos/" + cfg["repository"] + path): name
        for (method, path), name in routes.items()
    })
    return client, transport


@pytest.fixture
def cfg(tmp_path):
    cfg = policy(tmp_path)
    cfg["repository"] = SMOKE
    cfg["trusted_gate_workflow"] = ".github/workflows/review-gate.yml"
    return cfg


@pytest.mark.parametrize("method,path", [("GET", "/missing"), ("GET", "/pulls/3?extra=1"),
                                         ("POST", "/issues")])
def test_recorded_github_rejects_unrecorded_request(cfg, method, path):
    client, transport = client_for(cfg, {("GET", "/pulls/3"): "smoke-pr3-open.json"})
    with pytest.raises(pytest.fail.Exception, match="No GitHub recording.*" + path.split("?")[0]):
        client.request(method, path)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("closed,expected", [(False, "pass"), (True, "unavailable")])
def test_recorded_gate_binds_open_pr_head(cfg, closed, expected):
    pr = recording("smoke-pr3-open.json")
    run = recording("smoke-pr3-review-gate-run-open.json")
    routes = {
        ("GET", "/pulls/3"): "smoke-pr3-open.json",
        ("GET", f"/commits/{pr['head']['sha']}/check-runs?filter=all&per_page=100&page=1"):
            "smoke-pr3-head-check-runs.json",
        ("GET", f"/commits/{pr['base']['sha']}/check-runs?filter=all&per_page=100&page=1"):
            "smoke-pr2-base-check-runs.json",
        ("GET", "/actions/workflows/review-gate.yml"): "smoke-review-gate-workflow.json",
        ("GET", f"/actions/runs/{run['id']}"):
            "smoke-pr3-review-gate-run-closed.json" if closed else "smoke-pr3-review-gate-run-open.json",
    }
    client, transport = client_for(cfg, routes)
    pr = client.request("GET", "/pulls/3")
    assert core.trusted_gate(cfg, client, pr, strict_snapshot=True) == expected
    assert any(pr["head"]["sha"] in path for _, path in transport.calls)
    assert ("GET", "/repos/" + SMOKE + f"/actions/runs/{run['id']}") in transport.calls


def test_recorded_base_has_no_gate(cfg):
    pr = recording("smoke-pr2-closed-unmerged.json")
    path = f"/commits/{pr['base']['sha']}/check-runs?filter=all&per_page=100&page=1"
    client, _ = client_for(cfg, {
        ("GET", path): "smoke-pr2-base-check-runs.json",
        ("GET", "/actions/workflows/review-gate.yml"): "smoke-review-gate-workflow.json",
    })
    checks = client.request("GET", path)
    assert all(c["name"] != "review-gate" for c in checks["check_runs"])
    # Ask the same production lookup to inspect this commit, without changing a recording.
    snapshot = deepcopy(pr)
    snapshot["head"]["sha"] = pr["base"]["sha"]
    assert core.trusted_gate(cfg, client, snapshot) == "unavailable"


def replay_delivery(cfg, tmp_path, monkeypatch, filename):
    pr = recording(filename)
    client, transport = client_for(cfg, {
        ("GET", f"/pulls/{pr['number']}"): filename,
        ("GET", "/"): "smoke-repo-owner-view.json",
    })
    monkeypatch.setattr(core, "load_config", lambda _: cfg)
    monkeypatch.setattr(core, "admit_phase", lambda *args: True)
    monkeypatch.setattr(client, "issue", lambda _: {
        "state": "open", "body": "synthetic story", "labels": ["status:in-review"]})
    monkeypatch.setattr(integration, "reviewed", lambda *args: None)
    monkeypatch.setattr(integration, "category", lambda *args: ([], "standard"))
    monkeypatch.setattr(integration, "release_work", lambda *args: False)
    monkeypatch.setattr(integration, "merge_evidence", lambda *args: "pass")
    # No write response exists. An auto-merge attempt fails immediately at transport.
    client.enable_auto_merge = lambda _: client.opener.open(core.urllib.request.Request(
        "https://api.github.com/graphql", method="POST"))
    client.notify = lambda _: None
    opts = {"enabled": True, "declared_tier": "standard"}
    runner = SimpleNamespace(
        integration_api=lambda: integration, lifecycle_api=lambda: core,
        now=lambda: "synthetic-time", digest=lambda _: "policy",
        save_job=lambda *args: None, save_at_stop=lambda *args: None,
        save=lambda *args: None)
    config = {"integration": opts, "lifecycle_config": str(tmp_path / "synthetic.json"),
              "branch": pr["head"]["ref"]}
    job = {"status": "ready_for_integration", "github_number": 1,
           "head_commit": pr["head"]["sha"], "reviewed_head": pr["head"]["sha"],
           "assignment": {"task_text": "synthetic story"},
           "integration": {"stage": "checking", "pr": pr["number"], "body": pr["body"],
                           "options_sha256": "policy", "reason_notice_attempted": True}}
    integration.advance(runner, config, tmp_path / "job.json", job, client)
    return job, client, transport, config


def test_recorded_disabled_auto_merge_stops_delivery(cfg, tmp_path, monkeypatch):
    job, _, transport, _ = replay_delivery(cfg, tmp_path, monkeypatch, "smoke-pr3-open.json")
    assert job["status"] == "needs_owner"
    assert job["integration"]["reason"] == "owner_merge_required"
    assert all(method == "GET" for method, _ in transport.calls)
    assert transport.calls[-1] == ("GET", "/repos/" + SMOKE + "/")


@pytest.mark.parametrize("filename,merged", [
    ("smoke-pr1-merged.json", True),
    ("smoke-pr2-closed-unmerged.json", False),
])
def test_recorded_merge_delivery(cfg, tmp_path, monkeypatch, filename, merged):
    job, client, transport, config = replay_delivery(cfg, tmp_path, monkeypatch, filename)
    pr = integration.bound_pr(client, cfg, config, job, job["integration"])
    assert pr["merged"] is merged
    if merged:
        assert job["integration"]["stage"] == "merged"
        assert pr["merge_commit_sha"] == "59b42d0fa8592be51cbec044ec595edead325467"
    else:
        # GitHub also supplies a merge_commit_sha for an unmerged closed PR.
        assert pr["merge_commit_sha"]
        assert job["integration"]["stage"] == "owner"
        assert job["integration"]["reason"] == "merge_conditions_missing"
    assert all(method == "GET" for method, _ in transport.calls)


def test_recorded_one_click_approval_ignores_workflow_draft_removal(cfg):
    events = recording("adt-issue122-one-click-approval-events.json")
    owner = next(e["actor"]["login"] for e in events
                 if e.get("label", {}).get("name") == "status:approved"
                 and e["event"] == "labeled")
    cfg["actors"]["owner"] = owner
    cfg["identity_roles"][owner] = ["owner", "product"]
    cfg["repository"] = "akhilvis-oss/agentic-delivery-team"
    client, _ = client_for(cfg, {
        ("GET", "/issues/122/events?per_page=100&page=1"):
            "adt-issue122-one-click-approval-events.json",
    })
    events = client.events(122)
    state, approval = core.canonical_state(cfg, events)
    assert state == "approved"
    assert approval["actor"]["login"] == owner
    removal = next(e for e in events if e["event"] == "unlabeled"
                   and e.get("label", {}).get("name") == "status:draft")
    before = [e for e in events if e["id"] < removal["id"]]
    after = [e for e in events if e["id"] <= removal["id"]]
    assert removal["actor"]["login"] == cfg["actors"]["workflow"]
    assert core.canonical_state(cfg, before) == core.canonical_state(cfg, after)
    assert core.canonical_state(cfg, after) == ("approved", approval)
