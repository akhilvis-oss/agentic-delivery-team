"""Owner continuation uses synthetic clients and disposable repositories only."""

from pathlib import Path

import pytest

from lifecycle import core, service
from test_lifecycle import configured_service
from test_lifecycle_fetch import command
import test_runner as fixtures


def saved_story(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)

    def stopped(config, state, path, job, role):
        assert role == "worker"
        fixtures.deliver(config)
        return fixtures.result("blocked")

    monkeypatch.setattr(runner, "execute", stopped)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job["continuation"] and job["status"] == "needs_owner"
    return cfg, hub, runner, job


def approve(hub, head, block=None):
    hub.issues[1]["body"] += "\n\n" + (
        "### Continue from\nCommit: " + head if block is None else block
    )
    hub.event(1, "approved", "owner")
    hub.approve()


def newest_claim(cfg):
    claims = [core.read(p) for p in (Path(cfg["state_dir"]) / "claims").glob("*.json")]
    return max(claims, key=lambda c: c["event"])


def test_owner_continuation_merges_fetched_target_and_binds_full_review(tmp_path, monkeypatch):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    source = Path(cfg["repository_root"])
    (source / "target.txt").write_text("new target\n")
    command(source, "add", "target.txt")
    command(source, "commit", "-qm", "target advance")
    target = command(source, "rev-parse", "HEAD")
    approve(hub, old["head_commit"])
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    job = next(j for j in service.jobs(cfg) if j["id"] == claim["job"])
    tree = claim["worktree"]
    assert command(tree, "rev-parse", "HEAD^1") == old["head_commit"]
    assert command(tree, "rev-parse", "HEAD^2") == target
    assert Path(tree, "delivery.txt").exists() and Path(tree, "target.txt").exists()
    assert claim["continued_from"] == old["head_commit"]
    assert claim["continuation_number"] == 1
    assert job["review_base"] == target == job["assignment"]["review_base"]
    assert job["assignment"]["continuation"]["commit"] == old["head_commit"]
    config = runner.load_config(claim["config"])
    prompt = runner.prompt_for(config, job, "worker")
    assert "Continuing from commit " + old["head_commit"] in prompt
    assert "Previous stop reason: worker_reported_blocked" in prompt
    assert "acceptance criteria take priority" in prompt


@pytest.mark.parametrize("block", [
    "## Continue from\nCommit: {head}", "### Continue from\n\nCommit: {head}",
    "### Continue from\nCommit: {head} ", "### Continue from\ncommit: {head}",
    "### Continue from\nCommit: ABCDEF0123456789ABCDEF0123456789ABCDEF01",
    "### Continue from\nCommit: abc", " ### Continue from\nCommit: {head}",
])
def test_malformed_continuation_starts_fresh(tmp_path, monkeypatch, block):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    approve(hub, old["head_commit"], block.format(head=old["head_commit"]))
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    assert not claim.get("continued_from")
    assert claim["start_commit"] == command(cfg["repository_root"], "rev-parse", "main")
    assert not Path(claim["worktree"], "delivery.txt").exists()


@pytest.mark.parametrize("refusal", [
    "unrecorded", "failed", "blocked_integrity", "missing_ancestry", "no_flag", "other_issue", "other_actor",
])
def test_unverified_or_integrity_failed_saved_commit_is_refused(tmp_path, monkeypatch, refusal):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    head = old["head_commit"]
    if refusal == "unrecorded":
        head = "a" * 40
    elif refusal in {"failed", "blocked_integrity", "missing_ancestry"}:
        old["status"] = "failed" if refusal == "failed" else "blocked"
        if refusal == "blocked_integrity":
            old["commit_refusals"] = [{"reason": "history_changed"}]
        if refusal == "missing_ancestry":
            old["blocked_reason"] = runner.block_reason("continuation_not_applied")
        core.save(Path(cfg["state_dir"]) / "jobs" / old["id"] / "runner-state"
                  / "tasks" / (old["id"] + ".json"), old)
    else:
        for comment in hub.comments_by_issue[1]:
            if '"continuation": true' in comment["body"]:
                if refusal == "no_flag":
                    comment["body"] = comment["body"].replace('"continuation": true', '"continuation": false')
                elif refusal == "other_issue":
                    comment["body"] = comment["body"].replace('"issue": 1', '"issue": 2')
                else:
                    comment["user"]["login"] = cfg["actors"]["product"]
    approve(hub, head)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    assert claim["error"] == "continuation_commit_unrecorded"
    assert claim["stage"] == "finished"
    assert not Path(claim["worktree"]).exists()
    assert len(service.jobs(cfg)) == 1
    assert any(r["reason"] == "continuation_commit_unrecorded"
               for r in core.records(cfg, hub.comments_by_issue[1], 1))


def test_conflict_stops_before_builder_and_only_lists_relative_files(tmp_path, monkeypatch):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    source = Path(cfg["repository_root"])
    (source / "delivery.txt").write_text("conflicting target\n")
    command(source, "add", "delivery.txt")
    command(source, "commit", "-qm", "conflicting target")
    approve(hub, old["head_commit"])
    monkeypatch.setattr(runner, "execute", lambda *a: pytest.fail("builder must not start"))
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    assert claim["error"] == "continuation_conflict" and len(service.jobs(cfg)) == 1
    record, = [r for r in core.records(cfg, hub.comments_by_issue[1], 1)
               if r["reason"] == "continuation_conflict"]
    assert record["conflict_files"] == ["delivery.txt"]
    body = core.record_body(record)
    assert '"delivery.txt"' in body and str(tmp_path) not in body


def test_third_continuation_is_refused_from_claim_count(tmp_path, monkeypatch):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    directory = Path(cfg["state_dir"]) / "claims"
    # Durable claims, rather than issue text or builder-supplied counters, own the budget.
    for n in (1, 2):
        core.save(directory / f"past-{n}.json", {
            "job": f"past-{n}", "issue": 1, "event": n, "stage": "finished",
            "continued_from": old["head_commit"], "cleaned": True,
        })
    approve(hub, old["head_commit"])
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert newest_claim(cfg)["error"] == "continuation_limit"
    record = next(r for r in core.records(cfg, hub.comments_by_issue[1], 1)
                  if r["reason"] == "continuation_limit")
    body = core.record_body(record)
    assert "restart fresh" in body and "split the story" in body
    assert len(service.jobs(cfg)) == 1


def test_continuation_has_exactly_one_correction(tmp_path, monkeypatch):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    approve(hub, old["head_commit"])
    calls = []

    def execute(config, state, path, job, role):
        calls.append(role)
        if role == "worker":
            fixtures.deliver(config)
            return fixtures.result("ready_for_review")
        return fixtures.result("needs_changes", "reviewer")

    monkeypatch.setattr(runner, "execute", execute)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    job = next(j for j in service.jobs(cfg) if j["id"] == claim["job"])
    assert calls == ["worker", "reviewer", "worker", "reviewer"]
    assert job["round"] == 2 and len(job["review_history"]) == 2


def test_missing_continued_commit_stops_before_review(tmp_path, monkeypatch):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    approve(hub, old["head_commit"])
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    original = runner.git

    def git(config, *args, **kwargs):
        if args[:3] == ("merge-base", "--is-ancestor", old["head_commit"]):
            raise runner.CommitRefused("history_changed")
        return original(config, *args, **kwargs)

    def execute(config, state, path, job, role):
        assert role == "worker"
        fixtures.deliver(config)
        return fixtures.result("ready_for_review")

    monkeypatch.setattr(runner, "git", git)
    monkeypatch.setattr(runner, "execute", execute)
    service.poll(cfg, hub.client(), clock=lambda: 300)
    claim = newest_claim(cfg)
    job = next(j for j in service.jobs(cfg) if j["id"] == claim["job"])
    assert job["blocked_reason"]["reason_code"] == "continuation_not_applied"
    assert runner.public_block_reason(job["blocked_reason"]) == job["blocked_reason"]
    assert not job.get("review_history")
    assert any(r["reason"] == "continuation_not_applied"
               for r in core.records(cfg, hub.comments_by_issue[1], 1))


def test_old_public_records_still_validate_without_optional_fields(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    record = core.public_record(cfg, 1, "conflicted", 0, "blocked",
                                reason="continuation_conflict", conflict_files=["sub/file.py"])
    core.queue(cfg, record)
    core.flush(cfg, hub.client())
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    assert len(records) == 2
    approval, = [r for r in records if r["kind"] == "approval"]
    assert "conflict_files" not in approval and "continuation" not in approval
    assert next(r for r in records if r["to"] == "blocked")["conflict_files"] == ["sub/file.py"]


def test_continuation_recovers_after_merge_without_refetch_or_builder_replay(tmp_path, monkeypatch):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    source = Path(cfg["repository_root"])
    command(source, "commit", "--allow-empty", "-qm", "target advance")
    target = command(source, "rev-parse", "HEAD")
    approve(hub, old["head_commit"])
    original = service.prepare_continuation
    merged_heads = []

    class Crash(BaseException):
        pass

    def crash(c, claim):
        assert original(c, claim) == []
        merged_heads.append(command(claim["worktree"], "rev-parse", "HEAD"))
        raise Crash()

    monkeypatch.setattr(service, "prepare_continuation", crash)
    with pytest.raises(Crash):
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    monkeypatch.setattr(service, "prepare_continuation", original)
    monkeypatch.setattr(service, "fetch_target", lambda c: pytest.fail("must retain fetched target"))
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 300)
    claim = newest_claim(cfg)
    assert claim["stage"] == "enqueued" and claim["review_base"] == target
    assert command(claim["worktree"], "rev-parse", "HEAD") == merged_heads[0]
    assert len(service.jobs(cfg)) == 2
