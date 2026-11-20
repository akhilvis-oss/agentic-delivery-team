"""Synthetic admission and finished-story recovery regression coverage."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from lifecycle import core, service
from test_lifecycle import Hub, policy


def append_record(cfg, hub, target, *, job="old", commit=None, first=False):
    role = "workflow" if target == "merged" else "owner" if target == "accepted" else "runner"
    record = core.public_record(cfg, 1, job, 0, target, commit=commit)
    record.update({"from": ("pr-open" if target == "merged"
                             else "approved" if target == "dispatched" else "in-review"),
                   "actor": cfg["actors"][role]})
    if first:
        record["timestamp"] = "2025-01-01T00:00:00+00:00"
    comment = dict(user={"login": cfg["actors"][role]}, body=core.record_body(record))
    if first:
        hub.comments_by_issue[1].insert(0, comment)
    else:
        hub.comments_by_issue[1].append(comment)
    return record


@pytest.mark.parametrize("state, label, targets, expected", [
    ("closed", "approved", [], False),
    ("open", "merged", [], False),
    ("open", "accepted", [], False),
    ("open", "approved", ["merged", "approved"], False),
    ("open", "approved", ["accepted"], False),
    ("open", "approved", ["accepted", "approved"], True),
    ("open", "in-review", ["dispatched"], True),
    ("open", "approved", [], True),
])
def test_can_start_conditions(state, label, targets, expected):
    issue = {"state": state, "labels": [{"name": "status:" + label}]}
    assert core.can_start(issue, [{"to": value} for value in targets]) is expected


@pytest.mark.parametrize("branch", ["dispatch", "continue", "primary"])
def test_dispatch_refuses_earlier_merge_before_any_claim(tmp_path, monkeypatch, branch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    body = "Synthetic scope"
    if branch == "continue":
        body += "\n\n### Continue from\nCommit: " + "a" * 40
    hub.issue(body=body, state="needs-primary" if branch == "primary" else "approved")
    if branch != "primary":
        hub.approve()
    append_record(cfg, hub, "merged", first=True)
    head = "a" * 40
    if branch == "primary":
        append_record(cfg, hub, "needs-primary", job="source", commit=head)
    assert core.records(cfg, hub.comments_by_issue[1], 1)[0]["to"] == "merged"
    monkeypatch.setattr(service, "publish_poll", lambda *a: None)
    monkeypatch.setattr(service, "runner_module", lambda: SimpleNamespace())
    source = dict(id="source", head_commit=head, base_commit="b" * 40,
                  assignment={"lifecycle": {"event": 1}})
    monkeypatch.setattr(service, "jobs", lambda _: [source])
    monkeypatch.setattr(service, "delivery_template", lambda *a: pytest.fail("finished dispatch started"))
    service.poll(cfg, hub.client(), clock=lambda: 100)
    assert list((Path(cfg["state_dir"]) / "claims").glob("*.json")) == []
    assert hub.calls == []


@pytest.mark.parametrize("stage", ["claimed", "dispatched", "enqueued", "finished"])
def test_earlier_merge_blocks_dispatch_recovery_and_both_delivery_paths(tmp_path, monkeypatch, stage):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    issue = hub.issue(state="in-review")
    append_record(cfg, hub, "merged")
    append_record(cfg, hub, "dispatched", job="old")
    assert [r["to"] for r in core.records(cfg, hub.comments_by_issue[1], 1)] == ["merged", "dispatched"]
    path = Path(cfg["state_dir"]) / "claims" / "old.json"
    core.save(path, dict(job="old", issue=1, event=101, stage=stage,
                         config="synthetic", task=issue["body"]))
    job = dict(id="old", status="ready_for_integration", head_commit="a" * 40)
    runner = SimpleNamespace(load_config=lambda _: {"state_dir": str(tmp_path / "runner")},
                             read=lambda _: job, TERMINAL={"ready_for_integration"},
                             run_once=lambda *a, **kw: pytest.fail("finished delivery resumed"))
    monkeypatch.setattr(service, "runner_module", lambda: runner)
    monkeypatch.setattr(service, "publish_poll", lambda *a: None)
    monkeypatch.setattr(service, "cleanup", lambda *a, **kw: None)
    monkeypatch.setattr(service, "delivery_template", lambda *a: pytest.fail("finished dispatch recovered"))
    for at in (100, 200):
        service.poll(cfg, hub.client(), clock=lambda: at)
    assert core.read(path)["stage"] == "finished"
    assert hub.calls == []
    assert service.jobs(cfg) == []


def test_outbox_earlier_merge_prevents_runner_publication(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-review")
    append_record(cfg, hub, "merged")
    append_record(cfg, hub, "dispatched")
    assert [r["to"] for r in core.records(cfg, hub.comments_by_issue[1], 1)] == ["merged", "dispatched"]
    path = core.queue(cfg, core.public_record(cfg, 1, "old", 0, "needs-owner"))
    core.flush(cfg, hub.client())
    assert core.read(path)["status"] == "superseded"
    assert hub.calls == []


def test_phase_admission_raises_on_earlier_merge(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    issue = hub.issue(state="in-review")
    append_record(cfg, hub, "merged")
    append_record(cfg, hub, "dispatched", job="current")
    assert [r["to"] for r in core.records(cfg, hub.comments_by_issue[1], 1)] == ["merged", "dispatched"]
    monkeypatch.setattr(core, "load_config", lambda _: cfg)
    monkeypatch.setattr(core, "GitHub", lambda *a: hub.client())
    job = dict(id="current", assignment={"lifecycle": {"issue": 1}, "task_text": issue["body"]})
    with pytest.raises(ValueError, match="Admission changed"):
        core.admit_phase("synthetic", job)
    assert hub.calls == []


@pytest.mark.parametrize("number", [75, 110])
def test_closed_story_incident_replay_stays_closed_without_job(tmp_path, monkeypatch, number):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    issue = hub.issue(number, state="in-review")
    issue["state"] = "closed"
    path = Path(cfg["state_dir"]) / "claims" / f"old-{number}.json"
    job_id = f"old-{number}"
    core.save(path, dict(job=job_id, issue=number, stage="enqueued",
                         config="obsolete", task=issue["body"]))
    job = dict(id=job_id, status="ready_for_integration", head_commit="a" * 40)
    job_path = (Path(cfg["state_dir"]) / "jobs" / job_id / "runner-state"
                / "tasks" / (job_id + ".json"))
    core.save(job_path, job)
    monkeypatch.setattr(service, "runner_module", lambda: SimpleNamespace())
    monkeypatch.setattr(service, "cleanup", lambda *a, **kw: None)
    monkeypatch.setattr(service, "publish_poll", lambda *a: None)
    for at in (100, 200):
        service.poll(cfg, hub.client(), clock=lambda: at)
    assert issue["state"] == "closed"
    assert core.read(path)["stage"] == "finished"
    assert service.jobs(cfg) == [job]
    assert core.read(job_path) == job
    assert hub.calls == []
