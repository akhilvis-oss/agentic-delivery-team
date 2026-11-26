"""Check committed data using the trusted export, without leaking adopter data."""

import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_release_install', ROOT / 'scripts/verify_install.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def command(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL)


def commit(repo):
    command(repo, 'add', '.')
    command(repo, 'commit', '--allow-empty', '-m', 'example data')
    command(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    command(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')


@pytest.fixture
def adopter(tmp_path):
    repo = tmp_path / 'adopter'
    repo.mkdir()
    manifest = checker.release_files.load_manifest(ROOT)
    for entry in manifest['entries']:
        if entry['kind'] not in {'framework', 'adopter'}:
            continue
        dest = repo / entry['destination']
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = (ROOT / entry['source']).read_bytes()
        if entry['kind'] == 'adopter':
            if entry['destination'] == '.github/review-gate.json':
                value = json.loads(data)
                value['repo_url'] = 'https://github.com/example/project'
                data = json.dumps(value).encode()
            elif entry['destination'] == '.github/task-lifecycle.json':
                data = (ROOT / '.github/task-lifecycle.json').read_bytes()
            elif entry['destination'].endswith('.md'):
                data = re.sub(rb'<[A-Z][^>\r\n]*>', b'example', data)
        dest.write_bytes(data)
    command(repo, 'init', '-b', 'main')
    command(repo, 'config', 'user.name', 'Example Builder')
    command(repo, 'config', 'user.email', 'builder@example.invalid')
    commit(repo)
    return repo


def verify(repo, **kwargs):
    lines = []
    code = checker.verify(repo, emit=lines.append, **kwargs)
    return code, '\n'.join(lines)


def test_clean_adopter_passes_from_trusted_export_and_is_read_only(adopter):
    before = {str(p.relative_to(adopter)): p.read_bytes()
              for p in adopter.rglob('*') if p.is_file()}
    code, output = verify(adopter)
    after = {str(p.relative_to(adopter)): p.read_bytes()
             for p in adopter.rglob('*') if p.is_file()}
    assert code == 0 and ' FAIL ' not in output
    assert before == after


@pytest.mark.parametrize('kind', ['missing', 'changed', 'invalid', 'version', 'placeholder'])
def test_missing_changed_invalid_and_wrong_version_fail(adopter, kind):
    if kind == 'missing':
        (adopter / 'lifecycle/core.py').unlink()
    elif kind == 'changed':
        (adopter / 'lifecycle/core.py').write_text('changed')
    elif kind == 'invalid':
        (adopter / '.github/review-gate.json').write_text('{')
    elif kind == 'version':
        path = adopter / '.github/agentic-kit.json'
        value = json.loads(path.read_text())
        value['version'] = 'v999.0.0'
        path.write_text(json.dumps(value))
    else:
        (adopter / 'AGENTS.md').write_text('<PROJECT_NAME>')
    commit(adopter)
    code, output = verify(adopter)
    assert code == 1 and ' FAIL ' in output


def test_uncommitted_worktree_is_not_checked(adopter):
    (adopter / '.github/review-gate.json').write_text('not committed')
    assert verify(adopter)[0] == 0


def test_uses_committed_default_branch_not_feature_head(adopter):
    command(adopter, 'switch', '-c', 'feature')
    (adopter / 'lifecycle/core.py').write_text('changed')
    command(adopter, 'add', '.')
    command(adopter, 'commit', '-m', 'feature only')
    assert verify(adopter)[0] == 0


def test_invalid_adopter_canary_never_appears_in_output(adopter):
    canary = 'CANARY-PRIVATE-SETTING'
    (adopter / '.github/review-gate.json').write_text(json.dumps({'repo_url': canary}))
    commit(adopter)
    code, output = verify(adopter)
    assert code == 1
    assert canary not in output and str(adopter) not in output


def test_never_imports_or_executes_adopter_code(adopter, tmp_path):
    marker = tmp_path / 'executed'
    (adopter / 'lifecycle/core.py').write_text(f'from pathlib import Path; Path({str(marker)!r}).touch()')
    (adopter / 'scripts/review_gate.py').write_text(f'from pathlib import Path; Path({str(marker)!r}).touch()')
    commit(adopter)
    assert verify(adopter)[0] == 1
    assert not marker.exists()


def test_disabled_policy_is_present_but_not_loaded(adopter):
    path = adopter / '.github/task-lifecycle.json'
    path.write_text('not populated because lifecycle is off')
    commit(adopter)
    assert verify(adopter)[0] == 0
    kit = adopter / '.github/agentic-kit.json'
    value = json.loads(kit.read_text())
    value['features']['lifecycle'] = True
    kit.write_text(json.dumps(value))
    commit(adopter)
    assert verify(adopter)[0] == 1


def test_manual_service_install_matches_private_and_framework_sources(adopter, tmp_path):
    install = tmp_path / 'install'
    shutil.copytree(ROOT, install, ignore=shutil.ignore_patterns('.git', '__pycache__'))
    assert verify(adopter, service_install=install)[0] == 0
    (install / 'runner/task_runner.py').write_text('changed')
    assert verify(adopter, service_install=install)[0] == 1


@pytest.mark.parametrize(('comparison', 'expected'), [('ahead', 0), ('identical', 0), ('behind', 1), ('diverged', 1)])
def test_main_track_service_requires_tag_or_descendant(adopter, tmp_path, monkeypatch, comparison, expected):
    folder = tmp_path / 'versions'
    current, tag = 'a' * 40, 'b' * 40
    install = folder / current / 'install'
    install.mkdir(parents=True)
    files = {'release-files.json': (ROOT / 'release-files.json').read_bytes()}
    for item in checker.release_files.load_manifest(ROOT)['entries']:
        files[item['source']] = (ROOT / item['source']).read_bytes()
    for name, data in files.items():
        path = install / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (folder / 'switch').write_text(json.dumps({'current': current, 'ignored': 'CANARY-SWITCH-FIELD'}))
    requests = []
    def public(endpoint):
        requests.append(endpoint)
        return {'sha': tag} if endpoint.startswith('/commits/') else {'status': comparison}
    monkeypatch.setattr(checker, 'public_json', public)
    monkeypatch.setattr(checker, 'public_archive', lambda sha: files)
    code, output = verify(adopter, service_install=folder)
    assert code == expected
    assert requests == ['/commits/' + checker.kit((ROOT / 'templates/agentic-kit.example.json').read_bytes())['version'],
                        '/compare/' + tag + '...' + current]
    assert 'CANARY-SWITCH-FIELD' not in output


def test_main_track_service_empty_switch_and_changed_files_fail(adopter, tmp_path, monkeypatch):
    folder = tmp_path / 'versions'
    folder.mkdir()
    (folder / 'switch').write_text(json.dumps({'current': ''}))
    assert verify(adopter, service_install=folder)[0] == 1
    (folder / 'switch').write_text(json.dumps({'current': 'a' * 40}))
    monkeypatch.setattr(checker, 'public_json', lambda path: {'sha': 'b' * 40, 'status': 'ahead'})
    monkeypatch.setattr(checker, 'public_archive', lambda sha: {'runner/task_runner.py': b'expected'})
    path = folder / ('a' * 40) / 'install/runner/task_runner.py'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'changed')
    assert verify(adopter, service_install=folder)[0] == 1


def test_hash_asset_must_match_trusted_export(adopter, tmp_path):
    asset = checker.expected_hashes(checker.release_files.load_manifest(ROOT), None)
    asset['commit'] = 'a' * 40
    path = tmp_path / 'hashes.json'
    path.write_text(json.dumps(asset))
    assert verify(adopter, hashes=path)[0] == 0
    asset['files']['lifecycle/core.py'] = '0' * 64
    path.write_text(json.dumps(asset))
    assert verify(adopter, hashes=path)[0] == 1


def test_off_feature_templates_are_present_but_skipped_until_enabled(adopter):
    path = adopter / '.github/agentic-kit.json'
    value = json.loads(path.read_text())
    value['features']['product_files'] = False
    path.write_text(json.dumps(value))
    (adopter / 'AGENTS.md').write_text('<PROJECT_NAME>')
    commit(adopter)
    code, output = verify(adopter)
    assert code == 0 and 'AGENTS.md PASS present; feature off, validation skipped' in output
    (adopter / 'AGENTS.md').unlink()
    commit(adopter)
    assert verify(adopter)[0] == 1
    (adopter / 'AGENTS.md').write_text('<PROJECT_NAME>')
    value['features']['product_files'] = True
    path.write_text(json.dumps(value))
    commit(adopter)
    assert verify(adopter)[0] == 1


def test_missing_git_object_never_runs_a_promisor_helper(adopter):
    oid = command(adopter, 'rev-parse', 'HEAD:lifecycle/core.py').decode().strip()
    command(adopter, 'config', 'extensions.partialClone', 'origin')
    command(adopter, 'config', 'remote.origin.promisor', 'true')
    command(adopter, 'config', 'remote.origin.url', 'ext::touch remote-helper-executed')
    command(adopter, 'config', 'protocol.ext.allow', 'always')
    (adopter / '.git/objects' / oid[:2] / oid[2:]).unlink()
    assert verify(adopter)[0] == 1
    assert not (adopter / 'remote-helper-executed').exists()


def test_service_hash_asset_cannot_substitute_an_older_tag_commit(adopter, tmp_path, monkeypatch):
    manifest = checker.release_files.load_manifest(ROOT)
    asset = checker.expected_hashes(manifest, None)
    asset['commit'] = 'c' * 40
    hashes = tmp_path / 'hashes.json'
    hashes.write_text(json.dumps(asset))
    folder = tmp_path / 'versions'
    folder.mkdir()
    (folder / 'switch').write_text(json.dumps({'current': 'a' * 40}))
    monkeypatch.setattr(checker, 'public_json', lambda endpoint: {'sha': 'b' * 40, 'status': 'ahead'})
    code, output = verify(adopter, hashes=hashes, service_install=folder)
    assert code == 1 and 'service FAIL ' in output
