"""The trusted harness uses no live GitHub, credentials or agent in these tests."""

import copy
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import json
from pathlib import Path
import subprocess
import tarfile
import threading
from types import SimpleNamespace

import pytest

from lifecycle import app_tokens, core, private_content, service, smoke
from recorded_github import RecordedGitHub
from test_github_replay import recording
from test_lifecycle_app_tokens import API, AT, configured


@pytest.fixture(autouse=True)
def private_failure_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "failed_runs_directory",
                        lambda cfg: core.private(tmp_path / "failed-runs"))


def config(tmp_path):
    cfg = core.read(smoke.ROOT / "runner/smoke.example.json")
    cfg.update(framework_repository="example/framework", repository="example/scratch",
               framework_owner="fixture-owner", framework_runner="framework-runner[bot]",
               agent_name="codex", lock_dir=str(tmp_path / "locks"))
    cfg["actors"] = {"runner": "smoke-runner[bot]", "owner": "smoke-owner[bot]",
                     "framework_runner": "framework-runner[bot]"}
    cfg["app_auth"] = {role: {"app_id": i, "keychain_service": "fixture-" + role,
                              "keychain_account": "fixture"}
                       for i, role in enumerate(cfg["actors"], 1)}
    cfg["runner"].update(agent_home=str(tmp_path / "agent-home"), worker_model="fixture",
                         reviewer_model="fixture", commit_identity={"name": "Fixture", "email": "fixture@example.test"})
    cfg["runner"]["agent"].update(command=["/usr/bin/true"], version_command=["/usr/bin/true"],
                                     version_pattern="fixture", worker_flags=[], reviewer_flags=[])
    cfg["runner"]["verification"] = {"commands": [["/usr/bin/true"]]}
    path = tmp_path / "smoke.json"
    core.save(path, cfg)
    return smoke.load_config(path)


def test_candidate_has_no_framework_authority(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "framework-fixture")
    monkeypatch.setenv("GITHUB_TOKEN", "framework-fixture")
    monkeypatch.setenv("PYTHONPATH", "framework-fixture")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    root = tmp_path / "temporary"
    policy, path = smoke.candidate_config(cfg, root)
    serialized = "".join(p.read_text() for p in root.glob("*.json"))
    for forbidden in (cfg["framework_repository"], cfg["actors"]["framework_runner"],
                      "fixture-framework_runner", "fixture-owner", "framework-fixture"):
        assert forbidden not in serialized
        assert forbidden not in json.dumps(smoke.environment(root / "home"))
    assert policy["repository"] == cfg["repository"]
    assert set(policy["app_auth"]) == {"runner"}
    assert core.read(path)["actors"]["owner"] == cfg["actors"]["owner"]
    # Constructed configs must satisfy the existing runner/service interfaces.
    runner = service.runner_module()
    runner.load_config(root / "runner.json")
    service.validate_local(policy)


@pytest.mark.parametrize("mutation", ["repository", "identity", "auth", "environment", "runner"])
def test_config_rejects_credential_or_repository_confusion(tmp_path, mutation):
    cfg = config(tmp_path)
    if mutation == "repository":
        cfg["repository"] = cfg["framework_repository"]
    if mutation == "identity":
        cfg["actors"]["owner"] = cfg["actors"]["runner"]
    if mutation == "auth":
        cfg["app_auth"]["runner"]["app_id"] = cfg["app_auth"]["framework_runner"]["app_id"]
    if mutation == "environment":
        cfg["runner"]["agent"]["environment"]["GH_TOKEN"] = "fixture"
    if mutation == "runner":
        cfg["runner"]["lifecycle_config"] = "fixture"
    core.save(tmp_path / "invalid.json", cfg)
    with pytest.raises(ValueError):
        smoke.load_config(tmp_path / "invalid.json")


def test_status_token_is_status_only(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    api = API(cfg, "runner")
    api.responses[2]["permissions"] = {"statuses": "write", "metadata": "read"}
    monkeypatch.setattr(app_tokens, "keychain_pem", lambda _: b"synthetic")
    monkeypatch.setattr(app_tokens, "jwt", lambda *args: "synthetic")
    app_tokens.mint(cfg, "runner", status_only=True, at=AT, opener=api)
    assert json.loads(api.calls[2].data) == {
        "repositories": ["project"], "permissions": {"statuses": "write"}}
    assert api.calls[-1].full_url.endswith("/installation/repositories?per_page=100")


@pytest.mark.parametrize("options", [{"role": "product"}, {"integration_enabled": True},
                                     {"fetch_enabled": True}, {"comments_only": True}])
def test_status_scope_cannot_mix_authority(tmp_path, options):
    role = options.pop("role", "runner")
    with pytest.raises(app_tokens.AuthenticationError):
        app_tokens.mint(configured(tmp_path), role, status_only=True, **options)


def test_second_run_refused_before_export(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path)
    monkeypatch.setattr(smoke, "export_candidate", lambda *a: pytest.fail("must not export"))
    lock = Path(cfg["lock_dir"]) / (core.key(cfg["repository"]) + ".lock")
    with core.lock(lock) as acquired:
        assert acquired
        assert smoke.run(cfg, "main") == 1
    assert "stop=busy" in capsys.readouterr().out


def test_export_uses_anonymous_public_fetch_and_safe_archive(tmp_path, monkeypatch):
    calls = []
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as stream:
        item = tarfile.TarInfo("lifecycle/service.py")
        item.size = 4
        stream.addfile(item, io.BytesIO(b"pass"))
    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        if "rev-parse" in argv:
            return b"a" * 40 + b"\n"
        if "archive" in argv:
            return archive.getvalue()
        return b""
    monkeypatch.setattr(smoke, "command", command)
    cfg = config(tmp_path)
    install, sha = smoke.export_candidate(cfg, "v1.2.3", tmp_path, smoke.environment(tmp_path))
    assert sha == "a" * 40
    assert (install / "lifecycle/service.py").read_text() == "pass"
    fetch = next(argv for argv, _ in calls if "fetch" in argv)
    assert fetch[-2:] == ["https://github.com/example/framework.git", "v1.2.3"]
    assert all("GH_TOKEN" not in kw["env"] for _, kw in calls)


@pytest.mark.parametrize("name,kind", [("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)])
def test_unsafe_candidate_export_refused(tmp_path, monkeypatch, name, kind):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as stream:
        item = tarfile.TarInfo(name)
        item.type = kind
        item.linkname = "/etc/passwd"
        stream.addfile(item)
    monkeypatch.setattr(smoke, "command", lambda argv, **kw:
                        b"a" * 40 if "rev-parse" in argv else archive.getvalue())
    with pytest.raises(core.Unavailable, match="Unsafe"):
        smoke.export_candidate(config(tmp_path), "main", tmp_path, {})


class EvidenceClient:
    def __init__(self, policy, comments, pr):
        self.policy, self.rows, self.pr = policy, comments, pr

    def comments(self, number):
        return self.rows

    def pages(self, path):
        return [self.pr] if self.pr else []

    def request(self, method, path, data=None):
        return self.pr

    def issue(self, number):
        return {"labels": [{"name": "status:needs-owner"}]}


def evidence_setup(tmp_path):
    cfg = config(tmp_path)
    cfg["repository"] = "akhilvis-oss/adt-smoke"
    pr = recording("smoke-pr3-open.json")
    cfg["actors"]["runner"] = pr["user"]["login"]
    cfg["trusted_gate_workflow"] = ".github/workflows/review-gate.yml"
    policy, _ = smoke.candidate_config(cfg, tmp_path / "evidence")
    number, event = 10, 20
    job = "issue-10-event-20"
    pr = copy.deepcopy(pr)
    pr["head"]["ref"] = "runner/" + job
    pr["body"] = "Closes #10"
    head = pr["head"]["sha"]
    specs = [
        ("draft", "approved", dict(kind="approval", approved_by=cfg["actors"]["owner"], scope_sha256="a" * 64)),
        ("approved", "dispatched", {}),
        ("in-progress", "committed", dict(commit=head)),
        ("in-review", "in-review", dict(kind="review", outcome="ready_for_integration", commit=head)),
        ("pr-open", "needs-owner", dict(reason="owner_merge_required", commit=head, pr=pr["number"]))]
    comments = []
    for index, (source, target, kwargs) in enumerate(specs):
        record = core.public_record(policy, number, job, 1, target, event=event, **kwargs)
        actor = policy["actors"]["workflow"] if index == 0 else policy["actors"]["runner"]
        record.update(actor=actor)
        record["from"] = source
        record["timestamp"] = f"2026-01-01T00:00:0{index}+00:00"
        comments.append({"user": {"login": actor}, "body": core.record_body(record)})
    client = EvidenceClient(policy, comments, pr)
    # Gate traffic uses real production transport/parsing on immutable recordings.
    gate_client = core.GitHub(policy, token="synthetic")
    run = recording("smoke-pr3-review-gate-run-open.json")
    RecordedGitHub(gate_client, {
        ("GET", f"/repos/{cfg['repository']}/commits/{head}/check-runs?filter=all&per_page=100&page=1"):
            "smoke-pr3-head-check-runs.json",
        ("GET", f"/repos/{cfg['repository']}/actions/workflows/review-gate.yml"):
            "smoke-review-gate-workflow.json",
        ("GET", f"/repos/{cfg['repository']}/actions/runs/{run['id']}"):
            "smoke-pr3-review-gate-run-open.json"})
    return policy, client, gate_client


def test_all_six_conditions_with_recorded_gate(tmp_path, monkeypatch):
    policy, client, gate_client = evidence_setup(tmp_path)
    real = core.trusted_gate
    monkeypatch.setattr(core, "trusted_gate", lambda cfg, _, pr, **kw: real(cfg, gate_client, pr, **kw))
    assert smoke.evidence(policy, client, 10) == (6, True, "owner_merge_required")


@pytest.mark.parametrize("condition,progress", [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])
def test_each_missing_pass_condition_fails_at_its_stage(tmp_path, monkeypatch, condition, progress):
    policy, client, _ = evidence_setup(tmp_path)
    monkeypatch.setattr(core, "trusted_gate", lambda *a, **kw: "unavailable" if condition == 4 else "pass")
    if condition < 3:
        del client.rows[condition if condition == 0 else condition + 1]
    elif condition == 3:
        client.pr = None
    elif condition == 5:
        client.rows.pop()
    completed, _, category = smoke.evidence(policy, client, 10)
    assert completed == progress
    text = smoke.description("codex", False, completed, category)
    assert f"furthest={smoke.STAGES[progress]}" in text
    assert f"next={smoke.STAGES[progress + 1]}" in text


@pytest.mark.parametrize("mutation", ["foreign-head", "foreign-repository", "foreign-author", "wrong-job", "closed"])
def test_unbound_or_closed_evidence_never_passes(tmp_path, monkeypatch, mutation):
    policy, client, _ = evidence_setup(tmp_path)
    monkeypatch.setattr(core, "trusted_gate", lambda *a, **kw: "pass")
    if mutation == "foreign-head":
        client.pr["head"]["sha"] = "b" * 40
    elif mutation == "foreign-repository":
        client.pr["head"]["repo"]["full_name"] = "example/other"
    elif mutation == "foreign-author":
        client.pr["user"]["login"] = "other"
    elif mutation == "wrong-job":
        client.pr["head"]["ref"] = "runner/issue-10-event-21"
    else:
        client.pr["state"] = "closed"
    assert smoke.evidence(policy, client, 10)[0] < 6


class RunClient:
    def __init__(self):
        self.calls = []
        self.open_issue = False
        self.open_pr = False
        self.status_refused = False
        self.cleanup_refused = False

    def pages(self, path):
        if path == "/issues?state=open":
            return [{"number": 99}] if self.open_issue else []
        if path == "/pulls?state=open":
            return [{"number": 88}] if self.open_pr else []
        return [{"number": 2, "state": "open", "head": {"ref": "runner/issue-1-event-2"}}]

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if method == "GET" and path.startswith("/actions/runs?"):
            if "status=success&" in path:
                return {"total_count": 1, "workflow_runs": [{"id": 1}]}
            return {"total_count": 0, "workflow_runs": []}
        if method == "GET" and path == "/":
            return {"allow_auto_merge": False}
        if method == "GET" and "matching-refs" in path:
            ns = "agentic/continue" if "agentic/continue" in path else "heads/runner"
            return [{"ref": "refs/" + ns + "/issue-1-event-2"},
                    {"ref": "refs/heads/runner/issue-999-event-2"}]
        if method == "POST" and path == "/issues":
            return {"number": 1}
        if "/statuses/" in path and self.status_refused:
            raise core.Unavailable("PRIVATE-DETAIL")
        if path == "/pulls/2" and self.cleanup_refused:
            raise core.Unavailable("PRIVATE-DETAIL")
        return {}

    def issue(self, number):
        return self.request("GET", f"/issues/{number}")

    def guard_runs(self, status=None, *, timeout):
        query = "" if status is None else f"&status={status}"
        return self.request("GET", f"/actions/runs?event=issues{query}&per_page=1")


def run_setup(tmp_path, monkeypatch, outcome):
    cfg = config(tmp_path)
    monkeypatch.setattr(smoke, "failed_runs_directory",
                        lambda cfg: core.private(tmp_path / "failed-runs"))
    client = RunClient()
    child = SimpleNamespace(pid=999999, poll=lambda: None)
    stopped = []
    monkeypatch.setattr(smoke, "export_candidate", lambda cfg, candidate, root, env: (root / "install", "a" * 40))
    monkeypatch.setattr(smoke, "clients", lambda *args: (client, client))
    monkeypatch.setattr(smoke, "status_client", lambda *args: client)
    monkeypatch.setattr(smoke, "command", lambda *args, **kwargs: b"")
    monkeypatch.setattr(smoke, "start_child", lambda *args: child)
    monkeypatch.setattr(smoke, "stop_child", lambda c: stopped.append(c))
    monkeypatch.setattr(smoke, "evidence", lambda *args: outcome)
    monkeypatch.setattr(smoke, "guard_idle", lambda *args, **kwargs: True)
    monkeypatch.setattr(smoke, "guard_snapshot", lambda *args, **kwargs: 0)
    return cfg, client, child, stopped


@pytest.mark.parametrize("outcome,expected", [((6, True, "owner_merge_required"), 0),
                                             ((2, True, "reviewer_reported_blocked"), 1),
                                             ((1, False, "pending"), 1)])
def test_run_cleans_after_pass_failure_and_timeout(tmp_path, monkeypatch, outcome, expected):
    cfg, client, child, stopped = run_setup(tmp_path, monkeypatch, outcome)
    times = iter([0, 0 if outcome[1] else 2000])
    assert smoke.run(cfg, "main", clock=lambda: next(times)) == expected
    assert stopped == [child]
    assert ("PATCH", "/issues/1", {"state": "closed"}) in client.calls
    assert ("PATCH", "/pulls/2", {"state": "closed"}) in client.calls
    assert any(method == "DELETE" and "heads/runner/issue-1-event-2" in path for method, path, _ in client.calls)
    assert any(method == "DELETE" and "agentic/continue/issue-1-event-2" in path for method, path, _ in client.calls)
    assert not any(method == "DELETE" and "issue-999" in path for method, path, _ in client.calls)
    status = client.calls[-1][2]
    assert status["context"] == "smoke/codex"
    assert status["state"] == ("success" if expected == 0 else "failure")
    if not outcome[1]:
        assert "stop=timeout" in status["description"]


def test_cleanup_continues_after_write_failure(tmp_path, monkeypatch):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    client.cleanup_refused = True
    assert smoke.run(cfg, "main") == 1
    assert any(method == "DELETE" for method, _, _ in client.calls)
    assert ("PATCH", "/issues/1", {"state": "closed"}) in client.calls
    assert "cleanup_failed" in client.calls[-1][2]["description"]


def test_refused_status_prints_permission_message_and_fails(tmp_path, monkeypatch, capsys):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    client.status_refused = True
    assert smoke.run(cfg, "main") == 1
    text = capsys.readouterr().out
    assert "PASS" in text and "Commit status permission missing" in text
    assert "PRIVATE-DETAIL" not in text


@pytest.mark.parametrize("busy", ["open_issue", "open_pr"])
def test_open_scratch_work_refuses_start(tmp_path, monkeypatch, busy):
    cfg, client, _, stopped = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    setattr(client, busy, True)
    assert smoke.run(cfg, "main") == 1
    assert not stopped
    assert not any(method in {"PATCH", "DELETE"} or path == "/issues" for method, path, _ in client.calls)
    assert not any("/statuses/" in path for _, path, _ in client.calls)


def test_all_status_text_is_public_and_fits_github():
    for category in [*core.STOP_REASONS, "timeout", "cleanup_failed", "PRIVATE-DETAIL"]:
        for stage in range(7):
            text = smoke.description("a" * 24, False, stage, category)
            assert not list(private_content.scan_text(text))
            assert len(text) <= 140
            assert "PRIVATE-DETAIL" not in text


def test_cli_routes_smoke_before_service_config(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(smoke, "main", lambda *args: calls.append(args) or 1)
    assert service.main(["--config", str(tmp_path / "smoke.json"), "smoke", "--candidate", "main"]) == 1
    assert calls == [(tmp_path / "smoke.json", "main")]


def test_child_uses_export_and_filtered_environment(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)))
    env = smoke.environment(tmp_path / "home")
    smoke.start_child(tmp_path / "install", tmp_path / "config.json", env)
    argv, kwargs = calls[0]
    assert argv[1:3] == ["-m", "lifecycle.service"]
    assert argv[-1] == "serve"
    assert kwargs["cwd"] == tmp_path / "install"
    assert kwargs["env"] == env
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"].name == str(tmp_path / "candidate.log")
    assert kwargs["stderr"] is kwargs["stdout"]
    assert (tmp_path / "candidate.log").stat().st_mode & 0o777 == 0o600


def test_direct_script_cli_is_available_without_import_environment(tmp_path):
    import sys

    result = subprocess.run([sys.executable, "-I", str(smoke.ROOT / "lifecycle/service.py"), "--help"],
                            cwd=tmp_path, capture_output=True, check=True)
    assert b"smoke" in result.stdout and b"--candidate" in result.stdout


def test_cleanup_closes_candidate_heartbeat_report(tmp_path):
    cfg = config(tmp_path)
    policy, _ = smoke.candidate_config(cfg, tmp_path / "candidate")
    client = RunClient()
    previous = client.pages
    client.pages = lambda path: [{"number": 3, "body": "<!-- agentic-service-status-v1 -->",
                                 "user": {"login": cfg["actors"]["runner"]}}] if path == "/issues?state=open" else previous(path)
    assert smoke.cleanup(policy, client, 1)
    assert ("PATCH", "/issues/3", {"state": "closed"}) in client.calls


@pytest.mark.parametrize("failure", ["start_child", "evidence"])
def test_exception_after_creation_still_cleans(tmp_path, monkeypatch, failure):
    cfg, client, _, stopped = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    def fail(*args):
        raise RuntimeError("PRIVATE-DETAIL")
    monkeypatch.setattr(smoke, failure, fail)
    assert smoke.run(cfg, "main") == 1
    assert ("PATCH", "/issues/1", {"state": "closed"}) in client.calls
    assert bool(stopped) == (failure == "evidence")
    assert client.calls[-1][2]["state"] == "failure"


def test_uncertain_issue_create_is_discovered_and_closed(tmp_path, monkeypatch):
    cfg, client, _, stopped = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    previous_request, previous_pages = client.request, client.pages
    created = {}
    def request(method, path, data=None):
        if method == "POST" and path == "/issues":
            created.update(data, number=1, user={"login": cfg["actors"]["owner"]})
            raise core.Uncertain("PRIVATE-DETAIL")
        return previous_request(method, path, data)
    client.request = request
    client.pages = lambda path: [created] if path == "/issues?state=all" else previous_pages(path)
    assert smoke.run(cfg, "main") == 1
    assert not stopped
    assert ("PATCH", "/issues/1", {"state": "closed"}) in client.calls
    assert not any(path.endswith("/labels") for _, path, _ in client.calls)


def test_child_group_is_killed_after_grace_period(monkeypatch):
    calls = []
    waits = []
    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 1:
            raise subprocess.TimeoutExpired("synthetic", timeout)
    monkeypatch.setattr(smoke.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    smoke.stop_child(SimpleNamespace(pid=12345, wait=wait))
    assert calls == [(12345, smoke.signal.SIGINT), (12345, smoke.signal.SIGKILL)]
    assert waits == [10, 10]


def test_delete_ref_handles_empty_response_and_rejects_other_writes(tmp_path):
    cfg = {"repository": "example/scratch", "actors": {"runner": "runner", "owner": "owner"}}
    client = smoke.SmokeGitHub(cfg, token="synthetic")
    client.identity_verified = True
    calls = []
    client.opener = SimpleNamespace(open=lambda req, **kw: calls.append(req) or io.BytesIO(b""))
    assert client.request("DELETE", "/git/refs/heads/runner/issue-1-event-2") is None
    with pytest.raises(ValueError):
        client.request("DELETE", "/git/refs/heads/main")
    with pytest.raises(ValueError):
        client.request("PUT", "/pulls/3/merge", {})
    assert len(calls) == 1


def test_stop_status_cannot_write_issues(tmp_path):
    client = smoke.StatusGitHub({"repository": "example/framework", "actors": {
        "runner": "runner", "owner": "owner"}}, token="synthetic")
    client.identity_verified = True
    with pytest.raises(ValueError):
        client.request("POST", "/issues", {})


def test_app_metadata_may_omit_auto_merge_setting(tmp_path, monkeypatch):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    previous = client.request
    client.request = lambda method, path, data=None: {} if (method, path) == ("GET", "/") else previous(method, path, data)
    assert smoke.run(cfg, "main") == 0


def test_gate_snapshot_drift_cannot_pass(tmp_path, monkeypatch):
    policy, client, _ = evidence_setup(tmp_path)
    monkeypatch.setattr(core, "trusted_gate", lambda *a, **kw: "pass")
    first = copy.deepcopy(client.pr)
    fresh = copy.deepcopy(first)
    fresh["head"]["sha"] = "b" * 40
    snapshots = iter([first, fresh])
    client.request = lambda *args: next(snapshots)
    assert smoke.evidence(policy, client, 10) == (4, True, "unavailable")


def test_review_must_match_the_built_commit(tmp_path, monkeypatch):
    policy, client, _ = evidence_setup(tmp_path)
    record = core.public_record(policy, 10, "issue-10-event-20", 1, "committed", event=20, commit="b" * 40)
    record.update(actor=policy["actors"]["runner"])
    record["from"] = "in-progress"
    client.rows[2]["body"] = core.record_body(record)
    monkeypatch.setattr(core, "trusted_gate", lambda *a, **kw: pytest.fail("must not reach gate"))
    assert smoke.evidence(policy, client, 10)[0] == 2


def test_pass_evidence_after_deadline_is_a_timeout(tmp_path, monkeypatch):
    cfg, client, child, stopped = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    times = iter([0, 2000])
    assert smoke.run(cfg, "main", clock=lambda: next(times)) == 1
    assert stopped == [child]
    assert client.calls[-1][2]["state"] == "failure"
    assert "stop=timeout" in client.calls[-1][2]["description"]


def test_admission_settings_url_has_no_trailing_slash(tmp_path, monkeypatch):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    wire = core.GitHub({"repository": cfg["repository"], "actors": cfg["actors"]},
                       token="synthetic")
    wire.identity_verified = True
    urls = []
    wire.opener = SimpleNamespace(open=lambda req, **kw: (
        urls.append(req.full_url) or io.BytesIO(b'{"allow_auto_merge": false}')))
    previous = client.request
    client.request = lambda method, path, data=None: (
        wire.request(method, path) if (method, path) == ("GET", "/") else previous(method, path, data))
    assert smoke.run(cfg, "main") == 0
    assert urls == ["https://api.github.com/repos/example/scratch"]


def test_failure_diagnosis_stays_local(tmp_path, monkeypatch, capsys):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    def fail(*args):
        raise RuntimeError("local detail\nsecond line")
    monkeypatch.setattr(smoke, "start_child", fail)
    assert smoke.run(cfg, "main") == 1
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 2
    assert "step=start-child; RuntimeError: local detail second line" in captured.err
    assert "function=lifecycle.smoke.run" in captured.err
    assert "local detail" not in captured.out
    status = client.calls[-1][2]
    assert status["description"] == "FAIL; furthest=admission; next=approval; stop=unavailable."


@pytest.mark.parametrize("target", ["blocked", "needs-owner"])
def test_unrecorded_stop_label_is_detected(tmp_path, target):
    policy, client, _ = evidence_setup(tmp_path)
    client.rows = []
    client.issue = lambda number: {"labels": [{"name": "status:" + target}]}
    assert smoke.evidence(policy, client, 10) == (0, False, "stop_unrecorded")


def test_unrecorded_stop_ends_after_three_minutes(tmp_path, monkeypatch):
    cfg, client, _, stopped = run_setup(tmp_path, monkeypatch, (2, False, "stop_unrecorded"))
    elapsed = [0]
    def sleep(seconds):
        elapsed[0] += seconds
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0], sleep=sleep) == 1
    assert elapsed[0] == 180
    assert stopped
    assert "stop=stop_unrecorded" in client.calls[-1][2]["description"]


def test_unrecorded_stop_timer_resets_when_label_clears(tmp_path, monkeypatch):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (2, False, "stop_unrecorded"))
    elapsed = [0]
    def evidence(*args):
        if elapsed[0] == 175:
            return 2, False, "pending"
        return 2, False, "stop_unrecorded"
    def sleep(seconds):
        elapsed[0] += seconds
    monkeypatch.setattr(smoke, "evidence", evidence)
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0], sleep=sleep) == 1
    assert elapsed[0] == 360
    assert "stop=stop_unrecorded" in client.calls[-1][2]["description"]


def test_failed_runs_keep_private_state_and_logs_only_last_three(tmp_path, monkeypatch, capsys):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (2, True, "reviewer_reported_blocked"))
    roots = []
    def start(install, config_path, env):
        root = config_path.parent
        roots.append(root)
        core.save(root / "state" / "fixture.json", {"private": len(roots)})
        (root / "candidate.log").write_text("synthetic candidate output")
        return SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(smoke, "start_child", start)
    for _ in range(4):
        assert smoke.run(cfg, "main") == 1
    retained = sorted(p for p in (tmp_path / "failed-runs").iterdir() if p.is_dir())
    assert len(retained) == 3
    assert [core.read(p / "state" / "fixture.json")["private"] for p in retained] == [2, 3, 4]
    for folder in retained:
        assert folder.stat().st_mode & 0o777 == 0o700
        assert (folder / "candidate.log").read_text() == "synthetic candidate output"
    captured = capsys.readouterr()
    assert all(str(folder) in captured.err for folder in retained)
    assert str(tmp_path / "failed-runs") not in captured.out
    assert all(not root.exists() for root in roots)
    monkeypatch.setattr(smoke, "evidence", lambda *a: (6, True, "owner_merge_required"))
    assert smoke.run(cfg, "main") == 0
    assert sorted(p for p in (tmp_path / "failed-runs").iterdir() if p.is_dir()) == retained
    assert not roots[-1].exists()


def test_status_failure_also_retains_evidence(tmp_path, monkeypatch):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    client.status_refused = True
    assert smoke.run(cfg, "main") == 1
    assert len([p for p in (tmp_path / "failed-runs").iterdir() if p.is_dir()]) == 1


def test_concurrent_failed_runs_keep_at_most_three(tmp_path):
    cfg = config(tmp_path)
    barrier = threading.Barrier(4)
    def fail(index):
        with smoke.workspace(cfg) as (root, result):
            core.save(root / "state.json", {"run": index})
            barrier.wait(timeout=10)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fail, range(4)))
    retained = [p for p in (tmp_path / "failed-runs").iterdir() if p.is_dir()]
    assert len(retained) == 3
    assert len({core.read(p / "state.json")["run"] for p in retained}) == 3


def test_status_token_422_explains_installation_permission(tmp_path, monkeypatch, capsys):
    import urllib.error

    cfg, _, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    def refused(req, **kwargs):
        raise urllib.error.HTTPError(req.full_url, 422, "synthetic refusal", {}, None)
    opener = SimpleNamespace(open=refused)
    def status_client(_):
        app_tokens.request(opener, "synthetic", "POST", "/app/installations/1/access_tokens",
                           {"permissions": {"statuses": "write"}})
    monkeypatch.setattr(smoke, "status_client", status_client)
    assert smoke.run(cfg, "main") == 1
    err = capsys.readouterr().err
    assert "step=status; AuthenticationError:" in err
    assert "App installation lacks Commit statuses permission on the framework repository" in err
    assert "function=lifecycle.app_tokens.request" in err


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_approval_waits_for_guard(tmp_path, monkeypatch, status):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    monkeypatch.setattr(smoke, "guard_idle", smoke_guard_idle)
    elapsed = [0]
    previous = client.request
    def request(method, path, data=None):
        if path.startswith("/actions/runs?") and f"status={status}&" in path:
            active = elapsed[0] < 15
            return {"total_count": int(active), "workflow_runs": [{"id": 1}] if active else []}
        if method == "POST" and path == "/issues/1/labels":
            assert elapsed[0] == 15
        return previous(method, path, data)
    client.request = request
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0],
                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)) == 0
    assert sum(path == "/issues/1/labels" for _, path, _ in client.calls) == 1


@pytest.mark.parametrize("request_seconds", [0, 29, 30])
def test_busy_guard_cleans_without_approval_or_status(tmp_path, monkeypatch, capsys, request_seconds):
    cfg, client, _, stopped = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    monkeypatch.setattr(smoke, "guard_idle", smoke_guard_idle)
    elapsed = [0]
    previous = client.request
    def request(method, path, data=None):
        if path.startswith("/actions/runs?"):
            return {"total_count": 1, "workflow_runs": [{"id": 1}]}
        return previous(method, path, data)
    client.request = request
    budgets = []
    def guard_runs(status, *, timeout):
        budgets.append(timeout)
        assert 0 < timeout <= min(30, 120 - elapsed[0])
        elapsed[0] += min(request_seconds, timeout)
        return request("GET", f"/actions/runs?event=issues&status={status}&per_page=1")
    client.guard_runs = guard_runs
    completed = []
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0],
                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                     completed=lambda *args: completed.append(args)) == 1
    assert elapsed[0] == 120
    assert budgets
    assert not stopped and not completed
    assert not any(path == "/issues/1/labels" or "/statuses/" in path for _, path, _ in client.calls)
    assert ("PATCH", "/issues/1", {"state": "closed"}) in client.calls
    assert "stop=busy" in capsys.readouterr().out


def test_initially_empty_guard_results_wait_for_new_run_to_finish(tmp_path, monkeypatch):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    monkeypatch.setattr(smoke, "guard_idle", smoke_guard_idle)
    monkeypatch.setattr(smoke, "guard_snapshot", smoke_guard_snapshot)
    elapsed = [0]
    previous = client.request
    def request(method, path, data=None):
        if path == "/actions/runs?event=issues&per_page=1":
            return {"total_count": 1, "workflow_runs": [{"id": 9}]}
        if path.startswith("/actions/runs?"):
            # The opened run is initially invisible. Historical successes do
            # not prove the new story was processed.
            status = ("queued" if elapsed[0] < 15 else "in_progress") if elapsed[0] < 20 else "success"
            visible = elapsed[0] >= 5 and f"status={status}&" in path
            if "status=success&" in path and elapsed[0] < 20:
                return {"total_count": 1, "workflow_runs": [{"id": 9}]}
            return {"total_count": int(visible), "workflow_runs": [{"id": 10}] if visible else []}
        if path == "/issues/1/labels":
            assert elapsed[0] == 20
        return previous(method, path, data)
    client.request = request
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0],
                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)) == 0
    assert sum(path == "/issues/1/labels" for _, path, _ in client.calls) == 1


def test_guard_never_visible_is_busy():
    elapsed = [0]
    client = SimpleNamespace(guard_runs=lambda *args, **kwargs: {"total_count": 0, "workflow_runs": []})
    assert not smoke.guard_idle(client, 0, clock=lambda: elapsed[0],
                                sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    assert elapsed[0] == 120


@pytest.mark.parametrize("failed_request", [1, 4])
def test_guard_timeout_at_deadline_is_busy(failed_request):
    elapsed = [0]
    calls = []
    def guard_runs(status, *, timeout):
        calls.append(status)
        elapsed[0] += timeout
        if len(calls) == failed_request:
            raise core.Unavailable("synthetic timeout")
        return {"total_count": 0, "workflow_runs": []}
    assert not smoke.guard_idle(SimpleNamespace(guard_runs=guard_runs), 0,
                                clock=lambda: elapsed[0], sleep=lambda _: None)
    assert elapsed[0] == 120


def test_guard_transport_uses_remaining_budget():
    client = smoke.SmokeGitHub({"repository": "example/scratch", "actors": {
        "runner": "runner", "owner": "owner"}}, token="synthetic")
    calls = []
    def open_request(req, *, timeout):
        calls.append((req.full_url, timeout))
        return io.BytesIO(b'{"total_count": 0, "workflow_runs": []}')
    client.opener = SimpleNamespace(open=open_request)
    assert client.guard_runs("queued", timeout=2.5) == {"total_count": 0, "workflow_runs": []}
    assert calls == [("https://api.github.com/repos/example/scratch/actions/runs?event=issues&status=queued&per_page=1", 2.5)]


@pytest.mark.parametrize("failure", ["timeout", "server"])
@pytest.mark.parametrize("applied", [True, False])
def test_unclear_approval_is_read_and_never_resent(tmp_path, monkeypatch, failure, applied):
    import urllib.error

    cfg, client, _, stopped = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    wire = smoke.SmokeGitHub({"repository": cfg["repository"], "actors": {
        "runner": cfg["actors"]["owner"], "owner": cfg["actors"]["runner"]}}, token="synthetic")
    wire.identity_verified = True
    attempts = []
    def open_request(req, **kwargs):
        attempts.append(req.method)
        if req.method == "POST":
            if failure == "timeout":
                raise TimeoutError("synthetic")
            raise urllib.error.HTTPError(req.full_url, 503, "synthetic", {}, None)
        return io.BytesIO(json.dumps({"labels": [{"name": "status:approved"}] if applied else []}).encode())
    wire.opener = SimpleNamespace(open=open_request)
    previous = client.request
    def request(method, path, data=None):
        if path in {"/issues/1/labels", "/issues/1"} and method in {"POST", "GET"}:
            return wire.request(method, path, data)
        return previous(method, path, data)
    client.request = request
    assert smoke.run(cfg, "main") == (0 if applied else 1)
    assert attempts == ["POST", "GET"]
    assert bool(stopped) == applied
    assert ("PATCH", "/issues/1", {"state": "closed"}) in client.calls


def test_unrecorded_approval_ends_after_three_minutes(tmp_path, monkeypatch):
    cfg, client, _, stopped = run_setup(tmp_path, monkeypatch, (0, False, "pending"))
    # Exercise actual record/label detection, rather than supplying the new category.
    monkeypatch.setattr(smoke, "evidence", smoke_evidence)
    client.comments = lambda number: []
    client.issue = lambda number: {"labels": [{"name": "status:approved"}]}
    elapsed = [0]
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0],
                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)) == 1
    assert elapsed[0] == 180 and stopped
    assert client.calls[-1][2]["description"] == (
        "FAIL; furthest=admission; next=approval; stop=approval_unrecorded.")


# Preserve the real observer for integration tests whose setup replaces it.
smoke_evidence = smoke.evidence
smoke_guard_idle = smoke.guard_idle
smoke_guard_snapshot = smoke.guard_snapshot


@pytest.mark.parametrize("recovery", ["label-cleared", "record-arrived"])
def test_unrecorded_approval_timer_recovers(tmp_path, monkeypatch, recovery):
    policy, recorded, _ = evidence_setup(tmp_path / "recorded")
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (0, False, "pending"))
    elapsed = [0]
    client.comments = lambda number: recorded.rows[:1] if recovery == "record-arrived" and elapsed[0] >= 175 else []
    client.issue = lambda number: {"labels": [] if elapsed[0] == 175 else ["status:approved"]}
    # The recorded approval is for issue 10; observe it with its matching policy.
    monkeypatch.setattr(smoke, "evidence", lambda *args: smoke_evidence(policy, client, 10))
    cfg["timeout_seconds"] = 400 if recovery == "record-arrived" else 1800
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0],
                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)) == 1
    assert elapsed[0] == (400 if recovery == "record-arrived" else 360)
    assert ("stop=timeout" if recovery == "record-arrived" else "stop=approval_unrecorded") in client.calls[-1][2]["description"]


@pytest.mark.parametrize("bad_read", [
    {"total_count": 1, "workflow_runs": []},
    {"total_count": 0, "workflow_runs": [{"id": 1}]},
    None,
    core.Unavailable("synthetic read failure"),
])
@pytest.mark.parametrize("status", ["queued", "success"])
def test_guard_bad_read_retries_before_approval(tmp_path, monkeypatch, bad_read, status):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    monkeypatch.setattr(smoke, "guard_idle", smoke_guard_idle)
    elapsed = [0]
    reads = []
    previous = client.guard_runs
    def guard_runs(query=None, *, timeout):
        if query == status:
            reads.append(elapsed[0])
            if elapsed[0] == 0:
                if isinstance(bad_read, Exception):
                    raise bad_read
                return bad_read
        return previous(query, timeout=timeout)
    client.guard_runs = guard_runs
    request = client.request
    def approve(method, path, data=None):
        if path == "/issues/1/labels":
            assert elapsed[0] == 5
        return request(method, path, data)
    client.request = approve
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0],
                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)) == 0
    assert reads == [0, 5]
    assert sum(path == "/issues/1/labels" for _, path, _ in client.calls) == 1


@pytest.mark.parametrize("bad_read", [
    {"total_count": 1, "workflow_runs": []},
    {"total_count": 0, "workflow_runs": [{"id": 9}]},
    {"total_count": 1, "workflow_runs": [{"id": "unreadable"}]},
    None,
    core.Unavailable("synthetic read failure"),
])
def test_snapshot_bad_read_retries_before_filing(tmp_path, monkeypatch, bad_read):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    monkeypatch.setattr(smoke, "guard_snapshot", smoke_guard_snapshot)
    elapsed = [0]
    reads = []
    def guard_runs(query=None, *, timeout):
        assert query is None
        reads.append(elapsed[0])
        if elapsed[0] == 0:
            if isinstance(bad_read, Exception):
                raise bad_read
            return bad_read
        return {"total_count": 1, "workflow_runs": [{"id": 9}]}
    client.guard_runs = guard_runs
    previous = client.request
    def request(method, path, data=None):
        if method == "POST" and path == "/issues":
            assert elapsed[0] == 5
        return previous(method, path, data)
    client.request = request
    def guard_idle(_, previous_run, **kwargs):
        assert previous_run == 9
        return True
    monkeypatch.setattr(smoke, "guard_idle", guard_idle)
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0],
                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)) == 0
    assert reads == [0, 5]
    assert sum(method == "POST" and path == "/issues" for method, path, _ in client.calls) == 1


@pytest.mark.parametrize("request_seconds", [0, 29, 30])
def test_inconsistent_snapshot_expires_without_filing_or_status(tmp_path, monkeypatch, capsys,
                                                              request_seconds):
    cfg, client, _, stopped = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    monkeypatch.setattr(smoke, "guard_snapshot", smoke_guard_snapshot)
    elapsed = [0]
    reads = []
    def guard_runs(query=None, *, timeout):
        assert query is None
        assert 0 < timeout <= min(30, 120 - elapsed[0])
        reads.append(elapsed[0])
        elapsed[0] += min(request_seconds, timeout)
        return {"total_count": 1, "workflow_runs": []}
    client.guard_runs = guard_runs
    completed = []
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0],
                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                     completed=lambda *args: completed.append(args)) == 1
    assert elapsed[0] == 120 and reads
    assert not stopped and not completed
    assert not any(method != "GET" for method, _, _ in client.calls)
    assert "stop=busy" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["queued", "success"])
def test_inconsistent_guard_expires_without_approval_or_status(tmp_path, monkeypatch, capsys, status):
    cfg, client, _, stopped = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    monkeypatch.setattr(smoke, "guard_idle", smoke_guard_idle)
    elapsed = [0]
    previous = client.guard_runs
    def guard_runs(query=None, *, timeout):
        if query == status:
            return {"total_count": 1, "workflow_runs": []}
        return previous(query, timeout=timeout)
    client.guard_runs = guard_runs
    completed = []
    assert smoke.run(cfg, "main", clock=lambda: elapsed[0],
                     sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
                     completed=lambda *args: completed.append(args)) == 1
    assert elapsed[0] == 120
    assert not stopped and not completed
    assert not any(path == "/issues/1/labels" or "/statuses/" in path for _, path, _ in client.calls)
    assert ("PATCH", "/issues/1", {"state": "closed"}) in client.calls
    assert "stop=busy" in capsys.readouterr().out


def test_snapshot_transport_uses_remaining_budget():
    client = smoke.SmokeGitHub({"repository": "example/scratch", "actors": {
        "runner": "runner", "owner": "owner"}}, token="synthetic")
    calls = []
    def open_request(req, *, timeout):
        calls.append((req.full_url, timeout))
        return io.BytesIO(b'{"total_count": 1, "workflow_runs": [{"id": 9}]}')
    client.opener = SimpleNamespace(open=open_request)
    assert client.guard_runs(timeout=2.5) == {"total_count": 1, "workflow_runs": [{"id": 9}]}
    assert calls == [("https://api.github.com/repos/example/scratch/actions/runs?event=issues&per_page=1", 2.5)]


@pytest.mark.parametrize("query", [None, "queued", "success"])
@pytest.mark.parametrize("recovers", [True, False])
def test_truncated_guard_response_retries_or_expires_busy(tmp_path, monkeypatch, capsys,
                                                        query, recovers):
    cfg, client, _, stopped = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    monkeypatch.setattr(smoke, "guard_snapshot", smoke_guard_snapshot)
    monkeypatch.setattr(smoke, "guard_idle", smoke_guard_idle)
    wire = smoke.SmokeGitHub({"repository": cfg["repository"], "actors": {
        "runner": "runner", "owner": "owner"}}, token="synthetic")
    elapsed = [0]
    sleeps = []
    failed_reads = []

    class TruncatedResponse(io.BytesIO):
        def read(self):
            raise http.client.IncompleteRead(b'{"total_count":', 20)

    def open_request(req, *, timeout):
        assert 0 < timeout <= min(30, 120 - elapsed[0])
        status = next((s for s in ("queued", "success", "in_progress", "waiting", "pending", "requested")
                       if f"status={s}&" in req.full_url), None)
        if status == query and (not recovers or elapsed[0] == 0):
            failed_reads.append(elapsed[0])
            return TruncatedResponse()
        result = {"total_count": 1, "workflow_runs": [{"id": 9 if status is None else 10}]}
        if status not in (None, "success"):
            result = {"total_count": 0, "workflow_runs": []}
        return io.BytesIO(json.dumps(result).encode())

    wire.opener = SimpleNamespace(open=open_request)
    client.guard_runs = wire.guard_runs
    request = client.request

    def checked_request(method, path, data=None):
        if method == "POST" and path == ("/issues" if query is None else "/issues/1/labels"):
            assert recovers and elapsed[0] == 5
        return request(method, path, data)

    client.request = checked_request

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    assert smoke.run(cfg, "main", clock=lambda: elapsed[0], sleep=sleep) == (0 if recovers else 1)
    assert elapsed[0] == (5 if recovers else 120)
    assert failed_reads == ([0] if recovers else list(range(0, 120, 5)))
    assert sleeps == [5] * (1 if recovers else 24)
    if recovers:
        assert sum(path == "/issues/1/labels" for _, path, _ in client.calls) == 1
        assert stopped
    else:
        assert not stopped
        assert not any(path == "/issues/1/labels" or "/statuses/" in path for _, path, _ in client.calls)
        if query is None:
            assert not any(method != "GET" for method, _, _ in client.calls)
        else:
            assert ("PATCH", "/issues/1", {"state": "closed"}) in client.calls
        assert "stop=busy" in capsys.readouterr().out
