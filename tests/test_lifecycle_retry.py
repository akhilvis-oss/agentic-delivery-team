"""Loop failure and restart evidence using synthetic transport and virtual time."""

from pathlib import Path

import pytest

from digest import standup
from lifecycle import core, service
from test_lifecycle import Hub, policy


class StopLoop(BaseException):
    pass


def simulation(tmp_path, monkeypatch, outcomes):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    client = hub.client()
    at = [1000]
    sleeps, bodies = [], []
    original_poll = service.poll
    count = len(outcomes)
    outcomes = iter(outcomes)

    def poll(config, transport, **kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "skip":
            return []
        return original_poll(config, transport, clock=lambda: at[0], **kwargs)

    def sleep(delay):
        sleeps.append(delay)
        bodies.append([c["body"] for comments in hub.comments_by_issue.values()
                       for c in comments])
        at[0] += delay
        if len(sleeps) == count:
            raise StopLoop

    monkeypatch.setattr(service, "poll", poll)
    monkeypatch.setattr(service.time, "time", lambda: at[0])
    monkeypatch.setattr(service.time, "sleep", sleep)
    return cfg, client, sleeps, bodies, at


@pytest.mark.parametrize("error", [core.Unavailable, core.Uncertain])
def test_retry_backoff_caps_resets_and_recovery_is_public_safe(
    tmp_path, monkeypatch, error
):
    secret = "synthetic private message https://example.invalid/request?credential=hidden"
    cfg, client, sleeps, bodies, _ = simulation(
        tmp_path, monkeypatch, [error(secret) for _ in range(6)]
        + [None, error(secret), None, None]
    )
    with pytest.raises(StopLoop):
        service.run_loop(cfg, client, "serve")
    assert sleeps == [60, 120, 240, 480, 600, 600, 60, 60, 60, 60]
    line = bodies[6][0]
    assert "Polls first failed: " + service.timestamp(1000) in line
    assert "recovered: " + service.timestamp(3100) in line
    assert "failed polls: 6" in line
    assert "exception class: " + error.__name__ in line
    assert "failed polls: 1" in bodies[8][0]
    assert "Polls first failed" not in bodies[9][0]
    assert all(secret not in body and "https://" not in body and "hidden" not in body
               for snapshot in bodies for body in snapshot)
    assert not (Path(cfg["state_dir"]) / "poll-failure.json").exists()


def test_retry_records_time_and_class_without_exception_text(tmp_path, monkeypatch):
    cfg, client, _, _, _ = simulation(
        tmp_path, monkeypatch, [core.Uncertain("private response")]
    )
    with pytest.raises(StopLoop):
        service.run_loop(cfg, client, "serve")
    path = Path(cfg["state_dir"]) / "poll-failure.json"
    assert core.read(path) == dict(first_failed=1000, last_failed=1000, failures=1,
                                    exception_class="Uncertain")
    assert path.stat().st_mode & 0o777 == 0o600


def test_skipped_poll_does_not_consume_recovery(tmp_path, monkeypatch):
    cfg, client, _, bodies, _ = simulation(
        tmp_path, monkeypatch, [core.Unavailable("private"), "skip", None, None]
    )
    with pytest.raises(StopLoop):
        service.run_loop(cfg, client, "serve")
    assert bodies[1] == []
    assert "Polls first failed: " + service.timestamp(1000) in bodies[2][0]
    assert "recovered: " + service.timestamp(1120) in bodies[2][0]
    assert "failed polls: 1" in bodies[2][0]
    assert "Polls first failed" not in bodies[3][0]


def test_failed_notice_write_keeps_restart_receipt_for_retry(tmp_path, monkeypatch):
    cfg, client, _, bodies, _ = simulation(tmp_path, monkeypatch, [None, None, None])
    path = Path(cfg["state_dir"]) / "service-error.json"
    core.save(path, dict(exception_class="ValueError", function="poll"))
    original_request = client.request
    rejected = []
    published = []

    def request(method, route, data=None):
        if (method == "PATCH" and "Service restarted" in data["body"]
                and not rejected):
            assert path.exists()
            rejected.append(True)
            raise core.Uncertain("synthetic private response")
        if (method == "PATCH" and "Service restarted" in data["body"]
                and not published):
            assert path.exists()
            published.append(True)
        return original_request(method, route, data)

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(StopLoop):
        service.run_loop(cfg, client, "serve")
    assert rejected == [True]
    assert "Service restarted" not in bodies[0][0]
    assert "Service restarted after ValueError in poll." in bodies[1][0]
    assert "exception class: Uncertain" in bodies[1][0]
    assert "Service restarted" not in bodies[2][0]
    assert not path.exists()


@pytest.mark.parametrize("error", [core.Unavailable, core.Uncertain])
def test_standup_failure_is_retried(tmp_path, monkeypatch, error):
    cfg, client, sleeps, bodies, _ = simulation(
        tmp_path, monkeypatch, [None, None, None]
    )
    cfg["digest_config"] = "synthetic-config"
    monkeypatch.setattr(standup, "load_config", lambda _: {"lifecycle": cfg})
    monkeypatch.setattr(core, "GitHub", lambda *args: client)
    calls = []

    def run_once(*args):
        calls.append(True)
        if len(calls) < 3:
            raise error("private stand-up response")

    monkeypatch.setattr(standup, "run_once", run_once)
    with pytest.raises(StopLoop):
        service.run_loop(cfg, client, "serve")
    assert len(calls) == 3 and sleeps == [60, 120, 60]
    assert "failed polls: 2" in bodies[2][0]


def test_unexpected_error_exits_and_next_start_reports_then_clears(
    tmp_path, monkeypatch
):
    cfg = policy(tmp_path)
    client = Hub(cfg).client()

    def broken(*args, **kwargs):
        service.continuation_commit(None)

    with monkeypatch.context() as patch:
        patch.setattr(core, "GitHub", lambda *args: client)
        patch.setattr(service, "poll", broken)
        # Uncaught exceptions from main are nonzero CLI exits.
        with pytest.raises(AttributeError):
            service.main(["--config", cfg["config_path"], "serve"])
    path = Path(cfg["state_dir"]) / "service-error.json"
    assert core.read(path) == dict(exception_class="AttributeError",
                                    function="continuation_commit")
    assert path.stat().st_mode & 0o777 == 0o600
    cfg, client, _, bodies, _ = simulation(tmp_path, monkeypatch, [None, None])
    with pytest.raises(StopLoop):
        service.run_loop(cfg, client, "serve")
    assert "Service restarted after AttributeError in continuation_commit." in bodies[0][0]
    assert "Service restarted" not in bodies[1][0]
    assert not path.exists()


@pytest.mark.parametrize("action", ["run-once", "enqueue-only"])
@pytest.mark.parametrize("error", [core.Unavailable, core.Uncertain, ValueError])
def test_one_shot_actions_still_exit_on_first_error(tmp_path, monkeypatch, action, error):
    cfg = policy(tmp_path)
    calls = []

    def broken(*args, **kwargs):
        calls.append(True)
        raise error("synthetic failure")

    monkeypatch.setattr(service, "poll", broken)
    monkeypatch.setattr(core, "GitHub", lambda *args: object())
    with pytest.raises(error):
        service.main(["--config", cfg["config_path"], action])
    assert calls == [True]
    assert not (Path(cfg["state_dir"]) / "service-error.json").exists()
