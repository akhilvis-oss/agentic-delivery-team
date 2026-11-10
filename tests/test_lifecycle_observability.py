"""Local observability and fake alert transport; never contacts external services."""

import datetime as dt
from pathlib import Path
import plistlib
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from digest import standup
from lifecycle import core, service
from test_lifecycle import Client, Hub, ROOT, policy


class Alerts:
    def __init__(self, cfg):
        self.cfg = cfg
        self.issues = []
        self.posts = 0
        self.failure = None

    def pages(self, path):
        return self.issues

    def request(self, method, path, data):
        assert (method, path) == ("POST", "/issues")
        assert data["assignees"] == [self.cfg["actors"]["owner"]]
        self.posts += 1
        if self.failure == "rejected":
            raise core.Unavailable("rejected")
        issue = dict(
            data, number=self.posts, user={"login": self.cfg["actors"]["product"]}
        )
        if self.failure != "lost_without_create":
            self.issues.append(issue)
        if self.failure:
            raise core.Uncertain("lost response")
        return issue


def configured(tmp_path):
    cfg = policy(tmp_path)
    cfg["heartbeat_alert_seconds"] = 120
    return cfg, Alerts(cfg)


def beat(cfg, at):
    core.save(Path(cfg["state_dir"]) / "heartbeat.json", {"last_poll": at})


def test_status_uninitialized_cli_never_constructs_client_or_writes(
    tmp_path, monkeypatch, capsys
):
    cfg = policy(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("status constructed a GitHub client")

    monkeypatch.setattr(core, "GitHub", forbidden)
    assert service.main(["--config", cfg["config_path"], "status"]) == 0
    text = capsys.readouterr().out
    assert "not running" in text and "Last poll: never" in text
    assert "Next poll: unknown" in text
    assert not Path(cfg["state_dir"]).exists()


def test_poll_heartbeat_only_on_success_and_no_throttled_refresh(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    assert service.poll(cfg, hub.client(), clock=lambda: 100) == []
    assert service.heartbeat(cfg) == 100
    service.poll(cfg, hub.client(), clock=lambda: 110)
    assert service.heartbeat(cfg) == 100
    hub.fail_read = True
    with pytest.raises(core.Unavailable):
        service.poll(cfg, hub.client(), clock=lambda: 200)
    assert service.heartbeat(cfg) == 100
    hub.fail_read = False
    service.poll(cfg, hub.client(), clock=lambda: 300)
    assert service.heartbeat(cfg) == 300


def test_fresh_stale_once_recovery_and_second_outage(tmp_path):
    cfg, client = configured(tmp_path)
    beat(cfg, 100)
    assert service.watchdog(cfg, client, clock=lambda: 120)["status"] == "healthy"
    assert service.watchdog(cfg, client, clock=lambda: 221)["status"] == "alerted"
    assert (
        service.watchdog(cfg, client, clock=lambda: 500)["status"] == "already_alerted"
    )
    assert client.posts == 1
    assert core.is_report(cfg, client.issues[0])
    beat(cfg, 501)
    assert service.watchdog(cfg, client, clock=lambda: 502)["status"] == "healthy"
    assert core.read(Path(cfg["state_dir"]) / "watchdog.json") == {}
    assert service.watchdog(cfg, client, clock=lambda: 622)["status"] == "alerted"
    assert client.posts == 2


def test_disabled_and_never_polled(tmp_path):
    cfg, client = configured(tmp_path)
    threshold = cfg.pop("heartbeat_alert_seconds")
    assert service.watchdog(cfg, client)["status"] == "disabled"
    assert not Path(cfg["state_dir"]).exists()
    cfg["heartbeat_alert_seconds"] = threshold
    assert service.watchdog(cfg, client, clock=lambda: 1000)["status"] == "alerted"
    assert (
        service.watchdog(cfg, client, clock=lambda: 2000)["status"] == "already_alerted"
    )
    assert client.posts == 1


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("rejected", "alerted"),
        ("lost_response", "already_alerted"),
        ("lost_without_create", "uncertain_requires_reconciliation"),
    ],
)
def test_publication_failure_recovery(tmp_path, failure, expected):
    cfg, client = configured(tmp_path)
    client.failure = failure
    assert service.watchdog(cfg, client, clock=lambda: 500)["status"] == "unsynced"
    assert "reconciliation" in service.status(cfg)
    client.failure = None
    assert service.watchdog(cfg, client, clock=lambda: 600)["status"] == expected
    assert client.posts == (2 if failure == "rejected" else 1)


def test_concurrent_watchdogs_only_one_write(tmp_path):
    cfg, client = configured(tmp_path)
    core.bind_state(cfg)
    entered, release = threading.Event(), threading.Event()
    original = client.request

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    client.request = slow
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.watchdog, cfg, client, clock=lambda: 500)
        assert entered.wait(5)
        try:
            assert (
                service.watchdog(cfg, client, clock=lambda: 500)["status"] == "locked"
            )
        finally:
            release.set()
        assert first.result()["status"] == "alerted"
    assert client.posts == 1


def test_status_running_stale_and_reconciliation_without_changes(tmp_path):
    cfg, _ = configured(tmp_path)
    root = Path(cfg["state_dir"])
    beat(cfg, 100)
    core.save(root / "poll.json", {"next_poll": 160})
    core.save(root / "claims/a.json", dict(issue=7, job="a", stage="suspended"))
    core.save(
        root / "jobs/a/runner-state/tasks/a.json",
        dict(id="a", status="interrupted", github_number=7),
    )
    core.save(root / "outbox/a.json", dict(status="pending", record={"issue": 8}))
    core.save(root / "followups/a.json", dict(status="pending", parent_issue=9))
    with core.lock(root / "serve.lock") as acquired:
        assert acquired
        before = {
            p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
            for p in root.rglob("*")
            if p.is_file()
        }
        text = service.status(cfg, clock=lambda: 500)
        assert "Service: running" in text and "400 seconds ago" in text
        assert "1970-01-01T00:02:40+00:00" in text
        assert "Issue #7: a (suspended)" in text
        assert "Issue #7: a (interrupted)" in text
        assert "Issue #8: outbox" in text and "Issue #9: followups" in text
        after = {
            p: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
            for p in root.rglob("*")
            if p.is_file()
        }
        assert before == after
    assert "Service: not running" in service.status(cfg)


def test_digest_heartbeat_fact(tmp_path):
    cfg = policy(tmp_path)
    beat(cfg, 100)
    digest_cfg = dict(lifecycle=cfg, runner_state_dirs=[])
    sections = standup.collect(
        digest_cfg,
        Hub(cfg).client(),
        dt.datetime.fromtimestamp(0, dt.timezone.utc),
        dt.datetime.fromtimestamp(280, dt.timezone.utc),
    )
    assert any("Service last polled 3 minutes ago" in line for line in sections[4])


def observe(cfg, reader):
    if reader == "status":
        return service.status(cfg, clock=lambda: 280)
    if reader == "heartbeat":
        return service.heartbeat(cfg)
    return standup.collect(
        dict(lifecycle=cfg, runner_state_dirs=[]),
        Hub(cfg).client(),
        dt.datetime.fromtimestamp(0, dt.timezone.utc),
        dt.datetime.fromtimestamp(280, dt.timezone.utc),
    )


@pytest.mark.parametrize("reader", ["status", "heartbeat", "digest"])
@pytest.mark.parametrize("mismatch", ["repository", "actors", "identity_roles"])
def test_observation_rejects_foreign_state_without_reading_it_or_writing(
    tmp_path, monkeypatch, reader, mismatch
):
    cfg = policy(tmp_path)
    core.bind_state(cfg)
    root = Path(cfg["state_dir"])
    beat(cfg, 100)
    core.save(root / "claims/a.json", dict(issue=7, job="a", stage="suspended"))
    core.save(
        root / "jobs/a/runner-state/tasks/a.json",
        dict(id="a", status="interrupted", github_number=7),
    )
    foreign = dict(cfg)
    if mismatch == "repository":
        foreign["repository"] = "example/other"
    elif mismatch == "actors":
        foreign["actors"] = cfg["actors"] | {"runner": "other-runner-fixture"}
        foreign["identity_roles"] = dict(cfg["identity_roles"])
        foreign["identity_roles"]["other-runner-fixture"] = foreign[
            "identity_roles"
        ].pop(cfg["actors"]["runner"])
    else:
        foreign["identity_roles"] = cfg["identity_roles"] | {
            "other-runner-fixture": ["runner"]
        }
    before = {
        p: (p.read_bytes() if p.is_file() else None, p.stat().st_mode, p.stat().st_mtime_ns)
        for p in [root, *root.rglob("*")]
    }
    read = core.read

    def identity_only(path):
        assert Path(path) == root / "identity.json"
        return read(path)

    def forbidden(*args, **kwargs):
        pytest.fail("foreign state was consumed")

    with monkeypatch.context() as patch:
        patch.setattr(core, "read", identity_only)
        patch.setattr(service, "serving", forbidden)
        patch.setattr(service, "jobs", forbidden)
        patch.setattr(Client, "pages", forbidden)
        with pytest.raises(ValueError, match="another repository or actor configuration"):
            observe(foreign, reader)
    assert before == {
        p: (p.read_bytes() if p.is_file() else None, p.stat().st_mode, p.stat().st_mtime_ns)
        for p in [root, *root.rglob("*")]
    }
    # Correcting the configuration restores observation without rebinding state.
    assert observe(cfg, reader) is not None


@pytest.mark.parametrize("reader", ["status", "heartbeat", "digest"])
def test_observation_without_state_does_not_initialize_it(tmp_path, reader):
    cfg = policy(tmp_path)
    observe(cfg, reader)
    assert not Path(cfg["state_dir"]).exists()


@pytest.mark.parametrize("threshold", [True, 0, -1, 59, "120"])
def test_invalid_threshold(tmp_path, threshold):
    cfg = policy(tmp_path)
    cfg["heartbeat_alert_seconds"] = threshold
    core.save(cfg["config_path"], cfg | {})
    with pytest.raises(ValueError, match="Heartbeat alert threshold"):
        core.load_config(cfg["config_path"])


def test_launchd_templates_are_explicit_and_separate():
    for name, action in [("serve", "serve"), ("watchdog", "watchdog")]:
        with (ROOT / f"templates/lifecycle-{name}.plist.example").open("rb") as handle:
            config = plistlib.load(handle)
        assert config["ProgramArguments"][-1] == action
        assert config["RunAtLoad"] is True
        if action == "serve":
            assert config["KeepAlive"] is True
        else:
            assert config["StartInterval"] == 60 and "KeepAlive" not in config


def test_watchdog_disabled_cli_does_not_load_credentials(tmp_path, monkeypatch, capsys):
    cfg = policy(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("disabled watchdog constructed a client")

    monkeypatch.setattr(core, "GitHub", forbidden)
    assert service.main(["--config", cfg["config_path"], "watchdog"]) == 0
    assert '"disabled"' in capsys.readouterr().out
    assert not Path(cfg["state_dir"]).exists()


def test_coordinator_lifetime_lock_released_after_crash(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    monkeypatch.setattr(core, "GitHub", lambda *args: object())
    calls = []

    def loop(config, client, action):
        assert service.serving(Path(config["state_dir"]))
        with pytest.raises(core.Unavailable, match="already running"):
            service.main(["--config", cfg["config_path"], "serve"])
        calls.append(action)
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(service, "run_loop", loop)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="synthetic crash"):
            service.main(["--config", cfg["config_path"], "serve"])
        assert not service.serving(Path(cfg["state_dir"]))
    assert calls == ["serve", "serve"]


def test_untrusted_alert_marker_is_not_report(tmp_path):
    cfg = policy(tmp_path)
    assert not core.is_report(
        cfg,
        {
            "user": {"login": "untrusted-fixture"},
            "body": "<!-- agentic-service-alert-v1:fake -->",
        },
    )
