"""Process-wide App cache and rejected-credential transport coverage, offline."""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import subprocess
import threading
from types import SimpleNamespace
import urllib.error

import pytest

from lifecycle import app_tokens, core, service, smoke
from test_lifecycle_app_tokens import AT, configured


def fake_tokens(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    clock, calls = [AT], []

    def mint(config, role, **options):
        calls.append((config['repository'], role, options))
        return 'synthetic-' + str(len(calls)), clock[0] + 3600

    monkeypatch.setattr(app_tokens, 'mint', mint)
    monkeypatch.setattr(app_tokens.time, 'time', lambda: clock[0])
    return cfg, clock, calls


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reuse_equal_permissions_refresh_at_five_minutes_and_separate_keys(tmp_path, monkeypatch):
    cfg, clock, calls = fake_tokens(tmp_path, monkeypatch)
    first = app_tokens.get(cfg, 'runner')
    assert app_tokens.get(cfg, 'runner', comments_only=True) == first
    clock[0] += 3299
    assert app_tokens.get(cfg, 'runner') == first and len(calls) == 1
    clock[0] += 1
    assert app_tokens.get(cfg, 'runner') != first and len(calls) == 2
    assert app_tokens.get(cfg, 'runner', fetch_enabled=True) != first
    assert app_tokens.get(cfg, 'runner', integration_enabled=True) != first
    assert app_tokens.get(cfg, 'runner', status_only=True) != first
    app_tokens.get(cfg, 'product', comments_only=True)
    cfg['app_auth']['runner']['app_id'] += 1
    app_tokens.get(cfg, 'runner')
    cfg['repository'] = 'example/other'
    app_tokens.get(cfg, 'runner')
    assert len(calls) == 8


def test_fresh_module_and_standalone_runner_share_cache_and_concurrent_mint(tmp_path, monkeypatch):
    cfg, _, calls = fake_tokens(tmp_path, monkeypatch)
    copy = load('lifecycle/app_tokens.py', 'fresh_cache_tokens')
    runner = load('runner/task_runner.py', 'fresh_cache_runner')
    token = app_tokens.get(cfg, 'runner')
    assert copy.get(cfg, 'runner') == token
    assert runner.lifecycle_api().GitHub(cfg).token == token[0]
    app_tokens.clear_cache()
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(lambda _: app_tokens.get(cfg, 'runner'), range(32)))
    assert len(set(values)) == 1 and len(calls) == 2
    assert not list(tmp_path.rglob('*token*'))


def test_old_rejection_cannot_invalidate_new_concurrent_token(tmp_path, monkeypatch):
    cfg, clock, _ = fake_tokens(tmp_path, monkeypatch)
    old = app_tokens.get(cfg, 'runner')
    clock[0] += 3300
    new = app_tokens.get(cfg, 'runner')
    app_tokens.invalidate(old[0])
    assert app_tokens.get(cfg, 'runner') == new


def identity_response(cfg):
    return io.BytesIO(json.dumps({'data': {'viewer': {'login': cfg['actors']['runner']}}}).encode())


@pytest.mark.parametrize('transport', ['identity', 'rest', 'graphql', 'smoke_read', 'smoke_delete',
                                       'integration_status', 'status', 'preflight_scope'])
def test_401_drops_shared_token_and_same_client_refreshes_next_request(tmp_path, monkeypatch, transport):
    cfg, _, calls = fake_tokens(tmp_path, monkeypatch)
    integration = load('runner/integration.py', 'fresh_cache_integration')
    if transport == 'graphql':
        client = integration.client_for(core, cfg)
    elif transport in {'smoke_read', 'smoke_delete'}:
        client = smoke.SmokeGitHub(cfg)
    elif transport == 'integration_status':
        # Capture the real scoped client created by the standalone runner path.
        original = core.app_client
        captured = []

        def scoped(*args, **kwargs):
            result = original(*args, **kwargs)
            captured.append(result)
            return result

        monkeypatch.setattr(core, 'app_client', scoped)
        monkeypatch.setattr(core.GitHub, 'pages', lambda self, path: [])
        integration.client_for(core, cfg).smoke_statuses('a' * 40)
        client, = captured
    elif transport == 'status':
        client = smoke.StatusGitHub(cfg)
    elif transport == 'preflight_scope':
        client = core.app_client(cfg, comments_only=True)
    else:
        client = core.GitHub(cfg)
    client.identity_verified = transport != 'identity'
    before = client.token

    def unauthorized(*args, **kwargs):
        raise urllib.error.HTTPError('https://api.github.com', 401, 'fixture', {}, None)

    client.opener = SimpleNamespace(open=unauthorized)
    with pytest.raises(core.Unavailable):
        if transport == 'identity':
            client.ensure_actor()
        elif transport == 'graphql':
            client.enable_auto_merge({'node_id': 'fixture'})
        elif transport == 'smoke_read':
            client.guard_runs(timeout=1)
        elif transport == 'smoke_delete':
            client.request('DELETE', '/git/refs/heads/runner/issue-1-event-1')
        else:
            client.request('GET', '/issues/1')
    assert client.token_expiry == 0
    assert all(value[0] != before for value in app_tokens._cache.values())

    def success(req, **kwargs):
        if req.data and b'viewer' in req.data:
            return identity_response(cfg)
        return io.BytesIO(b'{"data": {"enablePullRequestAutoMerge": {"pullRequest": {"id": "fixture"}}}}')

    client.opener = SimpleNamespace(open=success)
    if transport == 'smoke_read':
        client.guard_runs(timeout=1)
    elif transport == 'graphql':
        client.enable_auto_merge({'node_id': 'fixture'})
    elif transport == 'smoke_delete':
        client.request('DELETE', '/git/refs/heads/runner/issue-1-event-1')
    else:
        client.request('GET', '/issues/1')
    assert client.token != before and client.identity_verified
    assert calls


def test_authenticated_fetch_failure_invalidates_cached_token(tmp_path, monkeypatch):
    from test_lifecycle_fetch import transport
    cfg, _, _, _, _, _ = transport(tmp_path, monkeypatch)
    core.private(cfg['state_dir'])
    app_tokens.get(cfg, 'runner', fetch_enabled=True, integration_enabled=False)
    assert app_tokens._cache
    original = subprocess.run

    def fail(argv, **kwargs):
        if 'https://github.com/example/project.git' in argv:
            raise subprocess.CalledProcessError(1, argv, stderr=b'private output')
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(core.Unavailable):
        service.fetch_target(cfg)
    assert not app_tokens._cache


def test_authenticated_push_failure_invalidates_cache_and_client(tmp_path, monkeypatch):
    cfg, _, _ = fake_tokens(tmp_path, monkeypatch)
    integration = load('runner/integration.py', 'push_cache_integration')
    client = integration.client_for(core, cfg)
    client.identity_verified = True
    token = client.token
    runner = SimpleNamespace(verification_copy=lambda config, head, folder: folder)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ['git'], stderr=b'private output')

    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        client.push(runner, {'state_dir': str(tmp_path), 'branch': 'fixture'}, 'a' * 40)
    assert client.token_expiry == 0
    assert all(value[0] != token for value in app_tokens._cache.values())


@pytest.mark.parametrize('explicit_at', [False, True])
def test_waiter_uses_clock_after_cache_lock_but_preserves_explicit_at(
        tmp_path, monkeypatch, explicit_at):
    cfg, clock, calls = fake_tokens(tmp_path, monkeypatch)
    first = app_tokens.get(cfg, 'runner')
    clock[0] = first[1] - 301
    requested_at = clock[0]
    lock = app_tokens._cache_lock
    waiting = threading.Event()

    class ObservedLock:
        def __enter__(self):
            waiting.set()
            lock.acquire()

        def __exit__(self, *exc):
            lock.release()

    monkeypatch.setattr(app_tokens, '_cache_lock', ObservedLock())
    options = {'at': requested_at} if explicit_at else {}
    # The waiter reaches acquisition before the boundary, then acquires at it.
    with ThreadPoolExecutor(max_workers=1) as executor:
        with lock:
            future = executor.submit(app_tokens.get, cfg, 'runner', **options)
            reached_lock = waiting.wait(2)
            clock[0] = first[1] - 300
        value = future.result(timeout=2)
    assert reached_lock
    assert len(calls) == (1 if explicit_at else 2)
    assert (value == first) is explicit_at
