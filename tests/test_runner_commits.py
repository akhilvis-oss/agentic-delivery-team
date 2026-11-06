"""Trusted commit boundaries: temporary repos and fake workers only."""

import json
from pathlib import Path

import pytest

from test_runner import commit, deliver, execute_delivery, result, runner
import test_runner as fixtures

setup = fixtures.setup


def fresh_job(cfg, path, job, policy=None):
    job["status"] = "blocked"
    runner.save_job(cfg, path, job)
    if policy is not None:
        cfg["project_profile"]["commit_policy"] = policy
    return runner.enqueue(cfg, "delivery", "synthetic delivery")


def test_runner_commit_identity_message_and_private_journal(setup, monkeypatch):
    cfg, state, path, _ = setup
    monkeypatch.setattr(runner, "execute", execute_delivery)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job["status"] == "ready_for_integration"
    row = job["runner_commits"][0]
    assert row == runner.read(state / "receipts/task.json")["runner_commits"][0]
    assert row["commit_sha"] == job["head_commit"] == job["reviewed_head"]
    assert row["job_id"] == "task" and row["round"] == 1 and row["job_kind"] == "work"
    assert row["file_count"] == 1 and row["committed_by"] == "runner" and row["state"] == "committed"
    assert runner.git(cfg, "log", "-1", "--format=%an|%ae|%cn|%ce") == (
        "Runner Fixture|runner@example.invalid|Runner Fixture|runner@example.invalid")
    assert runner.git(cfg, "log", "-1", "--format=%B") == "runner: job task kind work round 1"
    assert not runner.dirty_paths(cfg)
    assert not list((Path(cfg["worktree"]) / ".git").glob("*.lock"))
    assert not list(state.glob("commit-index-*"))


@pytest.mark.parametrize("name", [".env", ".env.local", "nested/.env.test", "secrets/data.txt",
                                   "credential.txt", "id_rsa", "id_ed25519", "signing.pem", "signing.key"])
def test_denied_paths_are_blocked_and_logged_without_commits(setup, monkeypatch, name):
    cfg, state, path, original = setup

    def execute(config, state, path, job, role):
        assert role == "worker"
        target = Path(config["worktree"]) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture only")
        return result("ready_for_review")

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job["status"] == "blocked" and job["commit_refusals"][0]["reason"] == "denied_path"
    assert job["commit_refusals"][0]["paths"] == [name]
    assert runner.git(cfg, "rev-parse", "HEAD") == original["base_commit"]
    receipt = runner.read(state / "receipts/task.json")
    assert receipt["commit_refusals"] == job["commit_refusals"]
    assert not receipt["runner_commits"]
    assert "paths" not in json.dumps(runner.public_receipt(receipt))


@pytest.mark.parametrize("name,reason", [("../outside.txt", "outside_or_invalid_path"),
    ("/absolute.txt", "outside_or_invalid_path"), ("a/../b", "outside_or_invalid_path"),
    (".git/config", "git_metadata"), ("nested/.GIT/config", "git_metadata")])
def test_malformed_or_metadata_candidate_paths_fail_closed(setup, monkeypatch, name, reason):
    cfg, _, path, _ = setup
    real = runner.trusted_git

    def injected(config, *args, **kwargs):
        if args[:2] == ("ls-files", "--others"):
            return name.encode() + b"\0"
        return real(config, *args, **kwargs)

    monkeypatch.setattr(runner, "trusted_git", injected)
    monkeypatch.setattr(runner, "execute", lambda *args: result("ready_for_review"))
    runner.run_once(cfg)
    job = runner.read(path)
    assert job["status"] == "blocked"
    assert job["commit_refusals"][0]["reason"] == reason
    assert job["commit_refusals"][0]["paths"] == [name]


def test_external_symlink_refused_without_reading_target_content(setup, monkeypatch, tmp_path):
    cfg, _, path, _ = setup
    outside = tmp_path / "outside.txt"
    outside.write_text("outside fixture")

    def execute(config, *args):
        (Path(config["worktree"]) / "link.txt").symlink_to(outside)
        return result("ready_for_review")

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job["status"] == "blocked"
    assert job["commit_refusals"][0]["reason"] == "external_or_invalid_symlink"
    assert outside.read_text() == "outside fixture"


def test_internal_symlink_is_committed_as_a_link(setup, monkeypatch):
    cfg, _, path, _ = setup

    def execute(config, state, path, job, role):
        if role == "worker":
            root = Path(config["worktree"])
            (root / "target.txt").write_text("target")
            (root / "link.txt").symlink_to("target.txt")
        return result("ready_for_review" if role == "worker" else "ready_for_integration", role)

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "ready_for_integration"
    assert runner.git(cfg, "ls-tree", "HEAD", "link.txt").startswith("120000 blob")
    assert runner.git(cfg, "show", "HEAD:link.txt") == "target.txt"


def test_gitlink_with_external_pointer_is_not_followed(setup, tmp_path):
    cfg, _, _, _ = setup
    root = Path(cfg["worktree"])
    module = root / "module"
    module.mkdir()
    (module / ".git").write_text("gitdir: " + str(tmp_path / "outside-git") + "\n")
    with pytest.raises(runner.CommitRefused, match="submodule_not_supported"):
        runner.snapshot_entry(cfg, root, "module", ("160000", "a" * 40), runner.DEFAULT_COMMIT_POLICY)
    assert not (tmp_path / "outside-git").exists()


@pytest.mark.parametrize("policy,names,reason", [
    ({"deny_patterns": ["restricted/*"]}, {"restricted/data.txt": "x"}, "denied_path"),
    ({"max_files": 1}, {"a": "x", "b": "x"}, "file_count_limit"),
    ({"max_file_bytes": 3}, {"a": "four"}, "file_size_limit"),
    ({"max_file_bytes": 10, "max_total_bytes": 5}, {"a": "abc", "b": "def"}, "total_size_limit"),
])
def test_configurable_commit_limits_and_additional_denies(setup, monkeypatch, policy, names, reason):
    cfg, _, original, job = setup
    path = fresh_job(cfg, original, job, policy)

    def execute(config, *args):
        for name, value in names.items():
            target = Path(config["worktree"]) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value)
        return result("ready_for_review")

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    final = runner.read(path)
    assert final["status"] == "blocked" and final["commit_refusals"][0]["reason"] == reason
    assert final["commit_refusals"][0]["paths"] == sorted(names)


def test_tracked_changes_deletions_new_files_and_exclusions(setup, monkeypatch):
    cfg, _, original, job = setup
    root = Path(cfg["worktree"])
    for name in ("tracked.txt", "remove.txt", "ignored.txt"):
        (root / name).write_text("baseline")
    (root / ".gitignore").write_text("ignored.txt\nignored-new.txt\n")
    runner.git(cfg, "add", "--force", "tracked.txt", "remove.txt", "ignored.txt", ".gitignore")
    commit(cfg)
    path = fresh_job(cfg, original, job)

    def execute(config, state, path, current, role):
        if role == "worker":
            (root / "tracked.txt").write_text("changed")
            (root / "remove.txt").unlink()
            (root / "new.txt").write_text("new")
            (root / "ignored.txt").write_text("excluded tracked change")
            (root / "ignored-new.txt").write_text("excluded new file")
            (root / "coordination.md").write_text("excluded coordination")
        return result("ready_for_review" if role == "worker" else "ready_for_integration", role)

    # Ignored tracked changes remain dirty and must not be silently reviewed as
    # part of the commit. Test the commit primitive directly to inspect the range.
    current = runner.read(path)
    current["round"] = 1
    execute(cfg, None, None, None, "worker")
    sha = runner.runner_commit(cfg, path, current, current["base_commit"])
    assert set(runner.git(cfg, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines()) == {
        "tracked.txt", "remove.txt", "new.txt"}
    assert runner.git(cfg, "show", sha + ":ignored.txt") == "baseline"
    assert set(runner.dirty_paths(cfg)) == {"ignored.txt", "coordination.md"}


def test_correction_rounds_have_distinct_runner_commits(setup, monkeypatch):
    cfg, _, path, _ = setup

    def execute(config, state, path, job, role):
        if role == "worker":
            deliver(config)
            return result("ready_for_review")
        return result("needs_changes" if job["round"] == 1 else "ready_for_integration", role)

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job["status"] == "ready_for_integration"
    assert [c["round"] for c in job["runner_commits"]] == [1, 2]
    assert len({c["commit_sha"] for c in job["runner_commits"]}) == 2
    assert runner.git(cfg, "rev-parse", job["runner_commits"][1]["commit_sha"] + "^") == job["runner_commits"][0]["commit_sha"]


def test_worker_never_gets_git_write_access_or_commit_instructions(setup):
    cfg, _, _, job = setup
    command = runner.command(cfg, "worker", Path("out"), Path("schema"))
    assert "--approve-for-me" in command
    assert not any(x in command for x in ["--add-dir", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"])
    assert all(".git" not in argument for argument in command)
    prompt = runner.prompt_for(cfg, job, "worker")
    assert "UNCOMMITTED" in prompt and "Never request .git write access" in prompt


def test_runner_does_not_execute_hooks_or_attribute_filters(setup, monkeypatch):
    cfg, _, original, job = setup
    root = Path(cfg["worktree"])
    (root / ".gitattributes").write_text("*.txt filter=tripwire\n")
    runner.git(cfg, "add", ".gitattributes")
    commit(cfg)
    path = fresh_job(cfg, original, job)
    hook = root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o700)
    runner.git(cfg, "config", "filter.tripwire.clean", "false")
    runner.git(cfg, "config", "filter.tripwire.required", "true")
    monkeypatch.setattr(runner, "execute", execute_delivery)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "ready_for_integration"
    assert runner.git(cfg, "show", "HEAD:delivery.txt") == "delivery"


def test_preexisting_stage_is_never_committed_or_reset(setup, monkeypatch):
    cfg, _, path, _ = setup
    root = Path(cfg["worktree"])
    (root / "owner.txt").write_text("owner data")
    runner.git(cfg, "add", "owner.txt")
    monkeypatch.setattr(runner, "execute", execute_delivery)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job["status"] == "blocked" and job["commit_refusals"][0]["reason"] == "preexisting_staged_changes"
    assert runner.git(cfg, "diff", "--cached", "--name-only") == "owner.txt"


def test_missing_identity_is_a_clear_refusal(setup, monkeypatch):
    cfg, _, path, _ = setup
    del cfg["commit_identity"]
    monkeypatch.setattr(runner, "execute", execute_delivery)
    runner.run_once(cfg)
    assert runner.read(path)["commit_refusals"][0]["reason"] == "configure_runner_identity"


def test_snapshot_replacement_with_external_link_cannot_be_staged(setup, monkeypatch, tmp_path):
    cfg, _, path, _ = setup
    real = runner.trusted_git
    outside = tmp_path / "outside.txt"
    outside.write_text("must not enter git")

    def change_after_snapshot(config, *args, **kwargs):
        out = real(config, *args, **kwargs)
        if args[:1] == ("hash-object",):
            p = Path(config["worktree"]) / "delivery.txt"
            p.unlink()
            p.symlink_to(outside)
        return out

    monkeypatch.setattr(runner, "trusted_git", change_after_snapshot)
    monkeypatch.setattr(runner, "execute", execute_delivery)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job["status"] == "blocked" and not job.get("runner_commits")


def test_prepared_receipt_is_persisted_before_ref_publication(setup, monkeypatch):
    cfg, state, path, _ = setup
    real = runner.trusted_git
    seen = []

    def inspect(config, *args, **kwargs):
        if args[:1] == ("update-ref",):
            receipt = runner.read(state / "receipts/task.json")
            seen.append(receipt["runner_commits"][0]["state"])
        return real(config, *args, **kwargs)

    monkeypatch.setattr(runner, "trusted_git", inspect)
    monkeypatch.setattr(runner, "execute", execute_delivery)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "ready_for_integration"
    assert seen == ["prepared"]


def test_uncommitted_operator_work_is_never_silently_committed(setup, monkeypatch):
    cfg, _, path, _ = setup
    (Path(cfg["worktree"]) / "owner-uncommitted.txt").write_text("preserve")
    monkeypatch.setattr(runner, "execute", lambda *args: pytest.fail("must not dispatch onto operator changes"))
    runner.run_once(cfg)
    job = runner.read(path)
    assert job["status"] == "blocked"
    assert job["commit_refusals"][0]["reason"] == "preexisting_worktree_changes"
