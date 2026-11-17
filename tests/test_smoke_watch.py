"""Automatic smoke selection uses synthetic GitHub and no candidate execution."""

import copy
import io
import json
from types import SimpleNamespace

import pytest

from lifecycle import core, smoke, smoke_watch
from runner.integration import smoke_notice
from test_smoke import config, run_setup

A, B, C = (c * 40 for c in "abc")


class Client:
    def __init__(self, cfg):
        self.cfg = cfg
        self.main = A
        self.prs = []
        self.statuses = {}
        self.calls = []
        self.files = [{"filename": "code.py"}]
        self.fresh = None

    def row(self, sha, state="success", actor=None):
        self.statuses.setdefault(sha, []).insert(0, {
            "context": "smoke/codex", "state": state,
            "creator": {"login": actor or self.cfg["framework_runner"]}})

    def pr(self, sha=B, author=None, repo=None, number=1):
        return {"number": number, "state": "open", "user": {"login": author or self.cfg["framework_owner"]},
                "head": {"sha": sha, "repo": {"full_name": repo or self.cfg["framework_repository"]}},
                "base": {"repo": {"full_name": self.cfg["framework_repository"]}}}

    def pages(self, path):
        if path == "/pulls?state=open":
            return copy.deepcopy(self.prs)
        if path.startswith("/commits/"):
            return self.statuses.get(path.split("/")[2], [])
        if path == "/issues?state=open":
            return [{"number": 10, "user": {"login": self.cfg["framework_runner"]},
                     "body": "<!-- agentic-service-status-v1 -->"}]
        raise AssertionError(path)

    def comments(self, number):
        return []

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if path == "/commits/main":
            return {"sha": self.main}
        if path.startswith("/compare/"):
            return {"status": "ahead", "files": self.files}
        if path.startswith("/pulls/"):
            return self.fresh or next(p for p in self.prs if p["number"] == int(path.split("/")[-1]))
        if path.startswith("/statuses/"):
            self.row(path.split("/")[-1], data["state"])
        return {"id": 20}


def setup(tmp_path):
    cfg = config(tmp_path)
    client = Client(cfg)
    runs = []

    def run(cfg, sha, *, completed):
        runs.append(sha)
        completed(sha, "success", "PASS; furthest=owner-merge; stop=owner_merge_required.")
        client.row(sha)
        return 0

    def watch(**kwargs):
        return smoke_watch.watch(cfg, client, run=run, status_client=client, **kwargs)

    return cfg, client, runs, watch


def test_candidate_order_and_already_tested_head(tmp_path):
    cfg, client, runs, watch = setup(tmp_path)
    client.prs = [client.pr(C, number=2), client.pr(B)]
    assert watch() == "completed"
    assert runs == [A]
    assert watch() == "completed"
    assert runs == [A, B]
    assert watch() == "completed"
    assert runs == [A, B, C]
    assert watch() == "idle"
    # A new journal still honors statuses posted by the framework App.
    cfg["lock_dir"] += "-fresh"
    assert watch() == "idle"


def test_docs_only_posts_success_naming_passed_commit(tmp_path):
    _, client, runs, watch = setup(tmp_path)
    watch()
    client.main = B
    client.files = [{"filename": "docs/guide.txt"}, {"filename": "README.md"}]
    assert watch() == "completed"
    assert runs == [A]
    status = next(data for method, path, data in client.calls if path == "/statuses/" + B)
    assert status["state"] == "success"
    assert A in status["description"]
    comments = [data["body"] for method, path, data in client.calls if path == "/issues/10/comments"]
    assert B in comments[-1] and "owner-merge" in comments[-1]


def test_docs_comparison_uses_real_github_path_validator():
    cfg = {"repository": "example/framework",
           "actors": {"runner": "fixture-runner", "owner": "fixture-owner"}}
    client = core.GitHub(cfg, token="synthetic")
    client.identity_verified = True
    calls = []

    def opened(req, timeout):
        calls.append(req.full_url)
        return io.BytesIO(json.dumps({"status": "ahead", "files": [
            {"filename": "docs/guide.md"}]}).encode())

    client.opener = SimpleNamespace(open=opened)
    assert smoke_watch.docs_only(client, A, B)
    assert calls == [f"https://api.github.com/repos/example/framework/compare/{A}...{B}"]


@pytest.mark.parametrize("method,path", [
    ("GET", f"/compare/{A}...{B}/../issues"),
    ("GET", f"/compare/{A}...{B}?redirect=../issues"),
    ("GET", "/compare/main...branch"),
    ("GET", f"/compare/{A}..{B}"),
    ("GET", f"/compare/{A}....{B}"),
    ("POST", f"/compare/{A}...{B}"),
])
def test_comparison_exception_rejects_other_dotted_paths(method, path):
    client = core.GitHub({"repository": "example/framework", "actors": {
        "runner": "fixture-runner", "owner": "fixture-owner"}}, token="synthetic")
    with pytest.raises(ValueError, match="Invalid repository API path"):
        client.request(method, path)


@pytest.mark.parametrize("files", [
    [{"filename": "docs/a.md", "status": "renamed", "previous_filename": "code.py"}],
    [{"filename": "docs/a.md"}] * 300,
    [],
])
def test_docs_shortcut_fails_closed(tmp_path, files):
    _, client, runs, watch = setup(tmp_path)
    watch()
    client.main, client.files = B, files
    watch()
    assert runs == [A, B]


def test_forks_other_authors_and_superseded_heads_never_run(tmp_path):
    _, client, runs, watch = setup(tmp_path)
    client.row(A)
    client.prs = [client.pr(author="outsider"), client.pr(repo="other/fork", number=2),
                  client.pr(C, number=3)]
    client.fresh = client.pr(B, number=3)
    assert watch() == "idle"
    assert not runs


def test_runner_app_author_is_eligible(tmp_path):
    cfg, client, runs, watch = setup(tmp_path)
    client.row(A)
    client.prs = [client.pr(author=cfg["framework_runner"])]
    assert watch() == "completed"
    assert runs == [B]


def test_foreign_status_does_not_suppress_run(tmp_path):
    _, client, runs, watch = setup(tmp_path)
    client.row(A, actor="untrusted")
    watch()
    assert runs == [A]


def test_busy_start_leaves_no_status_and_retries(tmp_path, monkeypatch):
    from lifecycle import smoke

    cfg, scratch, _, _ = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    scratch.open_issue = True
    client = Client(cfg)
    assert smoke_watch.watch(cfg, client, run=smoke.run) == "not-started"
    assert not any("/statuses/" in p for _, p, _ in scratch.calls)
    scratch.open_issue = False
    assert smoke_watch.watch(cfg, client, run=smoke.run) == "completed"
    assert sum("/statuses/" in p for _, p, _ in scratch.calls) == 1


@pytest.mark.parametrize("error", [core.Unavailable, OSError, ValueError])
def test_preparation_failure_releases_reservation_and_retries(tmp_path, monkeypatch, error):
    cfg, scratch, _, stopped = run_setup(tmp_path, monkeypatch, (6, True, "owner_merge_required"))
    cfg["daily_cap"] = 1
    client = Client(cfg)
    export = smoke.export_candidate

    def failed_export(*args):
        raise error("synthetic preparation failure")

    monkeypatch.setattr(smoke, "export_candidate", failed_export)
    with pytest.raises(smoke.PreparationFailed):
        smoke_watch.watch(cfg, client, run=smoke.run)
    assert not scratch.calls and not stopped
    monkeypatch.setattr(smoke, "export_candidate", export)
    assert smoke_watch.watch(cfg, client, run=smoke.run) == "completed"
    assert sum("/statuses/" in p for _, p, _ in scratch.calls) == 1


def test_uncertain_execution_keeps_reservation(tmp_path):
    cfg, client, _, _ = setup(tmp_path)
    calls = []

    def uncertain_run(cfg, sha, *, completed):
        calls.append(sha)
        raise core.Uncertain("synthetic execution uncertainty")

    with pytest.raises(core.Uncertain):
        smoke_watch.watch(cfg, client, run=uncertain_run)
    assert smoke_watch.watch(cfg, client, run=uncertain_run) == "idle"
    assert calls == [A]
    assert not any("/statuses/" in p for _, p, _ in client.calls)


def test_daily_cap_and_next_day(tmp_path):
    cfg, client, runs, watch = setup(tmp_path)
    cfg["daily_cap"] = 2
    watch(clock=lambda: 0)
    client.main = B
    watch(clock=lambda: 0)
    client.main = C
    assert watch(clock=lambda: 0) == "capped"
    assert runs == [A, B]
    assert watch(clock=lambda: 86400) == "completed"
    assert runs == [A, B, C]


def test_watcher_lock_prevents_concurrent_run(tmp_path):
    cfg, _, runs, watch = setup(tmp_path)
    key = core.key([cfg["framework_repository"], cfg["repository"], cfg["agent_name"]])
    from pathlib import Path

    with core.lock(Path(cfg["lock_dir"]) / (key + "-watch.lock")):
        assert watch() == "locked"
    assert not runs


def test_failed_completed_run_is_not_repeated(tmp_path):
    cfg, client, _, _ = setup(tmp_path)
    calls = []

    def run(cfg, sha, *, completed):
        calls.append(sha)
        completed(sha, "failure", "FAIL; furthest=review; stop=unavailable.")
        return 1

    assert smoke_watch.watch(cfg, client, run=run) == "completed"
    assert smoke_watch.watch(cfg, client, run=run) == "idle"
    assert calls == [A]


def test_ambiguous_status_write_never_reposted(tmp_path):
    cfg, client, runs, watch = setup(tmp_path)
    watch()
    client.main = B
    client.files = [{"filename": "docs/readme.md"}]
    calls = []

    class LostResponse:
        def pages(self, path):
            return client.pages(path)

        def request(self, *args):
            calls.append(args)
            raise core.Uncertain("synthetic")

    with pytest.raises(core.Uncertain):
        smoke_watch.watch(cfg, client, status_client=LostResponse())
    assert watch() == "idle"
    assert len(calls) == 1 and runs == [A]


def test_merge_notice_pass_and_pending_on_reviewed_head(tmp_path):
    cfg, client, _, _ = setup(tmp_path)
    policy = {"actors": {"runner": cfg["framework_runner"]}}
    assert "pending" in smoke_notice(client, policy, A)
    client.row(A)
    assert "smoke/codex: passed" in smoke_notice(client, policy, A)
    assert "pending" in smoke_notice(client, policy, B)
    client.row(A, state="failure")
    assert "failed" in smoke_notice(client, policy, A)


def test_null_head_repository_is_skipped(tmp_path):
    _, client, runs, watch = setup(tmp_path)
    client.row(A)
    pr = client.pr()
    pr["head"]["repo"] = None
    client.prs = [pr]
    assert watch() == "idle"
    assert not runs


@pytest.mark.parametrize("cap", [0, -1, True, "12"])
def test_invalid_daily_cap_is_rejected(tmp_path, cap):
    from lifecycle import smoke

    cfg = config(tmp_path)
    cfg["daily_cap"] = cap
    path = tmp_path / "invalid-smoke.json"
    core.save(path, cfg)
    with pytest.raises(ValueError):
        smoke.load_config(path)


def test_legacy_manual_config_remains_compatible(tmp_path):
    from lifecycle import smoke

    cfg = config(tmp_path)
    for key in ("framework_owner", "framework_runner", "daily_cap"):
        del cfg[key]
    path = tmp_path / "legacy-smoke.json"
    core.save(path, cfg)
    assert smoke.load_config(path) == cfg


def test_watcher_failure_has_local_diagnosis(monkeypatch, capsys):
    def fail(_):
        raise ValueError("local watcher detail")
    monkeypatch.setattr(smoke, "load_config", fail)
    assert smoke_watch.main("synthetic") == 1
    captured = capsys.readouterr()
    assert captured.err.count("\n") == 1
    assert "step=watcher; ValueError: local watcher detail" in captured.err
    assert "function=lifecycle.smoke_watch.main" in captured.err
    assert "local watcher detail" not in captured.out
