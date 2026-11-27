"""Story admission and merge trust boundaries, using synthetic API data only."""

import copy

import pytest

from lifecycle import core, service
from test_lifecycle import Hub, approval_picker_event, configured_service, policy
from test_lifecycle_status_writes import status_client
from test_runner_integration import advance, delivery, runner, setup  # noqa: F401


@pytest.mark.parametrize("picker", [False, True])
@pytest.mark.parametrize("author", ["owner", "product", "runner", "workflow", "outsider", None])
def test_approval_author_is_bound_event_author(tmp_path, picker, author):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    if picker:
        event = approval_picker_event(cfg, hub)
    else:
        event = dict(action="labeled", label={"name": "status:approved"},
                     sender={"login": cfg["actors"]["owner"]}, issue=copy.deepcopy(hub.issue()))
    event["issue"]["user"] = {"login": cfg["actors"].get(author, author)} if author else None
    # The live author must not substitute for the author in the bound event.
    hub.issues[1]["user"] = {"login": cfg["actors"]["owner"]}
    client = status_client(hub)
    if author in {"owner", "product"}:
        core.attest_approval(cfg, client, event)
        assert any(r["kind"] == "approval" for r in core.records(cfg, client.comments(1), 1))
    else:
        with pytest.raises(core.Unavailable, match="Only stories written"):
            core.attest_approval(cfg, client, event)
        record, = core.records(cfg, client.comments(1), 1)
        assert record["reason"] == "story_author_untrusted"
        assert core.state(hub.issues[1]) == "needs-owner"
        assert "copy what you want into a new story" in client.comments(1)[0]["body"]


def test_grandfathered_approval_still_dispatches_without_record_changes(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    issue = hub.issue(body="Old approved story <!-- legacy marker -->")
    issue["user"] = {"login": "outsider"}
    hub.approve()
    before = copy.deepcopy(hub.comments_by_issue[1])
    assert service.claim_event(cfg, issue, hub.client().events(1))
    assert hub.comments_by_issue[1] == before


def test_grandfathered_approval_enqueues_with_original_proof(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issues[1]["body"] = "Legacy story <!-- old marker -->"
    hub.issues[1]["user"] = {"login": "outsider"}
    hub.comments_by_issue[1].clear()
    hub.approve()  # Simulate the proof issued before this admission policy existed.
    original = copy.deepcopy(hub.comments_by_issue[1][0])
    assert service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100) == ["issue-1-event-101"]
    assert original in hub.comments_by_issue[1]
    assert len(service.jobs(cfg)) == 1


@pytest.mark.parametrize("source", ["comment", "review"])
@pytest.mark.parametrize("prefix", ["Checker", "Second review"])
@pytest.mark.parametrize("verdict", ["BLOCKED", "CHANGES REQUIRED"])
@pytest.mark.parametrize("actor", ["owner", "runner-bot", "product-bot", "second-bot", "outsider", None])
def test_negative_verdict_requires_trusted_author(delivery, source, prefix, verdict, actor):  # noqa: F811
    config, _, job, client = delivery
    config["integration"].update(reviewer_provider="first",
                                  second_reviewer={"actor": "second-bot", "provider": "second"})
    # The fixture's lifecycle policy is returned by the runner API.
    cfg = runner.lifecycle_api().load_config(config["lifecycle_config"])
    cfg["actors"]["product"] = "product-bot"
    row = {"body": f"{prefix}: {verdict}\nSynthetic decision.", "state": "COMMENTED",
           "user": {"login": actor} if actor else None, "commit_id": job["reviewed_head"]}
    (client.reviews if source == "review" else client.comment_rows).append(row)
    advance(delivery)
    if actor in {"owner", "runner-bot", "product-bot", "second-bot"}:
        assert job["integration"]["reason"] == "merge_conditions_missing"
        assert not any(w[0] == "AUTO" for w in client.writes)
    else:
        assert job["integration"]["stage"] == "auto_merge_enabled"
