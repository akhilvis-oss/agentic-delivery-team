"""App-authenticated fetches use synthetic transport and disposable repositories."""

import base64
from pathlib import Path
import subprocess

import pytest

from lifecycle import app_tokens, core, service
from test_lifecycle import configured_service
from test_lifecycle_app_tokens import AT

FETCH = service.fetch_target
SYNTHETIC_TOKEN = "synthetic-" + "fetch-token"


def command(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Fixture",
         "-c", "user.email=fixture@example.invalid", *args],
        check=True, capture_output=True,
    ).stdout.decode().strip()


def transport(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    source = Path(cfg["repository_root"])
    remote = tmp_path / "remote"
    subprocess.run(["git", "clone", "-q", str(source), str(remote)], check=True)
    # Deliberately select a configured non-default target and leave local main stale.
    command(remote, "checkout", "-b", "target")
    command(remote, "commit", "--allow-empty", "-qm", "remote advance")
    cfg["base_branch"] = "target"
    cfg["app_auth"] = {"runner": {"synthetic": True}}
    monkeypatch.setattr(service, "fetch_target", FETCH)
    minted, network = [], []

    def mint(c, role, **kwargs):
        assert c is cfg and role == "runner"
        minted.append(kwargs)
        return SYNTHETIC_TOKEN, AT + 3600

    monkeypatch.setattr(app_tokens, "mint", mint)
    original = subprocess.run

    def run(argv, **kwargs):
        if "https://github.com/example/project.git" in argv:
            network.append((list(argv), dict(kwargs)))
            env = kwargs["env"]
            assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
            assert env["GIT_CONFIG_NOSYSTEM"] == "1"
            assert env["GIT_TERMINAL_PROMPT"] == "0"
            assert not any(k in env for k in ("GH_TOKEN", "GIT_DIR", "GIT_ASKPASS"))
            encoded = base64.b64encode(("x-access-token:" + SYNTHETIC_TOKEN).encode()).decode()
            assert env["GIT_CONFIG_VALUE_0"] == "AUTHORIZATION: basic " + encoded
            assert SYNTHETIC_TOKEN not in str(argv)
            assert "credential.helper=" in argv and "http.followRedirects=false" in argv
            assert not (Path(argv[2]) / "config").read_text().count("include")
            argv = [str(remote) if x == "https://github.com/example/project.git" else x for x in argv]
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return cfg, hub, runner, remote, minted, network


def claims(cfg):
    return [core.read(p) for p in sorted((Path(cfg["state_dir"]) / "claims").glob("*.json"))]


def test_fetch_pins_remote_commit_and_recovery_keeps_it(tmp_path, monkeypatch):
    cfg, hub, runner, remote, minted, network = transport(tmp_path, monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "synthetic-owner-token")
    monkeypatch.setenv("GIT_ASKPASS", "/must-not-run")
    expected = command(remote, "rev-parse", "HEAD")
    original = service.ensure_worktree
    monkeypatch.setattr(service, "ensure_worktree", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert claims(cfg)[0]["start_commit"] == expected
    command(remote, "commit", "--allow-empty", "-qm", "later advance")
    monkeypatch.setattr(service, "ensure_worktree", original)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = claims(cfg)[0]
    assert command(claim["worktree"], "rev-parse", "HEAD") == expected
    assert command(cfg["repository_root"], "rev-parse", "main") != expected
    assert runner.read(next((Path(cfg["state_dir"]) / "jobs").glob("*/runner-state/tasks/*.json")))["base_commit"] == expected
    assert len(network) == 1 and network[0][0][-1] == "refs/heads/target"
    assert minted == [{"fetch_enabled": True, "integration_enabled": False}]


@pytest.mark.parametrize("failure", ["missing_branch", "transport", "authentication"])
def test_failed_fetch_waits_approved_and_recovers(tmp_path, monkeypatch, failure):
    cfg, hub, runner, remote, minted, network = transport(tmp_path, monkeypatch)
    real_run, real_mint = subprocess.run, app_tokens.mint
    if failure == "missing_branch":
        cfg["base_branch"] = "missing"
    elif failure == "transport":
        def fail(argv, **kwargs):
            if "https://github.com/example/project.git" in argv:
                raise subprocess.CalledProcessError(1, argv, stderr=b"synthetic-private-output")
            return real_run(argv, **kwargs)
        monkeypatch.setattr(subprocess, "run", fail)
    else:
        def fail_mint(*args, **kwargs):
            raise app_tokens.AuthenticationError("synthetic-private-output")
        monkeypatch.setattr(app_tokens, "mint", fail_mint)
    assert service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100) == []
    claim = claims(cfg)[0]
    assert claim["stage"] == "claimed" and claim["error"] == "target_branch_fetch_failed"
    assert "start_commit" not in claim and not Path(claim["worktree"]).exists()
    assert core.state(hub.issues[1]) == "approved" and service.jobs(cfg) == []
    assert "synthetic-private-output" not in str(claim)
    assert not list((Path(cfg["state_dir"]) / "outbox").glob("*.json"))
    cfg["base_branch"] = "target"
    monkeypatch.setattr(subprocess, "run", real_run)
    monkeypatch.setattr(app_tokens, "mint", real_mint)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert len(service.jobs(cfg)) == 1
    assert "error" not in claims(cfg)[0]


def test_next_story_fetches_after_previous_story_merge(tmp_path, monkeypatch):
    cfg, hub, runner, remote, minted, network = transport(tmp_path, monkeypatch)
    hub.issue(2)
    hub.approve(2)
    initial = command(remote, "rev-parse", "HEAD")
    starts = []

    def execute(config):
        path = next((Path(config["state_dir"]) / "tasks").glob("*.json"))
        job = runner.read(path)
        starts.append(job["base_commit"])
        assert len(network) == len(starts)  # Next fetch cannot precede this job.
        if len(starts) == 1:
            command(remote, "checkout", "-b", "previous-story")
            (remote / "merged.txt").write_text("previous story\n")
            command(remote, "add", "merged.txt")
            command(remote, "commit", "-qm", "previous story")
            command(remote, "checkout", "target")
            command(remote, "merge", "--no-ff", "-m", "merge previous story", "previous-story")
        else:
            assert (Path(config["worktree"]) / "merged.txt").read_text() == "previous story\n"
        job["status"] = "ready_for_integration"
        runner.save_job(config, path, job)

    monkeypatch.setattr(runner, "run_once", execute)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    assert starts == [initial, command(remote, "rev-parse", "target")]
    assert [c["start_commit"] for c in claims(cfg)] == starts


def test_fetch_requires_runner_app_without_environment_fallback(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    cfg.pop("app_auth", None)
    monkeypatch.setenv("RUNNER_GITHUB_TOKEN", "synthetic-owner-token")
    monkeypatch.setattr(service, "fetch_target", FETCH)
    monkeypatch.setattr(app_tokens, "mint", lambda *a, **k: pytest.fail("missing App must stop"))
    assert service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100) == []
    assert claims(cfg)[0]["error"] == "target_branch_fetch_failed"
    assert core.state(hub.issues[1]) == "approved" and not service.jobs(cfg)


def test_synthetic_fetch_fixture_passes_private_content_scan():
    from scripts.check_private_content import scan_text

    assert list(scan_text(Path(__file__).read_text())) == []


@pytest.mark.parametrize("enabled", [False, True])
def test_fetch_uses_configured_delivery_permissions(tmp_path, monkeypatch, enabled):
    cfg, hub, runner, remote, minted, network = transport(tmp_path, monkeypatch)
    settings = core.read(cfg["runner_config"])
    settings["integration"] = {"enabled": enabled, "declared_tier": "standard"}
    core.save(cfg["runner_config"], settings)
    core.private(cfg["state_dir"])
    assert FETCH(cfg) == command(remote, "rev-parse", "target")
    assert minted == [{"fetch_enabled": True, "integration_enabled": enabled}]


def test_fetch_defaults_to_main(tmp_path, monkeypatch):
    cfg, hub, runner, remote, minted, network = transport(tmp_path, monkeypatch)
    cfg.pop("base_branch")
    core.private(cfg["state_dir"])
    assert FETCH(cfg) == command(remote, "rev-parse", "main")
    assert network[0][0][-1] == "refs/heads/main"


def test_pre_fetch_dispatched_claim_retries_without_local_fallback(tmp_path, monkeypatch):
    cfg, hub, runner, remote, minted, network = transport(tmp_path, monkeypatch)
    original = service.ensure_worktree
    monkeypatch.setattr(service, "ensure_worktree", lambda *a: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    claim_path = next((Path(cfg["state_dir"]) / "claims").glob("*.json"))
    claim = core.read(claim_path)
    assert claim["stage"] == "dispatched"
    claim.pop("start_commit")  # A receipt from before authenticated fetches.
    core.save(claim_path, claim)
    monkeypatch.setattr(service, "ensure_worktree", original)
    cfg["base_branch"] = "missing"
    assert service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200) == []
    claim = core.read(claim_path)
    assert claim["error"] == "target_branch_fetch_failed"
    assert "start_commit" not in claim and not Path(claim["worktree"]).exists()
    assert not service.jobs(cfg)
    cfg["base_branch"] = "target"
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 300)
    claim = core.read(claim_path)
    assert "error" not in claim
    assert claim["start_commit"] == command(remote, "rev-parse", "target")
    assert len(service.jobs(cfg)) == 1


def test_fetch_isolates_source_config_and_inherited_credentials(tmp_path, monkeypatch):
    cfg, hub, runner, remote, minted, network = transport(tmp_path, monkeypatch)
    expected = command(remote, "rev-parse", "target")
    source = Path(cfg["repository_root"])
    command(source, "config", "url.file:///must-not-fetch/.insteadOf", "https://github.com/")
    command(source, "config", "credential.helper", "!exit 99")
    command(source, "config", "http.extraHeader", "synthetic-owner-header")
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text('[url "file:///must-not-fetch/"]\n    insteadOf = https://github.com/\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.file:///must-not-fetch/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    monkeypatch.setenv("GH_TOKEN", "synthetic-owner-token")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-git-dir"))
    monkeypatch.setenv("GIT_ASKPASS", "/must-not-run")
    core.private(cfg["state_dir"])
    assert FETCH(cfg) == expected
    assert len(network) == 1
    assert network[0][1]["env"]["GIT_CONFIG_COUNT"] == "1"
    assert network[0][1]["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
