"""Version policy, candidate byte reads and persistent advisory refusals."""

import copy
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from lifecycle import core, service, upgrade
from test_lifecycle import Hub, ROOT, policy
from test_lifecycle_upgrade import NEW, OLD, configured, exporter, transport


@pytest.mark.parametrize('version', [2, True, False, '1', 1.0, 0, -1, None])
def test_rules_version_must_match_code_integer(tmp_path, version):
    cfg = policy(tmp_path)
    cfg['statuses_version'] = version
    core.save(cfg['config_path'], cfg)
    with pytest.raises(ValueError, match='statuses_version'):
        core.load_config(cfg['config_path'])


def test_missing_public_version_defaults_to_one_and_private_refuses_it(tmp_path):
    cfg = policy(tmp_path)
    assert core.load_config(cfg['config_path'])['statuses_version'] == 1
    public = dict(cfg, statuses_version=1)
    for name in ['state_dir', 'repository_root', 'worktree_root', 'runner_config', 'primary_runner_config', 'token_env', 'product_token_env', 'config_path']:
        public.pop(name, None)
    path = tmp_path / 'public.json'
    core.save(path, public)
    local = {'policy_file': str(path), 'state_dir': cfg['state_dir'], 'token_env': cfg['token_env']}
    local_path = tmp_path / 'private.json'
    core.save(local_path, local)
    assert core.load_config(local_path)['statuses_version'] == 1
    core.save(local_path, dict(local, statuses_version=1))
    with pytest.raises(ValueError, match='Public policy fields'):
        core.load_config(local_path)


@pytest.mark.parametrize('contents', [None, b'', b'0', b'-1', b'01', b'1\n\n', b' 1', b'1 ', b'True', b'1\r\n', b'\xff'])
def test_runtime_version_missing_or_malformed_plain_message(tmp_path, contents):
    target = tmp_path / 'install'
    shutil.copytree(ROOT / 'lifecycle', target / 'lifecycle', ignore=shutil.ignore_patterns('__pycache__'))
    version = target / 'lifecycle/statuses-version'
    version.unlink(missing_ok=True)
    if contents is not None:
        version.write_bytes(contents)
    result = subprocess.run([sys.executable, '-B', '-I', str(target / 'lifecycle/core.py'), '--help'],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stdout == ''
    assert 'cannot load lifecycle/statuses-version' in result.stderr
    assert 'Traceback' not in result.stderr


def candidate(monkeypatch, contents):
    exporter(monkeypatch)
    original = upgrade.smoke.export_candidate
    def export(*args):
        install, commit = original(*args)
        (install / 'lifecycle').mkdir()
        # Executing candidate code to discover its version would create this marker.
        (install / 'lifecycle/__init__.py').write_text("raise RuntimeError('candidate code executed')\n")
        if contents is not None:
            (install / 'lifecycle/statuses-version').write_bytes(contents)
        return install, commit
    monkeypatch.setattr(upgrade.smoke, 'export_candidate', export)


@pytest.mark.parametrize('contents', [b'2', b'2\n'])
def test_higher_candidate_refused_before_candidate_check(tmp_path, monkeypatch, contents):
    cfg, folder = configured(tmp_path, monkeypatch)
    transport(monkeypatch, cfg)
    candidate(monkeypatch, contents)
    monkeypatch.setattr(upgrade, 'start_check', lambda *a: pytest.fail('candidate executed before version refusal'))
    initial = upgrade.empty()
    initial.update(current=OLD)
    upgrade.write(folder, initial)
    before = (folder / 'switch').read_bytes()
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert (folder / 'switch').read_bytes() == before
    notice = service.poll_body(cfg, 100, [])
    expected = f"Framework commit {NEW[:7]} needs the seven-status rules and guard. This service stays on {OLD[:7]}. Owner: follow 'Moving to seven statuses' in `docs/v04-lifecycle-digest.md`."
    assert expected in notice
    refusal = [p for p in folder.iterdir() if p.is_file() and p.name != 'switch']
    assert len(refusal) == 1
    assert not core.SHA.fullmatch(refusal[0].name) and not refusal[0].name.startswith('.switch-')
    assert '2' in refusal[0].read_text()
    restarted = copy.deepcopy(cfg)
    restarted.pop('_upgrade_notice', None)
    assert upgrade.context(restarted) is not None
    assert expected in service.poll_body(restarted, 200, [])


@pytest.mark.parametrize('contents', [b'', b'0', b'2\n\n', b' 1', b'01', b'1\r\n', b'\xff'])
def test_malformed_candidate_is_refused_before_any_candidate_code(tmp_path, monkeypatch, contents):
    cfg, folder = configured(tmp_path, monkeypatch)
    transport(monkeypatch, cfg)
    candidate(monkeypatch, contents)
    monkeypatch.setattr(upgrade, 'start_check', lambda *a: pytest.fail('malformed candidate executed'))
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert NEW in upgrade.read(folder)['refused']


@pytest.mark.parametrize('contents', [None, b'1', b'1\n'])
def test_missing_candidate_version_is_one_and_compatible_candidates_checked(tmp_path, monkeypatch, contents):
    cfg, folder = configured(tmp_path, monkeypatch)
    transport(monkeypatch, cfg)
    candidate(monkeypatch, contents)
    calls = []
    byte_reads = []
    read_bytes = Path.read_bytes
    def read_version(path):
        if path == folder / NEW / 'install/lifecycle/statuses-version':
            byte_reads.append(path)
        return read_bytes(path)
    monkeypatch.setattr(Path, 'read_bytes', read_version)
    def check(*args):
        assert byte_reads == [folder / NEW / 'install/lifecycle/statuses-version']
        calls.append(True)
        return 0
    monkeypatch.setattr(upgrade, 'start_check', check)
    assert upgrade.attempt(cfg, upgrade.context(cfg))
    assert calls == [True] and upgrade.read(folder)['pending'] == NEW


def test_refusal_same_poll_restart_new_commit_and_unreadable_file(tmp_path, monkeypatch):
    cfg, folder = configured(tmp_path, monkeypatch)
    remote, _ = transport(monkeypatch, cfg)
    candidate(monkeypatch, b'2')
    monkeypatch.setattr(upgrade, 'start_check', lambda *a: pytest.fail('candidate executed'))
    hub = Hub(cfg)
    assert service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100, upgrade_enabled=True) == []
    assert service.heartbeat(cfg) == 100
    bodies = [c['body'] for cs in hub.comments_by_issue.values() for c in cs]
    assert any('needs the seven-status rules and guard' in b for b in bodies)
    cfg.pop('_upgrade_notice', None)
    cfg.pop('_upgrade_last_check', None)
    assert service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200, upgrade_enabled=True) == []
    assert service.heartbeat(cfg) == 200
    assert 'needs the seven-status rules and guard' in service.poll_body(cfg, 200, [])
    remote.commit = OLD
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    assert 'needs the seven-status rules and guard' not in service.poll_body(cfg, 300, [])
    remote.commit = NEW
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
    refusal = next(p for p in folder.iterdir() if p.is_file() and p.name != 'switch')
    refusal.write_bytes(b'\xff malformed')
    assert upgrade.context(cfg) is not None
    assert 'needs the seven-status rules and guard' not in service.poll_body(cfg, 400, [])
    assert not upgrade.attempt(cfg, upgrade.context(cfg))
