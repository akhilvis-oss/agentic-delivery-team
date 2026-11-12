"""Synthetic actors, API responses, outages and disposable Git worktrees only."""

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from lifecycle import core, service
import test_runner as fixtures

ROOT = Path(__file__).resolve().parents[1]
WHEN = "2026-01-02T10:00:00+00:00"


def policy(tmp_path):
    cfg = core.read(ROOT / "templates/task-lifecycle.example.json")
    cfg.update(
        repository="example/project",
        state_dir=str(tmp_path / "lifecycle"),
        repository_root=str(tmp_path / "source"),
        worktree_root=str(tmp_path / "worktrees"),
        runner_config=str(tmp_path / "runner.json"),
        primary_runner_config=str(tmp_path / "primary.json"),
    )
    cfg["actors"] = dict(
        owner="owner-fixture",
        runner="runner-fixture",
        product="product-fixture",
        workflow="github-actions[bot]",
    )
    cfg["identity_roles"] = {
        cfg["actors"]["owner"]: ["owner", "product"],
        cfg["actors"]["product"]: ["product"],
        cfg["actors"]["runner"]: ["runner"],
        cfg["actors"]["workflow"]: ["workflow"],
    }
    path = tmp_path / "lifecycle.json"
    core.save(path, cfg)
    cfg["config_path"] = str(path)
    return cfg


class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.issues = {}
        self.comments_by_issue = {}
        self.events_by_issue = {}
        self.prs = {}
        self.calls = []
        self.fail_read = False
        self.fail_write = None
        self.labels = []
        self.checks = []
        self.counter = 100

    def client(self, role="runner"):
        return Client(self, role)

    def issue(
        self, number=1, state="approved", body="Implement the approved synthetic task."
    ):
        issue = dict(
            number=number,
            labels=[{"name": "status:" + state}],
            body=body,
            state="open",
            updated_at=WHEN,
            closed_at=None,
            user={"login": self.cfg["actors"]["owner"]},
            assignees=[],
        )
        self.issues[number] = issue
        self.comments_by_issue[number] = []
        self.events_by_issue[number] = []
        if state != "draft":
            # Build actual policy-valid history for each state.
            chain = ["approved"]
            if state in {
                "dispatched",
                "in-progress",
                "committed",
                "in-review",
                "changes-requested",
                "needs-primary",
                "pr-open",
                "accepted",
                "merged",
            }:
                chain += ["dispatched", "in-progress", "committed", "in-review"]
            if state not in chain:
                chain.append(state)
            for value in chain:
                role = (
                    "owner"
                    if value in {"approved", "accepted"}
                    else "workflow"
                    if value in {"pr-open", "merged"}
                    else "runner"
                )
                self.event(number, value, role)
        return issue

    def event(self, number, target, role="owner"):
        self.counter += 1
        item = dict(
            id=self.counter,
            event="labeled",
            label={"name": "status:" + target},
            actor={"login": self.cfg["actors"].get(role, role)},
            created_at=WHEN,
        )
        self.events_by_issue[number].append(item)
        self.issues[number]["labels"] = [
            x
            for x in self.issues[number]["labels"]
            if not x["name"].startswith("status:")
        ] + [{"name": "status:" + target}]
        return item

    def approve(self, number=1):
        event = self.events_by_issue[number][-1]
        record = core.public_record(
            self.cfg,
            number,
            "approval",
            0,
            "approved",
            event=event["id"],
            kind="approval",
            approved_by=self.cfg["actors"]["owner"],
            scope_sha256=hashlib.sha256(
                self.issues[number]["body"].encode()
            ).hexdigest(),
        )
        record.update({"from": "approved", "actor": self.cfg["actors"]["workflow"]})
        self.comments_by_issue[number].append(
            dict(
                user={"login": self.cfg["actors"]["workflow"]},
                body=core.record_body(record),
            )
        )
        return event


class Client:
    def __init__(self, hub, role):
        self.hub, self.cfg, self.role = hub, hub.cfg, role

    def issue(self, number):
        if self.hub.fail_read:
            raise core.Unavailable("offline")
        return copy.deepcopy(self.hub.issues[number])

    def comments(self, number):
        if self.hub.fail_read:
            raise core.Unavailable("offline")
        return copy.deepcopy(self.hub.comments_by_issue[number])

    def events(self, number):
        return copy.deepcopy(self.hub.events_by_issue[number])

    def pages(self, path):
        if self.hub.fail_read:
            raise core.Unavailable("offline")
        if path.startswith("/issues"):
            return [
                copy.deepcopy(i)
                for i in self.hub.issues.values()
                if "state=open" not in path or i["state"] == "open"
            ]
        if path.startswith("/pulls"):
            return copy.deepcopy(list(self.hub.prs.values()))
        if path.startswith("/labels"):
            return [{"name": name} for name in self.hub.labels]
        raise AssertionError(path)

    def set_state(self, number, target):
        self.hub.calls.append(("label", number, target))
        self.hub.event(number, target, self.role)

    def notify(self, number):
        if not any(
            a["login"] == self.cfg["actors"]["owner"]
            for a in self.hub.issues[number]["assignees"]
        ):
            self.hub.calls.append(("notify", number))
            self.hub.issues[number]["assignees"].append(
                {"login": self.cfg["actors"]["owner"]}
            )

    def request(self, method, path, data=None):
        self.hub.calls.append((method, path, copy.deepcopy(data)))
        if method == "DELETE" and path.endswith("/labels/status%3Adraft"):
            if self.hub.fail_write:
                raise core.Unavailable("label removal failed")
            number = int(path.split("/")[2])
            self.hub.issues[number]["labels"] = [
                x for x in self.hub.issues[number]["labels"]
                if x["name"] != "status:draft"
            ]
            return None
        if method == "GET" and path.startswith("/pulls/"):
            return copy.deepcopy(self.hub.prs[int(path.split("/")[2])])
        if method == "GET" and "/check-runs" in path:
            return {"check_runs": self.hub.checks, "total_count": len(self.hub.checks)}
        if method == "POST" and path.endswith("/comments"):
            number = int(path.split("/")[2])
            value = {
                "user": {"login": self.cfg["actors"][self.role]},
                "body": data["body"],
            }
            if self.hub.fail_write == "before":
                raise core.Unavailable("rejected")
            self.hub.comments_by_issue[number].append(value)
            if self.hub.fail_write == "after":
                raise core.Uncertain("response lost")
            return copy.deepcopy(value)
        if method == "POST" and path == "/issues":
            if self.hub.fail_write == "before":
                raise core.Unavailable("rejected")
            number = max(self.hub.issues, default=0) + 1
            value = self.hub.issue(number, "draft", data["body"])
            value.update(
                title=data["title"],
                labels=[{"name": x} for x in data["labels"]],
                user={"login": self.cfg["actors"][self.role]},
            )
            if self.hub.fail_write == "after":
                raise core.Uncertain("response lost")
            return copy.deepcopy(value)
        if method == "POST" and path == "/labels":
            self.hub.labels.append(data["name"])
            return data
        if method == "GET":
            raise core.Unavailable("metadata unavailable")
        raise AssertionError((method, path, data))


@pytest.mark.parametrize(
    "field,value", [("owner", "runner-fixture"), ("product", "owner-fixture")]
)
def test_owner_cannot_be_an_automated_actor(tmp_path, field, value):
    cfg = policy(tmp_path)
    cfg["actors"][field] = value
    core.save(cfg["config_path"], cfg)
    with pytest.raises(ValueError):
        core.load_config(cfg["config_path"])


def test_token_identity_checked_before_any_write(tmp_path):
    cfg = policy(tmp_path)
    client = core.GitHub(cfg, token="fake")
    calls = []

    def opened(req, timeout):
        calls.append(req.full_url)
        return io.BytesIO(
            json.dumps({"data": {"viewer": {"login": cfg["actors"]["owner"]}}}).encode()
        )

    client.opener = SimpleNamespace(open=opened)
    with pytest.raises(core.Unavailable, match="Wrong automation identity"):
        client.request("PUT", "/issues/1/labels", {"labels": ["status:dispatched"]})
    assert calls == ["https://api.github.com/graphql"]


def test_transition_records_bind_actor_role_and_issue(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    client = h.client()
    r = core.public_record(cfg, 1, "job", 0, "dispatched", event=101)
    core.queue(cfg, r)
    core.flush(cfg, client)
    assert len(core.records(cfg, client.comments(1), 1)) == 1
    assert core.records(cfg, client.comments(1), 2) == []
    forged = copy.deepcopy(client.comments(1)[0])
    forged["user"]["login"] = cfg["actors"]["product"]
    value = json.loads(forged["body"].split(core.PREFIX)[1][: -len(core.SUFFIX)])
    value["actor"] = cfg["actors"]["product"]
    forged["body"] = core.record_body(value)
    assert core.records(cfg, [forged], 1) == []
    forged["user"]["login"] = "untrusted-fixture"
    assert core.records(cfg, [forged], 1) == []


@pytest.mark.parametrize("failure", ["before", "after", "read"])
def test_outbox_recovers_outage_and_never_duplicates_comments(tmp_path, failure):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    client = h.client()
    r = core.public_record(cfg, 1, "job", 0, "dispatched", event=101)
    path = core.queue(cfg, r)
    h.fail_write = failure if failure != "read" else None
    h.fail_read = failure == "read"
    core.flush(cfg, client)
    h.fail_write = None
    h.fail_read = False
    core.queue(cfg, r)
    core.flush(cfg, client)
    core.flush(cfg, client)
    assert core.read(path)["status"] == "synced"
    assert len(client.comments(1)) == 1
    assert core.state(client.issue(1)) == "dispatched"


def test_atomic_queue_does_not_reset_attempted_or_synced(tmp_path):
    cfg = policy(tmp_path)
    r = core.public_record(cfg, 1, "job", 0, "dispatched", event=101)
    path = core.queue(cfg, r)
    entry = core.read(path)
    entry.update(comment_attempted=True, status="uncertain")
    core.save(path, entry)
    for _ in range(5):
        core.queue(cfg, r)
    assert core.read(path) == entry


def test_uncertain_comment_absent_is_not_reposted(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    client = h.client()
    r = core.public_record(cfg, 1, "job", 0, "dispatched", event=101)
    path = core.queue(cfg, r)
    entry = core.read(path)
    entry.update(comment_attempted=True)
    entry["record"].update({"from": "approved", "actor": cfg["actors"]["runner"]})
    core.save(path, entry)
    core.flush(cfg, client)
    assert core.read(path)["status"] == "uncertain" and client.comments(1) == []


def test_guard_reverts_invalid_actor_and_missing_multiple_labels(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    client = h.client("workflow")
    h.event(1, "dispatched", "attacker-fixture")
    assert core.guard_issue(cfg, client, 1)
    assert core.state(client.issue(1)) == "approved"
    h.issues[1]["labels"] += [{"name": "status:blocked"}]
    assert core.guard_issue(cfg, client, 1)
    assert core.state(client.issue(1)) == "approved"
    assert not core.guard_issue(cfg, client, 1)


def test_approval_attestation_uses_owner_event_and_scope_snapshot(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    issue = h.issue()
    client = h.client("workflow")
    event = dict(
        action="labeled",
        label={"name": "status:approved"},
        sender={"login": cfg["actors"]["owner"]},
        issue=copy.deepcopy(issue),
    )
    core.attest_approval(cfg, client, event)
    proof = core.records(cfg, client.comments(1), 1)[0]
    assert proof["scope_sha256"] == hashlib.sha256(issue["body"].encode()).hexdigest()
    assert service.claim_event(cfg, issue, client.events(1))
    h.event(1, "approved", "owner")  # same-second ambiguity is never guessed
    with pytest.raises(core.Unavailable, match="ambiguous"):
        core.attest_approval(cfg, client, event)


def approval_picker_event(cfg, h, *, actor="owner", extra="draft"):
    issue = h.issue(state="draft")
    h.event(1, "approved", actor)
    issue["labels"] += [{"name": "status:" + extra}, {"name": "story"}]
    return dict(
        action="labeled",
        label={"name": "status:approved"},
        sender={"login": cfg["actors"].get(actor, actor)},
        issue=copy.deepcopy(issue),
    )


def test_owner_picker_approval_removes_draft_and_binds_event_body(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    event = approval_picker_event(cfg, h)
    # An edit after the event cannot become approved through label cleanup.
    h.issues[1]["body"] = "Changed after the owner approved."
    client = h.client("workflow")
    core.attest_approval(cfg, client, event)
    proof, = core.records(cfg, client.comments(1), 1)
    assert proof["event"] == h.events_by_issue[1][0]["id"]
    assert proof["approved_by"] == cfg["actors"]["owner"]
    assert proof["scope_sha256"] == hashlib.sha256(event["issue"]["body"].encode()).hexdigest()
    assert proof["scope_sha256"] != hashlib.sha256(h.issues[1]["body"].encode()).hexdigest()
    assert client.issue(1)["labels"] == [{"name": "status:approved"}, {"name": "story"}]
    core.attest_approval(cfg, client, event)  # retry after normalization is idempotent
    assert len(client.comments(1)) == 1
    assert sum(call[0] == "DELETE" for call in h.calls) == 1
    assert not core.guard_issue(cfg, client, 1)


@pytest.mark.parametrize("guard_between_events", [False, True])
def test_failed_draft_cleanup_recovers_with_fresh_owner_approval(tmp_path, guard_between_events):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    failed_event = approval_picker_event(cfg, h)
    client = h.client("workflow")
    h.fail_write = "before"
    with pytest.raises(core.Unavailable, match="label removal failed"):
        core.attest_approval(cfg, client, failed_event)
    assert core.records(cfg, client.comments(1), 1) == []
    assert core.canonical_state(cfg, client.events(1))[0] == "approved"
    assert {"name": "status:draft"} in client.issue(1)["labels"]

    h.fail_write = None
    # Follow the recovery guidance: remove other statuses first, then approved.
    # Unlabeling does not reset canonical history; record those events explicitly.
    for target in ("draft", "approved"):
        h.issues[1]["labels"] = [
            x for x in h.issues[1]["labels"] if x["name"] != "status:" + target
        ]
        h.counter += 1
        h.events_by_issue[1].append(dict(
            id=h.counter,
            event="unlabeled",
            label={"name": "status:" + target},
            actor={"login": cfg["actors"]["owner"]},
            created_at=WHEN,
        ))
        if guard_between_events:
            core.guard_issue(cfg, client, 1)

    issue = h.issues[1]
    issue["body"] = "Revised scope for fresh owner approval."
    issue["updated_at"] = "2026-01-02T10:01:00+00:00"
    fresh = h.event(1, "approved")
    fresh["created_at"] = issue["updated_at"]
    event = dict(
        action="labeled",
        label={"name": "status:approved"},
        sender={"login": cfg["actors"]["owner"]},
        issue=copy.deepcopy(issue),
    )
    issue["body"] = "Unapproved edit after the fresh event."
    core.attest_approval(cfg, client, event)
    proof, = [r for r in core.records(cfg, client.comments(1), 1) if r["kind"] == "approval"]
    assert proof["event"] == fresh["id"] != h.events_by_issue[1][0]["id"]
    assert proof["approved_by"] == cfg["actors"]["owner"]
    assert proof["scope_sha256"] == hashlib.sha256(event["issue"]["body"].encode()).hexdigest()
    assert proof["scope_sha256"] != hashlib.sha256(issue["body"].encode()).hexdigest()
    assert core.state(client.issue(1)) == "approved"
    assert sum(call[0] == "DELETE" for call in h.calls) == 1


def test_draft_cleanup_transport_is_scoped_to_one_label(tmp_path):
    cfg = policy(tmp_path)
    client = core.GitHub(cfg, "workflow", token="fake")
    client.identity_verified = True
    requests = []

    def opened(req, timeout):
        requests.append(req)
        return io.BytesIO(b'[{"name":"status:approved"}]')

    client.opener = SimpleNamespace(open=opened)
    client.request("DELETE", "/issues/1/labels/status%3Adraft")
    assert requests[0].method == "DELETE"
    assert requests[0].full_url == (
        "https://api.github.com/repos/example/project/issues/1/labels/status%3Adraft"
    )
    for path in ("/issues/1/labels/status%3Aapproved", "/issues/1", "/labels"):
        with pytest.raises(ValueError, match="Only repository issue/label writes"):
            client.request("DELETE", path)
    assert len(requests) == 1


def test_picker_cleanup_preserves_concurrent_status_and_does_not_attest(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    event = approval_picker_event(cfg, h)
    client = h.client("workflow")
    request = client.request

    def concurrent_request(method, path, data=None):
        if method == "DELETE":
            h.issues[1]["labels"].append({"name": "status:blocked"})
        return request(method, path, data)

    monkeypatch.setattr(client, "request", concurrent_request)
    with pytest.raises(core.Unavailable, match="valid status labels"):
        core.attest_approval(cfg, client, event)
    assert {"name": "status:blocked"} in client.issue(1)["labels"]
    assert core.records(cfg, client.comments(1), 1) == []


@pytest.mark.parametrize("actor,extra", [
    ("attacker-fixture", "draft"),
    ("runner", "draft"),
    ("owner", "blocked"),
    ("owner", "unknown"),
])
def test_picker_invalid_approval_fails_closed_with_recovery_comment(tmp_path, actor, extra):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    event = approval_picker_event(cfg, h, actor=actor, extra=extra)
    client = h.client("workflow")
    with pytest.raises(core.Unavailable, match="valid status labels"):
        core.attest_approval(cfg, client, event)
    assert core.records(cfg, client.comments(1), 1) == []
    assert "Only the configured owner" in client.comments(1)[0]["body"]
    assert "remove status:approved" in client.comments(1)[0]["body"]
    assert not any(call[0] == "DELETE" for call in h.calls)
    assert not list((Path(cfg["state_dir"]) / "outbox").glob("*.json"))


def test_picker_approval_rejects_live_label_drift(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    event = approval_picker_event(cfg, h)
    h.issues[1]["labels"].append({"name": "status:blocked"})
    with pytest.raises(core.Unavailable):
        core.attest_approval(cfg, h.client("workflow"), event)
    assert not any(call[0] == "DELETE" for call in h.calls)
    assert core.records(cfg, h.comments_by_issue[1], 1) == []


@pytest.mark.parametrize("failure", ["removal", "ambiguous", "forbidden"])
def test_picker_cleanup_prerequisites_fail_before_attestation(tmp_path, failure):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    event = approval_picker_event(cfg, h)
    if failure == "removal":
        h.fail_write = "before"
    elif failure == "ambiguous":
        h.events_by_issue[1].append(copy.deepcopy(h.events_by_issue[1][0]))
    else:
        cfg["transitions"]["draft"].pop("approved")
    with pytest.raises(core.Unavailable):
        core.attest_approval(cfg, h.client("workflow"), event)
    assert core.records(cfg, h.comments_by_issue[1], 1) == []
    assert not list((Path(cfg["state_dir"]) / "outbox").glob("*.json"))


def configured_service(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    h.approve()
    repo = Path(cfg["repository_root"])
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "seed",
            "--allow-empty",
        ],
        check=True,
    )
    profile = tmp_path / "profile.json"
    core.save(
        profile,
        dict(
            worker_prompt="Implement.",
            reviewer_prompt="Review.",
            scope="Synthetic only.",
            extra_scopes={},
            coordination_files=[],
        ),
    )
    runner = fixtures.runner
    settings = dict(
        worktree=str(tmp_path / "unused"),
        branch="feature",
        state_dir=str(tmp_path / "unused-state"),
        agent_home=str(tmp_path / "agent-home"),
        profile=str(profile),
        worker_model="fixture",
        reviewer_model="fixture",
        agent=fixtures.agent_config("/must-not-launch"),
        commit_identity={"name": "Fixture", "email": "fixture@example.invalid"},
    )
    core.save(cfg["runner_config"], settings)
    core.save(cfg["primary_runner_config"], settings)
    monkeypatch.setattr(service, "runner_module", lambda: runner)
    monkeypatch.setattr(runner, "_LIFECYCLE", core)
    monkeypatch.setattr(core, "GitHub", lambda c, role="runner", **kw: h.client(role))
    monkeypatch.setattr(runner, "check_cli", lambda c: "fixture version")
    return cfg, h, runner


def test_claim_before_enqueue_repeat_poll_and_later_reapproval(tmp_path, monkeypatch):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    assert service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 100) == [
        "issue-1-event-101"
    ]
    claims = list((Path(cfg["state_dir"]) / "claims").glob("*.json"))
    assert len(claims) == 1
    service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 200)
    assert (
        len(list((Path(cfg["state_dir"]) / "jobs").glob("*/runner-state/tasks/*.json")))
        == 1
    )
    h.event(1, "approved", "owner")
    h.approve()
    service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 300)
    assert len(list((Path(cfg["state_dir"]) / "claims").glob("*.json"))) == 2


def test_scope_edit_or_owner_stop_prevents_dispatch(tmp_path, monkeypatch):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    h.issues[1]["body"] = "changed after approval"
    assert service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 100) == []
    assert not list((Path(cfg["state_dir"]) / "claims").glob("*.json"))
    h.event(1, "approved", "owner")
    h.approve()
    service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 200)
    h.event(1, "needs-owner", "owner")
    monkeypatch.setattr(runner, "run_once", lambda *a: pytest.fail("must not start"))
    service.poll(cfg, h.client(), clock=lambda: 300)
    claim = core.read(next((Path(cfg["state_dir"]) / "claims").glob("*.json")))
    assert claim["stage"] == "suspended"


def test_runner_phases_pushback_and_minor_followup_privacy(tmp_path, monkeypatch):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)

    def execute(config, state, path, job, role):
        job.update(status="running_" + role, updated_at=core.now())
        runner.save_job(config, path, job)
        if role == "worker":
            fixtures.deliver(config)
            return fixtures.result("ready_for_review")
        value = fixtures.result("needs_changes", "reviewer")
        value["findings"][0].update(
            severity="P1" if job["round"] == 1 else "P2",
            path="/workspace/private.bin",
            detail="PRIVATE-AGENT-OUTPUT",
        )
        return value

    monkeypatch.setattr(runner, "execute", execute)
    service.poll(cfg, h.client(), clock=lambda: 100)
    job = service.jobs(cfg)[0]
    assert job["status"] == "ready_for_integration" and len(job["runner_commits"]) == 2
    assert len(job["review_history"]) == 2
    public = json.dumps(h.comments_by_issue) + json.dumps(h.issues)
    assert (
        "PRIVATE-AGENT-OUTPUT" not in public and "/workspace/private.bin" not in public
    )
    assert any("Minor follow-up" in i.get("title", "") for i in h.issues.values())
    records = core.records(cfg, h.comments_by_issue[1], 1)
    assert {
        "dispatched",
        "in-progress",
        "committed",
        "in-review",
        "changes-requested",
    } <= {r["to"] for r in records}


def test_reconcile_reports_mismatch_without_writes(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    job = dict(
        id="job",
        round=1,
        status="running_worker",
        assignment={"lifecycle": {"issue": 1, "event": 101}},
        runner_commits=[],
    )
    assert core.reconcile(cfg, [job], h.client()) == [
        dict(issue=1, job="job", local="in-progress", remote="approved")
    ]
    assert h.calls == []


def test_workflow_uses_default_branch_no_pr_code_and_narrow_permissions():
    text = (ROOT / "templates/task-lifecycle.yml").read_text()
    assert (ROOT / ".github/workflows/task-lifecycle.yml").read_text() == text
    assert "ref: ${{ github.event.repository.default_branch }}" in text
    assert "pull_request.head" not in text and "pull_request.base" not in text
    assert "issues: write" in text and "contents: write" not in text
    assert "persist-credentials: false" in text and "secrets." not in text
    assert "python -I lifecycle/core.py" in text


@pytest.mark.parametrize(
    "verdict,rounds,status",
    [("blocked", 1, "needs_owner"), ("needs_changes", 3, "needs_primary_review")],
)
def test_blocked_has_no_rework_and_exhaustion_uses_two_corrections(
    tmp_path, monkeypatch, verdict, rounds, status
):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)

    def execute(config, state, path, job, role):
        job.update(status="running_" + role, updated_at=core.now())
        runner.save_job(config, path, job)
        if role == "worker":
            fixtures.deliver(config)
            return fixtures.result("ready_for_review")
        return fixtures.result(verdict, "reviewer")

    monkeypatch.setattr(runner, "execute", execute)
    service.poll(cfg, h.client(), clock=lambda: 100)
    job = service.jobs(cfg)[0]
    assert (
        job["status"] == status
        and job["round"] == rounds
        and len(job["review_history"]) == rounds
    )
    stopped = [r for r in core.records(cfg, h.comments_by_issue[1], 1)
               if r["to"] in {"needs-owner", "needs-primary"}]
    assert len(stopped) == 1 and stopped[0]["reason"] == "reviewer_reported_blocked"
    assert "owner:" in core.record_body(stopped[0])


@pytest.mark.parametrize("attempts", [3, 5])
@pytest.mark.parametrize("cause,expected", [
    ("unavailable", "Story tests couldn't run because a required tool"),
    ("attempts_exhausted", "Story tests still failing after {attempts} check attempts"),
    ("unrelated_failure", "Story tests failed for a reason unrelated"),
])
def test_worker_check_stop_is_immediate_public_and_not_replayed(
    tmp_path, monkeypatch, cause, expected, attempts
):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    settings = json.loads(Path(cfg["runner_config"]).read_text())
    settings["worker_check_attempts"] = attempts
    core.save(cfg["runner_config"], settings)
    calls = []

    def execute(config, state, path, job, role):
        calls.append(role)
        assert role == "worker"
        job.update(status="running_worker", updated_at=core.now())
        runner.save_job(config, path, job)
        fixtures.deliver(config)
        value = fixtures.result("blocked")
        value.update(blocked_check="story_tests", blocked_cause=cause,
                     summary="MISLEADING-CAUSE /private/secret/tool LOG-SECRET")
        return value

    monkeypatch.setattr(runner, "execute", execute)
    service.poll(cfg, h.client(), clock=lambda: 100)
    job = service.jobs(cfg)[0]
    assert calls == ["worker"] and job["status"] == "needs_owner"
    assert not job.get("runner_commits") and not job.get("verification_runs")
    stopped = [r for r in core.records(cfg, h.comments_by_issue[1], 1)
               if r["to"] == "needs-owner"]
    suffix = cause + ("_" + str(attempts) if cause == "attempts_exhausted" else "")
    assert len(stopped) == 1 and stopped[0]["reason"] == "worker_story_tests_" + suffix
    assert job["worker_check_attempts"] == attempts
    assert job["worker"]["blocked_cause"] == cause
    body = next(c["body"] for c in h.comments_by_issue[1]
                if stopped[0]["key"] in c["body"])
    assert expected.format(attempts=attempts) in body
    if cause == "unavailable":
        assert "still failing" not in body
    else:
        assert "couldn't run" not in body
    assert "MISLEADING-CAUSE" not in body
    assert "owner:" in body and "authorize a new task" in body
    assert "/private/secret" not in body and "LOG-SECRET" not in body
    before = list(h.comments_by_issue[1])
    service.poll(cfg, h.client(), clock=lambda: 200)
    assert calls == ["worker"] and before == h.comments_by_issue[1]


@pytest.mark.parametrize("status", [
    "blocked", "failed", "interrupted", "needs_owner", "needs_primary_review",
])
def test_every_runner_stop_records_fixed_reason_and_safe_issue_line(tmp_path, status):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue(state="in-review")
    job = dict(id="stop-job", round=1, status=status,
               assignment={"lifecycle": {"issue": 1, "event": 101}},
               error="/private/secret/trace LOG-SECRET",
               blocked_reason={"reason_code": "untrusted_private_value", "reason": "LOG-SECRET"},
               integration={"reason": "untrusted_private_value"})
    record = core.job_records(cfg, job)[0]
    assert record["reason"] in core.STOP_REASONS
    assert "untrusted_private_value" not in json.dumps(record)
    core.sync_job(cfg["config_path"], job, h.client())
    comments = [c for c in h.comments_by_issue[1] if record["key"] in c["body"]]
    assert len(comments) == 1 and "owner:" in comments[0]["body"]
    assert "/private/secret" not in comments[0]["body"] and "LOG-SECRET" not in comments[0]["body"]
    core.sync_job(cfg["config_path"], job, h.client())
    assert sum(record["key"] in c["body"] for c in h.comments_by_issue[1]) == 1


@pytest.mark.parametrize("failure", ["before", "after"])
def test_stop_line_survives_transport_failure_without_duplicate(tmp_path, failure):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue(state="in-progress")
    job = dict(id="stop-job", round=1, status="needs_owner",
               assignment={"lifecycle": {"issue": 1, "event": 101}},
               worker={"status": "blocked", "blocked_check": "build"})
    h.fail_write = failure
    core.sync_job(cfg["config_path"], job, h.client())
    h.fail_write = None
    core.sync_job(cfg["config_path"], job, h.client())
    comments = [c for c in h.comments_by_issue[1] if '"job": "stop-job"' in c["body"]]
    assert len(comments) == 1
    assert "Build: worker reported blocked without a specific cause" in comments[0]["body"] and "owner:" in comments[0]["body"]


def test_concurrent_stop_flush_posts_one_safe_line(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue(state="in-progress")
    job = dict(id="stop-job", round=1, status="needs_owner",
               assignment={"lifecycle": {"issue": 1, "event": 101}},
               worker={"status": "blocked", "blocked_check": "story_tests"})
    record = core.job_records(cfg, job)[0]
    core.queue(cfg, record)
    client = h.client()
    original = client.request
    entered, release = threading.Event(), threading.Event()

    def slow_post(method, path, data=None):
        if method == "POST":
            entered.set()
            assert release.wait(5)
        return original(method, path, data)

    client.request = slow_post
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(core.flush, cfg, client)
        assert entered.wait(5)
        try:
            core.flush(cfg, h.client())
        finally:
            release.set()
        first.result()
    comments = [c for c in h.comments_by_issue[1] if record["key"] in c["body"]]
    assert len(comments) == 1 and "Story tests: worker reported blocked without a specific cause" in comments[0]["body"]


@pytest.mark.parametrize(
    "kind,status",
    [
        ("technical", "needs_primary_review"),
        ("product", "needs_owner"),
        ("scope", "needs_owner"),
    ],
)
def test_one_disagreement_reconsideration_same_exact_head(
    tmp_path, monkeypatch, kind, status
):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    reviews = []

    def execute(config, state, path, job, role):
        job.update(status="running_" + role, updated_at=core.now())
        runner.save_job(config, path, job)
        if role == "worker":
            value = fixtures.result("ready_for_review")
            if job["round"] == 1:
                fixtures.deliver(config)
            else:
                value["disagreement"] = {
                    "kind": kind,
                    "reason": "Synthetic reason, retained privately.",
                }
            return value
        reviews.append(job["head_commit"])
        return fixtures.result("needs_changes", "reviewer")

    monkeypatch.setattr(runner, "execute", execute)
    service.poll(cfg, h.client(), clock=lambda: 100)
    job = service.jobs(cfg)[0]
    assert job["status"] == status and job["disagreement_exchanges"] == 1
    assert (
        len(reviews) == 2 and len(set(reviews)) == 1 and len(job["runner_commits"]) == 1
    )


def test_claim_recovers_after_comment_and_after_enqueue_without_duplicate_job(
    tmp_path, monkeypatch
):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    original = runner.enqueue
    calls = []

    def crash(*args, **kwargs):
        path = original(*args, **kwargs)
        calls.append(path)
        raise RuntimeError("crash after durable enqueue")

    monkeypatch.setattr(runner, "enqueue", crash)
    with pytest.raises(RuntimeError):
        service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 100)
    monkeypatch.setattr(runner, "enqueue", original)
    service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 200)
    assert len(service.jobs(cfg)) == 1 and len(calls) == 1
    records = core.records(cfg, h.comments_by_issue[1], 1)
    assert sum(r["to"] == "dispatched" for r in records) == 1


def test_claim_crash_after_record_and_git_outage_does_not_start_duplicate(
    tmp_path, monkeypatch
):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    original = service.ensure_worktree
    monkeypatch.setattr(
        service,
        "ensure_worktree",
        lambda *a: (_ for _ in ()).throw(core.Unavailable("git unavailable")),
    )
    with pytest.raises(core.Unavailable):
        service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 100)
    assert len(list((Path(cfg["state_dir"]) / "claims").glob("*.json"))) == 1
    monkeypatch.setattr(service, "ensure_worktree", original)
    service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 200)
    assert len(service.jobs(cfg)) == 1


def test_unclean_owned_phase_is_interrupted_without_model_replay(tmp_path, monkeypatch):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 100)
    path = next((Path(cfg["state_dir"]) / "jobs").glob("*/runner-state/tasks/*.json"))
    job = core.read(path)
    job.update(status="running_worker", child_pid=999999)
    core.save(path, job)
    h.event(1, "in-progress", "runner")
    monkeypatch.setattr(
        runner, "execute", lambda *a: pytest.fail("never replay crashed phase")
    )
    service.poll(cfg, h.client(), clock=lambda: 200)
    assert core.read(path)["status"] == "interrupted"


def test_state_binding_prevents_cross_repository_reuse(tmp_path):
    cfg = policy(tmp_path)
    core.bind_state(cfg)
    changed = copy.deepcopy(cfg)
    changed["repository"] = "example/other"
    with pytest.raises(ValueError, match="another repository"):
        core.bind_state(changed)


def test_owned_cleanup_preserves_dirty_and_unmerged_work(tmp_path, monkeypatch):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 100)
    path = next((Path(cfg["state_dir"]) / "claims").glob("*.json"))
    claim = core.read(path)
    root = Path(claim["worktree"])
    (root / "private.txt").write_text("synthetic local edit")
    issue = copy.deepcopy(h.issues[1])
    issue.update(state="closed", labels=[{"name": "status:merged"}])
    service.cleanup(cfg, claim, issue)
    assert root.exists() and claim["cleanup_pending"] == "dirty_worktree_requires_owner"
    (root / "private.txt").unlink()
    service.cleanup(cfg, claim, issue)
    assert not root.exists() and claim["cleaned"]


def test_service_git_does_not_inherit_git_dir_or_execute_hooks(tmp_path, monkeypatch):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    trap = Path(cfg["repository_root"]) / "hook-trap"
    hook = Path(cfg["repository_root"]) / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\ntouch " + str(trap) + "\n")
    hook.chmod(0o700)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "must-not-read"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "must-not-write"))
    service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 100)
    assert not trap.exists() and len(service.jobs(cfg)) == 1


def dispatch_comment(cfg, issue=1):
    record = core.public_record(cfg, issue, "job", 0, "dispatched", event=101)
    record.update({"from": "approved", "actor": cfg["actors"]["runner"]})
    return {
        "user": {"login": cfg["actors"]["runner"]},
        "body": core.record_body(record),
    }


def pr_fixture(cfg, h):
    sha = "a" * 40
    h.issue(state="in-review")
    h.comments_by_issue[1].append(dispatch_comment(cfg))
    review = core.public_record(
        cfg,
        1,
        "job",
        1,
        "in-review",
        commit=sha,
        kind="review",
        outcome="ready_for_integration",
    )
    review.update({"from": "in-review", "actor": cfg["actors"]["runner"]})
    h.comments_by_issue[1].append(
        {"user": {"login": cfg["actors"]["runner"]}, "body": core.record_body(review)}
    )
    pr = dict(
        number=7,
        state="open",
        merged=False,
        body="Closes #1",
        head={"sha": sha, "repo": {"full_name": cfg["repository"]}},
        base={"ref": "main", "sha": "b" * 40},
        user={"login": cfg["actors"]["owner"]},
    )
    h.prs[7] = pr
    return pr


@pytest.mark.parametrize(
    "action,merged,target",
    [
        ("opened", False, "pr-open"),
        ("closed", False, "needs-owner"),
        ("closed", True, "merged"),
    ],
)
def test_pr_lifecycle_requires_exact_trusted_review(tmp_path, action, merged, target):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    if action == "closed":
        h.event(1, "pr-open", "workflow")
    pr.update(merged=merged, merge_commit_sha="b" * 40 if merged else None)
    core.pr_event(
        cfg, h.client("workflow"), {"action": action, "pull_request": copy.deepcopy(pr)}
    )
    assert core.state(h.issues[1]) == target
    assert len(h.comments_by_issue[1]) == 3


@pytest.mark.parametrize("mutation", ["fork", "head", "author"])
def test_unrelated_pr_cannot_advance_task(tmp_path, mutation):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    if mutation == "fork":
        pr["head"]["repo"]["full_name"] = "example/foreign"
    elif mutation == "head":
        pr["head"]["sha"] = "c" * 40
    elif mutation == "author":
        pr["user"]["login"] = "attacker-fixture"
    with pytest.raises(core.Unavailable):
        core.pr_event(
            cfg,
            h.client("workflow"),
            {"action": "opened", "pull_request": copy.deepcopy(pr)},
        )
    assert core.state(h.issues[1]) != "pr-open"


def test_atomic_queue_concurrent_with_flush(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    client = h.client()
    record = core.public_record(cfg, 1, "job", 0, "dispatched", event=101)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: core.queue(cfg, record), range(20)))
    core.flush(cfg, client)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: core.queue(cfg, record), range(20)))
    core.flush(cfg, client)
    assert len(client.comments(1)) == 1
    assert core.read(core.queue(cfg, record))["status"] == "synced"


@pytest.mark.parametrize(
    "legacy,canonical",
    [
        ("critical", "P0"),
        ("high", "P1"),
        ("medium", "P1"),
        ("low", "P2"),
        ("minor", "P2"),
    ],
)
def test_historical_severity_mapping_does_not_downgrade_medium(legacy, canonical):
    assert core.canonical_severity(legacy) == canonical
    with pytest.raises(ValueError):
        core.canonical_severity("unknown-fixture")


def test_primary_dispatch_inherits_failed_head_and_reviews_original_range(
    tmp_path, monkeypatch
):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)

    def execute(config, state, path, job, role):
        job.update(status="running_" + role, updated_at=core.now())
        runner.save_job(config, path, job)
        if role == "worker":
            fixtures.deliver(config)
            return fixtures.result("ready_for_review")
        return fixtures.result("needs_changes", "reviewer")

    monkeypatch.setattr(runner, "execute", execute)
    service.poll(cfg, h.client(), clock=lambda: 100)
    original = service.jobs(cfg)[0]
    assert original["status"] == "needs_primary_review"
    service.poll(cfg, h.client(), clock=lambda: 200)
    all_jobs = service.jobs(cfg)
    primary = next(j for j in all_jobs if j["assignment"]["lifecycle"]["primary"])
    assert primary["base_commit"] == original["head_commit"]
    assert primary["review_base"] == original["base_commit"]
    assert primary["status"] == "needs_owner"
    prompt = runner.prompt_for(
        runner.load_config(
            next((Path(cfg["state_dir"]) / "jobs").glob(primary["id"] + "/config.json"))
        ),
        primary,
        "reviewer",
    )
    assert original["base_commit"] + ".." + primary["head_commit"] in prompt


def test_partial_or_name_colliding_gate_is_not_pass(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    client = h.client("workflow")
    h.checks = [
        dict(
            id=1,
            name="review-gate",
            app={"slug": "github-actions"},
            head_sha=pr["base"]["sha"],
            details_url="https://github.com/example/project/actions/runs/90/job/91",
            status="completed",
            conclusion="success",
        )
    ]
    original = client.request

    def request(method, path, data=None):
        if path == "/actions/workflows/pr-review-gate.yml":
            return {"id": 7, "path": ".github/workflows/pr-review-gate.yml"}
        if path == "/actions/runs/90":
            return dict(
                workflow_id=7,
                path=".github/workflows/pr-review-gate.yml",
                event="pull_request",
                head_sha=pr["base"]["sha"],
                pull_requests=[{"number": 7, "head": {"sha": pr["head"]["sha"]}}],
            )
        return original(method, path, data)

    client.request = request
    assert core.trusted_gate(cfg, client, pr) == "unavailable"

    def incomplete(method, path, data=None):
        if "/check-runs" in path:
            return {"total_count": 2, "check_runs": h.checks}
        return request(method, path, data)

    client.request = incomplete
    assert core.trusted_gate(cfg, client, pr) == "unavailable"


@pytest.fixture
def gate_evidence(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    client = h.client("workflow")
    check = dict(
        id=1,
        name="review-gate",
        app={"slug": "github-actions"},
        head_sha=pr["base"]["sha"],
        details_url="https://github.com/example/project/actions/runs/90/job/91",
        status="completed",
        conclusion="success",
    )
    run = dict(
        workflow_id=7,
        path=".github/workflows/pr-review-gate.yml",
        event="pull_request_target",
        head_sha=pr["base"]["sha"],
        pull_requests=[{"number": 7, "head": {"sha": pr["head"]["sha"]}}],
    )
    checks = [check]
    runs = {"90": run}

    def request(method, path, data=None):
        if "/check-runs" in path:
            assert path == f"/commits/{pr['base']['sha']}/check-runs?filter=all&per_page=100&page=1"
            return {"total_count": len(checks), "check_runs": checks}
        if path == "/actions/workflows/pr-review-gate.yml":
            return {"id": 7, "path": run["path"]}
        if path.startswith("/actions/runs/"):
            return runs[path.rsplit("/", 1)[-1]]
        raise core.Unavailable("unknown metadata")

    client.request = request
    return cfg, client, pr, check, run, checks, runs


def test_trusted_gate_binds_workflow_and_exact_head(gate_evidence):
    cfg, client, pr, check, run, _, _ = gate_evidence
    assert core.trusted_gate(cfg, client, pr) == "pass"
    assert core.trusted_gate(cfg, client, pr, strict_snapshot=True) == "unavailable"
    run["pull_requests"][0]["base"] = {"sha": pr["base"]["sha"]}
    assert core.trusted_gate(cfg, client, pr, strict_snapshot=True) == "pass"
    run["pull_requests"][0]["base"]["sha"] = "c" * 40
    assert core.trusted_gate(cfg, client, pr, strict_snapshot=True) == "unavailable"
    run["head_sha"] = "c" * 40
    assert core.trusted_gate(cfg, client, pr) == "unavailable"


@pytest.mark.parametrize("mutation", ["head", "number", "base", "check_sha", "run_sha", "event", "workflow", "app"])
def test_base_gate_still_rejects_unbound_or_untrusted_runs(gate_evidence, mutation):
    cfg, client, pr, check, run, _, _ = gate_evidence
    binding = run["pull_requests"][0]
    binding["base"] = {"sha": pr["base"]["sha"]}
    if mutation in {"head", "base"}:
        binding[mutation]["sha"] = "c" * 40
    elif mutation == "number":
        binding["number"] = 8
    elif mutation == "check_sha":
        check["head_sha"] = pr["head"]["sha"]
    elif mutation == "run_sha":
        run["head_sha"] = pr["head"]["sha"]
    elif mutation == "event":
        run["event"] = "pull_request"
    elif mutation == "workflow":
        run["workflow_id"] = 8
    else:
        check["app"]["slug"] = "other-app"
    assert core.trusted_gate(cfg, client, pr, strict_snapshot=True) == "unavailable"


@pytest.mark.parametrize("status,conclusion,expected", [
    ("completed", "success", "pass"),
    ("completed", "failure", "fail"),
    ("completed", "cancelled", "fail"),
    ("in_progress", None, "pending"),
])
def test_base_gate_selects_latest_matching_pr_run(gate_evidence, status, conclusion, expected):
    cfg, client, pr, check, run, checks, runs = gate_evidence
    run["pull_requests"][0]["base"] = {"sha": pr["base"]["sha"]}
    checks.append({**check, "id": 2, "status": status, "conclusion": conclusion,
                   "details_url": "https://github.com/example/project/actions/runs/92/job/93"})
    runs["92"] = copy.deepcopy(run)
    # A newer check on the same base for a different PR must not mask this PR.
    checks.append({**check, "id": 3,
                   "details_url": "https://github.com/example/project/actions/runs/94/job/95"})
    runs["94"] = copy.deepcopy(run)
    runs["94"]["pull_requests"][0]["number"] = 8
    assert core.trusted_gate(cfg, client, pr, strict_snapshot=True) == expected


@pytest.mark.parametrize("collection", ["complete", "partial", "duplicate", "drift"])
def test_base_gate_collection_is_complete_across_pages(gate_evidence, collection):
    cfg, client, pr, check, run, _, _ = gate_evidence
    run["pull_requests"][0]["base"] = {"sha": pr["base"]["sha"]}
    original = client.request
    seen = []

    def request(method, path, data=None):
        if "/check-runs?" not in path:
            return original(method, path, data)
        assert path.startswith(f"/commits/{pr['base']['sha']}/check-runs?filter=all&")
        page = int(path.rsplit("=", 1)[-1])
        seen.append(page)
        if page == 1:
            # Other checks sharing the base precede this PR's gate.
            return {"total_count": 101, "check_runs": [
                {"id": n, "name": "unrelated"} for n in range(1, 101)]}
        row = {**check, "id": 1 if collection == "duplicate" else 101}
        return {"total_count": 102 if collection == "drift" else 101,
                "check_runs": [] if collection == "partial" else [row]}

    client.request = request
    expected = "pass" if collection == "complete" else "unavailable"
    assert core.trusted_gate(cfg, client, pr, strict_snapshot=True) == expected
    assert seen == [1, 2]


def test_new_owner_approval_supersedes_stale_phase_without_replaying_it(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue()
    h.approve()
    client = h.client()
    record = core.public_record(
        cfg, 1, "issue-1-event-101", 1, "in-progress", event=101
    )
    path = core.queue(cfg, record)
    # Old phase cannot advance directly from approved; it becomes a conflict.
    core.flush(cfg, client)
    assert core.read(path)["status"] == "conflict"
    h.event(1, "approved", "owner")
    h.approve()
    core.flush(cfg, client)
    assert core.read(path)["status"] == "superseded"
    assert core.state(h.issues[1]) == "approved"


def test_workflow_uncertain_rerun_does_not_repeat_absent_comment(tmp_path):
    cfg = policy(tmp_path)
    cfg["uncertain_workflow_resume"] = True
    h = Hub(cfg)
    h.issue()
    client = h.client()
    record = core.public_record(cfg, 1, "job", 0, "dispatched", event=101)
    path = core.queue(cfg, record)
    core.flush(cfg, client)
    assert core.read(path)["status"] == "uncertain" and not client.comments(1)


@pytest.mark.parametrize("closed", [False, True])
def test_claim_recovers_durable_terminal_job_without_readmission(
    tmp_path, monkeypatch, closed
):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 100)
    path = next((Path(cfg["state_dir"]) / "jobs").glob("*/runner-state/tasks/*.json"))
    job = core.read(path)
    job.update(status="ready_for_integration", head_commit="a" * 40)
    core.save(path, job)
    h.event(1, "in-progress", "runner")
    h.event(1, "committed", "runner")
    h.event(1, "in-review", "runner")
    if closed:
        h.issues[1]["state"] = "closed"
    monkeypatch.setattr(
        runner, "run_once", lambda *a: pytest.fail("never replay completed job")
    )
    service.poll(cfg, h.client(), clock=lambda: 200)
    claim_path = next((Path(cfg["state_dir"]) / "claims").glob("*.json"))
    assert core.read(claim_path)["stage"] == "finished"
    assert core.read(claim_path)["head"] == "a" * 40
    service.poll(cfg, h.client(), clock=lambda: 300)
    assert core.read(claim_path)["stage"] == "finished"


@pytest.mark.parametrize(
    "action,merged", [("opened", False), ("closed", False), ("closed", True)]
)
def test_non_runner_pr_closing_issue_is_noop(
    tmp_path, monkeypatch, capsys, action, merged
):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    h.comments_by_issue[1] = []
    pr.update(merged=merged, user={"login": "outside-contributor"})
    pr["head"]["repo"]["full_name"] = "outside/project"
    before = copy.deepcopy(h.issues)
    monkeypatch.setattr(
        core,
        "trusted_gate",
        lambda *a: pytest.fail("unmanaged PR needs no task gate query"),
    )
    core.pr_event(
        cfg, h.client("workflow"), {"action": action, "pull_request": copy.deepcopy(pr)}
    )
    assert "not a lifecycle task; skipped" in capsys.readouterr().out
    assert h.issues == before and h.comments_by_issue[1] == []
    assert not (Path(cfg["state_dir"]) / "outbox").exists()


@pytest.mark.parametrize("source", ["untrusted", "product_info", "guard", "review"])
def test_only_authenticated_runner_transition_dispatch_marks_managed(
    tmp_path, monkeypatch, capsys, source
):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    forged = dispatch_comment(cfg)
    value = json.loads(forged["body"].split(core.PREFIX)[1][: -len(core.SUFFIX)])
    if source == "untrusted":
        forged["user"]["login"] = "untrusted-fixture"
    else:
        role, kind = {
            "product_info": ("product", "clarification"),
            "guard": ("workflow", "guard"),
            "review": ("runner", "review"),
        }[source]
        value = core.public_record(
            cfg,
            1,
            "job",
            0,
            "dispatched",
            kind=kind,
            reason="invalid_status_change" if kind == "guard" else None,
        )
        value.update({"from": "dispatched", "actor": cfg["actors"][role]})
        forged["user"]["login"] = cfg["actors"][role]
    forged["body"] = core.record_body(value)
    h.comments_by_issue[1] = [forged]
    before = copy.deepcopy(h.issues)
    monkeypatch.setattr(
        core, "trusted_gate", lambda *a: pytest.fail("no trusted dispatch")
    )
    core.pr_event(
        cfg,
        h.client("workflow"),
        {"action": "opened", "pull_request": copy.deepcopy(pr)},
    )
    assert "not a lifecycle task; skipped" in capsys.readouterr().out
    assert h.issues == before and len(h.comments_by_issue[1]) == 1
    assert not (Path(cfg["state_dir"]) / "outbox").exists()


def test_managed_pr_missing_exact_review_still_fails(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    h.comments_by_issue[1] = [dispatch_comment(cfg)]
    with pytest.raises(core.Unavailable, match="no trusted successful task review"):
        core.pr_event(
            cfg,
            h.client("workflow"),
            {"action": "opened", "pull_request": copy.deepcopy(pr)},
        )
    assert core.state(h.issues[1]) == "in-review"
    assert not (Path(cfg["state_dir"]) / "outbox").exists()


def test_mixed_managed_and_unmanaged_links_change_only_managed_issue(
    tmp_path, monkeypatch, capsys
):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    h.issue(2, state="draft")
    pr["body"] = "Closes #2 and Closes #1"
    untouched = copy.deepcopy(h.issues[2])
    queried = []
    monkeypatch.setattr(core, "trusted_gate", lambda *a: queried.append(True) or "pass")
    core.pr_event(
        cfg,
        h.client("workflow"),
        {"action": "opened", "pull_request": copy.deepcopy(pr)},
    )
    assert core.state(h.issues[1]) == "pr-open" and h.issues[2] == untouched
    assert h.comments_by_issue[2] == [] and queried == [True]
    assert "Issue #2: not a lifecycle task; skipped" in capsys.readouterr().out
    records = core.records(cfg, h.comments_by_issue[1], 1)
    assert records[-1]["to"] == "pr-open" and records[-1]["gate"] == "pass"


def test_all_managed_reviews_validated_before_any_intent_is_queued(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    h.issue(2, state="in-review")
    h.comments_by_issue[2] = [dispatch_comment(cfg, 2)]
    pr["body"] = "Closes #1 and Closes #2"
    before = copy.deepcopy(h.issues)
    with pytest.raises(core.Unavailable, match="no trusted successful task review"):
        core.pr_event(
            cfg,
            h.client("workflow"),
            {"action": "opened", "pull_request": copy.deepcopy(pr)},
        )
    assert h.issues == before and not (Path(cfg["state_dir"]) / "outbox").exists()


def test_unmanaged_pr_does_not_flush_preexisting_intents(tmp_path, capsys):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    h.comments_by_issue[1] = []
    pending = core.queue(cfg, core.public_record(cfg, 1, "pending", 0, "needs-owner"))
    before = core.read(pending)
    core.pr_event(
        cfg,
        h.client("workflow"),
        {"action": "opened", "pull_request": copy.deepcopy(pr)},
    )
    assert "not a lifecycle task; skipped" in capsys.readouterr().out
    assert core.read(pending) == before and core.state(h.issues[1]) == "in-review"


def test_no_closing_link_pr_is_skipped(tmp_path, capsys):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    pr["body"] = "Refs #1"
    core.pr_event(
        cfg,
        h.client("workflow"),
        {"action": "opened", "pull_request": copy.deepcopy(pr)},
    )
    assert capsys.readouterr().out.strip() == "not a lifecycle task; skipped"
    assert core.state(h.issues[1]) == "in-review"


def test_unavailable_comment_history_is_not_treated_as_unmanaged(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    h.fail_read = True
    with pytest.raises(core.Unavailable):
        core.pr_event(
            cfg,
            h.client("workflow"),
            {"action": "opened", "pull_request": copy.deepcopy(pr)},
        )
    assert core.state(h.issues[1]) == "in-review"
    assert not (Path(cfg["state_dir"]) / "outbox").exists()


def test_finished_claim_polls_opted_in_delivery_without_worker_replay(tmp_path, monkeypatch):
    cfg, h, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, h.client(), run_jobs=False, clock=lambda: 100)
    claim_path = next((Path(cfg["state_dir"]) / "claims").glob("*.json"))
    claim = core.read(claim_path)
    claim["stage"] = "finished"
    core.save(claim_path, claim)
    settings = core.read(claim["config"])
    settings["integration"] = {"enabled": True, "declared_tier": "standard"}
    core.save(claim["config"], settings)
    job_path = next((Path(cfg["state_dir"]) / "jobs").glob("*/runner-state/tasks/*.json"))
    job = core.read(job_path)
    job["status"] = "ready_for_integration"
    core.save(job_path, job)
    calls = []
    monkeypatch.setattr(runner.integration_api(), "advance", lambda *args: calls.append(args[3]["id"]))
    monkeypatch.setattr(runner, "execute", lambda *args: pytest.fail("must not replay agents"))
    service.poll(cfg, h.client(), clock=lambda: 200)
    service.poll(cfg, h.client(), clock=lambda: 300)
    assert calls == [claim["job"], claim["job"]]


def test_published_job_leaves_pr_states_to_trusted_workflow(tmp_path):
    cfg = policy(tmp_path)
    job = {"status": "ready_for_integration", "assignment": {"lifecycle": {"issue": 1, "event": 2}},
           "integration": {"pr": 9, "stage": "checking"}}
    assert core.job_records(cfg, job) == []


@pytest.mark.parametrize("stage,expected", [
    ("created", "pr-open"), ("checking", "pr-open"),
    ("auto_merge_enabled", "pr-open"), ("merged", "merged"),
])
@pytest.mark.parametrize("matched", [True, False])
def test_reconcile_published_job_reports_workflow_state_without_writes(tmp_path, stage, expected, matched):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.issue(state=expected if matched else "in-review")
    job = {"id": "job", "status": "ready_for_integration",
           "assignment": {"lifecycle": {"issue": 1, "event": 2}},
           "integration": {"pr": 9, "stage": stage}}
    before = copy.deepcopy(job)
    assert core.reconcile(cfg, [job], h.client()) == (
        [] if matched else [dict(issue=1, job="job", local=expected, remote="in-review")])
    assert job == before
    assert h.calls == []
    assert not (Path(cfg["state_dir"]) / "outbox").exists()


def test_reconcile_published_job_reports_unavailable_remote(tmp_path):
    cfg = policy(tmp_path)
    h = Hub(cfg)
    h.fail_read = True
    job = {"id": "job", "status": "ready_for_integration",
           "assignment": {"lifecycle": {"issue": 1, "event": 2}},
           "integration": {"pr": 9, "stage": "merged"}}
    assert core.reconcile(cfg, [job], h.client()) == [
        dict(issue=1, job="job", error="remote_unavailable")]
    assert h.calls == []


@pytest.mark.parametrize("ordering", ["event_after_handoff", "queued_before_handoff"])
@pytest.mark.parametrize("reason", ["owner_merge_required", "gate_category_higher"])
def test_delayed_pr_open_preserves_owner_handoff(tmp_path, ordering, reason):
    cfg = policy(tmp_path)
    # Exercise the installed policy, including its otherwise permitted edge.
    cfg["transitions"] = core.read(ROOT / ".github/task-lifecycle.json")["transitions"]
    assert core.permitted(cfg, "needs-owner", "pr-open", cfg["actors"]["workflow"])
    h = Hub(cfg)
    pr = pr_fixture(cfg, h)
    workflow_cfg = {**cfg, "state_dir": str(tmp_path / "workflow-state")}
    if ordering == "queued_before_handoff":
        record = core.public_record(cfg, 1, "pr-7", 0, "pr-open", pr=7,
                                    commit=pr["head"]["sha"], gate="pass")
        core.queue(workflow_cfg, record, role="workflow")
    job = {"id": "job", "round": 2, "status": "needs_owner",
           "assignment": {"lifecycle": {"issue": 1, "event": 101}},
           "integration": {"pr": 7, "stage": "owner", "reason": reason}}
    for handoff in core.job_records(cfg, job):
        core.queue(cfg, handoff)
    # The workflow's separate outbox can be delayed while the runner completes
    # the real needs-owner transition, then recovered by a fresh workflow poll.
    core.flush(cfg, h.client())
    assert core.state(h.issues[1]) == "needs-owner"
    if ordering == "queued_before_handoff":
        core.flush(workflow_cfg, h.client("workflow"))
    for _ in range(2):
        core.pr_event(workflow_cfg, h.client("workflow"), {"action": "opened", "pull_request": copy.deepcopy(pr)})
        core.flush(workflow_cfg, h.client("workflow"))
        assert core.state(h.issues[1]) == "needs-owner"
    assert not any(call == ("label", 1, "pr-open") for call in h.calls)


@pytest.mark.parametrize("cause,attempts", [
    ("/private/secret/tool", 3), ("attempts_exhausted_3", 3),
    ([], 3), ({}, 3), ("attempts_exhausted", None),
    ("attempts_exhausted", True), ("attempts_exhausted", "3"),
    ("attempts_exhausted", 0), ("attempts_exhausted", 11),
])
def test_stop_cause_or_bound_not_validated_is_not_published(tmp_path, cause, attempts):
    cfg = policy(tmp_path)
    job = dict(id="stop-job", round=1, status="needs_owner",
               assignment={"lifecycle": {"issue": 1, "event": 101}},
               worker={"status": "blocked", "blocked_check": "story_tests", "blocked_cause": cause},
               worker_check_attempts=attempts)
    record = core.job_records(cfg, job)[0]
    assert record["reason"] == "worker_story_tests"
    body = core.record_body(record)
    assert "without a specific cause" in body
    assert "/private/secret" not in body
