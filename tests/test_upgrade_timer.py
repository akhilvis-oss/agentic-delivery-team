"""Pending-version deadlines, with synthetic clocks and isolated process exits."""

import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from lifecycle import core, service, upgrade
from test_lifecycle import Hub, ROOT
from test_lifecycle_upgrade import NEW, OLD, configured, launch


class Exit(BaseException):
    pass


def exit_process(code):
    assert code == 70
    raise Exit


def pending(tmp_path, monkeypatch, since='100'):
    cfg, folder = configured(tmp_path, monkeypatch)
    record = upgrade.empty()
    record.update(current=OLD, pending=NEW, pending_since=since, pending_starts='1')
    upgrade.write(folder, record)
    actual = upgrade.is_pending
    monkeypatch.setattr(upgrade, 'is_pending',
                        lambda ctx: actual(ctx, folder / NEW / 'install'))
    return cfg, folder, (folder, record)


def arm(monkeypatch, ctx, now, **kwargs):
    # Keep timer execution on the test thread; the exit function never uses os._exit.
    monkeypatch.setattr(upgrade.PendingTimer, 'start', lambda self: None)
    upgrade.start_pending_timer(ctx, clock=lambda: now[0], exit_func=exit_process, **kwargs)
    return upgrade._pending_timer


def test_unconfirmed_deadline_exits_70(tmp_path, monkeypatch):
    _, _, ctx = pending(tmp_path, monkeypatch)
    now = [100]
    timer = arm(monkeypatch, ctx, now)
    assert timer.daemon
    now[0] = 700
    with pytest.raises(Exit):
        timer.watch()


@pytest.mark.parametrize('since,now,deadline', [('', 500, 1100), ('100', 650, 700)])
def test_deadline_uses_original_start_or_empty_fallback(tmp_path, monkeypatch,
                                                       since, now, deadline):
    _, _, ctx = pending(tmp_path, monkeypatch, since)
    timer = arm(monkeypatch, ctx, [now])
    assert timer.deadline == deadline


def test_only_one_timer_across_polls(tmp_path, monkeypatch):
    _, _, ctx = pending(tmp_path, monkeypatch)
    now = [100]
    timer = arm(monkeypatch, ctx, now)
    now[0] = 200
    upgrade.start_pending_timer(ctx, clock=lambda: now[0], exit_func=exit_process)
    assert upgrade._pending_timer is timer
    assert timer.deadline == 700


def test_wall_clock_rechecked_after_backward_and_forward_changes():
    now = [699.9]
    timer = upgrade.PendingTimer(700, lambda: now[0], exit_process)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        now[0] = 698 if len(waits) == 1 else 701

    timer.finished.wait = wait
    with pytest.raises(Exit):
        timer.watch()
    assert waits == [1, 1]


def test_expired_poll_exits_synchronously_before_poll_writes(tmp_path, monkeypatch):
    cfg, folder, _ = pending(tmp_path, monkeypatch)
    thread = threading.get_ident()

    def expired(ctx):
        def stop(code):
            assert threading.get_ident() == thread
            exit_process(code)
        upgrade.start_pending_timer(ctx, clock=lambda: 700, exit_func=stop)

    monkeypatch.setattr(upgrade, 'attempt', lambda *a: pytest.fail('expired poll continued'))
    with pytest.raises(Exit):
        service.poll(cfg, Hub(cfg).client(), upgrade_enabled=True, pending_hook=expired)
    assert not (Path(cfg['state_dir']) / 'poll.json').exists()
    assert upgrade.read(folder)['pending'] == NEW
    assert upgrade._pending_timer is None


def test_confirmation_cancels_after_rename_before_claim(tmp_path, monkeypatch):
    cfg, folder, ctx = pending(tmp_path, monkeypatch)
    timer = arm(monkeypatch, ctx, [100])
    replace = upgrade.os.replace
    calls = []

    def rename(source, destination):
        assert not timer.finished.is_set()
        assert upgrade.read(folder)['pending'] == NEW
        replace(source, destination)
        assert upgrade.read(folder)['current'] == NEW
        assert not timer.finished.is_set()
        calls.append('rename')

    hub = Hub(cfg)
    hub.issue(state='draft')

    def claim(*args):
        assert timer.finished.is_set()
        assert upgrade.read(folder)['current'] == NEW
        calls.append('claim')

    # Other state writes use os.replace too; observe only the switch rename.
    def switch_rename(source, destination):
        if Path(destination) == folder / 'switch':
            rename(source, destination)
        else:
            replace(source, destination)

    monkeypatch.setattr(upgrade.os, 'replace', switch_rename)
    monkeypatch.setattr(service, 'claim_event', claim)
    service.poll(cfg, hub.client(), upgrade_enabled=True)
    assert calls == ['rename', 'claim']


def test_serve_first_poll_arms_then_confirms(tmp_path, monkeypatch):
    cfg, folder, _ = pending(tmp_path, monkeypatch, '')
    start = upgrade.start_pending_timer
    timers = []

    def hook(ctx):
        start(ctx, clock=lambda: 100, exit_func=exit_process)
        timers.append(upgrade._pending_timer)

    monkeypatch.setattr(upgrade, 'start_pending_timer', hook)
    monkeypatch.setattr(upgrade.PendingTimer, 'start', lambda self: None)
    monkeypatch.setattr(service.time, 'sleep', lambda *a: (_ for _ in ()).throw(Exit()))
    with pytest.raises(Exit):
        service.run_loop(cfg, Hub(cfg).client(), 'serve')
    assert len(timers) == 1
    assert timers[0].finished.is_set()
    assert upgrade.read(folder)['current'] == NEW


def test_blocked_confirmation_write_remains_protected(tmp_path, monkeypatch):
    cfg, folder, ctx = pending(tmp_path, monkeypatch)
    now = [100]
    timer = arm(monkeypatch, ctx, now)

    def blocked_write(*args):
        assert not timer.finished.is_set()
        now[0] = 700
        timer.watch()

    monkeypatch.setattr(upgrade, 'write', blocked_write)
    with pytest.raises(Exit):
        upgrade.confirm(cfg, ctx)
    assert upgrade.read(folder)['pending'] == NEW
    assert not timer.finished.is_set()


def test_failed_confirmation_write_does_not_cancel(tmp_path, monkeypatch):
    cfg, _, ctx = pending(tmp_path, monkeypatch)
    timer = arm(monkeypatch, ctx, [100])

    def fail(*args):
        raise OSError('synthetic write failure')

    monkeypatch.setattr(upgrade, 'write', fail)
    with pytest.raises(OSError):
        upgrade.confirm(cfg, ctx)
    assert not timer.finished.is_set()


@pytest.mark.parametrize('upgrades', [False, True])
def test_direct_poll_never_arms_timer(tmp_path, monkeypatch, upgrades):
    cfg, _, _ = pending(tmp_path, monkeypatch)
    monkeypatch.setattr(upgrade, 'start_pending_timer',
                        lambda *a: pytest.fail('direct poll armed timer'), raising=False)
    service.poll(cfg, Hub(cfg).client(), upgrade_enabled=upgrades)
    assert getattr(upgrade, '_pending_timer', None) is None


@pytest.mark.parametrize('action', ['run-once', 'enqueue-only'])
def test_non_serve_never_arms_timer(tmp_path, monkeypatch, action):
    cfg, _, _ = pending(tmp_path, monkeypatch)
    monkeypatch.setattr(upgrade, 'start_pending_timer',
                        lambda *a: pytest.fail('non-serve armed timer'), raising=False)
    service.run_loop(cfg, Hub(cfg).client(), action)
    assert getattr(upgrade, '_pending_timer', None) is None


def test_non_pending_serve_never_calls_hook(tmp_path, monkeypatch):
    cfg, _ = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(upgrade, 'attempt', lambda *a: False)
    monkeypatch.setattr(upgrade, 'start_pending_timer',
                        lambda *a: pytest.fail('non-pending timer'), raising=False)
    monkeypatch.setattr(service.time, 'sleep', lambda *a: (_ for _ in ()).throw(Exit()))
    with pytest.raises(Exit):
        service.run_loop(cfg, Hub(cfg).client(), 'serve')


def test_unavailable_polls_exit_without_service_error(tmp_path, monkeypatch):
    cfg, folder, ctx = pending(tmp_path, monkeypatch)
    now = [100]
    timer = arm(monkeypatch, ctx, now)
    calls = []

    def fail(*args):
        calls.append(True)
        raise core.Unavailable('synthetic outage')

    client = Hub(cfg).client()
    monkeypatch.setattr(client, 'pages', fail)

    def sleep(*args):
        now[0] += 300
        if now[0] >= 700:
            timer.watch()

    monkeypatch.setattr(service.time, 'sleep', sleep)
    with pytest.raises(Exit):
        service.run_loop(cfg, client, 'serve')
    assert len(calls) == 2
    assert upgrade.read(folder)['pending'] == NEW
    assert not (Path(cfg['state_dir']) / 'service-error.json').exists()


@pytest.mark.parametrize('blocked_write', [False, True])
def test_sleeping_process_exits_and_launcher_rolls_back(tmp_path, blocked_write):
    script = '''
import sys, time
from pathlib import Path
import pytest
from lifecycle import service, upgrade
from test_lifecycle import Hub
from test_lifecycle_upgrade import configured, OLD, NEW
cfg, folder = configured(Path(sys.argv[1]), pytest.MonkeyPatch())
record = upgrade.empty()
record.update(current=OLD, pending=NEW, pending_since=str(int(time.time())))
upgrade.write(folder, record)
upgrade.is_pending = lambda ctx: True
start = upgrade.start_pending_timer
# Inject a short duration; no real ten-minute wait is needed.
upgrade.start_pending_timer = lambda ctx: start(ctx, limit=2)
def block(*args):
    (Path(cfg['state_dir']) / 'blocked').touch()
    time.sleep(60)
if sys.argv[2] == 'True':
    upgrade.write = block
else:
    service.publish_poll = block
service.run_loop(cfg, Hub(cfg).client(), 'serve')
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), str(blocked_write)],
                            cwd=ROOT, env={**os.environ,
                                           'PYTHONPATH': str(ROOT / 'tests') + ':' + str(ROOT)},
                            capture_output=True, timeout=10)
    assert result.returncode == 70, result.stderr.decode()
    folder = tmp_path / 'versions'
    record = upgrade.read(folder)
    assert record['pending'] == NEW
    assert (tmp_path / 'lifecycle' / 'blocked').exists()
    assert not (tmp_path / 'lifecycle' / 'service-error.json').exists()
    # Advance the launcher's deadline without changing its existing mechanism.
    record['pending_since'] = '1'
    upgrade.write(folder, record)
    (folder / OLD / 'install').mkdir(parents=True)
    assert launch(folder, tmp_path)[0] == str(folder / OLD / 'install')
    rolled_back = upgrade.read(folder)
    assert rolled_back['refused'] == NEW
    assert rolled_back['notice'] == f'Rolled back from {NEW}: did not confirm'
