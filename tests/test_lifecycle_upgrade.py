"""Upgrade boundaries with synthetic clients, exports and temporary installs only."""

import os
from pathlib import Path
import plistlib
import subprocess
import time

import pytest

from lifecycle import app_tokens, core, service, upgrade
from test_lifecycle import Hub, ROOT, policy

OLD = 'a' * 40
NEW = 'b' * 40
PREVIOUS = 'c' * 40


def configured(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    folder = tmp_path / 'versions'
    folder.mkdir()
    cfg['upgrade'] = dict(track='main', smoke_agent='example-agent',
                          versions_dir=str(folder), initial_commit=OLD)
    monkeypatch.setenv('ADT_VERSIONS_DIR', str(folder))
    return cfg, folder


class Remote:
    def __init__(self):
        self.statuses = [{'context': 'smoke/example-agent', 'state': 'success',
                          'creator': {'login': 'runner-fixture'}}]
        self.comparison = 'ahead'
        self.calls = []
        self.commit = NEW
        self.failure = None

    def request(self, method, path):
        self.calls.append(path)
        if self.failure:
            raise self.failure
        if path == '/commits/main':
            return {'sha': self.commit}
        assert path == f'/compare/{OLD}...{self.commit}'
        return {'status': self.comparison}

    def pages(self, path):
        self.calls.append(path)
        assert path == f'/commits/{self.commit}/statuses'
        return self.statuses


def transport(monkeypatch, cfg):
    remote = Remote()
    roles = []

    def client(config, role='runner', token=None):
        assert config is cfg
        roles.append((role, token))
        return remote

    def mint(config, role, **kwargs):
        assert config is cfg and role == 'runner' and kwargs == {'status_only': True}
        return 'synthetic-status-token', 999

    monkeypatch.setattr(core, 'GitHub', client)
    monkeypatch.setattr(app_tokens, 'get', mint)
    return remote, roles


def exporter(monkeypatch, code=0, failure=None):
    exports = []

    def export(cfg, commit, root, env):
        assert cfg == {'framework_repository': 'example/project'}
        assert env['GIT_CONFIG_GLOBAL'] == os.devnull
        assert 'RUNNER_GITHUB_TOKEN' not in env
        exports.append(root)
        (root / 'download').mkdir()
        (root / 'install').mkdir()
        if failure:
            raise failure
        return root / 'install', commit

    def check(install, cfg):
        assert install.is_dir() and not (install.parent / 'download').exists()
        if isinstance(code, Exception):
            raise code
        return code

    monkeypatch.setattr(upgrade.smoke, 'export_candidate', export)
    monkeypatch.setattr(upgrade, 'start_check', check)
    return exports


@pytest.mark.parametrize('claim', [dict(stage='claimed'), dict(stage='suspended'),
                                  dict(stage='finished', error='reconcile'),
                                  dict(stage='finished', cleanup_pending='reconcile')])
def test_upgrade_waits_for_claims(tmp_path, monkeypatch, claim):
    cfg, folder = configured(tmp_path, monkeypatch)
    remote, _ = transport(monkeypatch, cfg)
    core.save(Path(cfg['state_dir']) / 'claims' / 'one.json', claim)
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert remote.calls == [] and not (folder / 'switch').exists()


def test_pending_and_refused_are_not_retried(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    remote, _ = transport(monkeypatch, cfg)
    record = upgrade.empty()
    record['pending'] = NEW
    upgrade.write(folder, record)
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert remote.calls == []
    record.update(pending='', refused=NEW)
    upgrade.write(folder, record)
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert remote.calls == ['/commits/main']


@pytest.mark.parametrize('statuses', [[],
    [{'context': 'smoke/example-agent', 'state': 'success', 'creator': {'login': 'impostor'}}],
    [{'context': 'smoke/other', 'state': 'success', 'creator': {'login': 'runner-fixture'}}],
    [{'context': 'smoke/example-agent', 'state': 'failure', 'creator': {'login': 'runner-fixture'}},
     {'context': 'smoke/example-agent', 'state': 'success', 'creator': {'login': 'runner-fixture'}}]])
def test_only_latest_matching_runner_success_upgrades(tmp_path, monkeypatch, statuses):
    cfg, folder = configured(tmp_path, monkeypatch)
    remote, _ = transport(monkeypatch, cfg)
    remote.statuses = statuses
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert not (folder / NEW).exists()
    assert not any('/compare/' in p for p in remote.calls)


@pytest.mark.parametrize('comparison', ['identical', 'behind', 'diverged', None])
def test_candidate_must_be_ahead(tmp_path, monkeypatch, comparison):
    cfg, folder = configured(tmp_path, monkeypatch)
    remote, _ = transport(monkeypatch, cfg)
    remote.comparison = comparison
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert not (folder / NEW).exists()


def test_status_only_and_product_scopes_and_retry_export(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    remote, roles = transport(monkeypatch, cfg)
    exports = exporter(monkeypatch, code=75)
    for _ in range(2):
        assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert exports == [folder / NEW, folder / NEW]
    assert roles == [('product', None), ('runner', 'synthetic-status-token')] * 2
    assert not upgrade.read(folder)['refused']
    remote.statuses.insert(0, {'context': 'smoke/example-agent', 'state': 'failure',
                               'creator': {'login': 'someone-else'}})
    exporter(monkeypatch, code=0)
    assert upgrade.attempt(cfg, upgrade.context(cfg))
    assert upgrade.read(folder)['pending'] == NEW
    assert upgrade.read(folder)['current'] == ''


@pytest.mark.parametrize('code,refused', [(1, True), (-9, True), (75, False),
    (subprocess.TimeoutExpired('synthetic', 120), True), (OSError('not started'), False)])
def test_check_failure_classification(tmp_path, monkeypatch, code, refused):
    cfg, folder = configured(tmp_path, monkeypatch)
    transport(monkeypatch, cfg)
    exporter(monkeypatch, code=code)
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert (NEW in upgrade.read(folder)['refused']) is refused
    assert not upgrade.read(folder)['pending']


@pytest.mark.parametrize('message,refused', [('Unsafe candidate archive', True),
                                            ('Disposable install unavailable', False)])
def test_export_failures(tmp_path, monkeypatch, message, refused):
    cfg, folder = configured(tmp_path, monkeypatch)
    transport(monkeypatch, cfg)
    exporter(monkeypatch, failure=core.Unavailable(message))
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert (NEW in upgrade.read(folder)['refused']) is refused


@pytest.mark.parametrize('candidate', [OLD, PREVIOUS])
def test_never_removes_current_or_previous(tmp_path, monkeypatch, candidate):
    cfg, folder = configured(tmp_path, monkeypatch)
    record = upgrade.empty()
    record.update(current=OLD, previous=PREVIOUS)
    upgrade.write(folder, record)
    saved = folder / candidate / 'install' / 'keep'
    saved.parent.mkdir(parents=True)
    saved.write_text('keep')
    remote, _ = transport(monkeypatch, cfg)
    remote.commit = candidate
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert saved.read_text() == 'keep'


def test_mismatch_and_unreadable_notices_are_published(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    monkeypatch.delenv('ADT_VERSIONS_DIR')
    hub = Hub(cfg)
    service.poll(cfg, hub.client(), clock=lambda: 100, upgrade_enabled=True)
    bodies = [c['body'] for cs in hub.comments_by_issue.values() for c in cs]
    assert any(upgrade.MISMATCH in b for b in bodies)
    monkeypatch.setenv('ADT_VERSIONS_DIR', str(folder))
    (folder / 'switch').write_text('current=bad\n')
    service.poll(cfg, hub.client(), clock=lambda: 200, upgrade_enabled=True)
    bodies = [c['body'] for cs in hub.comments_by_issue.values() for c in cs]
    assert any(upgrade.UNREADABLE in b for b in bodies)
    assert (folder / 'switch').read_text() == 'current=bad\n'


def test_versions_folder_resolves_symlinks_and_trailing_slashes(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    alias = tmp_path / 'alias'
    alias.symlink_to(folder, target_is_directory=True)
    monkeypatch.setenv('ADT_VERSIONS_DIR', str(alias) + '/')
    assert upgrade.context(cfg)[0] == folder.resolve()
    assert not cfg['_upgrade_notice']


def test_run_once_and_enqueue_never_upgrade(tmp_path, monkeypatch):
    cfg, _ = configured(tmp_path, monkeypatch)
    hub = Hub(cfg)
    monkeypatch.setattr(upgrade, 'context', lambda *a: pytest.fail('upgrade outside serve'))
    for action in ('run-once', 'enqueue-only'):
        assert service.run_loop(cfg, hub.client(), action) == 0


def test_failed_upgrade_reads_still_process_claims(tmp_path, monkeypatch):
    cfg, _ = configured(tmp_path, monkeypatch)
    remote, _ = transport(monkeypatch, cfg)
    remote.failure = core.Unavailable('synthetic outage')
    hub = Hub(cfg)
    hub.issue(state='draft')
    job = 'finished-fixture'
    path = Path(cfg['state_dir']) / 'claims' / 'finished.json'
    core.save(path, dict(job=job, issue=1, stage='finished', task='', cleaned=False))
    cleaned = []
    monkeypatch.setattr(service, 'cleanup', lambda *a, **kw: cleaned.append(True))
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100, upgrade_enabled=True)
    assert remote.calls == ['/commits/main']
    assert cleaned == [True]
    assert service.heartbeat(cfg) == 100


def test_passing_switch_returns_cleanly_without_error_receipt(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    transport(monkeypatch, cfg)
    exporter(monkeypatch)
    assert service.run_loop(cfg, Hub(cfg).client(), 'serve') == 0
    record = upgrade.read(folder)
    assert record['pending'] == NEW and record['pending_starts'] == '0'
    assert record['pending_since'] == '' and record['current'] == ''
    assert not (Path(cfg['state_dir']) / 'service-error.json').exists()


@pytest.mark.parametrize('publication_fails', [False, True])
def test_confirmation_ignores_cursor_and_precedes_claims(tmp_path, monkeypatch, publication_fails):
    cfg, folder = configured(tmp_path, monkeypatch)
    record = upgrade.empty()
    record.update(current=OLD, pending=NEW, pending_starts='1', pending_since='1')
    upgrade.write(folder, record)
    actual_pending = upgrade.is_pending
    monkeypatch.setattr(upgrade, 'is_pending',
                        lambda ctx: actual_pending(ctx, folder / NEW / 'install'))
    core.save(Path(cfg['state_dir']) / 'poll.json', {'next_poll': 10000})
    hub = Hub(cfg)
    hub.issue(state='draft')
    reached = []
    original_publish = service.publish_poll

    def publish(*args):
        reached.append('publish')
        assert upgrade.read(folder)['pending'] == NEW
        if publication_fails:
            raise core.Unavailable('status failed')
        return original_publish(*args)

    def claim(*args):
        reached.append('claim')
        assert upgrade.read(folder)['current'] == NEW
        assert not upgrade.read(folder)['pending']

    monkeypatch.setattr(service, 'publish_poll', publish)
    monkeypatch.setattr(service, 'claim_event', claim)
    service.poll(cfg, hub.client(), clock=lambda: 100, upgrade_enabled=True)
    if publication_fails:
        service.poll(cfg, hub.client(), clock=lambda: 101, upgrade_enabled=True)
        assert reached == ['publish', 'publish']
        assert upgrade.read(folder)['pending'] == NEW
    else:
        assert reached == ['publish', 'claim']
        confirmed = upgrade.read(folder)
        assert confirmed['previous'] == OLD
        assert not confirmed['pending_since'] and not confirmed['pending_starts']


@pytest.mark.parametrize('notice', [f'Upgraded from {OLD} to {NEW}',
                                  f'Rolled back from {NEW}: did not confirm'])
def test_notices_survive_later_successful_cycles(tmp_path, monkeypatch, notice):
    cfg, folder = configured(tmp_path, monkeypatch)
    record = upgrade.empty()
    record.update(current=OLD, notice=notice)
    upgrade.write(folder, record)
    monkeypatch.setattr(upgrade, 'attempt', lambda *a: False)
    hub = Hub(cfg)
    for at in (100, 200, 300):
        service.poll(cfg, hub.client(), clock=lambda: at, upgrade_enabled=True)
        service.report_recovery(cfg, hub.client(), None, None)
        assert notice in next(iter(hub.comments_by_issue.values()))[0]['body']


def test_first_confirmation_retains_empty_previous_and_initial_notice(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    record = upgrade.empty()
    record['pending'] = NEW
    upgrade.write(folder, record)
    upgrade.confirm(cfg, upgrade.context(cfg))
    record = upgrade.read(folder)
    assert record['current'] == NEW and record['previous'] == ''
    assert record['notice'] == f'Upgraded from {OLD} to {NEW}'


def launch(folder, first):
    return subprocess.run(['/bin/sh', str(ROOT / 'templates/lifecycle-launch.sh'),
                           str(folder), str(first), '/bin/sh', '-c',
                           'printf "%s\\n%s\\n" "$PWD" "$ADT_VERSIONS_DIR"'],
                          capture_output=True, text=True, check=True).stdout.splitlines()


def installs(tmp_path):
    folder = tmp_path / 'versions with spaces'
    folder.mkdir()
    first = tmp_path / 'first install'
    first.mkdir()
    for commit in (OLD, NEW):
        (folder / commit / 'install').mkdir(parents=True)
    return folder, first


@pytest.mark.parametrize('record', [None, '', 'current=bad\n', 'current=\n',
                                  'current=$(touch forbidden)\n',
                                  'current=' + OLD + '\ncurrent=' + NEW + '\n'])
def test_launcher_first_install_and_malformed_record_unchanged(tmp_path, record):
    folder, first = installs(tmp_path)
    if record is not None:
        (folder / 'switch').write_text(record)
    assert launch(folder, first) == [str(first), str(folder)]
    if record is not None:
        assert (folder / 'switch').read_text() == record
    assert not (first / 'forbidden').exists()


def test_launcher_empty_current_runs_first(tmp_path):
    folder, first = installs(tmp_path)
    upgrade.write(folder, upgrade.empty())
    assert launch(folder, first)[0] == str(first)


@pytest.mark.parametrize('current', ['', OLD])
def test_launcher_rolls_back_fourth_start(tmp_path, current):
    folder, first = installs(tmp_path)
    record = upgrade.empty()
    record.update(current=current, pending=NEW, pending_starts='0')
    upgrade.write(folder, record)
    for count in range(1, 4):
        assert launch(folder, first)[0] == str(folder / NEW / 'install')
        assert upgrade.read(folder)['pending_starts'] == str(count)
        assert int(upgrade.read(folder)['pending_since']) > 0
    expected = folder / OLD / 'install' if current else first
    assert launch(folder, first)[0] == str(expected)
    result = upgrade.read(folder)
    assert result['refused'] == NEW
    assert result['pending'] == result['pending_starts'] == result['pending_since'] == ''
    assert result['notice'] == f'Rolled back from {NEW}: did not confirm'
    before = (folder / 'switch').read_bytes()
    launch(folder, first)
    assert (folder / 'switch').read_bytes() == before


def test_launcher_rolls_back_after_ten_minutes(tmp_path):
    folder, first = installs(tmp_path)
    record = upgrade.empty()
    record.update(current=OLD, pending=NEW, pending_starts='1',
                  pending_since=str(int(time.time()) - 601))
    upgrade.write(folder, record)
    assert launch(folder, first)[0] == str(folder / OLD / 'install')
    assert upgrade.read(folder)['refused'] == NEW


def test_launcher_keeps_confirmed_version_and_notice(tmp_path):
    folder, first = installs(tmp_path)
    record = upgrade.empty()
    record.update(current=NEW, previous=OLD, notice=f'Upgraded from {OLD} to {NEW}')
    upgrade.write(folder, record)
    for _ in range(5):
        assert launch(folder, first)[0] == str(folder / NEW / 'install')
    assert upgrade.read(folder) == record


def test_upgrade_clear_lock_and_cli(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    core.save(cfg['config_path'], cfg)
    record = upgrade.empty()
    record['refused'] = NEW + ' ' + OLD
    upgrade.write(folder, record)
    with core.lock(Path(cfg['state_dir']) / 'service.lock') as acquired:
        assert acquired
        with pytest.raises(core.Unavailable):
            upgrade.clear(cfg, NEW)
    assert upgrade.read(folder)['refused'] == NEW + ' ' + OLD
    assert service.main(['--config', cfg['config_path'], 'upgrade-clear', NEW]) == 0
    assert upgrade.read(folder)['refused'] == OLD


@pytest.mark.parametrize('message,exit_code', [
    ('App authentication transport failed', 75),
    ('App authentication TLS verification failed', 75),
    ('App authentication token issuance failed (HTTP 503)', 75),
    ('App authentication token issuance failed (HTTP 429)', 75),
    ('App authentication token issuance failed (HTTP 401)', 1),
    ('App authentication token issuance failed (HTTP 403)', 1),
    ('Keychain credential unavailable or malformed', 1)])
def test_upgrade_check_mint_error_codes(tmp_path, monkeypatch, message, exit_code):
    cfg = policy(tmp_path)
    cfg['app_auth'] = {'runner': {}, 'product': {}}
    monkeypatch.setattr(core, 'load_config', lambda p: cfg)

    def mint(*a):
        raise app_tokens.AuthenticationError(message)

    monkeypatch.setattr(app_tokens, 'get', mint)
    assert upgrade.check(Path(cfg['config_path'])) == exit_code


def test_upgrade_check_mints_all_roles_and_never_runs_agent(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    cfg['app_auth'] = {'runner': {}, 'product': {}}
    monkeypatch.setattr(core, 'load_config', lambda p: cfg)
    roles = []
    monkeypatch.setattr(app_tokens, 'get', lambda cfg, role: roles.append(role))
    monkeypatch.setattr(service, 'poll', lambda *a, **kw: pytest.fail('check dispatched'))
    assert upgrade.check(Path(cfg['config_path'])) == 0
    assert roles == ['runner', 'product']


def test_upgrade_check_own_timeout_is_temporary(tmp_path, monkeypatch):
    monkeypatch.setattr(core, 'load_config', lambda p: (_ for _ in ()).throw(TimeoutError()))
    assert upgrade.check(tmp_path / 'config.json') == 75


def test_check_argv_isolated_and_bounded(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, 'run', run)
    assert upgrade.start_check(tmp_path, {'config_path': 'synthetic.json'}) == 0
    argv, kwargs = calls[0]
    assert argv[1:] == ['-I', str(tmp_path / 'lifecycle/service.py'),
                        '--config', 'synthetic.json', 'upgrade-check']
    assert kwargs['timeout'] == 120


@pytest.mark.parametrize('field,value', [('track', 'release'), ('versions_dir', 'relative'),
                                       ('initial_commit', 'bad'), ('smoke_agent', '../bad')])
def test_upgrade_setting_validation(tmp_path, monkeypatch, field, value):
    cfg, _ = configured(tmp_path, monkeypatch)
    cfg['upgrade'][field] = value
    core.save(cfg['config_path'], cfg)
    with pytest.raises(ValueError, match='upgrade'):
        core.load_config(cfg['config_path'])


def test_upgrade_private_setting_allowed_with_policy_file(tmp_path, monkeypatch):
    cfg, _ = configured(tmp_path, monkeypatch)
    public = core.read(cfg['config_path'])
    for key in ('state_dir', 'repository_root', 'worktree_root', 'runner_config',
                'primary_runner_config', 'token_env', 'product_token_env', 'app_auth',
                'system_retry_minutes'):
        public.pop(key, None)
    path = tmp_path / 'public.json'
    core.save(path, public)
    private = dict(policy_file=str(path), upgrade=cfg['upgrade'], state_dir=cfg['state_dir'],
                   token_env=cfg['token_env'])
    private_path = tmp_path / 'private.json'
    core.save(private_path, private)
    assert core.load_config(private_path)['upgrade'] == cfg['upgrade']


def test_upgrade_plist_uses_shell_launcher_and_keepalive():
    cfg = plistlib.loads((ROOT / 'templates/lifecycle-serve-upgrade.plist.example').read_bytes())
    assert cfg['ProgramArguments'][:2] == ['/bin/sh', '__INSTALLED_LAUNCHER__']
    assert cfg['KeepAlive'] is True


@pytest.mark.parametrize('failure_stage', ['mint', 'export', 'check-start'])
def test_temporary_upgrade_failures_leave_poll_available(tmp_path, monkeypatch, failure_stage):
    cfg, folder = configured(tmp_path, monkeypatch)
    transport(monkeypatch, cfg)
    exporter(monkeypatch)

    def fail(*a, **kw):
        raise OSError('synthetic temporary failure')

    if failure_stage == 'mint':
        monkeypatch.setattr(app_tokens, 'get', fail)
    elif failure_stage == 'export':
        monkeypatch.setattr(upgrade.smoke, 'export_candidate', fail)
    else:
        monkeypatch.setattr(upgrade, 'start_check', fail)
    hub = Hub(cfg)
    assert service.poll(cfg, hub.client(), clock=lambda: 100, upgrade_enabled=True) == []
    assert service.heartbeat(cfg) == 100
    assert not upgrade.read(folder)['refused']


@pytest.mark.parametrize('bad_line', ['pending=not-a-commit', 'pending_starts=-1',
                                     'notice=untrusted text', 'unknown=value',
                                     'current=' + OLD + '\r'])
def test_record_parsers_reject_malformed_fields_without_changes(tmp_path, bad_line):
    folder, first = installs(tmp_path)
    upgrade.write(folder, upgrade.empty())
    path = folder / 'switch'
    key = bad_line.split('=')[0]
    lines = path.read_text().splitlines()
    lines = [line for line in lines if not line.startswith(key + '=')]
    text = '\n'.join(lines + [bad_line]) + '\n'
    path.write_bytes(text.encode())
    with pytest.raises(ValueError):
        upgrade.read(folder)
    assert launch(folder, first)[0] == str(first)
    assert path.read_bytes() == text.encode()


def test_pending_status_failure_defers_digest_too(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    cfg['digest_config'] = 'not-read.json'
    record = upgrade.empty()
    record['pending'] = NEW
    upgrade.write(folder, record)
    monkeypatch.setattr(upgrade, 'is_pending', lambda ctx: True)
    hub = Hub(cfg)

    def fail(*args):
        raise core.Unavailable('synthetic publication failure')

    monkeypatch.setattr(service, 'publish_poll', fail)
    from digest import standup
    monkeypatch.setattr(standup, 'load_config', lambda *a: pytest.fail('unconfirmed digest'))

    class StopLoop(BaseException):
        pass

    def stop(*a):
        raise StopLoop

    monkeypatch.setattr(service.time, 'sleep', stop)
    with pytest.raises(StopLoop):
        service.run_loop(cfg, hub.client(), 'serve')
    assert upgrade.read(folder)['pending'] == NEW


def test_upgrade_clear_accepts_resolved_versions_alias(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    record = upgrade.empty()
    record['refused'] = NEW
    upgrade.write(folder, record)
    alias = tmp_path / 'alias'
    alias.symlink_to(folder, target_is_directory=True)
    cfg['upgrade']['versions_dir'] = str(alias)
    upgrade.clear(cfg, NEW)
    assert not upgrade.read(folder)['refused']


def test_upgrade_accepts_existing_smoke_agent_identifier(tmp_path, monkeypatch):
    cfg, _ = configured(tmp_path, monkeypatch)
    cfg['upgrade']['smoke_agent'] = 'a' * 80
    upgrade.validate(cfg['upgrade'])
