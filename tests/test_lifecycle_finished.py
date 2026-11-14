"""Finished stories and rejected outbox entries use disposable synthetic state."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from lifecycle import core, service
from test_lifecycle import Hub, policy
from test_lifecycle_retry import StopLoop, simulation


@pytest.mark.parametrize("state", ["closed", "merged", "accepted", "in-review"])
def test_many_finished_cleaned_claims_make_no_per_claim_requests(tmp_path, monkeypatch, state):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    for number in range(1, 41):
        issue = hub.issue(number, state="in-review" if state == "closed" else state)
        if state == "closed":
            issue["state"] = "closed"
        core.save(Path(cfg["state_dir"]) / "claims" / f"old-{number}.json",
                  dict(job=f"old-{number}", issue=number, stage="finished", cleaned=True,
                       config="obsolete"))
    client = hub.client()
    requests = []
    for name in ("issue", "comments"):
        original = getattr(client, name)

        def counted(number, method=name, read=original):
            requests.append((method, number))
            return read(number)

        monkeypatch.setattr(client, name, counted)
    monkeypatch.setattr(service, "publish_poll", lambda *a: None)
    monkeypatch.setattr(service, "runner_module", lambda: SimpleNamespace())
    for at in (100, 200):
        service.poll(cfg, client, clock=lambda: at)
    assert requests == []
    assert not hub.calls


@pytest.mark.parametrize("state", ["closed", "merged", "accepted"])
def test_listed_finished_claim_uses_listing_and_retains_cleanup(tmp_path, monkeypatch, state):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    issue = hub.issue(state="merged" if state == "closed" else state)
    if core.state(issue) == "merged":
        hub.events_by_issue[1].pop()
        hub.event(1, "pr-open", "workflow")
        hub.event(1, "merged", "workflow")
    if state == "closed":
        issue["state"] = "closed"
    path = Path(cfg["state_dir"]) / "claims" / "old.json"
    core.save(path, dict(job="old", issue=1, stage="finished", config="obsolete",
                         cleanup_pending="branch_not_verified_merged"))
    client = hub.client()
    monkeypatch.setattr(client, "issue", lambda _: pytest.fail("redundant issue request"))
    monkeypatch.setattr(client, "comments", lambda _: pytest.fail("redundant comments request"))
    monkeypatch.setattr(service, "publish_poll", lambda *a: None)
    monkeypatch.setattr(service, "runner_module", lambda: SimpleNamespace())
    cleanups = []
    monkeypatch.setattr(service, "cleanup", lambda *a, **kw: cleanups.append((a, kw)))
    service.poll(cfg, client, clock=lambda: 100)
    assert len(cleanups) == 1
    assert cleanups[0][0][2] == issue
    assert cleanups[0][0][3] == hub.events_by_issue[1]
    assert not hub.calls


@pytest.mark.parametrize("state", ["closed", "merged", "accepted"])
def test_active_claim_checks_fresh_finished_state_before_recovery(tmp_path, monkeypatch, state):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-review")
    path = Path(cfg["state_dir"]) / "claims" / "old.json"
    core.save(path, dict(job="old", issue=1, stage="enqueued", config="obsolete"))
    client = hub.client()
    read_issue, read_comments = client.issue, client.comments
    requests = []

    def fresh_issue(number):
        requests.append("issue")
        issue = read_issue(number)
        if state == "closed":
            issue["state"] = "closed"
        else:
            issue["labels"] = [{"name": "status:" + state}]
        return issue

    def comments(number):
        requests.append("comments")
        return read_comments(number)

    monkeypatch.setattr(client, "issue", fresh_issue)
    monkeypatch.setattr(client, "comments", comments)
    monkeypatch.setattr(service, "runner_module", lambda: SimpleNamespace())
    monkeypatch.setattr(service, "cleanup", lambda *a, **kw: None)
    monkeypatch.setattr(service, "publish_poll", lambda *a: None)
    service.poll(cfg, client, clock=lambda: 100)
    assert requests == ["issue", "comments"]
    assert core.read(path)["stage"] == "finished"
    assert not hub.calls


def rejected(cfg, issue=1, job="bad", role="runner"):
    return core.queue(cfg, core.public_record(cfg, issue, job, 0, "merged"), role=role)


def test_confirmed_conflict_is_final(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    path = rejected(cfg)
    with pytest.raises(ValueError, match="rejected by policy"):
        core.flush(cfg, hub.client())
    calls = list(hub.calls)
    core.flush(cfg, hub.client())
    assert hub.calls == calls
    assert core.read(path)["rejection_confirmed"] is True


@pytest.mark.parametrize("failure", [None, "before", "after", "absent"])
def test_rejection_isolates_issue_and_reconciles_without_duplicate(tmp_path, monkeypatch, failure):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    hub.issue(2)
    path = rejected(cfg)
    same_issue = core.queue(cfg, core.public_record(cfg, 1, "later", 0, "blocked"))
    other_issue = core.queue(cfg, core.public_record(cfg, 2, "good", 0, "dispatched"))
    client = hub.client()
    request = client.request

    def send(method, route, data=None):
        if route == "/issues/1/comments":
            hub.fail_write = "after" if failure == "after" else "before" if failure else None
            if failure == "absent":
                raise core.Uncertain("synthetic lost request")
        else:
            hub.fail_write = None
        return request(method, route, data)

    monkeypatch.setattr(client, "request", send)
    with pytest.raises(ValueError, match="rejected by policy"):
        core.flush(cfg, client)
    assert core.read(other_issue)["status"] == "synced"
    assert core.read(same_issue)["status"] == "pending"
    assert core.read(path)["status"] == "conflict"
    monkeypatch.setattr(client, "request", request)
    hub.fail_write = None
    core.flush(cfg, client)
    if failure == "absent":
        assert not hub.comments_by_issue[1]
        assert core.read(same_issue)["status"] == "pending"
        assert not core.read(path).get("rejection_confirmed")
    else:
        assert core.read(path)["rejection_confirmed"] is True
        assert sum("was rejected" in c["body"] for c in hub.comments_by_issue[1]) == 1


def test_legacy_attempted_does_not_mean_confirmed(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    path = rejected(cfg)
    entry = core.read(path)
    entry.update(status="conflict", rejection_attempted=True, error="transition_not_allowed")
    entry["record"].update({"from": "draft", "actor": cfg["actors"]["runner"]})
    core.save(path, entry)
    later = core.queue(cfg, core.public_record(cfg, 1, "later", 0, "blocked"))
    core.flush(cfg, hub.client())
    assert not core.read(path).get("rejection_confirmed")
    assert core.read(later)["status"] == "pending"
    assert not hub.calls


def test_workflow_new_rejection_still_fails_after_flushing_other_issue(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    hub.issue(2, state="in-review")
    rejected(cfg, role="workflow")
    good = core.queue(cfg, core.public_record(cfg, 2, "pr", 0, "pr-open"), role="workflow")
    with pytest.raises(ValueError, match="rejected by policy"):
        core.flush(cfg, hub.client("workflow"))
    assert core.read(good)["status"] == "synced"


def test_serve_keeps_polling_after_actual_rejection(tmp_path, monkeypatch):
    cfg, client, sleeps, _, _ = simulation(tmp_path, monkeypatch, [None, None])
    client.hub.issue(state="draft")
    path = rejected(cfg)
    with pytest.raises(StopLoop):
        service.run_loop(cfg, client, "serve")
    assert sleeps == [60, 60]
    assert service.heartbeat(cfg) == 1060
    assert core.read(path)["status"] == "conflict"
    assert "Rejected transitions: 1" in service.status(cfg)
    assert not (Path(cfg["state_dir"]) / "service-error.json").exists()


@pytest.mark.parametrize("stage", ["enqueued", "finished"])
@pytest.mark.parametrize("finished", ["closed", "closed_merged", "merged", "accepted", "record"])
def test_finished_ready_job_never_resumes_or_publishes(tmp_path, monkeypatch, stage, finished):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    label = "merged" if finished == "closed_merged" else finished
    issue = hub.issue(state=label if label in {"merged", "accepted"} else "in-review")
    if finished in {"closed", "closed_merged"}:
        issue["state"] = "closed"
    if finished == "record":
        record = core.public_record(cfg, 1, "accepted", 0, "accepted")
        record.update({"from": "in-review", "actor": cfg["actors"]["owner"]})
        hub.comments_by_issue[1].append(dict(user={"login": cfg["actors"]["owner"]},
                                               body=core.record_body(record)))
    root = Path(cfg["state_dir"])
    claim_path = root / "claims" / "old.json"
    core.save(claim_path, dict(job="old", issue=1, event=101, stage=stage,
                               config="synthetic", task=issue["body"]))
    job = dict(id="old", status="ready_for_integration", head_commit="a" * 40)
    runner = SimpleNamespace(
        load_config=lambda _: {"state_dir": str(root / "runner")},
        read=lambda _: job,
        run_once=lambda *a, **kw: pytest.fail("finished story resumed"),
        TERMINAL={"ready_for_integration"},
    )
    monkeypatch.setattr(service, "runner_module", lambda: runner)
    monkeypatch.setattr(service, "cleanup", lambda *a, **kw: None)
    monkeypatch.setattr(service, "publish_poll", lambda *a: None)
    # A stale job record already queued must also remain local.
    queued = core.queue(cfg, core.public_record(cfg, 1, "old", 0, "needs-owner"))
    for at in (100, 200):
        service.poll(cfg, hub.client(), clock=lambda: at)
    assert core.read(claim_path)["stage"] == "finished"
    assert core.read(queued)["status"] == "superseded"
    assert not hub.calls


@pytest.mark.parametrize("failure", ["read", "wrong_actor", "wrong_key"])
def test_pending_rejection_evidence_holds_only_its_issue(tmp_path, monkeypatch, failure):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    hub.issue(2)
    path = rejected(cfg)
    hub.fail_write = "after"
    with pytest.raises(ValueError):
        core.flush(cfg, hub.client())
    hub.fail_write = None
    if failure == "wrong_actor":
        hub.comments_by_issue[1][0]["user"]["login"] = cfg["actors"]["owner"]
    if failure == "wrong_key":
        hub.comments_by_issue[1][0]["body"] = hub.comments_by_issue[1][0]["body"].replace(
            core.read(path)["record"]["key"], "0" * 64)
    later = core.queue(cfg, core.public_record(cfg, 1, "later", 0, "blocked"))
    other = core.queue(cfg, core.public_record(cfg, 2, "other", 0, "dispatched"))
    client = hub.client()
    comments = client.comments

    def read(number):
        if number == 1 and failure == "read":
            raise core.Unavailable("synthetic read failure")
        return comments(number)

    monkeypatch.setattr(client, "comments", read)
    calls = list(hub.calls)
    core.flush(cfg, client)
    assert core.read(path)["status"] == "conflict"
    assert not core.read(path).get("rejection_confirmed")
    assert core.read(later)["status"] == "pending"
    assert core.read(other)["status"] == "synced"
    assert not any(c[0] == "POST" and c[1] == "/issues/1/comments"
                   for c in hub.calls[len(calls):])


def test_closed_issue_does_not_retry_unconfirmed_rejection(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    issue = hub.issue(state="draft")
    path = rejected(cfg)
    hub.fail_write = "before"
    with pytest.raises(ValueError):
        core.flush(cfg, hub.client())
    issue["state"] = "closed"
    hub.fail_write = None
    calls = list(hub.calls)
    core.flush(cfg, hub.client())
    assert hub.calls == calls
    assert core.read(path)["status"] == "superseded"


def test_service_does_not_swallow_programming_value_error(tmp_path, monkeypatch):
    cfg, client, sleeps, _, _ = simulation(tmp_path, monkeypatch, [None])

    def broken(*args):
        raise ValueError("synthetic programming error")

    monkeypatch.setattr(core, "flush", broken)
    with pytest.raises(ValueError, match="synthetic programming error"):
        service.run_loop(cfg, client, "serve")
    assert sleeps == []
    assert core.read(Path(cfg["state_dir"]) / "service-error.json")["exception_class"] == "ValueError"


def test_closed_claim_finishes_without_loading_obsolete_runner_config(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="merged")["state"] = "closed"
    path = Path(cfg["state_dir"]) / "claims" / "old.json"
    core.save(path, dict(job="old", issue=1, stage="enqueued", config="obsolete"))
    runner = SimpleNamespace(load_config=lambda _: pytest.fail("obsolete runner config loaded"))
    monkeypatch.setattr(service, "runner_module", lambda: runner)
    monkeypatch.setattr(service, "cleanup", lambda *a, **kw: None)
    monkeypatch.setattr(service, "publish_poll", lambda *a: None)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    assert core.read(path)["stage"] == "finished"
    assert not hub.calls


def test_issue_closing_during_flush_cannot_publish_job_record(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-review")
    path = core.queue(cfg, core.public_record(cfg, 1, "old", 0, "needs-owner"))
    client = hub.client()
    read_issue = client.issue
    reads = []

    def issue(number):
        reads.append(number)
        if len(reads) == 2:
            hub.issues[number]["state"] = "closed"
        return read_issue(number)

    monkeypatch.setattr(client, "issue", issue)
    core.flush(cfg, client)
    assert not hub.calls
    assert core.read(path)["status"] == "superseded"
