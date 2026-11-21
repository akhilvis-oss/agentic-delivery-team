"""Exhaustive stop classification and transport-only notice changes."""

from collections import Counter
import json
from pathlib import Path
import re

import pytest

from lifecycle import core
from test_lifecycle import Hub, policy
from test_runner_integration import advance, delivery, integration, runner, setup  # noqa: F401


# Independent specification: explicit fixed codes and all generated check cases.
DECISION = {
    "owner_question", "question_withheld", "scope_change", "acceptance_criteria_change",
    "story_approval", "clarification_unverified", "product_request_unclassified",
    "repository_evidence", "approved_scope_changed", "pr_closed_unmerged",
    "owner_requested_stop", "continuation_limit", "pr_open_reapproval",
}
MERGE = {"owner_merge_required"}
FIX = {
    "checks_worktree_changed",
    "commit_refused", "delivery_disabled", "delivery_configuration_invalid",
    "continuation_commit_unrecorded", "continuation_conflict", "continuation_not_applied",
    "invalid_status_change", "approval_not_attested", "prerequisite_failed",
    "assignment_preflight", "worker_reported_blocked", "reviewer_reported_blocked",
    "execution_failed", "publication_failed", "publication_uncertain", "stale_head",
    "gate_category_higher", "required_check_failed", "merge_conditions_missing",
    "auto_merge_disable_uncertain",
}
CHECKS = ("preflight", "build", "story_tests", "runner_checks", "prerequisite")
WAITING = {f"worker_{check}_unavailable" for check in CHECKS}
FIX |= {f"worker_{check}" for check in CHECKS}
FIX |= {f"worker_{check}_unrelated_failure" for check in CHECKS}
FIX |= {f"worker_{check}_attempts_exhausted_{attempt}" for check in CHECKS
        for attempt in range(1, 11)}
EXPECTED = {code: kind for kind, codes in [
    ("Needs your decision", DECISION), ("Needs your merge", MERGE),
    ("Waiting on the system", WAITING), ("Needs a fix", FIX),
] for code in codes}


def test_every_stop_has_exactly_one_specified_kind():
    assert len(DECISION | MERGE | WAITING | FIX) == sum(map(len, [DECISION, MERGE, WAITING, FIX]))
    assert set(EXPECTED) == set(core.STOP_REASONS)
    assert core.STOP_KINDS == EXPECTED
    assert Counter(core.STOP_KINDS.values()) == {
        "Needs your decision": 13, "Needs your merge": 1,
        "Waiting on the system": 5, "Needs a fix": 81,
    }


@pytest.mark.parametrize("code,kind", sorted(EXPECTED.items()))
@pytest.mark.parametrize("saved_body", [False, True])
def test_flush_prefixes_every_stop_at_send_time(tmp_path, code, kind, saved_body):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state="in-progress")
    record = core.public_record(cfg, 1, "stop", 1, "needs-owner", event=101, reason=code)
    record.update({"from": core.state(hub.issues[1]), "actor": cfg["actors"]["runner"]})
    original = core.record_body(record)
    public_body = "Previously saved notice\n" + original if saved_body else original
    path = core.queue(cfg, record, **({"public_body": public_body} if saved_body else {}))
    core.flush(cfg, hub.client())
    body = next(c["body"] for c in hub.comments_by_issue[1] if record["key"] in c["body"])
    assert body == kind + ": " + public_body
    assert core.record_body(record) == original
    stored = core.read(path)
    assert stored["record"] == record
    if saved_body:
        assert stored["public_body"] == public_body
    assert core.records(cfg, hub.comments_by_issue[1], 1)[-1] == record
    core.flush(cfg, hub.client())
    assert sum(record["key"] in c["body"] for c in hub.comments_by_issue[1]) == 1


@pytest.mark.parametrize("guard", [False, True])
def test_flush_non_stop_notice_is_unchanged(tmp_path, guard):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    record = core.public_record(cfg, 1, "ordinary", 0, "approved" if guard else "dispatched",
                                event=101, kind="guard" if guard else "transition")
    record.update({"from": "approved",
                   "actor": cfg["actors"]["workflow" if guard else "runner"]})
    original = "Guard explanation\n" + core.record_body(record) if guard else core.record_body(record)
    core.queue(cfg, record, role="workflow" if guard else "runner", public_body=original)
    core.flush(cfg, hub.client("workflow" if guard else "runner"))
    assert next(c["body"] for c in hub.comments_by_issue[1] if record["key"] in c["body"]) == original


@pytest.mark.parametrize("code", sorted(integration.REASONS))
def test_delivery_record_prefix_preserves_notice_and_stored_reason(delivery, monkeypatch, code):  # noqa: F811
    from test_single_stop_notice import delivery_records

    cfg, hub = delivery_records(delivery, monkeypatch)
    config, path, job, client = delivery
    monkeypatch.setattr(integration, "merge_evidence", lambda *args: code)
    advance(delivery)
    assert job["integration"]["reason"] == code
    assert job["blocked_reason"] == {"reason_code": code, "reason": integration.REASONS[code]}
    notices = [data["body"] for method, endpoint, data in client.writes
               if method == "POST" and endpoint == "/issues/1/comments"
               and "Reviewed head: " in data["body"]]
    original = integration.REASONS[code]
    if code == "owner_merge_required":
        original += "\nhttps://github.com/example/kit/pull/9"
    original += "\nReviewed head: " + job["reviewed_head"]
    assert len(notices) == 1
    assert notices[0].startswith(EXPECTED[code] + ": " + original)
    assert core.PREFIX in notices[0]
    assert core.records(cfg, hub.comments_by_issue[1], 1)[-1]["reason"] == code
    before = list(client.writes)
    advance(delivery)
    assert client.writes == before


runner.integration_api()


@pytest.mark.parametrize("key", sorted(runner.BLOCK_REASONS))
def test_runner_receipt_prefix_preserves_allowlisted_reason(monkeypatch, key):
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: calls.append(kwargs))
    reason = runner.block_reason(key)
    receipt = dict(status="blocked", base_commit="a" * 40, head_commit=None,
                   summary=runner.SUMMARIES["blocked"], timestamp="2026-01-01T10:00:00Z",
                   blocked_reason=reason,
                   runner_commits=[{"commit_sha": "b" * 40}])
    runner.GitHubClient().post("example/project", 7, receipt)
    original = (f"Task receipt: blocked\nCommits: {'a' * 40}..None\n"
                f"Summary: {receipt['summary']}\nRun: {receipt['timestamp']}\n"
                f"Blocked: {reason['reason_code']}: {reason['reason']}\n"
                f"Runner commit: {'b' * 40} (committed_by: runner)")
    assert json.loads(calls[0]["input"])["body"] == EXPECTED[reason["reason_code"]] + ": " + original
    assert receipt["blocked_reason"] == reason


def test_non_stop_runner_receipt_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: calls.append(kwargs))
    receipt = dict(status="ready_for_integration", base_commit="a" * 40, head_commit="b" * 40,
                   summary=runner.SUMMARIES["ready_for_integration"], timestamp="2026-01-01T10:00:00Z")
    runner.GitHubClient().post("example/project", 7, receipt)
    assert json.loads(calls[0]["input"])["body"] == (
        f"Task receipt: ready_for_integration\nCommits: {'a' * 40}..{'b' * 40}\n"
        f"Summary: {receipt['summary']}\nRun: {receipt['timestamp']}")


def test_troubleshooting_table_covers_each_stop_once_in_its_kind():
    text = (Path(__file__).resolve().parents[1] / "starter-kit/docs/troubleshooting.md").read_text()
    table = text.split("| Kind | Stop reason | What happened and what to do |\n", 1)[1].split("\n\n", 1)[0]
    seen = []
    for row in table.splitlines()[1:]:
        _, kind, codes, action, _ = row.split("|")
        matched = set()
        for pattern in re.findall(r"`([^`]+)`", codes):
            if pattern == "worker_*_attempts_exhausted_N":
                matches = {code for code in EXPECTED if re.fullmatch(
                    r"worker_(?:preflight|build|story_tests|runner_checks|prerequisite)_attempts_exhausted_(?:[1-9]|10)", code)}
            elif pattern.startswith("worker_*_"):
                matches = {code for code in EXPECTED if code.startswith("worker_")
                           and code.endswith(pattern.removeprefix("worker_*"))}
            else:
                matches = {pattern}
            assert matches and matches <= set(EXPECTED)
            assert all(EXPECTED[code] == kind.strip() for code in matches)
            assert not matched & matches
            matched |= matches
        seen.extend(matched)
        if kind.strip() == "Waiting on the system":
            assert "re-approve" in action and "automatically" not in action
    assert Counter(seen) == Counter({code: 1 for code in EXPECTED})
