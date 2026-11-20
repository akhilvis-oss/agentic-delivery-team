"""Owned process-group cleanup: native zombie evidence and bounded EPERM recovery."""

import errno
import importlib.util
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("task_runner_stop_group", ROOT / "runner/task_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        assert 0 < seconds <= 0.05
        self.sleeps.append(seconds)
        self.elapsed += seconds


class Process:
    pid = 424242

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.waits = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        assert self.returncode is not None, "A live root must not enter the EPERM retry"
        return self.returncode


@pytest.mark.parametrize("signal_stage", [signal.SIGTERM, signal.SIGKILL])
def test_exited_owned_group_eperm_retries_until_gone(monkeypatch, signal_stage):
    proc = Process()
    clock = Clock()
    attempts = []

    def killpg(pid, sig):
        assert pid == proc.pid
        attempts.append(sig)
        if sig != signal_stage:
            return
        stage_calls = attempts.count(sig)
        if stage_calls <= 2:
            raise PermissionError(errno.EPERM, "synthetic exited group")
        assert proc.waits, "Root must be waited for before retrying"
        raise ProcessLookupError(errno.ESRCH, "synthetic reaped group")

    monkeypatch.setattr(runner.os, "killpg", killpg)
    runner.stop_group(proc, clock=clock.monotonic, sleep=clock.sleep)
    assert attempts.count(signal_stage) == 3
    assert clock.elapsed == pytest.approx(0.1)
    assert proc.waits[-1] is None


@pytest.mark.parametrize("signal_stage", [signal.SIGTERM, signal.SIGKILL])
def test_persistent_eperm_raises_at_two_second_budget(monkeypatch, signal_stage):
    proc = Process()
    clock = Clock()
    seen = []

    def killpg(pid, sig):
        assert pid == proc.pid
        seen.append(sig)
        if sig == signal_stage:
            raise PermissionError(errno.EPERM, "synthetic persistent permission failure")

    monkeypatch.setattr(runner.os, "killpg", killpg)
    with pytest.raises(PermissionError) as stopped:
        runner.stop_group(proc, clock=clock.monotonic, sleep=clock.sleep)
    assert stopped.value.errno == errno.EPERM
    assert clock.elapsed == pytest.approx(2)
    assert seen.count(signal_stage) > 1
    assert max(clock.sleeps) <= 0.05


def test_term_and_kill_share_one_eperm_budget(monkeypatch):
    proc = Process()
    clock = Clock()
    term_attempts = 0
    seen = []

    def killpg(pid, sig):
        nonlocal term_attempts
        assert pid == proc.pid
        seen.append(sig)
        if sig == signal.SIGTERM:
            term_attempts += 1
            if term_attempts > 10:
                return
        raise PermissionError(errno.EPERM, "synthetic delayed reap")

    monkeypatch.setattr(runner.os, "killpg", killpg)
    with pytest.raises(PermissionError):
        runner.stop_group(proc, clock=clock.monotonic, sleep=clock.sleep)
    assert signal.SIGKILL in seen
    assert clock.elapsed == pytest.approx(2)
    assert term_attempts == 11


@pytest.mark.parametrize("number", [errno.EPERM, errno.EACCES])
def test_live_root_permission_error_is_not_retried(monkeypatch, number):
    proc = Process(returncode=None)
    clock = Clock()
    seen = []

    def killpg(pid, sig):
        assert pid == proc.pid
        seen.append(sig)
        raise PermissionError(number, "synthetic live root")

    monkeypatch.setattr(runner.os, "killpg", killpg)
    with pytest.raises(PermissionError) as stopped:
        runner.stop_group(proc, clock=clock.monotonic, sleep=clock.sleep)
    assert stopped.value.errno == number
    assert seen == [signal.SIGTERM]
    assert not clock.sleeps
    assert not proc.waits


def test_exited_root_other_permission_error_is_not_retried(monkeypatch):
    proc = Process()
    clock = Clock()
    seen = []

    def killpg(pid, sig):
        assert pid == proc.pid
        seen.append(sig)
        raise PermissionError(errno.EACCES, "synthetic other error")

    monkeypatch.setattr(runner.os, "killpg", killpg)
    with pytest.raises(PermissionError) as stopped:
        runner.stop_group(proc, clock=clock.monotonic, sleep=clock.sleep)
    assert stopped.value.errno == errno.EACCES
    assert seen == [signal.SIGTERM]
    assert not clock.sleeps
    assert not proc.waits


def test_missing_owned_group_is_already_stopped(monkeypatch):
    proc = Process()
    seen = []

    def killpg(pid, sig):
        assert pid == proc.pid
        seen.append(sig)
        raise ProcessLookupError(errno.ESRCH, "synthetic absent group")

    monkeypatch.setattr(runner.os, "killpg", killpg)
    runner.stop_group(proc)
    assert seen == [signal.SIGTERM]
    assert proc.waits == [None]


def test_normal_cleanup_signals_only_owned_group_then_waits(monkeypatch):
    proc = Process()
    seen = []
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: seen.append((pid, sig)))
    runner.stop_group(proc)
    assert seen == [(proc.pid, signal.SIGTERM), (proc.pid, signal.SIGKILL)]
    assert proc.waits == [0.5, 2, None]


def test_retry_defaults_are_independent_of_runner_test_clock(monkeypatch):
    proc = Process()
    seen = []

    def killpg(pid, sig):
        assert pid == proc.pid
        seen.append(sig)
        if len(seen) == 1:
            raise PermissionError(errno.EPERM, "synthetic zombie")
        raise ProcessLookupError(errno.ESRCH, "synthetic reaped group")

    def forbidden(*args):
        raise AssertionError("Retry must not use the phase readiness clock")

    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=forbidden, sleep=forbidden))
    runner.stop_group(proc)
    assert seen == [signal.SIGTERM, signal.SIGTERM]


@pytest.mark.skipif(sys.platform != "darwin", reason="Native macOS zombie-group errno evidence")
def test_native_zombie_only_group_is_reaped_and_stopped():
    # kqueue observes exit without poll()/wait(), preserving a deterministic zombie.
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    queue = select.kqueue()
    try:
        watch = select.kevent(proc.pid, filter=select.KQ_FILTER_PROC,
                              flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                              fflags=select.KQ_NOTE_EXIT)
        queue.control([watch], 0, 0)
        proc.stdin.write(b"x")
        proc.stdin.flush()
        events = queue.control(None, 1, 5)
        assert events and events[0].ident == proc.pid
        assert events[0].fflags & select.KQ_NOTE_EXIT
        # Only this fixture's newly created group is ever signalled.
        with pytest.raises(PermissionError) as before_reap:
            os.killpg(proc.pid, signal.SIGTERM)
        assert before_reap.value.errno == errno.EPERM
        runner.stop_group(proc)
        assert proc.returncode == 0
        with pytest.raises(ProcessLookupError) as after_reap:
            os.killpg(proc.pid, 0)
        assert after_reap.value.errno == errno.ESRCH
    finally:
        queue.close()
        proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
