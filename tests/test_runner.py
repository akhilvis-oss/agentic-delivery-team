"""Runner safety tests: synthetic repositories, fake agents, no real credentials."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("task_runner", ROOT / "runner/task_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
REAL_CHECK_CLI = runner.check_cli
CLI_VERSION = "0.164.0-alpha.11.1"


def agent_config(executable):
    agent = runner.read(ROOT / "runner/config.example.json")["agent"]
    agent["command"][0] = executable
    agent["version_command"][0] = executable
    return agent


def use_fake(cfg, state, source):
    cfg["agent"] = agent_config(fake_agent(state, source))


def result(status, role="worker"):
    value = dict(status=status, summary="synthetic result", tests=["focused tests"], limitations=[])
    if role == "reviewer":
        value["findings"] = [] if status == "ready_for_integration" else [
            dict(severity="P1", path="example.py", detail="correct this")]
    return value


def commit(config):
    runner.git(config, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "commit", "-qm", "delivery", "--allow-empty")
    return runner.git(config, "rev-parse", "HEAD")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feature", str(repo)], check=True)
    cfg = dict(worktree=str(repo), branch="feature", state_dir=str(tmp_path / "state"),
               agent_home=str(tmp_path / "agent-home"), agent=agent_config("/must-not-launch"),
               worker_model="fixture-worker", reviewer_model="fixture-reviewer", max_rounds=2,
               phase_timeout_seconds=5,
               commit_identity={"name": "Runner Fixture", "email": "runner@example.invalid"},
               project_profile={"worker_prompt": "Implement assigned changes.",
                                "reviewer_prompt": "Check correctness and failure modes.",
                                "scope": "Only the assigned synthetic feature.",
                                "extra_scopes": {"extended-check": "Run the explicitly authorized extra check."},
                                "coordination_files": ["coordination.md"]})
    commit(cfg)
    state = runner.private_dir(cfg["state_dir"])
    path = runner.enqueue(cfg, "task", "bounded task")
    monkeypatch.setattr(runner, "check_cli", lambda _: CLI_VERSION)
    return cfg, state, path, runner.read(path)


def deliver(config):
    path = Path(config["worktree"]) / "delivery.txt"
    path.write_text((path.read_text() if path.exists() else "") + "delivery\n")


def execute_delivery(config, state, path, job, role):
    if role == "worker":
        deliver(config)
    return result("ready_for_review" if role == "worker" else "ready_for_integration", role)



def test_bounded_repair_then_independent_review_and_no_duplicate(setup, monkeypatch):
    cfg, state, path, _ = setup
    calls = []

    def execute(config, state, path, job, role):
        calls.append(role)
        if role == "worker":
            deliver(config)
            return result("ready_for_review")
        return result("needs_changes" if job["round"] == 1 else "ready_for_integration", role)

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    assert calls == ["worker", "reviewer", "worker", "reviewer"]
    assert runner.read(path)["status"] == "ready_for_integration"
    runner.run_once(cfg)
    assert len(calls) == 4


def test_delayed_job_waits_without_spending_model_calls(setup, monkeypatch):
    cfg, state, path, job = setup
    job["not_before_epoch"] = 2000
    runner.save_job(cfg, path, job)
    monkeypatch.setattr(runner.time, "time", lambda: 1999)
    monkeypatch.setattr(runner, "check_cli", lambda _: pytest.fail("not due; no CLI probes"))
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "queued"
    monkeypatch.setattr(runner, "check_cli", lambda _: CLI_VERSION)
    monkeypatch.setattr(runner.time, "time", lambda: 2000)
    monkeypatch.setattr(runner, "execute", execute_delivery)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "ready_for_integration"


def test_retry_limit(setup, monkeypatch):
    cfg, state, path, _ = setup

    def execute(config, state, path, job, role):
        if role == "worker":
            deliver(config)
            return result("ready_for_review")
        return result("needs_changes", role)

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "needs_primary_review"
    assert runner.read(path)["round"] == 2



def test_head_change_blocks_not_silent_rebase(setup):
    cfg, state, path, _ = setup
    commit(cfg)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "blocked"


def test_crashed_run_not_replayed(setup, monkeypatch):
    cfg, state, path, job = setup
    job.update(status="running_worker", child_pid=123)
    runner.save_job(cfg, path, job)
    monkeypatch.setattr(runner, "execute", lambda *args: pytest.fail("never replay"))
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "interrupted"
    second = runner.enqueue(cfg, "second", "new explicit task")
    runner.run_once(cfg)
    assert runner.read(second)["status"] == "queued"
    assert runner.read(path)["child_pid"] == 123  # Never signal a persisted/reused PID.


def test_single_runner_and_pause(setup, monkeypatch):
    cfg, state, path, _ = setup
    monkeypatch.setattr(runner, "execute", lambda *args: pytest.fail("locked/paused"))
    with runner.lock(state / "runner.lock") as acquired:
        assert acquired
        runner.run_once(cfg)
    (state / "PAUSE").touch()
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "queued"
    (state / "PAUSE").unlink()
    (state / "CANCEL").touch()
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "queued"


def test_unexpected_worker_commit_blocks_review(setup, monkeypatch):
    cfg, state, path, _ = setup

    def execute(config, state, path, job, role):
        assert role == "worker"
        commit(config)
        (Path(config["worktree"]) / "forgotten.py").write_text("pass\n")
        return result("ready_for_review")

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "blocked"
    assert runner.read(path)["commit_refusals"][0]["reason"] == "history_changed"


def test_review_mutation_detected(setup, monkeypatch):
    cfg, state, path, _ = setup

    def execute(config, state, path, job, role):
        if role == "worker":
            deliver(config)
            return result("ready_for_review")
        (Path(config["worktree"]) / "unexpected.py").write_text("pass\n")
        return result("ready_for_integration", role)

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "failed"


def test_auth_isolation_and_permissions(setup, monkeypatch):
    cfg, _, _, _ = setup
    for key in ["OPENAI_API_KEY", "CODEX_API_KEY", "GH_TOKEN", "OTHER_PROVIDER_KEY", "PYTHONPATH"]:
        monkeypatch.setenv(key, "do-not-inherit")
    monkeypatch.setenv("CODEX_HOME", "primary")
    env = runner.environment(cfg)
    assert env["CODEX_HOME"] == cfg["agent_home"]
    assert not any(key in env for key in ["OPENAI_API_KEY", "CODEX_API_KEY", "GH_TOKEN", "OTHER_PROVIDER_KEY", "PYTHONPATH"])
    worker = runner.command(cfg, "worker", Path("out"), Path("schema"), Path("prompt"))
    reviewer = runner.command(cfg, "reviewer", Path("out"), Path("schema"), Path("prompt"))
    assert "--approve-for-me" in worker and "--sandbox" not in worker
    assert "read-only" in reviewer and "never" in reviewer
    assert not any(flag in worker + reviewer for flag in ("resume", "fork"))
    assert not any("bypass" in flag for flag in worker + reviewer)


def fake_agent(state, source):
    path = state / "fake-agent"
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o700)
    return str(path)


EVENTS = '''
print(json.dumps(dict(type='thread.started',thread_id='fixture-managed')),flush=True)
print(json.dumps(dict(type='turn.started')),flush=True)
print(json.dumps(dict(type='turn.completed')),flush=True)
'''
OUTPUT = '''
output=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1])
output.write_text(json.dumps(dict(status='ready_for_review',summary='done',tests=[],limitations=[])))
'''


def test_real_subprocess_structured_protocol(setup):
    cfg, state, path, job = setup
    use_fake(cfg, state, "import json,pathlib,sys\nsys.stdin.read()\n" + OUTPUT + EVENTS)
    job["round"] = 1
    assert runner.execute(cfg, state, path, job, "worker")["status"] == "ready_for_review"
    assert "child_pid" not in job
    assert not (state / "worker_thread.json").exists()
    for name in (state / "runs/task/round_1_worker").iterdir():
        assert name.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancel_stop_owned_process(setup, cancel):
    cfg, state, path, job = setup
    use_fake(cfg, state, "import sys,time\nsys.stdin.read()\ntime.sleep(60)\n")
    cfg["phase_timeout_seconds"] = 0.05
    job["round"] = 1
    if cancel:
        (state / "CANCEL").touch()
    with pytest.raises((runner.Cancelled, TimeoutError), match="Cancelled|time limit"):
        runner.execute(cfg, state, path, job, "worker")
    assert "child_pid" not in job


def test_missing_final_answer_is_failure(setup):
    cfg, state, path, job = setup
    use_fake(cfg, state, 'import sys\nsys.stdin.read()\nprint(\'{"type":"turn.started"}\')\n')
    job["round"] = 1
    with pytest.raises(FileNotFoundError):
        runner.execute(cfg, state, path, job, "worker")


def test_invalid_review_cannot_approve(setup, monkeypatch):
    cfg, _, path, _ = setup
    bad = result("ready_for_integration", "reviewer")
    bad["findings"] = [dict(severity="P1", path="x", detail="broken")]
    with pytest.raises(ValueError, match="unresolved"):
        runner.validate_result(bad, "reviewer")

    def execute(config, state, path, job, role):
        if role == "worker":
            deliver(config)
            return result("ready_for_review")
        return bad

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "failed"


def test_optional_scope_is_explicit_and_worker_only(setup):
    cfg, state, _, job = setup
    job["head_commit"] = job["base_commit"]
    assert "Explicit worker-only scope" not in runner.prompt_for(cfg, job, "worker")
    path = runner.enqueue(cfg, "extra", "bounded task", scopes=["extended-check"])
    extra = runner.read(path)
    extra["head_commit"] = extra["base_commit"]
    assert "explicitly authorized extra check" in runner.prompt_for(cfg, extra, "worker")
    assert "explicitly authorized extra check" not in runner.prompt_for(cfg, extra, "reviewer")
    with pytest.raises(ValueError):
        runner.enqueue(cfg, "invalid-scope", "task", scopes=["unknown"])


def test_dependent_task_waits_for_reviewed_predecessor(setup):
    cfg, state, _, _ = setup
    path = runner.enqueue(cfg, "second", "next task", after_task="task")
    job = runner.read(state / "tasks/task.json")
    job["status"] = "needs_primary_review"
    runner.save_job(cfg, state / "tasks/task.json", job)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "queued"
    assert runner.read(path)["waiting_for_task"] == "task"


def predecessor(cfg, path, job, head):
    job.update(status="ready_for_integration", head_commit=head, reviewed_head=head,
               review=result("ready_for_integration", "reviewer"))
    runner.save_job(cfg, path, job)


def test_dependency_uses_only_exact_reviewed_clean_head(setup, monkeypatch):
    cfg, state, first, job = setup
    second = runner.enqueue(cfg, "next", "next task", after_task="task")
    original = runner.read(second)["assignment"]["base_commit"]
    head = commit(cfg)
    predecessor(cfg, first, job, head)
    monkeypatch.setattr(runner, "execute", execute_delivery)
    runner.run_once(cfg)
    final = runner.read(second)
    assert final["status"] == "ready_for_integration"
    assert final["assignment"]["base_commit"] == original
    assert final["dependency_reviewed_head"] == head


@pytest.mark.parametrize("dirty", [False, True])
def test_dependency_rejects_unrelated_worktree_changes(setup, dirty):
    cfg, state, first, job = setup
    second = runner.enqueue(cfg, "next", "next task", after_task="task")
    predecessor(cfg, first, job, job["base_commit"])
    if dirty:
        (Path(cfg["worktree"]) / "unrelated.txt").write_text("uncommitted")
    else:
        commit(cfg)
    runner.run_once(cfg)
    assert runner.read(second)["status"] == "blocked"


# New v0.3 guarantees and additional adversarial cases.

def test_immutable_assignment_profile_and_unique_ids(setup):
    cfg, state, path, job = setup
    cfg["project_profile"]["scope"] = "edited later"
    assert runner.read(path)["assignment"]["profile"]["scope"] != "edited later"
    with pytest.raises(ValueError, match="Task ID"):
        runner.enqueue(cfg, "task", "replacement")
    job["assignment"]["task_text"] = "tampered"
    runner.save_job(cfg, path, job)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "blocked"


def test_base_binding_tampering_blocks(setup):
    cfg, _, path, job = setup
    job["base_commit"] = commit(cfg)
    runner.save_job(cfg, path, job)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "blocked"


@pytest.mark.parametrize("version", ["codex-cli 99.0.0", "unknown", "codex-cli 0.164.0", "codex-cli 0.164.0-alpha.11.2"])
def test_unsupported_cli_blocks_before_any_model_call(setup, monkeypatch, version):
    cfg, state, path, _ = setup
    use_fake(cfg, state, f"print({version!r})\n")
    monkeypatch.setattr(runner, "check_cli", REAL_CHECK_CLI)
    monkeypatch.setattr(runner, "execute", lambda *args: pytest.fail("unsupported version"))
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "blocked"
    assert "CLI" in runner.read(path)["error"]


@pytest.mark.parametrize("verdict,status", [("ready_for_integration", "ready_for_integration"),
                                            ("needs_changes", "needs_primary_review"), ("blocked", "blocked")])
def test_review_only_exact_range_and_no_worker(setup, monkeypatch, verdict, status):
    cfg, state, first, job = setup
    job["status"] = "blocked"
    runner.save_job(cfg, first, job)
    base, head = job["base_commit"], commit(cfg)
    path = runner.enqueue(cfg, "review", "review primary delivery", kind="review", base=base, head=head)
    calls = []

    def execute(config, state, path, current, role):
        assert role == "reviewer"
        assert current["base_commit"] == base and current["head_commit"] == head
        calls.append(role)
        return result(verdict, role)

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    assert calls == ["reviewer"] and runner.read(path)["status"] == status


def test_review_only_uses_same_schema_and_fingerprint_checks(setup, monkeypatch):
    cfg, state, first, job = setup
    job["status"] = "blocked"
    runner.save_job(cfg, first, job)
    base, head = job["base_commit"], commit(cfg)
    path = runner.enqueue(cfg, "review", "review", kind="review", base=base, head=head)

    def execute(config, state, path, current, role):
        (Path(config["worktree"]) / "coordination.md").write_text("not read-only")
        return result("ready_for_integration", role)

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "failed"


def test_receipts_always_local_opt_in_public_allowlist_and_fake_client(setup, monkeypatch):
    cfg, state, path, job = setup
    assert runner.read(state / "receipts/task.json")["status"] == "queued"
    cfg["github_receipts"] = dict(enabled=False, repository="example/project")
    job["github_number"] = 7
    runner.save_job(cfg, path, job)
    calls = []

    class Fake:
        def post(self, repository, number, receipt):
            calls.append((repository, number, receipt))

    monkeypatch.setattr(runner, "execute", execute_delivery)
    runner.run_once(cfg, client=Fake())
    assert calls == []
    job = runner.read(path)
    job["worker"]["summary"] = "SENSITIVE-TASK-OR-ENVIRONMENT"
    cfg["github_receipts"]["enabled"] = True
    runner.publish_receipt(cfg, path, job, Fake())
    runner.publish_receipt(cfg, path, job, Fake())
    assert len(calls) == 1 and calls[0][:2] == ("example/project", 7)
    receipt = calls[0][2]
    assert set(receipt) == {"status", "base_commit", "head_commit", "summary", "timestamp", "runner_commits"}
    assert "SENSITIVE" not in json.dumps(receipt)
    assert runner.public_receipt(runner.read(state / "receipts/task.json")) == receipt


def test_public_receipt_failure_preserves_local_success_without_retry(setup, monkeypatch):
    cfg, state, path, job = setup
    cfg["github_receipts"] = dict(enabled=True, repository="example/project")
    job["github_number"] = 7
    runner.save_job(cfg, path, job)

    class Failed:
        def post(self, *args):
            raise OSError("PRIVATE-TRANSPORT-DETAIL")

    monkeypatch.setattr(runner, "execute", execute_delivery)
    runner.run_once(cfg, client=Failed())
    assert runner.read(path)["status"] == "ready_for_integration"
    assert runner.read(path)["receipt_delivery"].startswith("unconfirmed")
    assert "PRIVATE" not in (state / "receipts/task.json").read_text()


def test_worker_without_changes_cannot_pass(setup, monkeypatch):
    cfg, _, path, _ = setup
    monkeypatch.setattr(runner, "execute", lambda *args: result("ready_for_review"))
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "blocked"


def test_allowed_coordination_files_do_not_allow_dirty_code(setup, monkeypatch):
    cfg, _, path, _ = setup

    def execute(config, state, path, current, role):
        if role == "worker":
            deliver(config)
            (Path(config["worktree"]) / "coordination.md").write_text("status only")
        return result("ready_for_review" if role == "worker" else "ready_for_integration", role)

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "ready_for_integration"


def test_private_atomic_files_and_runner_lock_modes(setup):
    cfg, state, path, job = setup
    assert state.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    with runner.lock(state / "runner.lock"):
        assert (state / "runner.lock").stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob(".write-*"))


def test_timeout_stops_owned_descendant_group(setup, monkeypatch):
    cfg, state, path, job = setup
    heartbeat = state / "heartbeat"
    child = ("import pathlib,time\n" + f"p=pathlib.Path({str(heartbeat)!r})\n"
             "while True:\n p.write_text(str(time.monotonic_ns()))\n time.sleep(0.02)\n")
    source = ("import subprocess,sys,time\nsys.stdin.read()\n"
              + f"subprocess.Popen([sys.executable,'-c',{child!r}])\n"
              + "time.sleep(60)\n")
    use_fake(cfg, state, source)
    cfg["phase_timeout_seconds"] = 0.5
    # Start the simulated deadline only after the descendant is demonstrably
    # running. Do not assume nested process startup fits a wall-clock fraction.
    real_monotonic = time.monotonic
    watchdog_start = real_monotonic()
    first_clock_read = True

    def readiness_clock():
        nonlocal first_clock_read
        if first_clock_read:
            first_clock_read = False
            return 0
        if heartbeat.exists():
            return 1
        if real_monotonic() - watchdog_start > 10:
            raise RuntimeError("Synthetic descendant failed to become ready")
        return 0

    monkeypatch.setattr(runner, "time", SimpleNamespace(
        monotonic=readiness_clock, sleep=time.sleep, time=time.time))
    job["round"] = 1
    with pytest.raises(TimeoutError):
        runner.execute(cfg, state, path, job, "worker")
    last = heartbeat.read_text()
    time.sleep(0.15)
    assert heartbeat.read_text() == last
    with pytest.raises(ProcessLookupError):
        os.kill(runner.read(path)["child_pid"], 0)


def populated_config(setup, tmp_path):
    cfg, _, _, _ = setup
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(cfg["project_profile"]))
    settings = {key: value for key, value in cfg.items() if key != "project_profile"}
    settings.update(profile=str(profile), phase_timeout_seconds=60)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(settings))
    return path, settings


def test_cli_enqueue_status_and_interruption_reconciliation(setup, tmp_path, capsys):
    cfg, state, path, job = setup
    config_path, _ = populated_config(setup, tmp_path)
    task = tmp_path / "approved.txt"
    task.write_text("PRIVATE-TASK-TEXT")
    assert runner.main(["--config", str(config_path), "enqueue", "--id", "from-cli", "--task", str(task)]) == 0
    assert runner.main(["--config", str(config_path), "status"]) == 0
    assert "PRIVATE-TASK-TEXT" not in capsys.readouterr().out
    job["status"] = "running_reviewer"
    runner.save_job(cfg, path, job)
    runner.run_once(cfg)
    assert runner.main(["--config", str(config_path), "acknowledge-interruption", "--id", "task"]) == 0
    assert runner.read(path)["reconciled"] is True
    assert runner.read(path)["status"] == "interrupted"
    assert runner.main(["--config", str(config_path), "acknowledge-interruption", "--id", "../escape"]) == 2


@pytest.mark.parametrize("override", [dict(branch="main"), dict(max_rounds=3), dict(max_rounds=True),
    dict(phase_timeout_seconds=0), dict(worktree="relative"), dict(worker_model="<MODEL>"),
    dict(github_receipts={"enabled": "false"}), dict(github_receipts={"enabled": True, "repository": "../x"})])
def test_bad_config_is_rejected(setup, tmp_path, override):
    path, settings = populated_config(setup, tmp_path)
    settings.update(override)
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError):
        runner.load_config(path)


def test_state_and_agent_home_must_be_outside_worktree(setup, tmp_path):
    cfg, _, _, _ = setup
    path, settings = populated_config(setup, tmp_path)
    for key in ("state_dir", "agent_home"):
        altered = {**settings, key: str(Path(cfg["worktree"]) / "private")}
        path.write_text(json.dumps(altered))
        with pytest.raises(ValueError):
            runner.load_config(path)


def test_profile_inputs_and_coordination_paths_are_validated(tmp_path):
    base = dict(worker_prompt="work", reviewer_prompt="review", scope="scope", extra_scopes={}, coordination_files=[])
    path = tmp_path / "profile.json"
    for override in [dict(worker_prompt=""), dict(extra_scopes={"../escape": "x"}),
                     dict(coordination_files=["../escape"]), dict(coordination_files=["/absolute"]),
                     dict(coordination_files="*.md")]:
        path.write_text(json.dumps({**base, **override}))
        with pytest.raises(ValueError):
            runner.load_profile(path)


def test_setup_assets_are_placeholders_and_plist_parses():
    import plistlib
    config = runner.read(ROOT / "runner/config.example.json")
    for key in ("worktree", "state_dir", "agent_home", "profile", "branch", "worker_model", "reviewer_model"):
        assert config[key].startswith("<") and config[key].endswith(">")
    assert config["github_receipts"]["enabled"] is False
    profile = runner.read(ROOT / "runner/profile.example.json")
    assert profile["coordination_files"] == []
    assert all(profile[key].startswith("<") for key in ["worker_prompt", "reviewer_prompt", "scope"])
    plist = plistlib.loads((ROOT / "runner/launchd.plist.example").read_bytes())
    assert plist["Label"] == "__SERVICE_LABEL__" and plist["RunAtLoad"] is False
    assert plist["Umask"] == 0o077
    assert all(arg.startswith("__") for arg in plist["ProgramArguments"][:2])


@pytest.mark.parametrize("bad", [[], {}, {"status": "ready_for_integration"},
    dict(status="ready_for_integration", summary="x", tests=[], limitations=[], findings=[{"severity": 1, "path": "x", "detail": "x"}]),
    dict(status="ready_for_integration", summary="x", tests="pass", limitations=[], findings=[])])
def test_review_only_rejects_invalid_result_schema(setup, monkeypatch, bad):
    cfg, _, first, job = setup
    job["status"] = "blocked"
    runner.save_job(cfg, first, job)
    base, head = job["base_commit"], commit(cfg)
    path = runner.enqueue(cfg, "review", "review", kind="review", base=base, head=head)
    monkeypatch.setattr(runner, "execute", lambda *args: bad)
    runner.run_once(cfg)
    assert runner.read(path)["status"] == "failed"


def test_git_receipt_client_posts_only_allowlisted_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kw: calls.append((args, kw)))
    receipt = dict(status="blocked", base_commit="a" * 40, head_commit=None,
                   summary=runner.SUMMARIES["blocked"], timestamp="2026-01-01T10:00:00Z")
    runner.GitHubClient().post("example/project", 7, receipt)
    args, kwargs = calls[0]
    assert args[0] == ["gh", "api", "--method", "POST", "repos/example/project/issues/7/comments", "--input", "-"]
    body = json.loads(kwargs["input"])["body"]
    assert "Commits:" in body and "Run:" in body
    assert "shell" not in kwargs


def test_runner_does_not_use_stale_phase_result(setup):
    cfg, state, path, job = setup
    job["round"] = 1
    phase = state / "runs/task/round_1_worker"
    phase.mkdir(parents=True)
    runner.save(phase / "result.json", result("ready_for_review"))
    with pytest.raises(RuntimeError, match="never reuse"):
        runner.execute(cfg, state, path, job, "worker")


def test_fingerprint_includes_ignored_and_index_changes(setup):
    cfg, _, _, _ = setup
    root = Path(cfg["worktree"])
    (root / ".gitignore").write_text("ignored.txt\n")
    runner.git(cfg, "add", ".gitignore")
    commit(cfg)
    before = runner.fingerprint(cfg)
    (root / "ignored.txt").write_text("new ignored data")
    assert runner.fingerprint(cfg) != before
    before = runner.fingerprint(cfg)
    (root / ".gitignore").write_text("ignored.txt\nother\n")
    runner.git(cfg, "add", ".gitignore")
    (root / ".gitignore").write_text("ignored.txt\n")
    assert runner.fingerprint(cfg) != before


def test_end_to_end_fake_worker_and_fresh_reviewer(setup, monkeypatch):
    cfg, state, path, _ = setup
    source = '''import json,os,pathlib,subprocess,sys
if '--version' in sys.argv:
 print('codex-cli VERSION'); sys.exit(0)
prompt=sys.stdin.read()
review='INDEPENDENT READ-ONLY REVIEW' in prompt
if review:
 assert 'read-only' in sys.argv and 'never' in sys.argv
 status='ready_for_integration'
else:
 path=pathlib.Path('delivery.txt'); path.write_text((path.read_text() if path.exists() else '')+'delivery\\n')
 status='ready_for_review'
value=dict(status=status,summary='synthetic',tests=[],limitations=[])
if review: value['findings']=[]
output=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1])
output.write_text(json.dumps(value))
identity='fixture-review' if review else 'fixture-managed'
root=pathlib.Path(os.environ['CODEX_HOME'])/'sessions'; root.mkdir(parents=True,exist_ok=True)
rows=[dict(type='session_meta',payload=dict(id=identity,cwd=os.getcwd(),cli_version='VERSION')),dict(type='event_msg',payload=dict(type='task_complete'))]
(root/(identity+'.jsonl')).write_text(''.join(json.dumps(dict(timestamp='2026-01-01T10:00:00Z',**r))+'\\n' for r in rows))
print(json.dumps(dict(type='thread.started',thread_id=identity)))
print(json.dumps(dict(type='turn.started')))
print(json.dumps(dict(type='turn.completed')))
'''.replace('VERSION', CLI_VERSION)
    use_fake(cfg, state, source)
    monkeypatch.setattr(runner, "check_cli", REAL_CHECK_CLI)
    runner.run_once(cfg)
    final = runner.read(path)
    assert final["status"] == "ready_for_integration"
    assert final["reviewed_head"] == runner.git(cfg, "rev-parse", "HEAD")
    assert final["cli_version"] == 'codex-cli ' + CLI_VERSION
    assert runner.read(state / "receipts/task.json")["status"] == "ready_for_integration"
    # Another assignment also starts fresh, without consulting stored sessions.
    next_path = runner.enqueue(cfg, "next", "next bounded task")
    runner.run_once(cfg)
    assert runner.read(next_path)["status"] == "ready_for_integration"


def test_fingerprint_frames_names_and_file_types(setup):
    cfg, _, _, _ = setup
    root = Path(cfg["worktree"])
    (root / "a").write_text("bc")
    before = runner.fingerprint(cfg)
    (root / "a").unlink()
    (root / "ab").write_text("c")
    assert runner.fingerprint(cfg) != before
    (root / "ab").unlink()
    (root / "a").write_text("target")
    before = runner.fingerprint(cfg)
    (root / "a").unlink()
    (root / "a").symlink_to("target")
    assert runner.fingerprint(cfg) != before


@pytest.mark.parametrize("probe", [2, 3, 4])
@pytest.mark.parametrize("change", ["head", "branch"])
def test_phase_probes_cannot_change_bound_head_or_branch(setup, monkeypatch, probe, change):
    cfg, _, path, _ = setup
    probes, calls = [], []

    def check(config):
        probes.append(True)
        if len(probes) == probe:
            if change == "head":
                commit(config)
            else:
                runner.git(config, "checkout", "-qb", "unexpected")
        return CLI_VERSION

    def execute(config, state, path, job, role):
        calls.append(role)
        if role == "worker":
            deliver(config)
            return result("ready_for_review")
        return result("needs_changes", role)

    monkeypatch.setattr(runner, "check_cli", check)
    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    final = runner.read(path)
    assert final["status"] == "blocked"
    assert calls == ([] if probe == 2 else ["worker"] if probe == 3 else ["worker", "reviewer"])



@pytest.mark.parametrize('output', ['', 'unstructured progress\n', '{"type":"future.event"}\n',
                                  '{"type":"turn.failed"}\n{partial', '\\xff'])
def test_extra_stdout_is_opaque_and_never_required(setup, output):
    cfg, state, path, job = setup
    use_fake(cfg, state, 'import json,pathlib,sys\nsys.stdin.read()\n' + OUTPUT
             + f'print({output!r})\n')
    job['round'] = 1
    assert runner.execute(cfg, state, path, job, 'worker')['status'] == 'ready_for_review'


@pytest.mark.parametrize('failure', ['nonzero', 'missing', 'malformed', 'incomplete', 'contradictory', 'duplicate', 'nonjson'])
def test_required_contract_signals_cannot_approve(setup, failure):
    cfg, state, path, _ = setup
    source = 'import json,pathlib,sys\nsys.stdin.read()\n' + OUTPUT
    source += "pathlib.Path('delivery.txt').write_text('delivery')\n"
    if failure == 'nonzero':
        source += 'sys.exit(2)\n'
    elif failure == 'missing':
        source += 'output.unlink()\n'
    elif failure == 'malformed':
        source += "output.write_text('{partial')\n"
    elif failure == 'incomplete':
        source += "output.write_text('{}')\n"
    elif failure == 'duplicate':
        duplicate = '{"status":"blocked",' + json.dumps(result('ready_for_review'))[1:]
        source += f'output.write_text({duplicate!r})\n'
    elif failure == 'nonjson':
        source += "output.write_text(output.read_text().replace('[]', '[NaN]', 1))\n"
    else:
        source += "output.write_text(json.dumps(dict(status='ready_for_integration',summary='x',tests=[],limitations=[])))\n"
    use_fake(cfg, state, source)
    runner.run_once(cfg)
    final = runner.read(path)
    assert final['status'] == 'failed'
    assert not final.get('runner_commits') and 'review' not in final
    if failure == 'duplicate':
        answer = state / 'runs/task/round_1_worker/result.json'
        assert json.loads(answer.read_text())['status'] == 'ready_for_review'
        with pytest.raises(ValueError, match='Duplicate'):
            runner.read_answer(answer)


@pytest.mark.parametrize('mode', ['resume', 'fork'])
def test_nonfresh_configs_block_before_probe_or_model(setup, monkeypatch, tmp_path, mode):
    cfg, _, path, _ = setup
    config_path, settings = populated_config(setup, tmp_path)
    settings['worker_start'] = mode
    config_path.write_text(json.dumps(settings))
    with pytest.raises(runner.Blocked, match='Fresh sessions only'):
        runner.load_config(config_path)
    cfg['worker_start'] = mode
    monkeypatch.setattr(runner, 'check_cli', lambda _: pytest.fail('must not probe'))
    monkeypatch.setattr(runner, 'execute', lambda *a: pytest.fail('must not dispatch'))
    runner.run_once(cfg)
    assert runner.read(path)['status'] == 'blocked'


@pytest.mark.parametrize('override', [
    {'command': 'shell command'}, {'command': []}, {'version_command': []},
    {'command': ['/agent', '{unknown}']}, {'command': ['/agent', '{model!r}']},
    {'command': ['/agent', '{model.__class__}']}, {'command': ['relative']},
    {'command': ['/agent', '--flags={role_flags}']},
    {'version_command': ['/agent', '{output_file}']}, {'version_pattern': ''},
    {'version_pattern': '['}, {'environment': {'PYTHONPATH': '/untrusted'}},
    {'environment': {'AGENT_HOME': '{output_file}'}}, {'worker_flags': ['<UNFILLED>']},
])
def test_invalid_agent_definitions_fail_before_dispatch(setup, tmp_path, override):
    config_path, settings = populated_config(setup, tmp_path)
    settings['agent'].update(override)
    config_path.write_text(json.dumps(settings))
    with pytest.raises((ValueError, runner.re.error)):
        runner.load_config(config_path)


def test_templates_preserve_argument_boundaries_and_prompt_file(setup):
    cfg, _, _, _ = setup
    cfg['agent']['command'] += ['{prompt_file}']
    cfg['worker_model'] = 'model with spaces; $(untrusted)'
    command = runner.command(cfg, 'worker', Path('/output with spaces'), Path('/schema'), Path('/prompt file'))
    assert cfg['worker_model'] in command
    assert '/output with spaces' in command and command[-1] == '/prompt file'
    assert '--ask-for-approval' not in command


def test_generic_agent_fresh_corrections_and_no_session_reads(setup, monkeypatch):
    cfg, state, path, _ = setup
    # No Codex flags, event stream or protocol. Prompt-file input, arbitrary
    # version string, JSON-file output, role flags and optional environment only.
    source = '''import json,pathlib,sys
if '--build' in sys.argv:
 print('Example Agent build 7'); sys.exit(0)
assert len(sys.argv)==5
prompt=pathlib.Path(sys.argv[1]).read_text()
review=sys.argv[4]=='inspect'
assert ('INDEPENDENT READ-ONLY REVIEW' in prompt)==review
assert 'resume' not in sys.argv and 'fork' not in sys.argv
marker=pathlib.Path(sys.argv[3])
count=int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(count+1))
if review:
 status='needs_changes' if count==1 else 'ready_for_integration'
else:
 delivery=pathlib.Path('delivery.txt')
 delivery.write_text((delivery.read_text() if delivery.exists() else '')+'change\\n')
 status='ready_for_review'
value=dict(status=status,summary='synthetic',tests=[],limitations=[])
if review: value['findings']=[]
pathlib.Path(sys.argv[2]).write_text(json.dumps(value))
print('anything, including unknown records and malformed JSON: {')
'''
    binary = fake_agent(state, source)
    cfg['agent'] = dict(command=[binary, '{prompt_file}', '{output_file}', str(state / 'starts'), '{role_flags}'],
                        version_command=[binary, '--build'], version_pattern='Example Agent build 7',
                        worker_flags=['implement'], reviewer_flags=['inspect'])
    # Even stale/malformed legacy identities and arbitrary agent storage are
    # irrelevant. Fail if the coordinator attempts to open or enumerate them.
    home = runner.private_dir(cfg['agent_home'])
    (home / 'sessions').mkdir()
    (home / 'sessions/broken.jsonl').write_text('{unknown')
    (state / 'worker_thread.json').write_text('{stale')
    real_open, real_iterdir, real_glob, real_rglob = Path.open, Path.iterdir, Path.glob, Path.rglob

    def forbidden(p):
        return home == p or home in p.parents or p == state / 'worker_thread.json'

    def guarded_open(p, *a, **kw):
        assert not forbidden(p), 'runner must never read agent storage'
        return real_open(p, *a, **kw)

    def guarded_iterdir(p):
        assert not forbidden(p), 'runner must never enumerate agent storage'
        return real_iterdir(p)

    def guarded_glob(p, pattern, **kw):
        assert not forbidden(p), 'runner must never glob agent storage'
        return real_glob(p, pattern, **kw)

    def guarded_rglob(p, pattern, **kw):
        assert not forbidden(p), 'runner must never recurse into agent storage'
        return real_rglob(p, pattern, **kw)

    monkeypatch.setattr(Path, 'rglob', guarded_rglob)
    monkeypatch.setattr(Path, 'open', guarded_open)
    monkeypatch.setattr(Path, 'iterdir', guarded_iterdir)
    monkeypatch.setattr(Path, 'glob', guarded_glob)
    monkeypatch.setattr(runner, 'check_cli', REAL_CHECK_CLI)
    runner.run_once(cfg)
    final = runner.read(path)
    assert final['status'] == 'ready_for_integration'
    assert final['cli_version'] == 'Example Agent build 7'
    assert final['protocol'] == 'agent-file-v1'
    assert (state / 'starts').read_text() == '4'
    assert len(final['runner_commits']) == 2
    assert 'worker_thread_id' not in final
    assert not any(key.startswith('CODEX') for key in runner.environment(cfg))


@pytest.mark.parametrize('key', ['command', 'version_command'])
@pytest.mark.parametrize('mode', ['missing', 'nonexecutable'])
def test_agent_programs_preflight_before_version_probe(setup, monkeypatch, tmp_path, key, mode):
    cfg, _, _, _ = setup
    cfg['agent'] = agent_config(sys.executable)
    tool = tmp_path / 'unavailable tool'
    if mode == 'nonexecutable':
        tool.write_text('not executable')
        tool.chmod(0o600)
    cfg['agent'][key][0] = str(tool)
    monkeypatch.setattr(runner.subprocess, 'run', lambda *a, **kw: pytest.fail('no version probe'))
    with pytest.raises(runner.Blocked, match=key + ' program missing or not executable'):
        REAL_CHECK_CLI(cfg)


@pytest.mark.parametrize("role", ["worker", "reviewer"])
@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize("pool", [False, True])
def test_every_output_schema_object_obeys_strict_mode(role, lifecycle, pool):
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                assert set(node["required"]) == set(node["properties"])
                assert node.get("additionalProperties") is False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    spec = runner.schema(role, lifecycle=lifecycle, pool=pool)
    walk(spec)
    if role == "reviewer":
        assert spec["properties"]["findings"]["items"]["properties"]["criterion"] == {"type": "string"}


def test_reviewer_note_explains_empty_criterion():
    assert ("Set criterion to the AC ID the finding relates to, or an empty string if none."
            in runner.LIFECYCLE_NOTES["reviewer"])
