"""Adopter upgrades use public reads and a poll-clock request budget."""

import io
import json
from pathlib import Path
import urllib.error

import pytest

from lifecycle import app_tokens, core, service, upgrade
from test_lifecycle import Hub
from test_lifecycle_upgrade import OLD, NEW, Remote, configured, transport

FRAMEWORK = 'example/framework'
ACTOR = 'framework-runner[bot]'


def response(data, *, has_next=False):
    value = io.BytesIO(json.dumps(data).encode())
    value.headers = {'Link': '<https://api.github.com/ignored>; rel="next"'} if has_next else {}
    return value


def adopter(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    cfg['upgrade'].update(framework_repository=FRAMEWORK, framework_runner=ACTOR)
    return cfg, folder


@pytest.mark.parametrize('field,value', [
    ('framework_repository', None), ('framework_runner', None),
    ('framework_repository', 'bad'), ('framework_repository', '../framework'),
    ('framework_repository', 'https://example.test/repo'),
    ('framework_repository', 123), ('framework_runner', ''),
    ('framework_runner', 'bad/login'), ('framework_runner', 123),
])
def test_public_settings_pair_and_validation(tmp_path, monkeypatch, field, value):
    cfg, _ = adopter(tmp_path, monkeypatch)
    upgrade.validate(cfg['upgrade'])
    core.save(cfg['config_path'], cfg)
    assert core.load_config(cfg['config_path'])['upgrade'] == cfg['upgrade']
    if value is None:
        del cfg['upgrade'][field]
    else:
        cfg['upgrade'][field] = value
    with pytest.raises(ValueError, match='upgrade'):
        upgrade.validate(cfg['upgrade'])


@pytest.mark.parametrize('repository', [FRAMEWORK, 'example/project'])
def test_public_client_has_no_token_and_only_get(monkeypatch, repository):
    monkeypatch.setenv('RUNNER_GITHUB_TOKEN', 'must-not-send')
    monkeypatch.setenv('PRODUCT_GITHUB_TOKEN', 'must-not-send')
    client = core.PublicGitHub(repository)
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 30
            return response({'sha': 'synthetic'})

    client.opener = Opener()
    assert client.request('GET', '/commits/main') == {'sha': 'synthetic'}
    request = requests[0]
    assert request.full_url == f'https://api.github.com/repos/{repository}/commits/main'
    assert request.get_method() == 'GET'
    assert request.data is None
    assert not any(k.lower() == 'authorization' for k in request.headers)
    for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'get'):
        with pytest.raises(ValueError):
            client.request(method, '/commits/main')
    for path in ('//elsewhere', '/../../secret', '/commits/main#fragment',
                 '/commits/main?token=secret', 'https://example.test', '/issues'):
        with pytest.raises(ValueError):
            client.request('GET', path)
    with pytest.raises(ValueError):
        client.request('GET', '/commits/main', {'write': True})
    assert len(requests) == 1


@pytest.mark.parametrize('count', [0, 1, 100, 101, 200, 201])
def test_public_status_pagination_and_request_bound(monkeypatch, count):
    client = core.PublicGitHub(FRAMEWORK)
    requests = []
    rows = [{'id': n, 'context': 'smoke/example-agent', 'state': 'success',
             'creator': {'login': ACTOR}} for n in range(count)]

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            page = int(request.full_url.rsplit('=', 1)[1])
            batch = rows[(page - 1) * 100:page * 100]
            return response(batch, has_next=page * 100 < count)

    client.opener = Opener()
    assert client.pages(f'/commits/{NEW}/statuses') == rows
    assert len(requests) == max(1, (count + 99) // 100)
    assert all(r.get_method() == 'GET' and r.get_header('Authorization') is None
               for r in requests)


def public_transport(monkeypatch, cfg):
    remote = Remote()
    remote.statuses = [{'context': 'smoke/example-agent', 'state': 'success',
                        'creator': {'login': ACTOR}}]
    repositories = []

    def client(repository):
        repositories.append(repository)
        return remote

    def forbidden(*args, **kwargs):
        pytest.fail('adopter used authenticated GitHub or minted a token')

    monkeypatch.setattr(core, 'PublicGitHub', client)
    monkeypatch.setattr(core, 'GitHub', forbidden)
    monkeypatch.setattr(app_tokens, 'mint', forbidden)
    return remote, repositories


def public_exporter(monkeypatch):
    exports = []

    def export(settings, commit, root, env):
        exports.append((settings, commit))
        (root / 'download').mkdir()
        (root / 'install').mkdir()
        return root / 'install', commit

    monkeypatch.setattr(upgrade.smoke, 'export_candidate', export)
    monkeypatch.setattr(upgrade, 'start_check', lambda *args: 0)
    return exports


def test_adopter_exports_framework_and_ignores_other_creator(tmp_path, monkeypatch):
    cfg, folder = adopter(tmp_path, monkeypatch)
    remote, repositories = public_transport(monkeypatch, cfg)
    exports = public_exporter(monkeypatch)
    remote.statuses[0]['creator']['login'] = cfg['actors']['runner']
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert not exports
    assert not any('/compare/' in p for p in remote.calls)
    remote.statuses.append({'context': 'smoke/example-agent', 'state': 'success',
                            'creator': {'login': ACTOR}})
    assert upgrade.attempt(cfg, upgrade.context(cfg))
    assert repositories == [FRAMEWORK, FRAMEWORK]
    assert exports == [({'framework_repository': FRAMEWORK}, NEW)]
    assert upgrade.read(folder)['pending'] == NEW
    assert remote.calls[-3:] == ['/commits/main', f'/commits/{NEW}/statuses',
                                 f'/compare/{OLD}...{NEW}']


@pytest.mark.parametrize('code', [403, 429])
@pytest.mark.parametrize('stage', ['main', 'statuses', 'compare'])
def test_public_rate_limit_is_unavailable_and_retriable(tmp_path, monkeypatch, code, stage):
    cfg, folder = adopter(tmp_path, monkeypatch)
    client = core.PublicGitHub(FRAMEWORK)
    calls = []
    limited = [True]

    class Opener:
        def open(self, request, timeout):
            url = request.full_url
            calls.append(url)
            target = ('/commits/main' if stage == 'main' else
                      '/statuses' if stage == 'statuses' else '/compare/')
            if limited[0] and target in url:
                raise urllib.error.HTTPError(url, code, 'rate limit', {}, None)
            if '/commits/main' in url:
                data = {'sha': NEW}
            elif '/statuses' in url:
                data = [{'id': 1, 'context': 'smoke/example-agent', 'state': 'success',
                         'creator': {'login': ACTOR}}]
            else:
                data = {'status': 'ahead'}
            return response(data)

    client.opener = Opener()
    monkeypatch.setattr(core, 'PublicGitHub', lambda repository: client)
    exports = public_exporter(monkeypatch)
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert not upgrade.read(folder)['refused'] and not exports
    limited[0] = False
    assert upgrade.attempt(cfg, upgrade.context(cfg))
    assert exports == [({'framework_repository': FRAMEWORK}, NEW)]
    assert len(calls) <= 6


@pytest.mark.parametrize('public', [False, True])
@pytest.mark.parametrize('result', ['same', 'outage', 'status'])
def test_upgrade_checks_spaced_by_poll_clock(tmp_path, monkeypatch, public, result):
    cfg, _ = adopter(tmp_path, monkeypatch) if public else configured(tmp_path, monkeypatch)
    cfg['poll_seconds'] = 1
    hub = Hub(cfg)
    remote, _ = public_transport(monkeypatch, cfg) if public else transport(monkeypatch, cfg)
    if result == 'same':
        remote.commit = OLD
    elif result == 'outage':
        remote.failure = core.Unavailable('outage')
    else:
        remote.statuses = []
    for at, expected in ((100, 1), (200, 1), (699, 1), (700, 2), (800, 2), (1300, 3)):
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: at, upgrade_enabled=True)
        assert remote.calls.count('/commits/main') == expected
    # A new process reconstructs config, so the in-memory interval starts afresh.
    cfg.pop('_upgrade_last_check')
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 1400, upgrade_enabled=True)
    assert remote.calls.count('/commits/main') == 4


@pytest.mark.parametrize('claim', [dict(stage='claimed'), dict(stage='suspended'),
                                  dict(stage='finished', error='reconcile'),
                                  dict(stage='finished', cleanup_pending='reconcile'), None])
def test_local_skip_does_not_start_check_interval(tmp_path, monkeypatch, claim):
    cfg, folder = adopter(tmp_path, monkeypatch)
    hub = Hub(cfg)
    remote, _ = public_transport(monkeypatch, cfg)
    remote.commit = OLD
    # Keep this poll focused on upgrade scheduling, without claim reconciliation.
    monkeypatch.setattr(service, 'flush_outbox', lambda *args: (_ for _ in ()).throw(
        core.Unavailable('stop after upgrade scheduling')))
    path = Path(cfg['state_dir']) / 'claims' / 'one.json'
    if claim is not None:
        core.save(path, claim)
    else:
        record = upgrade.empty()
        record['pending'] = NEW
        upgrade.write(folder, record)
    with pytest.raises(core.Unavailable):
        service.poll(cfg, hub.client(), clock=lambda: 100, upgrade_enabled=True)
    assert not remote.calls
    path.unlink(missing_ok=True)
    upgrade.write(folder, upgrade.empty())
    with pytest.raises(core.Unavailable):
        service.poll(cfg, hub.client(), clock=lambda: 200, upgrade_enabled=True)
    assert remote.calls == ['/commits/main']
    with pytest.raises(core.Unavailable):
        service.poll(cfg, hub.client(), clock=lambda: 300, upgrade_enabled=True)
    assert remote.calls == ['/commits/main']


def test_interval_starts_at_read_not_preflight_or_completion(tmp_path, monkeypatch):
    cfg, _ = adopter(tmp_path, monkeypatch)
    cfg['poll_seconds'] = 1
    hub = Hub(cfg)
    now = [100]
    reads = []

    class RemoteClock:
        def request(self, method, path):
            reads.append(now[0])
            now[0] += 200
            return {'sha': OLD}

    def client(repository):
        now[0] += 20
        return RemoteClock()

    monkeypatch.setattr(core, 'PublicGitHub', client)
    service.poll(cfg, hub.client(), clock=lambda: now[0], upgrade_enabled=True)
    assert reads == [120]
    now[0] = 719
    service.poll(cfg, hub.client(), clock=lambda: now[0], upgrade_enabled=True)
    assert reads == [120]
    now[0] = 720
    service.poll(cfg, hub.client(), clock=lambda: now[0], upgrade_enabled=True)
    assert reads == [120, 740]


@pytest.mark.parametrize('rows', [[{'id': 1}, {'id': 1}], [{}], {'not': 'a list'}])
def test_public_pagination_rejects_incomplete_collections(rows):
    client = core.PublicGitHub(FRAMEWORK)

    class Opener:
        def open(self, request, timeout):
            return response(rows)

    client.opener = Opener()
    with pytest.raises(core.Unavailable):
        client.pages(f'/commits/{NEW}/statuses')
