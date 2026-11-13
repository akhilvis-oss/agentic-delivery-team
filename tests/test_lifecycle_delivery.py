"""Continuation delivery uses synthetic APIs and temporary repositories only."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

from lifecycle import core, service
from test_lifecycle import configured_service
from test_lifecycle_continuation import approve, newest_claim
from test_lifecycle_fetch import command
import test_runner as fixtures


def settings(cfg, field, integration):
    value = core.read(cfg[field])
    if integration is None:
        value.pop("integration", None)
    else:
        value["integration"] = integration
    value["verification"] = {"commands": [[sys.executable, "-c", "print('1 passed')"]],
                             "timeout_seconds": 5}
    core.save(cfg[field], value)


def story(tmp_path, monkeypatch, enabled=False):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    hub.issues[1]["body"] = "## Story\nSynthetic story.\n\n- AC-1: Deliver the change."
    # Re-attest the edited synthetic scope before dispatch.
    hub.event(1, "approved", "owner")
    hub.approve()
    opts = {"enabled": enabled, "declared_tier": "standard"}
    settings(cfg, "runner_config", opts)
    calls, publications = [], []

    def execute(config, state, path, job, role):
        calls.append((job["id"], role))
        job.update(status="running_" + role, updated_at=core.now())
        runner.save_job(config, path, job)
        if role == "worker":
            fixtures.deliver(config)
            return fixtures.result("ready_for_review")
        return fixtures.result("ready_for_integration", "reviewer")

    class Delivery:
        def __init__(self):
            self.client = hub.client()
            self.pr = None

        def issue(self, number):
            return self.client.issue(number)

        def push(self, tool, config, head):
            publications.append(("push", head))
            self.branch = config["branch"]

        def notify(self, number):
            pass

        def request(self, method, path, data=None):
            if path == "/pulls":
                publications.append(("pr", data))
                self.pr = {"number": 9, "node_id": "synthetic", "body": data["body"],
                           "state": "open", "head": {"sha": publications[-2][1],
                           "ref": self.branch, "repo": {"full_name": cfg["repository"]}},
                           "base": {"ref": "main", "repo": {"full_name": cfg["repository"]}}}
                return deepcopy(self.pr)
            if path == "/pulls/9":
                return deepcopy(self.pr)
            if path.endswith("/reviews"):
                publications.append(("checker", data))
                return {}
            result = self.client.request(method, path, data)
            result = result or {}
            result.setdefault("html_url", "https://github.com/example/project/issues/1#issuecomment-1")
            return result

    api = runner.integration_api()
    monkeypatch.setattr(runner, "execute", execute)
    monkeypatch.setattr(api, "client_for", lambda *a: Delivery())
    monkeypatch.setattr(api, "category", lambda *a: ([], "standard"))
    monkeypatch.setattr(api, "merge_evidence", lambda *a: "pending")
    return cfg, hub, runner, calls, publications


def test_passing_review_with_delivery_off_saves_reviewed_continuation(tmp_path, monkeypatch):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job["status"] == "needs_owner"
    assert job["blocked_reason"]["reason_code"] == "delivery_disabled"
    assert job["head_commit"] == job["reviewed_head"] and job["continuation"]
    assert core.state(hub.issues[1]) == "needs-owner" and not publications
    record, = [r for r in core.records(cfg, hub.comments_by_issue[1], 1)
               if r["reason"] == "delivery_disabled" and r.get("continuation")]
    assert record["outcome"] == "ready_for_integration"
    body = core.record_body(record)
    assert "Delivery is off for this repository" in body
    assert "### Continue from\nCommit: " + job["reviewed_head"] in body
    before = list(calls)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert calls == before


@pytest.mark.parametrize("enabled", [False, True])
def test_primary_inherits_original_delivery_and_publishes_only_when_enabled(tmp_path, monkeypatch, enabled):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch, enabled)
    execute = runner.execute

    def rework(config, state, path, job, role):
        if role == "reviewer" and not job["assignment"]["lifecycle"]["primary"]:
            job["status"] = "running_reviewer"
            runner.save_job(config, path, job)
            return fixtures.result("needs_changes", "reviewer")
        return execute(config, state, path, job, role)

    monkeypatch.setattr(runner, "execute", rework)
    # Primary's own preference must never override the original generated config.
    settings(cfg, "primary_runner_config", {"enabled": not enabled, "declared_tier": "standard"})
    service.poll(cfg, hub.client(), clock=lambda: 100)
    original, = service.jobs(cfg)
    assert original["status"] == "needs_primary_review"
    settings(cfg, "runner_config", {"enabled": not enabled, "declared_tier": "standard"})
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    config = core.read(claim["config"])
    assert config["integration"]["enabled"] is enabled
    primary = next(j for j in service.jobs(cfg) if j["id"] == claim["job"])
    if enabled:
        assert [p[0] for p in publications] == ["push", "pr", "checker"]
        assert publications[0][1] == primary["reviewed_head"]
    else:
        assert not publications and primary["status"] == "needs_owner"
        assert primary["blocked_reason"]["reason_code"] == "delivery_disabled"


@pytest.mark.parametrize("invalid", [None, {}, {"enabled": True}])
def test_incomplete_primary_template_stops_before_dispatch(tmp_path, monkeypatch, invalid):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)

    def rework(config, state, path, job, role):
        job["status"] = "running_" + role
        runner.save_job(config, path, job)
        if role == "worker":
            fixtures.deliver(config)
            return fixtures.result("ready_for_review")
        return fixtures.result("needs_changes", "reviewer")

    monkeypatch.setattr(runner, "execute", rework)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    settings(cfg, "primary_runner_config", invalid)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    assert claim["error"] == "delivery_configuration_invalid"
    assert len(service.jobs(cfg)) == 1 and not Path(claim["worktree"]).exists()
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    assert not any(r["job"] == claim["job"] and r["to"] == "dispatched" for r in records)
    assert any("Nothing was dispatched" in c["body"] for c in hub.comments_by_issue[1])


@pytest.mark.parametrize("change", ["none", "legacy", "form", "target", "gate", "scope"])
def test_reviewed_head_reapproval_reuses_review_or_records_fallback(tmp_path, monkeypatch, change):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    api = runner.integration_api()
    advance = api.advance
    if change == "form":
        hub.issues[1]["body"] += ("\n\n### Saved work (optional)\n\n_No response_"
                                  "\n\n### Dependencies\n\n_No response_")
        hub.event(1, "approved", "owner")
        hub.approve()
    if change == "legacy":
        monkeypatch.setattr(api, "advance", lambda *a, **kw: None)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    monkeypatch.setattr(api, "advance", advance)
    original, = service.jobs(cfg)
    if change == "legacy":
        assert original["status"] == "ready_for_integration"
        assert not original.get("continuation")
    source = Path(cfg["repository_root"])
    if change in {"target", "gate"}:
        if change == "gate":
            (source / ".github").mkdir()
            (source / ".github/review-gate.json").write_text("{}\n")
            command(source, "add", ".github/review-gate.json")
        command(source, "commit", "--allow-empty", "-qm", "advance target")
    if change == "scope":
        hub.issues[1]["body"] += "\n- AC-2: More work."
    settings(cfg, "runner_config", {"enabled": True, "declared_tier": "standard"})
    if change == "form":
        hub.issues[1]["body"] = hub.issues[1]["body"].replace(
            "### Saved work (optional)\n\n_No response_",
            "### Saved work (optional)\n\n### Continue from\nCommit: " + original["reviewed_head"])
        hub.event(1, "approved", "owner")
        hub.approve()
    else:
        approve(hub, original["reviewed_head"])
    before = list(calls)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    resumed = next(j for j in service.jobs(cfg) if j["id"] == claim["job"])
    assert [p[0] for p in publications] == ["push", "pr", "checker"]
    assert not resumed.get("lifecycle_unsynced")
    if change in {"none", "legacy", "form"}:
        assert calls == before
        assert publications[0][1] == original["reviewed_head"]
        assert resumed["review_session"] == original["review_session"]
        assert claim["delivery_source"] == original["id"]
        assert resumed["verification_runs"] == original["verification_runs"]
    else:
        reasons = {"target": "target_branch_moved", "gate": "trusted_gate_changed",
                   "scope": "approved_scope_changed"}
        assert claim["delivery_resume_fallback"] == reasons[change]
        assert [role for _, role in calls[len(before):]] == ["worker", "reviewer"]
        assert resumed["review_session"] != original["review_session"]
        if change in {"target", "gate"}:
            assert resumed["review_base"] == command(source, "rev-parse", "main")


@pytest.mark.parametrize("stage", ["enqueued", "finished"])
def test_accepted_legacy_job_is_not_silently_skipped(tmp_path, monkeypatch, stage):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    api = runner.integration_api()
    advance = api.advance
    monkeypatch.setattr(api, "advance", lambda *a, **kw: None)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job["status"] == "ready_for_integration"
    claim = newest_claim(cfg)
    claim["stage"] = stage
    path = Path(cfg["state_dir"]) / "claims" / (claim["job"] + ".json")
    core.save(path, claim)
    monkeypatch.setattr(api, "advance", advance)
    before = list(calls)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    job, = service.jobs(cfg)
    assert calls == before and not publications
    assert job["status"] == "needs_owner" and job["continuation"]
    assert job["blocked_reason"]["reason_code"] == "delivery_disabled"
    assert core.state(hub.issues[1]) == "needs-owner"
