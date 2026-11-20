"""Readiness uses synthetic agents and APIs; the replay scope is approval-pinned."""

import hashlib
import json
from pathlib import Path

import pytest

from lifecycle import core, service
from test_lifecycle import policy
from test_lifecycle_continuation import newest_claim, saved_story
from test_lifecycle_delivery import story, settings
from test_one_continuation import reapprove
import test_runner as fixtures
from test_runner_pool import setup as pool_setup  # noqa: F401

runner = fixtures.runner
setup = fixtures.setup
QUESTION = "Should the workflow policy change be delivered by the owner?"
SCOPE = "3551a80d682fc7e05266293644b42338ab9107238800d50816fc93068ef37bf7"


def enable(cfg):
    for field in ("runner_config", "primary_runner_config"):
        value = core.read(cfg[field])
        value["readiness"] = True
        core.save(cfg[field], value)


def readiness_agent(monkeypatch, tool, answer, *, mutate=False):
    execute = tool.execute
    calls = []

    def phase(config, state, path, job, role):
        calls.append(role)
        if role != "readiness":
            return execute(config, state, path, job, role)
        assert job["round"] == 1
        job["status"] = "running_readiness"
        tool.save_job(config, path, job)
        if mutate:
            Path(config["worktree"], "unexpected.txt").write_text("changed")
        return answer

    monkeypatch.setattr(tool, "execute", phase)
    return calls


def test_first_approval_replay_stops_before_build_and_quotes_question(tmp_path, monkeypatch):
    cfg, hub, tool, _, _ = story(tmp_path, monkeypatch)
    fixture = Path(__file__).parent / "fixtures/issue113-first-approval.md"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == SCOPE
    hub.issues[1]["body"] = fixture.read_text()
    reapprove(hub)
    enable(cfg)
    calls = readiness_agent(monkeypatch, tool, {"status": "questions", "questions": [QUESTION]})
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert calls == ["readiness"]
    assert job["status"] == "needs_owner" and job["round"] == 1
    assert job["readiness_sessions"] == 1
    assert job["assignment"]["task_sha256"] == SCOPE
    assert not job.get("head_commit") and not job.get("continuation")
    assert not job.get("runner_commits")
    record, = [r for r in core.records(cfg, hub.comments_by_issue[1], 1)
               if r["reason"] == "readiness_question"]
    assert record["round"] == 1 and record["owner_question"] == QUESTION
    notice = next(c["body"] for c in hub.comments_by_issue[1] if record["key"] in c["body"])
    assert "> " + QUESTION in notice
    assert "Readiness cost: one extra agent session for this approval." in notice
    assert newest_claim(cfg)["readiness_stopped"]


def test_ready_runs_once_before_build_and_preserves_correction_budget(tmp_path, monkeypatch):
    cfg, hub, tool, _ = saved_story(tmp_path, monkeypatch)
    enable(cfg)

    def repair(config, state, path, job, role):
        if role == "reviewer":
            return fixtures.result("needs_changes", "reviewer")
        return fixtures.execute_delivery(config, state, path, job, role)

    monkeypatch.setattr(tool, "execute", repair)
    calls = readiness_agent(monkeypatch, tool, {"status": "ready", "questions": []})
    reapprove(hub)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert calls == ["readiness", "worker", "reviewer", "worker", "reviewer"]
    job = next(j for j in service.jobs(cfg) if j["id"] == newest_claim(cfg)["job"])
    assert job["round"] == 2 and job["status"] == "needs_owner"


@pytest.mark.parametrize("answer", [
    None, {}, {"status": "ready", "questions": [QUESTION]},
    {"status": "questions", "questions": []},
    {"status": "questions", "questions": [""]},
    {"status": "questions", "questions": [1]},
    {"status": "questions", "questions": "question"},
    {"status": "blocked", "questions": [QUESTION]},
    {"status": "ready", "questions": [], "extra": True},
])
def test_strict_readiness_schema_rejects_malformed(answer):
    with pytest.raises(ValueError):
        runner.validate_result(answer, "readiness")


@pytest.mark.parametrize("answer", [{"status": "ready", "questions": []},
                                    {"status": "questions", "questions": [QUESTION]}])
def test_readiness_schema_accepts_exact_results(answer):
    spec = runner.schema("readiness")
    assert set(spec["properties"]) == {"status", "questions"}
    assert set(spec["required"]) == {"status", "questions"}
    assert spec["additionalProperties"] is False
    runner.validate_result(answer, "readiness")


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_switch_requires_boolean(setup, tmp_path, value):
    path, config = fixtures.populated_config(setup, tmp_path)
    config["readiness"] = value
    runner.save(path, config)
    with pytest.raises(ValueError, match="readiness"):
        runner.load_config(path)


def test_prompt_and_command_use_read_only_reviewer_without_new_profile_keys(setup):
    cfg, _, _, job = setup
    job["round"] = 1
    job["assignment"]["profile"]["commit_policy"] = {"deny_patterns": ["private-*.txt"]}
    prompt = runner.prompt_for(cfg, job, "readiness")
    for phrase in ["AGENTS.md", "documents", "contradict", "read-only reviewer",
                   "owner-delivered", "Restart: fresh", "Continue from", "2,000",
                   "private-*.txt", "credential", "slashes"]:
        assert phrase in prompt
    for pattern in runner.DEFAULT_COMMIT_POLICY["deny_patterns"]:
        assert pattern in prompt
    args = runner.command(cfg, "readiness", "output", "schema", "prompt")
    assert cfg["reviewer_model"] in args and cfg["worker_model"] not in args
    assert "read-only" in args


@pytest.mark.parametrize("fault", ["mutation", "malformed", "null", "list"])
def test_bad_session_stops_without_build_or_saved_work(tmp_path, monkeypatch, fault):
    cfg, hub, tool, _, _ = story(tmp_path, monkeypatch)
    enable(cfg)
    answer = {"mutation": {"status": "ready", "questions": []}, "malformed": {},
              "null": None, "list": []}[fault]
    calls = readiness_agent(monkeypatch, tool, answer, mutate=fault == "mutation")
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert calls == ["readiness"]
    assert job["status"] == "failed" and job["round"] == 1
    assert not job.get("head_commit") and not job.get("runner_commits")
    assert core.job_records(cfg, job)[0]["reason"] == "execution_failed"


@pytest.mark.parametrize("question", ["Change runner file?", "credential needed?", "a/b?", "x" * 2001,
                                    ["First decision?", "Second decision?"],
                                    ["Which change? " * 100, "Which change? " * 100]],
                         ids=["safe", "withheld-word", "path", "over-limit", "multiple", "combined-limit"])
def test_readiness_uses_unchanged_public_question_filter(tmp_path, monkeypatch, question):
    cfg, hub, tool, _, _ = story(tmp_path, monkeypatch)
    enable(cfg)
    questions = question if isinstance(question, list) else [question]
    supplied = "\n".join(questions)
    readiness_agent(monkeypatch, tool, {"status": "questions", "questions": questions})
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    record = core.job_records(cfg, job)[0]
    safe = core.safe_question(cfg, supplied)
    assert record["reason"] == ("readiness_question" if safe else "question_withheld")
    assert record.get("owner_question") == (supplied if safe else None)
    assert "Readiness cost: one extra agent session for this approval." in core.record_body(record)


@pytest.mark.parametrize("lost_claim_marker", [False, True])
def test_stops_do_not_spend_continuations_or_replace_saved_source(tmp_path, monkeypatch, lost_claim_marker):
    cfg, hub, tool, old = saved_story(tmp_path, monkeypatch)
    enable(cfg)
    calls = readiness_agent(monkeypatch, tool, {"status": "questions", "questions": [QUESTION]})
    for clock in (200, 300, 400):
        reapprove(hub)
        service.poll(cfg, hub.client(), clock=lambda: clock)
        claim = newest_claim(cfg)
        assert claim.get("error") is None
        assert claim["continuation_source"] == old["id"]
        assert claim["continuation_number"] == 1
        assert claim["readiness_stopped"]
        if lost_claim_marker:
            # Crash after the runner saved the stop, before the service saved its claim.
            claim.pop("readiness_stopped")
            claim["stage"] = "enqueued"
            core.save(Path(cfg["state_dir"]) / "claims" / (claim["job"] + ".json"), claim)
    assert calls == ["readiness"] * 3


def test_delivery_only_spends_no_readiness_session(tmp_path, monkeypatch):
    cfg, hub, tool, calls, publications = story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    before = list(calls)
    settings(cfg, "runner_config", {"enabled": True, "declared_tier": "standard"})
    enable(cfg)
    readiness_calls = readiness_agent(monkeypatch, tool, {"status": "ready", "questions": []})
    reapprove(hub)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert newest_claim(cfg)["delivery_source"]
    assert calls == before and readiness_calls == []
    assert [p[0] for p in publications] == ["push", "pr", "checker"]


@pytest.mark.parametrize("switch", [None, False])
def test_switch_off_preserves_original_phases(setup, monkeypatch, switch):
    cfg, _, path, _ = setup
    if switch is not None:
        cfg["readiness"] = switch
    calls = []

    def execute(config, state, path, job, role):
        calls.append(role)
        return fixtures.execute_delivery(config, state, path, job, role)

    monkeypatch.setattr(runner, "execute", execute)
    runner.run_once(cfg)
    assert calls == ["worker", "reviewer"]
    assert "readiness_sessions" not in runner.read(path)


def test_running_readiness_maps_to_in_progress(tmp_path):
    cfg = policy(tmp_path)
    job = dict(id="readiness", status="running_readiness", round=1,
               assignment={"lifecycle": {"issue": 1, "event": 101}})
    assert core.job_records(cfg, job)[0]["to"] == "in-progress"


def test_example_defaults_off():
    assert json.loads((fixtures.ROOT / "runner/config.example.json").read_text())["readiness"] is False


def test_real_fresh_session_writes_strict_result_with_readonly_flags(setup):
    cfg, state, path, job = setup
    job["round"] = 1
    fixtures.use_fake(cfg, state, '''import json, pathlib, sys
assert 'read-only' in sys.argv
assert 'fixture-reviewer' in sys.argv
output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])
output.write_text(json.dumps({'status': 'ready', 'questions': []}))
''')
    assert runner.execute(cfg, state, path, job, "readiness") == {"status": "ready", "questions": []}
    folder = state / "runs/task/round_1_readiness"
    assert (folder / "schema.json").exists()
    with pytest.raises(RuntimeError, match="never reuse"):
        runner.execute(cfg, state, path, job, "readiness")


def test_automatic_primary_does_not_run_readiness(tmp_path, monkeypatch):
    cfg, hub, tool, _, _ = story(tmp_path, monkeypatch)
    execute = tool.execute

    def repair(config, state, path, job, role):
        if role == "reviewer":
            job["status"] = "running_reviewer"
            tool.save_job(config, path, job)
            return fixtures.result("needs_changes", "reviewer")
        return execute(config, state, path, job, role)

    monkeypatch.setattr(tool, "execute", repair)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    enable(cfg)
    calls = readiness_agent(monkeypatch, tool, {"status": "ready", "questions": []})
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert newest_claim(cfg)["primary"]
    assert calls == ["worker", "reviewer"]
    job = next(j for j in service.jobs(cfg) if j["id"] == newest_claim(cfg)["job"])
    assert job["round"] == 1 and not job.get("readiness_sessions")


def test_ready_initial_approval_proceeds_to_build(tmp_path, monkeypatch):
    cfg, hub, tool, _, _ = story(tmp_path, monkeypatch)
    enable(cfg)
    calls = readiness_agent(monkeypatch, tool, {"status": "ready", "questions": []})
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert calls == ["readiness", "worker", "reviewer"]
    assert job["readiness_sessions"] == 1 and job["head_commit"]
    assert not job.get("readiness_stopped")


@pytest.mark.parametrize("reason", ["pr_open_reapproval", "continuation_limit", "continuation_commit_unrecorded"])
def test_pre_dispatch_stops_spend_no_readiness_session(tmp_path, monkeypatch, reason):
    cfg, hub, tool, old = saved_story(tmp_path, monkeypatch)
    enable(cfg)
    if reason == "continuation_limit":
        for n in (1, 2):
            core.save(Path(cfg["state_dir"]) / "claims" / f"past-{n}.json", dict(
                job=f"past-{n}", issue=1, event=n, stage="finished",
                continued_from=old["head_commit"], cleaned=True))
    elif reason == "continuation_commit_unrecorded":
        hub.comments_by_issue[1] = [c for c in hub.comments_by_issue[1]
                                   if '"continuation": true' not in c["body"]]
    else:
        old["integration"] = {"pr": 9}
        core.save(Path(cfg["state_dir"]) / "jobs" / old["id"] / "runner-state/tasks"
                  / (old["id"] + ".json"), old)
        request = type(hub.client()).request

        def open_pr(self, method, path, data=None):
            return {"state": "open"} if path == "/pulls/9" else request(self, method, path, data)

        monkeypatch.setattr(type(hub.client()), "request", open_pr)
    calls = readiness_agent(monkeypatch, tool, {"status": "ready", "questions": []})
    reapprove(hub)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert newest_claim(cfg)["error"] == reason
    assert calls == [] and len(service.jobs(cfg)) == 1


def test_pool_phases_spend_no_readiness_session(pool_setup, monkeypatch):  # noqa: F811
    from runner import pool
    import test_runner_pool as pool_fixtures

    cfg, path = pool_setup
    for story_config in cfg["stories"]:
        config = core.read(story_config["runner_config"])
        config["readiness"] = True
        core.save(story_config["runner_config"], config)
    calls = []

    def execute(config, state, path, job, role):
        calls.append(role)
        assert role in {"worker", "reviewer"}
        return pool_fixtures.delivery(config, state, path, job, role)

    monkeypatch.setattr(pool_fixtures.runner, "execute_phase", execute)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert sorted(calls) == ["reviewer", "reviewer", "worker", "worker"]
    assert all(j["status"] == "ready_for_integration" for j in engine.data["stories"].values())


def test_readiness_cost_survives_later_worker_stop(tmp_path):
    cfg = policy(tmp_path)
    job = dict(id="cost", status="needs_owner", round=1, readiness_sessions=1,
               assignment={"lifecycle": {"issue": 1, "event": 101}},
               worker={"status": "blocked", "summary": "Required check could not run."})
    body = core.record_body(core.job_records(cfg, job)[0])
    assert "Readiness cost: one extra agent session for this approval." in body
