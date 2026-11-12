"""Owner-visible stops and polling, using synthetic transports and temporary state."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from lifecycle import core, product, service
from test_lifecycle import Hub, policy
import test_runner as fixtures

setup = fixtures.setup


def stopped_job(**worker):
    return dict(id="question-job", round=1, status="needs_owner",
                assignment={"lifecycle": {"issue": 1, "event": 101}},
                worker=dict(status="blocked", **worker))


@pytest.mark.parametrize("target", ["needs-owner", "needs-primary", "blocked"])
def test_every_stop_defaults_to_fixed_reason_and_posts_action(tmp_path, target):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-review")
    record = core.public_record(cfg, 1, "stopped", 1, target)
    assert record["reason"] in core.STOP_REASONS
    core.queue(cfg, record)
    core.flush(cfg, hub.client())
    comment, = hub.client().comments(1)
    assert "owner:" in comment["body"] and "re-approve" in comment["body"]
    assert core.records(cfg, [comment], 1)[0]["reason"] == record["reason"]
    with pytest.raises(ValueError, match="Unknown stop reason"):
        core.public_record(cfg, 1, "stopped", 1, target, reason="arbitrary_text")


def test_builder_question_is_quoted_verbatim_and_survives_retry(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-progress")
    question = 'May I add the optional dependency?\nOr should I keep the current implementation?'
    job = stopped_job(owner_question=question)
    core.sync_job(cfg["config_path"], job, hub.client())
    core.sync_job(cfg["config_path"], job, hub.client())
    comment, = hub.client().comments(1)
    assert "\n".join("> " + line for line in question.split("\n")) in comment["body"]
    assert 'add the answer to the story under "Owner decisions", then re-approve' in comment["body"]
    record, = core.records(cfg, [comment], 1)
    assert record["owner_question"] == question and record["reason"] == "owner_question"


@pytest.mark.parametrize("question", [
    "May I read /private/fixture/secret?", "Use C:\\private\\fixture?",
    "Ask owner-fixture?", "Contact person@example.invalid?",
    "token=" + "a" * 24, "sk-" + "x" * 25, "Provider response: raw data",
    "Should I use account customer-fixture for this integration?",
    "Should I reuse the access token " + "abcdef1234567890" * 2 + "?",
    "Should I use the password synthetic-fixture?",
    "Should I reuse API key synthetic-fixture?",
    "Should I use login customer-fixture?",
    "Should I reuse credentials from customer-fixture?",
])
def test_private_question_is_withheld_in_full(tmp_path, question):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-progress")
    job = stopped_job(owner_question=question)
    core.sync_job(cfg["config_path"], job, hub.client())
    comment, = hub.client().comments(1)
    assert question not in comment["body"]
    assert "customer-fixture" not in comment["body"]
    assert "abcdef1234567890" not in comment["body"]
    record, = core.records(cfg, [comment], 1)
    assert record["reason"] == "question_withheld" and "owner_question" not in record


def test_test_failure_counts_and_names_are_bounded_without_paths_or_logs(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-progress")
    job = stopped_job(blocked_check="story_tests", blocked_cause="attempts_exhausted",
                      failed_tests={"count": 8, "names": [f"test_case_{i}" for i in range(8)]
                                    + ["/private/fixture/test_bad.py::test_secret[secret]"]})
    job["worker_check_attempts"] = 3
    job["worker"]["summary"] = "PRIVATE LOG AND PROVIDER RESPONSE"
    core.sync_job(cfg["config_path"], job, hub.client())
    comment, = hub.client().comments(1)
    assert "Failing tests: 8" in comment["body"]
    assert "test_case_0" in comment["body"] and "test_case_4" in comment["body"]
    assert "test_case_5" not in comment["body"] and "/private" not in comment["body"]
    assert "PRIVATE LOG" not in comment["body"] and "test_secret" not in comment["body"]


def test_runner_test_output_only_exports_function_names(tmp_path):
    text = ("FAILED /private/fixture/tests.py::test_one[private-value] - SECRET\n"
            "FAILED tests/sample.py::TestSuite::test_two - PROVIDER RESPONSE\n"
            "2 failed, 3 passed in 0.12s\n")
    failures = fixtures.runner.verification_failures(text)
    assert failures == {"count": 2, "names": ["test_one", "test_two"]}
    cfg = policy(tmp_path)
    job = stopped_job()
    job["verification_runs"] = [{"results": [{"status": "failed", "reported_counts": {"failed": 2},
                                               "failed_tests": failures, "output_tail": text}]}]
    record, = core.job_records(cfg, job)
    assert record["failed_tests"] == failures and "/private" not in core.record_body(record)


def approval_event(cfg, hub):
    return dict(action="labeled", label={"name": "status:approved"},
                sender={"login": cfg["actors"]["owner"]}, issue=copy.deepcopy(hub.issues[1]))


def test_same_second_reapproval_attested_exactly_once(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    original = hub.approve()
    hub.event(1, "needs-owner", "runner")
    latest = hub.event(1, "approved", "owner")
    assert original["created_at"] == latest["created_at"]
    event = approval_event(cfg, hub)
    core.attest_approval(cfg, hub.client("workflow"), event)
    core.attest_approval(cfg, hub.client("workflow"), event)
    proofs = [r for r in core.records(cfg, hub.client().comments(1), 1)
              if r["kind"] == "approval" and r["event"] == latest["id"]]
    assert len(proofs) == 1


@pytest.mark.parametrize("fresh_first", [False, True])
@pytest.mark.parametrize("same_second", [False, True])
def test_delayed_webhook_cannot_attest_stale_scope_or_reject_new_approval(
    tmp_path, fresh_first, same_second
):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(body="Original approved story.")
    old = approval_event(cfg, hub)
    hub.event(1, "needs-owner", "runner")
    hub.issues[1]["body"] = "Revised story with the owner's answer."
    latest = hub.event(1, "approved", "owner")
    if not same_second:
        latest["created_at"] = hub.issues[1]["updated_at"] = "2026-01-02T10:01:00+00:00"
    fresh = approval_event(cfg, hub)
    client = hub.client("workflow")
    if fresh_first:
        core.attest_approval(cfg, client, fresh)
    for _ in range(2):
        with pytest.raises(core.Unavailable):
            core.attest_approval(cfg, client, old)
        assert core.state(hub.issues[1]) == "approved"
        assert not any(call[0] == "DELETE" for call in hub.calls)
    core.attest_approval(cfg, client, fresh)
    core.attest_approval(cfg, client, fresh)
    proofs = [r for r in core.records(cfg, client.comments(1), 1) if r["kind"] == "approval"]
    assert len(proofs) == 1
    assert proofs[0]["event"] == latest["id"]
    assert proofs[0]["scope_sha256"] == hashlib.sha256(fresh["issue"]["body"].encode()).hexdigest()
    core.guard_issue(cfg, client, 1)
    assert core.state(hub.issues[1]) == "approved"
    explanations = [c["body"] for c in client.comments(1) if "ignored" in c["body"]]
    assert len(explanations) == 1 and "re-approve" in explanations[0]
    assert all(cfg["state_dir"] not in body and "Original approved story" not in body
               for body in explanations)


def test_replayed_snapshot_cannot_add_a_second_scope_to_proven_event(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    client = hub.client("workflow")
    event = approval_event(cfg, hub)
    core.attest_approval(cfg, client, event)
    event["issue"]["body"] = "A different same-second snapshot."
    with pytest.raises(core.Unavailable):
        core.attest_approval(cfg, client, event)
    assert len([r for r in core.records(cfg, client.comments(1), 1)
                if r["kind"] == "approval"]) == 1
    assert not core.guard_issue(cfg, client, 1)
    assert core.state(hub.issues[1]) == "approved"


def test_event_read_outage_on_replay_recovers_only_after_history_returns(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    client = hub.client("workflow")
    event = approval_event(cfg, hub)
    core.attest_approval(cfg, client, event)
    events = client.events

    def unavailable(*args):
        raise core.Unavailable("synthetic history outage")

    monkeypatch.setattr(client, "events", unavailable)
    with pytest.raises(core.Unavailable):
        core.attest_approval(cfg, client, event)
    # A webhook has no label-event ID. Without history a replay cannot be
    # distinguished from another same-second approval, even of the same body.
    assert {"name": "status:approved"} not in hub.issues[1]["labels"]
    assert any("could not be attested" in c["body"] for c in client.comments(1))
    monkeypatch.setattr(client, "events", events)
    core.guard_issue(cfg, client, 1)
    # History now proves that the original attestation is still current.
    assert core.state(hub.issues[1]) == "approved"


@pytest.mark.parametrize("revised_story", [False, True])
def test_history_outage_rejects_reapproval_after_attested_cycle(
    tmp_path, monkeypatch, revised_story
):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    client = hub.client("workflow")
    core.attest_approval(cfg, client, approval_event(cfg, hub))
    original = hub.events_by_issue[1][-1]
    hub.event(1, "needs-owner", "runner")
    if revised_story:
        hub.issues[1]["body"] = "Revised story with an Owner decisions answer."
    latest = hub.event(1, "approved")
    assert latest["created_at"] == original["created_at"]
    event = approval_event(cfg, hub)
    events = client.events

    def unavailable(*args):
        raise core.Unavailable("synthetic history outage")

    monkeypatch.setattr(client, "events", unavailable)
    with pytest.raises(core.Unavailable, match="evidence unavailable"):
        core.attest_approval(cfg, client, event)
    assert {"name": "status:approved"} not in hub.issues[1]["labels"]
    explanations = [c["body"] for c in client.comments(1) if "could not be attested" in c["body"]]
    assert len(explanations) == 1
    assert "add it again" in explanations[0] and cfg["state_dir"] not in explanations[0]
    assert event["issue"]["body"] not in explanations[0]
    monkeypatch.setattr(client, "events", events)
    core.guard_issue(cfg, client, 1)
    assert core.state(hub.issues[1]) == "needs-owner"
    retry = hub.event(1, "approved")
    fresh = approval_event(cfg, hub)
    core.attest_approval(cfg, client, fresh)
    core.attest_approval(cfg, client, fresh)
    proofs = [r for r in core.records(cfg, client.comments(1), 1) if r["kind"] == "approval"]
    assert [r["event"] for r in proofs] == [original["id"], retry["id"]]
    core.guard_issue(cfg, client, 1)
    assert core.state(hub.issues[1]) == "approved"


def test_uncertain_approval_intent_cannot_be_rebound_to_another_scope(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    client = hub.client("workflow")
    event = approval_event(cfg, hub)
    request = client.request

    def uncertain(method, path, data=None):
        if method == "POST" and core.PREFIX in data.get("body", ""):
            raise core.Uncertain("synthetic lost publication")
        return request(method, path, data)

    monkeypatch.setattr(client, "request", uncertain)
    with pytest.raises(core.Unavailable):
        core.attest_approval(cfg, client, event)
    monkeypatch.setattr(client, "request", request)
    # Restore the label to model the guard observing approval while its first
    # publication is still uncertain; the cycle and event ID are unchanged.
    hub.issues[1]["labels"] = [{"name": "status:approved"}]
    event["issue"]["body"] = "Conflicting snapshot while publication is uncertain."
    with pytest.raises(core.Unavailable):
        core.attest_approval(cfg, client, event)
    intents = [core.read(p)["record"] for p in (Path(cfg["state_dir"]) / "outbox").glob("*.json")]
    assert len([r for r in intents if r["kind"] == "approval"]) == 1
    assert not any(r["kind"] == "approval" for r in core.records(cfg, client.comments(1), 1))


def test_valid_proof_takes_precedence_over_old_rejection_marker(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    hub.approve()
    proof, = core.records(cfg, hub.client().comments(1), 1)
    client = hub.client("workflow")
    client.request("POST", "/issues/1/comments", {"body": (
        f"<!-- agentic-approval-rejected:{proof['event']}:synthetic -->"
    )})
    assert not core.guard_issue(cfg, client, 1)
    assert core.state(hub.issues[1]) == "approved"


def test_failed_attestation_removes_approval_explains_and_guard_does_not_restore(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    hub.event(1, "approved")  # ambiguous events within the same approval cycle
    event = approval_event(cfg, hub)
    with pytest.raises(core.Unavailable, match="ambiguous"):
        core.attest_approval(cfg, hub.client("workflow"), event)
    assert {"name": "status:approved"} not in hub.issues[1]["labels"]
    comment, = hub.client().comments(1)
    assert "could not be attested" in comment["body"] and "add it again" in comment["body"]
    core.guard_issue(cfg, hub.client("workflow"), 1)
    assert core.state(hub.issues[1]) == "needs-owner"
    records = core.records(cfg, hub.client().comments(1), 1)
    assert records[-1]["reason"] == "approval_not_attested"


@pytest.mark.parametrize("ambiguous", [False, True])
@pytest.mark.parametrize("removal_fails", [False, True])
def test_comments_read_outage_still_rejects_explains_and_recovers(
    tmp_path, monkeypatch, ambiguous, removal_fails
):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    if ambiguous:
        hub.event(1, "approved")
    client = hub.client("workflow")
    comments, request = client.comments, client.request

    def unavailable(*args):
        raise core.Unavailable("synthetic comment read outage")

    def write(method, path, data=None):
        if removal_fails and method == "DELETE":
            raise core.Unavailable("synthetic label removal outage")
        return request(method, path, data)

    monkeypatch.setattr(client, "comments", unavailable)
    monkeypatch.setattr(client, "request", write)
    with pytest.raises(core.Unavailable):
        core.attest_approval(cfg, client, approval_event(cfg, hub))
    explanation, = comments(1)
    body = explanation["body"]
    assert "Approval could not be attested" in body and "add it again" in body
    assert cfg["state_dir"] not in body
    assert hub.issues[1]["body"] not in body and "synthetic" not in body
    assert not core.records(cfg, comments(1), 1)
    if removal_fails:
        assert "remove that label yourself" in body
    else:
        assert {"name": "status:approved"} not in hub.issues[1]["labels"]
        assert "guard removed status:approved" in body

    monkeypatch.setattr(client, "comments", comments)
    monkeypatch.setattr(client, "request", request)
    core.guard_issue(cfg, client, 1)
    assert core.state(hub.issues[1]) == "needs-owner"
    retry = hub.event(1, "approved")
    fresh = approval_event(cfg, hub)
    core.attest_approval(cfg, client, fresh)
    core.attest_approval(cfg, client, fresh)
    proofs = [r for r in core.records(cfg, comments(1), 1) if r["kind"] == "approval"]
    assert len(proofs) == 1 and proofs[0]["event"] == retry["id"]


def test_delayed_snapshot_explains_and_preserves_new_proof_during_comment_outage(
    tmp_path, monkeypatch
):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(body="Original synthetic story.")
    old = approval_event(cfg, hub)
    hub.event(1, "needs-owner", "runner")
    hub.issues[1]["body"] = "Revised synthetic story."
    hub.event(1, "approved")
    client = hub.client("workflow")
    core.attest_approval(cfg, client, approval_event(cfg, hub))
    comments = client.comments

    def unavailable(*args):
        raise core.Unavailable("synthetic comment read outage")

    monkeypatch.setattr(client, "comments", unavailable)
    with pytest.raises(core.Unavailable, match="current approval preserved"):
        core.attest_approval(cfg, client, old)
    assert core.state(hub.issues[1]) == "approved"
    assert not any(call[0] == "DELETE" for call in hub.calls)
    explanation = comments(1)[-1]["body"]
    assert "snapshot was ignored" in explanation and "re-approve" in explanation
    assert cfg["state_dir"] not in explanation and old["issue"]["body"] not in explanation


def test_manual_stop_label_also_gets_reason_and_comment(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="needs-owner")
    core.guard_issue(cfg, hub.client("workflow"), 1)
    core.guard_issue(cfg, hub.client("workflow"), 1)
    record, = core.records(cfg, hub.client().comments(1), 1)
    assert record["reason"] == "owner_requested_stop"


def test_unavailable_event_read_rejection_survives_recovery(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    client = hub.client("workflow")
    events = client.events

    def unavailable(*args):
        raise core.Unavailable("synthetic event read outage")

    monkeypatch.setattr(client, "events", unavailable)
    with pytest.raises(core.Unavailable, match="evidence unavailable"):
        core.attest_approval(cfg, client, approval_event(cfg, hub))
    assert {"name": "status:approved"} not in hub.issues[1]["labels"]
    monkeypatch.setattr(client, "events", events)
    core.guard_issue(cfg, client, 1)
    assert core.state(hub.issues[1]) == "needs-owner"
    hub.event(1, "approved")
    core.attest_approval(cfg, client, approval_event(cfg, hub))
    core.guard_issue(cfg, client, 1)
    assert core.state(hub.issues[1]) == "approved"


def test_failed_label_removal_still_explains_approval_failure(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    hub.event(1, "approved")
    client = hub.client("workflow")
    request = client.request

    def cannot_remove(method, path, data=None):
        if method == "DELETE":
            raise core.Unavailable("synthetic removal failure")
        return request(method, path, data)

    monkeypatch.setattr(client, "request", cannot_remove)
    with pytest.raises(core.Unavailable, match="removal failure"):
        core.attest_approval(cfg, client, approval_event(cfg, hub))
    comment, = client.comments(1)
    assert "could not confirm removal" in comment["body"]
    assert "remove that label yourself" in comment["body"]


@pytest.mark.parametrize("kind", sorted(product.OWNER_DECISIONS | {"unknown", "clarification"}))
def test_product_stop_has_explanation(tmp_path, kind):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="draft")
    product.answer(cfg, dict(id="decision", issue=1, kind=kind), hub.client("product"))
    comment, = hub.client().comments(1)
    record, = core.records(cfg, [comment], 1)
    assert record["reason"] in core.STOP_REASONS and "Owner:" in comment["body"]


@pytest.mark.parametrize("standup", [False, True])
def test_poll_time_is_public_pinned_and_updated_in_place(tmp_path, standup):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    if standup:
        cfg["digest_config"] = "synthetic-config-reference"
        issue = hub.issue(body="<!-- agentic-digest-v1:synthetic -->")
        issue.update(user={"login": cfg["actors"]["product"]}, labels=[{"name": "report:standup"}])
    service.poll(cfg, hub.client(), clock=lambda: 100)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert len(hub.issues) == 1
    comment, = hub.client().comments(1)
    assert "1970-01-01T00:03:20+00:00" in comment["body"] and comment["pin"]
    assert "private" not in comment["body"] and cfg["state_dir"] not in comment["body"]
    assert sum(call[0] == "PUT" for call in hub.calls) == 1
    assert core.read(Path(cfg["state_dir"]) / "public-heartbeat.json")["last_poll"] == 200


def test_heartbeat_ambiguous_issue_create_recovers_without_duplicate(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.fail_write = "after"
    with pytest.raises(core.Uncertain):
        service.poll(cfg, hub.client(), clock=lambda: 100)
    hub.fail_write = None
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert len(hub.issues) == 1 and len(hub.client().comments(1)) == 1


@pytest.mark.parametrize("failure", ["before", "after"])
def test_heartbeat_comment_create_recovers_without_duplicate(tmp_path, monkeypatch, failure):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    client = hub.client()
    request = client.request

    def fail_comment(method, path, data=None):
        hub.fail_write = failure if path.endswith("/comments") else None
        return request(method, path, data)

    monkeypatch.setattr(client, "request", fail_comment)
    with pytest.raises(core.Unavailable):
        service.poll(cfg, client, clock=lambda: 100)
    hub.fail_write = None
    monkeypatch.setattr(client, "request", request)
    service.poll(cfg, client, clock=lambda: 200)
    assert len(hub.issues) == 1 and len(client.comments(1)) == 1


def test_heartbeat_uncertain_absent_create_is_not_repeated(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    client = hub.client()
    calls = []

    def lost(*args):
        calls.append(args)
        raise core.Uncertain("synthetic lost reply without creation")

    monkeypatch.setattr(client, "request", lost)
    with pytest.raises(core.Uncertain):
        service.poll(cfg, client, clock=lambda: 100)
    with pytest.raises(core.Unavailable, match="reconciliation"):
        service.poll(cfg, client, clock=lambda: 200)
    assert len(calls) == 1


def test_heartbeat_is_published_before_builder_processing(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)

    def after_heartbeat():
        assert "last poll" in hub.client().comments(1)[0]["body"]
        raise RuntimeError("synthetic long-running builder boundary")

    monkeypatch.setattr(service, "runner_module", after_heartbeat)
    with pytest.raises(RuntimeError, match="builder boundary"):
        service.poll(cfg, hub.client(), clock=lambda: 100)


def test_optional_result_fields_accept_legacy_and_block_commit_on_question():
    worker = fixtures.result("blocked")
    fixtures.runner.validate_result(worker, "worker")
    worker.update(owner_question="May I change the scope?", failed_tests={"count": 1, "names": ["test_one"]})
    fixtures.runner.validate_result(worker, "worker")
    assert "owner_question" in fixtures.runner.schema("worker", lifecycle=True)["properties"]
    assert "owner_question" in fixtures.runner.schema("worker")["properties"]
    worker["status"] = "ready_for_review"
    with pytest.raises(ValueError, match="blocked result"):
        fixtures.runner.validate_result(worker, "worker")
    assert "Owner decisions" in fixtures.runner.LIFECYCLE_NOTES["worker"]
    assert "owner_question" in json.dumps(fixtures.runner.schema("worker", lifecycle=True))


def test_next_builder_receives_owner_decisions_and_instruction_to_read_them(setup):
    cfg, _, _, job = setup
    decision = "### Owner decisions\nKeep the current dependency."
    job["assignment"]["task_text"] += "\n\n" + decision
    prompt = fixtures.runner.prompt_for(cfg, job, "worker")
    assert decision in prompt
    assert "Read the story's Owner decisions section before building" in prompt
