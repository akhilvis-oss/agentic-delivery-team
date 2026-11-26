"""Evidence-only lifecycle rounds use synthetic clients and disposable repositories."""

import json
from pathlib import Path
import sys

import pytest

from lifecycle import core, service
from test_lifecycle import configured_service
from test_lifecycle_continuation import newest_claim, saved_story
import test_runner as fixtures


EVIDENCE_REASON = (
    "The last correction round changed no files and the review still needs changes, "
    "usually because it asked for evidence the runner's own checks don't produce. "
    "Owner: read the findings; if they name a check, add it to the runner's required "
    "check commands, then re-approve to continue from the saved work."
)


@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("exhausted,retry", [(False, False), (True, False), (True, True)])
def test_evidence_round_review_and_exhaustion(tmp_path, monkeypatch, continuation, exhausted, retry):
    if continuation:
        cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
        hub.event(1, "approved", "owner")
        hub.approve()
    else:
        cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    cfg["pushback"]["max_correction_rounds"] = 1
    cfg["system_retry_minutes"] = [1]
    core.save(cfg["config_path"], cfg)
    settings = core.read(cfg["runner_config"])
    settings["verification"] = {"commands": [[sys.executable, "-c", "print('runner check')"]]}
    core.save(cfg["runner_config"], settings)
    calls, commits, checks, heads = [], [], [], []
    original_commit = runner.runner_commit
    original_verify = runner.verify_required_tests
    failed = []

    def commit(config, path, job, head, **kwargs):
        if not kwargs.get("saved_at_stop"):
            commits.append(job["round"])
        return original_commit(config, path, job, head, **kwargs)

    def verify(config, state, path, job):
        checks.append((job["round"], job["head_commit"]))
        return original_verify(config, state, path, job)

    def execute(config, state, path, job, role):
        job.update(status="running_" + role, updated_at=core.now())
        runner.save_job(config, path, job)
        calls.append((job["round"], role))
        if role == "worker":
            if not continuation and job["round"] == 1:
                fixtures.deliver(config)
            return fixtures.result("ready_for_review")
        heads.append(job["head_commit"])
        if retry and job["round"] == 2 and not failed:
            failed.append(True)
            raise runner.AgentExit("agent_sign_in_failed", retryable=True)
        return fixtures.result("needs_changes" if exhausted or job["round"] == 1
                               and not continuation else "ready_for_integration", role)

    # Publication is outside this worker/reviewer cycle test.
    monkeypatch.setattr(runner.integration_api(), "advance", lambda *args: None)
    monkeypatch.setattr(runner, "runner_commit", commit)
    monkeypatch.setattr(runner, "verify_required_tests", verify)
    monkeypatch.setattr(runner, "execute", execute)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    job = next(j for j in service.jobs(cfg) if j["id"] == claim["job"])
    if retry:
        assert job["status"] == "waiting_system"
        assert job["evidence_rounds"] == ([1, 2] if continuation else [2])
        checks_before = list(job["verification_runs"])
        cfg = core.load_config(cfg["config_path"])
        service.poll(cfg, hub.client(), clock=lambda: 260, full_list=True)
        job = next(j for j in service.jobs(cfg) if j["id"] == claim["job"])
        assert job["verification_runs"] == checks_before
    rounds = 2 if exhausted or not continuation else 1
    assert job["evidence_rounds"] == (list(range(1, rounds + 1)) if continuation else [2])
    assert commits == ([] if continuation else [1])
    assert len(checks) == rounds and len({head for _, head in checks}) == 1
    assert all(v["status"] == "passed" for v in job["verification_runs"])
    assert len(job["review_history"]) == rounds
    assert len(set(heads)) == 1
    assert calls.count((2, "worker")) == (1 if rounds == 2 else 0)
    assert not job.get("commit_refusals")
    assert job["status"] == ("needs_owner" if exhausted else "ready_for_integration")
    if continuation:
        assert job["head_commit"] == old["head_commit"]
    records = [r for r in core.records(cfg, hub.comments_by_issue[1], 1) if r.get("job") == job["id"]]
    for number in job["evidence_rounds"]:
        states = [r["to"] for r in records if r["round"] == number]
        assert states.index("committed") < states.index("in-review")
        assert all(r["commit"] == job["head_commit"] for r in records
                   if r["round"] == number and r["to"] in {"committed", "in-review"})
    assert not any("was rejected" in c["body"] for c in hub.comments_by_issue[1])
    if exhausted:
        assert job["blocked_reason"] == {"reason_code": "runner_evidence_missing", "reason": EVIDENCE_REASON}
        assert records[-1]["to"] == "needs-owner"
        assert records[-1]["reason"] == "runner_evidence_missing"


def test_evidence_round_record_survives_saved_job(tmp_path):
    job = dict(id="evidence", round=2, status="running_worker", head_commit="a" * 40,
               evidence_rounds=[2], assignment={"lifecycle": {"issue": 1, "event": 101}})
    path = tmp_path / "job.json"
    core.save(path, job)
    record, = core.job_records({"repository": "example/project"}, core.read(path))
    assert record["to"] == "committed" and record["commit"] == job["head_commit"]


def test_examples_require_all_common_checks():
    root = Path(__file__).resolve().parents[1]
    commands = [["<ABSOLUTE_PYTHON_EXECUTABLE>", "-m", "pytest", "-q"],
                ["<ABSOLUTE_PYTHON_EXECUTABLE>", "-m", "ruff", "check", "."],
                ["<ABSOLUTE_PYTHON_EXECUTABLE>", "scripts/check_private_content.py"]]
    assert json.loads((root / "runner/config.example.json").read_text())["verification"]["commands"] == commands
    assert json.loads((root / "runner/pool.example.json").read_text())["test_commands"] == commands


def test_worker_note_explains_evidence_rounds():
    assert ("If a correction needs no file change, for example a request for evidence "
            "the runner's checks produce, return `ready_for_review` without changes. "
            "The runner re-runs its checks and asks for a fresh review.") in fixtures.runner.LIFECYCLE_NOTES["worker"]


@pytest.mark.parametrize("mode", ["fresh", "coordination", "blocked"])
def test_no_change_exceptions_remain_refused(tmp_path, monkeypatch, mode):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    profile_path = core.read(cfg["runner_config"])["profile"]
    profile = core.read(profile_path)
    profile["coordination_files"] = ["coordination.md"]
    core.save(profile_path, profile)
    reviews = []

    def execute(config, state, path, job, role):
        job.update(status="running_" + role, updated_at=core.now())
        runner.save_job(config, path, job)
        if role == "worker":
            if mode != "fresh" and job["round"] == 1:
                fixtures.deliver(config)
            elif mode == "coordination":
                Path(config["worktree"], "coordination.md").write_text("coordination only\n")
            elif mode == "blocked":
                return fixtures.result("blocked")
            return fixtures.result("ready_for_review")
        reviews.append(job["round"])
        return fixtures.result("needs_changes", role)

    monkeypatch.setattr(runner, "execute", execute)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert not job.get("evidence_rounds")
    assert reviews == ([] if mode == "fresh" else [1])
    if mode == "blocked":
        assert job["status"] == "needs_owner"
        assert job["blocked_reason"]["reason_code"] == "worker_reported_blocked"
    else:
        assert job["status"] == "blocked"
        assert job["commit_refusals"][-1]["reason"] == "no_committable_changes"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("continuation", [False, True])
def test_evidence_round_refuses_hidden_tracked_changes(tmp_path, monkeypatch, flag, continuation):
    if continuation:
        cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
        hub.event(1, "approved", "owner")
        hub.approve()
    else:
        cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    reviews, checks, hidden = [], [], []
    original_verify = runner.verify_required_tests

    def verify(config, state, path, job):
        checks.append(job["round"])
        return original_verify(config, state, path, job)

    def execute(config, state, path, job, role):
        job.update(status="running_" + role, updated_at=core.now())
        runner.save_job(config, path, job)
        if role == "worker":
            if not continuation and job["round"] == 1:
                fixtures.deliver(config)
            else:
                head = runner.git(config, "rev-parse", "HEAD")
                if continuation:
                    assert head == old["head_commit"]
                committed = runner.git(config, "show", head + ":delivery.txt")
                runner.git(config, "update-index", flag, "delivery.txt")
                tracked = Path(config["worktree"], "delivery.txt")
                tracked.write_text("hidden modified tracked bytes\n")
                assert tracked.read_text().rstrip("\n") != committed
                assert runner.git(config, "rev-parse", "HEAD") == head
                assert runner.git(config, "status", "--porcelain=v1") == ""
                assert runner.git(config, "diff", "HEAD", "--binary") == ""
                assert runner.dirty_paths(config) == []
                hidden.append((config, head, runner.git(config, "ls-files", "-v")))
            return fixtures.result("ready_for_review")
        reviews.append(job["round"])
        return fixtures.result("needs_changes" if job["round"] == 1 and not continuation
                               else "ready_for_integration", role)

    monkeypatch.setattr(runner.integration_api(), "advance", lambda *args: None)
    monkeypatch.setattr(runner, "verify_required_tests", verify)
    monkeypatch.setattr(runner, "execute", execute)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    job = next(j for j in service.jobs(cfg) if j["id"] == claim["job"])
    config, starting_head, flags = hidden.pop()
    assert not hidden
    assert runner.git(config, "rev-parse", "HEAD") == starting_head
    assert Path(config["worktree"], "delivery.txt").read_text() == "hidden modified tracked bytes\n"
    assert runner.git(config, "ls-files", "-v") == flags
    assert not job.get("evidence_rounds"), (
        f"hidden modified bytes admitted as an evidence round: checks={checks}, reviews={reviews}, "
        f"status={job['status']}, unchanged_head={starting_head}"
    )
    assert checks == ([] if continuation else [1])
    assert reviews == ([] if continuation else [1])
    assert job["status"] == "blocked"
