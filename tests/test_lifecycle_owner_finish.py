"""Owner finish attestation uses synthetic GitHub history and disposable Git only."""

import copy
import json
from pathlib import Path

import pytest

from lifecycle import core, service
from test_lifecycle import Client, Hub, configured_service, policy, pr_fixture


class FinishClient(Client):
    def request(self, method, path, data=None):
        if method == "PATCH" and path == "/issues/1":
            self.hub.calls.append((method, path, data))
            self.hub.issues[1]["state"] = data["state"]
            return self.issue(1)
        return super().request(method, path, data)


def owner_label(hub):
    old = copy.deepcopy(hub.issues[1]["labels"])
    event = hub.event(1, "merged", "owner")
    hub.issues[1]["labels"] += old  # GitHub adds a label; it does not replace it.
    return event


def proofs(cfg, hub):
    return [r for r in core.records(cfg, hub.comments_by_issue[1], 1)
            if r["kind"] == "finish"]


def finish_record(cfg, event=102, **kwargs):
    fields = dict(kind="finish", event=event, reason="finished_by_owner")
    fields.update(kwargs)
    record = core.public_record(cfg, 1, "owner-finish", 0, "merged", **fields)
    record.update({"from": "draft", "actor": cfg["actors"]["workflow"]})
    return record


@pytest.mark.parametrize("source", sorted(core.STATES - {"merged"}))
@pytest.mark.parametrize("closed", [False, True])
def test_owner_finish_from_two_labels_records_once_and_closes(tmp_path, closed, source):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state=source)["state"] = "closed" if closed else "open"
    event = owner_label(hub)
    client = FinishClient(hub, "workflow")
    for _ in range(2):
        core.guard_issue(cfg, client, 1)
    assert core.state(hub.issues[1]) == "merged"
    assert hub.issues[1]["state"] == "closed"
    assert len(proofs(cfg, hub)) == 1
    assert proofs(cfg, hub)[0]["event"] == event["id"]
    assert proofs(cfg, hub)[0]["reason"] == "finished_by_owner"
    assert not any("restored" in c["body"] for c in hub.comments_by_issue[1])
    assert sum(c[0] == "PATCH" for c in hub.calls) == (0 if closed else 1)


@pytest.mark.parametrize("action", ["labeled", "edited", "reopened", "labeled-approved"])
def test_dropped_finish_recovers_through_guard_event(tmp_path, monkeypatch, action):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="approved")
    owner_label(hub)
    event = dict(repository={"full_name": cfg["repository"]}, action=action,
                 issue=copy.deepcopy(hub.issues[1]), sender={"login": "unrelated"})
    if action == "labeled-approved":
        event.update(action="labeled", label={"name": "status:approved"},
                     sender={"login": cfg["actors"]["owner"]})
    if action == "edited":
        event["changes"] = {"body": {"from": "old scope"}}
    path = tmp_path / "event.json"
    core.save(path, event)
    monkeypatch.setattr(core, "load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(core, "GitHub", lambda *a: FinishClient(hub, "workflow"))
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    assert core.main(["--config", cfg["config_path"], "guard-event", "--event", str(path)]) == 0
    assert len(proofs(cfg, hub)) == 1
    assert hub.issues[1]["state"] == "closed"


@pytest.mark.parametrize("action", ["labeled", "edited", "reopened", "labeled-approved"])
def test_resumed_guard_event_never_appends_unproven_owner_finish(tmp_path, monkeypatch, action):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="approved")
    owner_label(hub)
    event = dict(repository={"full_name": cfg["repository"]}, action=action,
                 issue=copy.deepcopy(hub.issues[1]), sender={"login": "unrelated"})
    if action == "labeled-approved":
        event.update(action="labeled", label={"name": "status:approved"},
                     sender={"login": cfg["actors"]["owner"]})
    if action == "edited":
        event["changes"] = {"body": {"from": "old scope"}}
    path = tmp_path / "event.json"
    core.save(path, event)
    monkeypatch.setattr(core, "load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(core, "GitHub", lambda *a: FinishClient(hub, "workflow"))
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    assert core.main(["--config", cfg["config_path"], "guard-event", "--event", str(path)]) == 0
    assert cfg["uncertain_workflow_resume"] is True
    assert not proofs(cfg, hub)
    assert hub.issues[1]["state"] == "open"
    assert not any(method == "PATCH" and path == "/issues/1" for method, path, _ in hub.calls)


@pytest.mark.parametrize("failure", ["before", "after"])
def test_finish_write_failure_never_closes_without_confirmed_record(tmp_path, failure):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    owner_label(hub)
    hub.fail_write = failure
    client = FinishClient(hub, "workflow")
    core.guard_issue(cfg, client, 1)
    assert hub.issues[1]["state"] == ("open" if failure == "before" else "closed")
    hub.fail_write = None
    core.guard_issue(cfg, client, 1)
    assert hub.issues[1]["state"] == "closed"
    assert len(proofs(cfg, hub)) == 1


@pytest.mark.parametrize("role,source,allowed", [
    ("product", "draft", False), ("stranger", "draft", False),
    ("runner", "draft", False), ("workflow", "draft", False),
    ("runner", "needs-owner", True), ("workflow", "pr-open", True),
])
def test_non_owner_handling_regression(tmp_path, role, source, allowed):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state=source)
    hub.event(1, "merged", role)
    core.guard_issue(cfg, FinishClient(hub, "workflow"), 1)
    assert core.state(hub.issues[1]) == ("merged" if allowed else source)
    assert not proofs(cfg, hub)
    assert hub.issues[1]["state"] == "open"
    assert any("restored" in c["body"] for c in hub.comments_by_issue[1]) != allowed


@pytest.mark.parametrize("kind", ["transition", "finish"])
def test_owner_authored_merged_never_counts_even_with_product_role(tmp_path, kind):
    cfg = policy(tmp_path)
    record = (finish_record(cfg) if kind == "finish" else
              core.public_record(cfg, 1, "manual", 0, "merged"))
    record.update({"from": "draft", "actor": cfg["actors"]["owner"]})
    assert core.records(cfg, [dict(user={"login": cfg["actors"]["owner"]},
                                  body=core.record_body(record))], 1) == []


@pytest.mark.parametrize("field,value", [
    ("event", None), ("event", True), ("reason", "other"), ("commit", "a" * 40),
    ("pr", 1), ("gate", "pass"), ("outcome", "ready_for_integration"),
    ("severities", {"P0": 1}),
])
def test_finish_schema_rejects_unrelated_metadata(tmp_path, field, value):
    cfg = policy(tmp_path)
    with pytest.raises(ValueError):
        finish_record(cfg, **{field: value})


@pytest.mark.parametrize("mutation", ["actor", "event", "source", "runner", "owner"])
def test_finish_publication_checks_real_owner_history(tmp_path, mutation):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    event = owner_label(hub)
    record = finish_record(cfg, event=event["id"])
    role = "workflow"
    if mutation == "actor":
        event["actor"]["login"] = "stranger"
    elif mutation == "event":
        record = finish_record(cfg, event=event["id"] + 10)
    elif mutation == "source":
        record["from"] = "approved"
    else:
        role = "product" if mutation == "owner" else "runner"
        record["actor"] = cfg["actors"]["owner" if mutation == "owner" else "runner"]
    hub.issues[1]["labels"] = [{"name": "status:merged"}]
    core.queue(cfg, record, role=role)
    if role == "runner":
        core.flush(cfg, FinishClient(hub, role))  # Finished job intents are superseded.
    else:
        with pytest.raises(core.TransitionRejected):
            core.flush(cfg, FinishClient(hub, role))
    assert not proofs(cfg, hub)
    assert hub.issues[1]["state"] == "open"


@pytest.mark.parametrize("prior", ["label", "record"])
def test_previously_finished_story_gets_no_new_finish(tmp_path, prior):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="pr-open")
    if prior == "label":
        hub.event(1, "merged", "workflow")
    else:
        record = core.public_record(cfg, 1, "pr-1", 0, "merged", pr=1)
        record.update({"from": "pr-open", "actor": cfg["actors"]["workflow"]})
        hub.comments_by_issue[1].append(dict(user={"login": cfg["actors"]["workflow"]},
                                            body=core.record_body(record)))
    owner_label(hub)
    core.guard_issue(cfg, FinishClient(hub, "workflow"), 1)
    assert not proofs(cfg, hub)


@pytest.mark.parametrize("name", [".github/task-lifecycle.json", "templates/task-lifecycle.example.json"])
def test_policy_allows_only_new_owner_merge_rights(name):
    cfg = core.read(Path(__file__).resolve().parents[1] / name)
    for source in core.STATES - {"merged"}:
        assert "owner" in cfg["transitions"][source]["merged"]
    assert "owner" not in cfg["transitions"]["merged"]["merged"]
    for role, sources in [("workflow", {"needs-owner", "pr-open", "accepted", "merged"}),
                          ("runner", {"needs-owner"}), ("product", set())]:
        assert {s for s, targets in cfg["transitions"].items()
                if role in targets.get("merged", [])} == sources


@pytest.mark.parametrize("merged,closed_when", [(False, None), (True, None),
                                               (False, "before"), (False, "after")])
def test_owner_cleanup_preserves_unmerged_refs_and_reports_until_branch_removed(
        tmp_path, monkeypatch, merged, closed_when):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    claim_path = next((Path(cfg["state_dir"]) / "claims").glob("*.json"))
    claim = core.read(claim_path)
    head = service.git(cfg, "rev-parse", "HEAD", worktree=claim["worktree"]).stdout.strip()
    if not merged:
        # A distinct local commit absent from the target, made only in disposable test state.
        service.git(cfg, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "--allow-empty", "-m", "synthetic saved work", worktree=claim["worktree"])
        head = service.git(cfg, "rev-parse", "HEAD", worktree=claim["worktree"]).stdout.strip()
    claim["head"] = head
    ref = "refs/agentic/continue/" + claim["job"]
    service.git(cfg, "update-ref", ref, head)
    close = dict(id=hub.counter + 1, event="closed", actor={"login": cfg["actors"]["owner"]})
    if closed_when == "before":
        hub.counter += 1
        hub.events_by_issue[1].append(close)
    event = owner_label(hub)
    core.guard_issue(cfg, FinishClient(hub, "workflow"), 1)
    if closed_when == "after":
        hub.counter += 1
        hub.events_by_issue[1].append(dict(close, id=hub.counter))
    assert proofs(cfg, hub)[0]["event"] == event["id"]
    service.cleanup(cfg, claim, hub.issues[1], hub.events_by_issue[1], client=hub.client())
    core.save(claim_path, claim)
    assert claim["cleaned"]
    assert not Path(claim["worktree"]).exists()
    assert (service.git(cfg, "show-ref", "--verify", ref, check=False).returncode == 0) == (
        not merged and closed_when != "before")
    branch_ref = "refs/heads/" + claim["branch"]
    assert (service.git(cfg, "show-ref", "--verify", branch_ref, check=False).returncode == 0) != merged
    notice = "Issue #1: owner finish kept unmerged work"
    for at in (200, 300):
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: at)
        assert core.read(claim_path)["stage"] == "finished"
        assert (notice in service.status(cfg)) != merged
        journal = core.read(Path(cfg["state_dir"]) / "public-heartbeat.json")
        comment = next(c for c in hub.comments_by_issue[journal["issue"]]
                       if c["id"] == journal["comment"])
        assert (notice in comment["body"]) != merged
    if not merged:
        service.git(cfg, "branch", "-D", "--", claim["branch"])
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 400)
        assert notice not in service.status(cfg)
        assert notice not in comment["body"]


def test_owner_finish_readers_trust_only_workflow_and_strict_schema(tmp_path):
    cfg = policy(tmp_path)
    record = finish_record(cfg)
    for role in ("workflow", "runner", "product"):
        value = dict(record, actor=cfg["actors"][role])
        comment = dict(user={"login": value["actor"]}, body=core.record_body(value))
        assert bool(core.records(cfg, [comment], 1)) == (role == "workflow")
    value = dict(record, event=None)
    body = core.PREFIX + json.dumps(value) + core.SUFFIX
    assert not core.records(cfg, [dict(user={"login": record["actor"]}, body=body)], 1)


def test_finish_does_not_close_when_post_is_not_confirmed_on_issue(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    owner_label(hub)
    client = FinishClient(hub, "workflow")
    monkeypatch.setattr(client, "comments", lambda _: [])
    core.guard_issue(cfg, client, 1)
    assert hub.issues[1]["state"] == "open"
    assert len(hub.comments_by_issue[1]) == 1
    monkeypatch.undo()
    core.guard_issue(cfg, client, 1)
    assert hub.issues[1]["state"] == "closed"
    assert len(proofs(cfg, hub)) == 1


def test_publication_rejects_owner_product_merged_transition(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    record = core.public_record(cfg, 1, "forged", 0, "merged")
    record.update({"from": "draft", "actor": cfg["actors"]["owner"]})
    core.queue(cfg, record, role="product")
    client = FinishClient(hub, "product")
    with pytest.raises(core.TransitionRejected):
        core.flush(cfg, client)
    assert not core.records(cfg, hub.comments_by_issue[1], 1)
    assert core.state(hub.issues[1]) == "draft"


def test_concurrent_owner_finish_publishes_one_record(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    owner_label(hub)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: core.guard_issue(cfg, FinishClient(hub, "workflow"), 1), range(2)))
    core.guard_issue(cfg, FinishClient(hub, "workflow"), 1)
    assert len(proofs(cfg, hub)) == 1
    assert hub.issues[1]["state"] == "closed"
    assert sum(c[0] == "PATCH" for c in hub.calls) == 1


def test_guard_event_never_finishes_status_report(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    issue = hub.issue(state="draft", body="<!-- agentic-service-status-v1 -->")
    issue["user"]["login"] = cfg["actors"]["runner"]
    owner_label(hub)
    event = dict(repository={"full_name": cfg["repository"]}, action="reopened", issue=issue)
    path = tmp_path / "event.json"
    core.save(path, event)
    monkeypatch.setattr(core, "load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(core, "GitHub", lambda *a: FinishClient(hub, "workflow"))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    core.main(["--config", cfg["config_path"], "guard-event", "--event", str(path)])
    assert not hub.calls
    assert not proofs(cfg, hub)


def test_closing_runner_pr_after_finish_fails_once_without_changing_finish(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    pr = pr_fixture(cfg, hub)
    owner_label(hub)
    client = FinishClient(hub, "workflow")
    core.guard_issue(cfg, client, 1)
    before = copy.deepcopy(hub.issues[1])
    pr["state"] = "closed"
    with pytest.raises(core.TransitionRejected):
        core.pr_event(cfg, client, {"action": "closed", "pull_request": copy.deepcopy(pr)})
    # The confirmed rejection is durable; subsequent flushes leave it alone.
    calls = list(hub.calls)
    core.flush(cfg, client)
    assert hub.calls == calls
    assert hub.issues[1] == before
    assert len(proofs(cfg, hub)) == 1
