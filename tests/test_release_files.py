"""The release inventory is complete, safe and hashed from a tag's archive."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from scripts import release_files

ROOT = Path(__file__).resolve().parents[1]


def test_release_manifest_covers_tracked_tree_and_declared_sources():
    manifest = release_files.load_manifest(ROOT)
    paths = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines()
    assert release_files.classified(manifest, paths)
    assert not release_files.classified(manifest, paths + ['unclassified-new-file'])
    for item in manifest['entries']:
        assert (ROOT / item['source']).is_file()
    assert not any('sha256' in item for item in manifest['entries'])
    assert any(e['source'] == 'LICENSE' and e['kind'] == 'framework' for e in manifest['entries'])
    for item in manifest['entries']:
        if item['kind'] == 'adopter':
            assert item['feature'] in release_files.FEATURES


def test_both_guard_sparse_paths_are_installed_destinations():
    manifest = release_files.load_manifest(ROOT)
    destinations = {e['destination'] for e in manifest['entries']
                    if e['kind'] in {'framework', 'adopter'}}
    for name in ['templates/task-lifecycle.yml', '.github/workflows/task-lifecycle.yml']:
        text = (ROOT / name).read_text()
        block = re.search(r'(?m)^( +)sparse-checkout: \|\n((?:\1 +[^\n]+\n)+)', text)[2]
        for line in block.splitlines():
            path = line.strip().strip('/')
            assert path in destinations or any(d.startswith(path + '/') for d in destinations)
    assert {e['kind'] for e in manifest['entries'] if e['source'] == 'lifecycle/core.py'} == {
        'framework', 'private'}


@pytest.mark.parametrize('bad', ['../escape', '/absolute', 'a\\b', 'a//b', 'bad\nname', 'a:ref'])
def test_manifest_refuses_nonrelative_paths(bad):
    value = release_files.load_manifest(ROOT)
    value['entries'][0]['source'] = bad
    with pytest.raises(ValueError):
        release_files.validate_manifest(value)


def test_hash_asset_uses_tag_bytes_and_rejects_missing_source(tmp_path):
    repo = tmp_path / 'release'
    repo.mkdir()
    manifest = {'schema': 1, 'features': {f: [] for f in release_files.FEATURES},
                'entries': [{'source': 'code.py', 'destination': 'code.py', 'kind': 'framework'}],
                'kit_only': ['release-files.json']}
    (repo / 'release-files.json').write_text(json.dumps(manifest))
    (repo / 'code.py').write_bytes(b'tag bytes\n')
    for args in [('init', '-b', 'main'), ('config', 'user.name', 'Example Builder'),
                 ('config', 'user.email', 'builder@example.invalid'), ('add', '.'),
                 ('commit', '-m', 'release'), ('tag', 'v1.2.3')]:
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    (repo / 'code.py').write_bytes(b'uncommitted bytes\n')
    asset = release_files.hash_asset(repo, 'v1.2.3')
    assert asset['files']['code.py'] == hashlib.sha256(b'tag bytes\n').hexdigest()
    assert asset['tag'] == 'v1.2.3' and release_files.SHA.fullmatch(asset['commit'])
    manifest['entries'].append({'source': 'absent.py', 'destination': 'absent.py', 'kind': 'framework'})
    (repo / 'release-files.json').write_text(json.dumps(manifest))
    subprocess.run(['git', 'add', 'release-files.json'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'invalid inventory'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'tag', 'v1.2.4'], cwd=repo, check=True, capture_output=True)
    with pytest.raises(KeyError):
        release_files.hash_asset(repo, 'v1.2.4')


def test_new_install_authority_files_classify_control_critical():
    import importlib.util
    spec = importlib.util.spec_from_file_location('inventory_gate', ROOT / 'gate/review_gate.py')
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    config = gate.load_config(ROOT / '.github/review-gate.json')
    paths = ['INSTALL.md', 'UPGRADE.md', 'release-files.json', 'scripts/release_files.py',
             'scripts/verify_install.py', 'tests/test_release_files.py', 'tests/test_verify_install.py']
    for path in paths:
        assert gate.required_tiers([path], config)[0] == 'control-critical'


def test_install_runbooks_avoid_owner_paste_instructions():
    for name in ['INSTALL.md', 'UPGRADE.md']:
        text = (ROOT / name).read_text()
        assert not re.search(r'(?i)(paste|copy).{0,50}(commit|sha)', text)
        assert 'never instructions' in text
        assert 'Keychain' in text
        assert 'python3 -I -B' in text


# This is a scripted decision model of the runbook, not a shipped upgrade executor.
MISSING_NEW_ROW = (
    '| Destination absent and the current release did not carry it (or has no manifest) '
    '| New framework or adopter file: create it from the target export and list it in the PR |')
MISSING_CARRIED_ROW = (
    '| Destination absent and the current release carried it '
    '| Stop and ask the owner about the missing file |')


def _upgrade_contract():
    text = (ROOT / 'UPGRADE.md').read_text()
    rows = text.splitlines()
    assert MISSING_NEW_ROW in rows
    assert MISSING_CARRIED_ROW in rows
    assert rows.index(MISSING_NEW_ROW) < rows.index(MISSING_CARRIED_ROW)
    assert 'upgrading from v0.5.x creates `LICENSE.agentic-delivery-team` from' in text
    return text


def test_upgrade_license_mapping_and_legacy_example():
    _upgrade_contract()
    assert {'source': 'LICENSE', 'destination': 'LICENSE.agentic-delivery-team',
            'kind': 'framework'} in release_files.load_manifest(ROOT)['entries']


def test_upgrade_exact_carried_destination_table_rows():
    text = _upgrade_contract()
    assert 'only `framework` and `adopter` entries count, never `private` or' in text
    assert '`settings`' in text
    assert 'An unreadable or malformed current manifest stops the upgrade' in text
    assert 'files the project deliberately removed' in text
    assert 'before\nwriting or deleting any file (including the kit record)' in text


def _trial_git(repo, *args):
    import os
    return subprocess.check_output(
        ['git', '--literal-pathspecs', '-c', 'core.hooksPath=' + os.devnull,
         '-c', 'core.fsmonitor=false', '-C', str(repo), *args],
        env={'PATH': os.defpath, 'GIT_CONFIG_NOSYSTEM': '1',
             'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
             'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_LAZY_FETCH': '1',
             'GIT_ALLOW_PROTOCOL': '', 'GIT_NO_REPLACE_OBJECTS': '1'},
        stderr=subprocess.DEVNULL)


def _trial_commit(repo):
    _trial_git(repo, 'add', '.')
    _trial_git(repo, '-c', 'user.name=Example Builder',
               '-c', 'user.email=builder@example.invalid', 'commit', '--allow-empty', '-m', 'fixture')
    _trial_git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    _trial_git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')


def _trial_write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _release_blob(repo, ref, name):
    release_files.relative(name)
    row = _trial_git(repo, 'ls-tree', ref, '--', name)
    if not row:
        return None  # Only a successful tree lookup proves genuine absence.
    if not row.startswith((b'100644 blob ', b'100755 blob ')):
        raise ValueError('nonregular release file')
    return _trial_git(repo, 'show', ref + ':' + name)


@pytest.fixture
def upgrade_trial(tmp_path):
    """Fully local synthetic history, shaped like v0.5.7, with no historical tags needed.

    All switches off and the populated example URL are fixture choices, not adopter
    migration requirements. The kit version is explicitly v0.5.7 (the historical
    example still recorded v0.5.6). No production checker is modified.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location('upgrade_trial_checker', ROOT / 'scripts/verify_install.py')
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    release = tmp_path / 'release'
    project = tmp_path / 'project'
    release.mkdir()
    project.mkdir()
    for repo in (release, project):
        _trial_git(repo, 'init', '-b', 'main')
    manifest = release_files.load_manifest(ROOT)
    sources = {e['source'] for e in manifest['entries']}
    for source in sources - {'lifecycle/kit_switch.py'}:
        _trial_write(release, source, (ROOT / source).read_bytes())
    _trial_write(release, 'lifecycle/__init__.py', b'# previous release bytes\n')
    _trial_write(release, 'retired.py', b'# unedited retired framework\n')
    _trial_commit(release)
    _trial_git(release, 'tag', 'v0.5.7')
    for source in sources:
        _trial_write(release, source, (ROOT / source).read_bytes())
    (release / 'retired.py').unlink()
    _trial_write(release, 'release-files.json', json.dumps(manifest).encode())
    _trial_commit(release)
    target = json.loads((ROOT / 'templates/agentic-kit.example.json').read_bytes())['version']
    _trial_git(release, 'tag', target)
    for entry in manifest['entries']:
        if entry['kind'] not in {'framework', 'adopter'}:
            continue
        name = entry['destination']
        if name == 'LICENSE.agentic-delivery-team':
            continue
        data = _release_blob(release, 'v0.5.7', entry['source'])
        if data is not None:
            _trial_write(project, name, data)
    record = json.loads((ROOT / 'templates/agentic-kit.example.json').read_bytes())
    record['version'] = 'v0.5.7'
    record['features'] = dict.fromkeys(release_files.FEATURES, False)
    _trial_write(project, '.github/agentic-kit.json', json.dumps(record).encode())
    policy = json.loads((project / '.github/review-gate.json').read_bytes())
    policy['repo_url'] = 'https://github.com/example/project'
    _trial_write(project, '.github/review-gate.json', json.dumps(policy).encode())
    _trial_commit(project)
    return project, release, target, manifest, checker


def _trial_current_manifest(trial, value):
    """Add a synthetic CURRENT release; never rely on or modify repository tags."""
    project, release, _, _, _ = trial
    _trial_write(release, 'lifecycle/__init__.py', b'# previous release bytes\n')
    _trial_write(release, 'release-files.json', value)
    _trial_commit(release)
    _trial_git(release, 'tag', 'v0.5.8')
    for name, source in [('LICENSE.agentic-delivery-team', 'LICENSE'),
                         ('lifecycle/kit_switch.py', 'lifecycle/kit_switch.py')]:
        _trial_write(project, name, (ROOT / source).read_bytes())
    record = json.loads((project / '.github/agentic-kit.json').read_bytes())
    record['version'] = 'v0.5.8'
    _trial_write(project, '.github/agentic-kit.json', json.dumps(record).encode())
    _trial_commit(project)


def _scripted_upgrade(trial):
    _upgrade_contract()  # Bind this test-only model to the exact runbook rule.
    project, release, target, manifest, checker = trial
    if _trial_git(project, 'status', '--porcelain'):
        raise ValueError('uncommitted changes')
    default = checker.default_commit(project)
    record = checker.kit(checker.committed_file(project, default, '.github/agentic-kit.json'))
    current = record['version']
    tags = _trial_git(release, 'tag', '--list', 'v*', '--sort=version:refname').decode().splitlines()
    def version(tag):
        return tuple(map(int, tag[1:].split('.')))
    if current not in tags or target not in tags or version(current) >= version(target):
        raise ValueError('unresolvable version or downgrade')
    tags = [t for t in tags if release_files.TAG.fullmatch(t)
            and version(current) <= version(t) <= version(target)]
    raw = _release_blob(release, current, 'release-files.json')
    old = None if raw is None else release_files.validate_manifest(json.loads(raw))
    carried = set() if old is None else {
        e['destination'] for e in old['entries'] if e['kind'] in {'framework', 'adopter'}}
    writes, deletes, created = {}, [], []
    entries = [e for e in manifest['entries'] if e['kind'] in {'framework', 'adopter'}]
    target_destinations = {e['destination'] for e in entries}
    unmatched = []
    if old is not None:
        for entry in old['entries']:
            name = entry['destination']
            if entry['kind'] == 'framework' and name not in target_destinations:
                data = _release_blob(project, default, name)
                if data is None or data != _release_blob(release, current, entry['source']):
                    raise ValueError('edited deletion candidate')
                deletes.append(name)
    else:
        tracked = _trial_git(project, 'ls-tree', '-r', '--name-only', default).decode().splitlines()
        unmatched = [n for n in tracked if n.startswith('lifecycle/') and n not in target_destinations]
    for entry in entries:
        name, source = entry['destination'], entry['source']
        data = _release_blob(project, default, name)
        exported = _release_blob(release, target, source)
        if exported is None:
            raise ValueError('missing target source')
        if data is None:
            if name in carried:
                raise ValueError('missing carried destination')
            writes[name] = exported
            created.append(name)
        elif entry['kind'] == 'framework':
            known = [_release_blob(release, tag, source) for tag in tags if tag != target]
            if not any(blob is not None for blob in known):
                raise ValueError('new source collision')
            if data not in [*known, exported]:
                raise ValueError('local framework edit')
            writes[name] = exported
        # Existing adopter values are preserved: this trial names no migrations.
    record['version'] = target
    writes['.github/agentic-kit.json'] = json.dumps(record).encode()
    # Everything above is preflight. No planned file or kit write/delete happens early.
    for name, data in writes.items():
        _trial_write(project, name, data)
    for name in deletes:
        (project / name).unlink()
    return created, deletes, unmatched


def _trial_snapshot(project):
    return {str(p.relative_to(project)): p.read_bytes() for p in project.rglob('*')
            if p.is_file() and '.git' not in p.relative_to(project).parts}


def test_upgrade_scripted_v057_creates_license_and_restores_deliberate_deletion(upgrade_trial):
    project, _, target, manifest, checker = upgrade_trial
    removed = ['lifecycle/core.py', 'AGENTS.md']
    for name in removed:
        (project / name).unlink()
    _trial_write(project, 'lifecycle/legacy.py', b'# unmatched legacy file\n')
    _trial_commit(project)
    policy = (project / '.github/review-gate.json').read_bytes()
    assert json.loads((project / '.github/agentic-kit.json').read_bytes())['version'] == 'v0.5.7'
    created, deleted, unmatched = _scripted_upgrade(upgrade_trial)
    assert set(created) == {'LICENSE.agentic-delivery-team', 'lifecycle/kit_switch.py', *removed}
    assert deleted == []
    assert unmatched == ['lifecycle/legacy.py']
    assert (project / 'LICENSE.agentic-delivery-team').read_bytes() == (ROOT / 'LICENSE').read_bytes()
    assert (project / 'lifecycle/__init__.py').read_bytes() == (ROOT / 'lifecycle/__init__.py').read_bytes()
    assert (project / '.github/review-gate.json').read_bytes() == policy
    record = json.loads((project / '.github/agentic-kit.json').read_bytes())
    assert record['version'] == target
    assert record['features'] == dict.fromkeys(release_files.FEATURES, False)
    # Fill only the recreated agent rules, as the upgrading agent would.
    agents = project / 'AGENTS.md'
    agents.write_text(re.sub(r'<[^<>\r\n]+>', 'example', agents.read_text()))
    _trial_commit(project)
    # Poison only the worktree: the unmodified checker must use committed default data.
    (project / 'LICENSE.agentic-delivery-team').write_bytes(b'uncommitted distractor')
    lines = []
    assert checker.verify(project, emit=lines.append) == 0
    count = 1 + sum(e['kind'] in {'framework', 'adopter'} for e in manifest['entries'])
    assert len(lines) == count + 1 and lines[-1] == f'{count} passed, 0 failed'
    assert all(' PASS ' in line for line in lines[:-1])
    assert len({line.split(' PASS ')[0] for line in lines[:-1]}) == count


@pytest.mark.parametrize('kind,name', [
    ('framework', 'LICENSE.agentic-delivery-team'), ('adopter', 'AGENTS.md')])
@pytest.mark.parametrize('listed', [False, True])
def test_upgrade_missing_listed_and_unlisted_both_carried_kinds(upgrade_trial, kind, name, listed):
    project, _, _, manifest, _ = upgrade_trial
    old = json.loads(json.dumps(manifest))
    if not listed:
        old['entries'] = [e for e in old['entries'] if e['destination'] != name]
    assert any(e['kind'] == kind and e['destination'] == name for e in manifest['entries'])
    _trial_current_manifest(upgrade_trial, json.dumps(old).encode())
    (project / name).unlink(missing_ok=True)
    _trial_commit(project)
    before = _trial_snapshot(project)
    if listed:
        with pytest.raises(ValueError, match='missing carried destination'):
            _scripted_upgrade(upgrade_trial)
        assert _trial_snapshot(project) == before
    else:
        created, _, _ = _scripted_upgrade(upgrade_trial)
        assert name in created
        assert (project / name).is_file()


@pytest.mark.parametrize('ignored_kind', ['private', 'settings'])
@pytest.mark.parametrize('name', ['LICENSE.agentic-delivery-team', 'AGENTS.md'])
def test_upgrade_private_and_settings_never_count_as_carried(upgrade_trial, ignored_kind, name):
    project, _, _, manifest, _ = upgrade_trial
    old = json.loads(json.dumps(manifest))
    old['entries'] = [e for e in old['entries'] if e['destination'] != name]
    old['entries'].append({'source': 'LICENSE', 'destination': name,
                           'kind': ignored_kind, 'owner_action': 'Owner handles this separately.'})
    _trial_current_manifest(upgrade_trial, json.dumps(old).encode())
    (project / name).unlink(missing_ok=True)
    _trial_commit(project)
    assert name in _scripted_upgrade(upgrade_trial)[0]


@pytest.mark.parametrize('failure', ['local-edit', 'collision', 'edited-deletion'])
def test_upgrade_late_refusal_leaves_planned_writes_deletes_and_kit_untouched(upgrade_trial, failure):
    project, release, _, manifest, _ = upgrade_trial
    # A late entry fails after an update, licence creation and deletion are planned.
    name = 'lifecycle/kit_switch.py' if failure == 'collision' else 'lifecycle/core.py'
    manifest['entries'].sort(key=lambda e: e['destination'] == name)
    if failure == 'edited-deletion':
        old = json.loads(json.dumps(manifest))
        old['entries'] = [e for e in old['entries'] if e['destination'] != 'LICENSE.agentic-delivery-team']
        old['entries'].append({'source': 'retired.py', 'destination': 'retired.py', 'kind': 'framework'})
        _trial_write(release, 'retired.py', b'# unedited retired framework\n')
        _trial_current_manifest(upgrade_trial, json.dumps(old).encode())
        (project / 'LICENSE.agentic-delivery-team').unlink()
        _trial_write(project, 'retired.py', b'# local edit\n')
        reason = 'edited deletion candidate'
    else:
        data = (ROOT / name).read_bytes() if failure == 'collision' else b'# local edit\n'
        _trial_write(project, name, data)
        reason = 'new source collision' if failure == 'collision' else 'local framework edit'
    _trial_commit(project)
    before = _trial_snapshot(project)
    with pytest.raises(ValueError, match=reason):
        _scripted_upgrade(upgrade_trial)
    assert _trial_snapshot(project) == before
    assert not (project / 'LICENSE.agentic-delivery-team').exists()
    assert (project / 'lifecycle/__init__.py').read_bytes() == b'# previous release bytes\n'


@pytest.mark.parametrize('bad', [b'{', b'{}', b'\xff'])
def test_upgrade_malformed_present_current_manifest_stops(upgrade_trial, bad):
    _trial_current_manifest(upgrade_trial, bad)
    before = _trial_snapshot(upgrade_trial[0])
    with pytest.raises((ValueError, UnicodeError)):
        _scripted_upgrade(upgrade_trial)
    assert _trial_snapshot(upgrade_trial[0]) == before


def test_upgrade_unreadable_present_current_manifest_stops(upgrade_trial, monkeypatch):
    _trial_current_manifest(upgrade_trial, json.dumps(upgrade_trial[3]).encode())
    original = _release_blob
    def unreadable(repo, ref, name):
        if repo == upgrade_trial[1] and ref == 'v0.5.8' and name == 'release-files.json':
            raise PermissionError('simulated unreadable committed manifest')
        return original(repo, ref, name)
    monkeypatch.setattr(__import__(__name__, fromlist=['_release_blob']), '_release_blob', unreadable)
    before = _trial_snapshot(upgrade_trial[0])
    with pytest.raises(PermissionError):
        _scripted_upgrade(upgrade_trial)
    assert _trial_snapshot(upgrade_trial[0]) == before



def test_upgrade_late_missing_carried_preserves_planned_deletion_and_updates(upgrade_trial):
    project, release, _, manifest, _ = upgrade_trial
    old = json.loads(json.dumps(manifest))
    old['entries'].append({'source': 'retired.py', 'destination': 'retired.py', 'kind': 'framework'})
    _trial_write(release, 'retired.py', b'# unedited retired framework\n')
    _trial_current_manifest(upgrade_trial, json.dumps(old).encode())
    _trial_write(project, 'retired.py', b'# unedited retired framework\n')
    (project / 'AGENTS.md').unlink()
    # The licence is new to this synthetic CURRENT, and is also planned for creation.
    # Keep the manifest authoritative by omitting its carried entry, not by changing kinds.
    old['entries'] = [e for e in old['entries'] if e['destination'] != 'LICENSE.agentic-delivery-team']
    # Amend only the synthetic tag data using a new fixture release version.
    _trial_write(release, 'release-files.json', json.dumps(old).encode())
    _trial_commit(release)
    _trial_git(release, 'tag', 'v0.5.9')
    record = json.loads((project / '.github/agentic-kit.json').read_bytes())
    record['version'] = 'v0.5.9'
    _trial_write(project, '.github/agentic-kit.json', json.dumps(record).encode())
    (project / 'LICENSE.agentic-delivery-team').unlink()
    manifest['entries'].sort(key=lambda e: e['destination'] == 'AGENTS.md')
    _trial_commit(project)
    before = _trial_snapshot(project)
    with pytest.raises(ValueError, match='missing carried destination'):
        _scripted_upgrade(upgrade_trial)
    assert _trial_snapshot(project) == before
    assert (project / 'retired.py').read_bytes() == b'# unedited retired framework\n'
    assert not (project / 'LICENSE.agentic-delivery-team').exists()
    assert json.loads((project / '.github/agentic-kit.json').read_bytes())['version'] == 'v0.5.9'


def test_upgrade_deletes_only_unedited_current_framework_destinations(upgrade_trial):
    project, release, _, manifest, _ = upgrade_trial
    old = json.loads(json.dumps(manifest))
    old['entries'].append({'source': 'retired.py', 'destination': 'retired.py', 'kind': 'framework'})
    _trial_write(release, 'retired.py', b'# unedited retired framework\n')
    _trial_current_manifest(upgrade_trial, json.dumps(old).encode())
    _trial_write(project, 'retired.py', b'# unedited retired framework\n')
    _trial_commit(project)
    assert _scripted_upgrade(upgrade_trial)[1] == ['retired.py']
    assert not (project / 'retired.py').exists()


@pytest.mark.parametrize('problem', ['missing-default', 'uncommitted-edit'])
def test_upgrade_requires_committed_default_and_clean_worktree(upgrade_trial, problem):
    _upgrade_contract()
    project = upgrade_trial[0]
    if problem == 'missing-default':
        _trial_git(project, 'symbolic-ref', '--delete', 'refs/remotes/origin/HEAD')
    else:
        (project / 'lifecycle/core.py').write_bytes(b'# uncommitted local edit\n')
    before = _trial_snapshot(project)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        _scripted_upgrade(upgrade_trial)
    assert _trial_snapshot(project) == before


# Test-only reference for the first-install runbook. The kit ships no installer.
def _install_contract():
    text = (ROOT / 'INSTALL.md').read_text()
    prose = ' '.join(text.replace('`', '').split())
    for phrase in (
            'Review gate only, about 30 minutes, macOS, Linux or WSL2',
            'Full lifecycle and runner (macOS)',
            'POSIX shell', 'native Windows', 'python3 3.11 or later',
            'GitHub CLI', 'gh auth status', 'Actions minutes', 'agent usage',
            'GitHub Free', 'gate runs but cannot block merges',
            'git -C "$CHECKOUT" remote set-head origin --auto',
            '<!-- added by agentic-delivery-team vX.Y.Z -->',
            "AGENTS.md and .github/pull_request_template.md.** Keep the project's file. "
            'Append each missing kit section (a ## heading and its content) beneath one',
            'If the project already has that heading, keep that section unchanged and list it in the PR.',
            "Stop and ask the owner if the project's rules conflict with the kit's owner authority or merge rules",
            'agents may merge', 'push to the default branch',
            "docs/product/*.** Keep the project's file byte for byte and list it in the PR.",
            'For every other existing adopter file, including a JSON config or workflow, stop and ask the owner. '
            'Do not merge into it or overwrite it.',
            'Existing GitHub rulesets stay with the owner.',
            'For an existing framework file, stop and ask the owner.',
            'A repeat install leaves existing files unchanged.',
            'Refuse symlinks, absolute paths and parent traversal',
            'preflight before writing any file',
            'docs/agentic-delivery-troubleshooting.md',
            "remove the kit's explanatory HTML comments",
    ):
        assert phrase in (text if phrase.startswith('<!--') else prose), phrase
    assert not re.search(r'`python(?:[ \t]|`)|(?m:^[ \t]*python(?:[ \t]|$))', text)


def _install_path(root, name):
    components = release_files.relative(name).split('/')
    path = root
    for component in components:
        path /= component
        if path.is_symlink():
            raise ValueError('refuse symlink')
    return path


def _merge_install_sections(current, template, tag):
    existing = current.decode()
    if re.search(r'agents may merge|(?<!never )push to the default branch', existing, re.I):
        raise ValueError('contradictory owner or merge rule')
    sections = re.split(r'(?m)^## ', template)[1:]
    headings = set(re.findall(r'(?m)^## (.+)$', existing))
    matched, added = [], []
    for section in sections:
        title, _, _ = section.partition('\n')
        (matched if title in headings else added).append(section)
    if added:
        marker = f'<!-- added by agentic-delivery-team {tag} -->'
        addition = '\n\n' + marker + '\n\n' + '\n'.join('## ' + section for section in added)
        current += addition.encode()
    return current, [section.splitlines()[0] for section in matched]


def _reference_first_install(release, project, manifest):
    _install_contract()
    record = json.loads((release / 'templates/agentic-kit.example.json').read_bytes())
    tag = record['version']
    writes, retained = {}, {}
    for entry in manifest['entries']:
        kind, name = entry['kind'], entry['destination']
        if kind not in {'framework', 'adopter'}:
            continue
        source = _install_path(release, entry['source'])
        dest = _install_path(project, name)
        data = source.read_bytes()
        if name == 'AGENTS.md':
            data = re.sub(rb'<[^<>\r\n]+>', b'example', data)
        elif name == '.github/pull_request_template.md':
            data = re.sub(rb'<!--.*?-->\n?', b'', data, flags=re.S)
        if dest.exists():
            if kind == 'adopter' and name in {'AGENTS.md', '.github/pull_request_template.md'}:
                data, matched = _merge_install_sections(dest.read_bytes(), data.decode(), tag)
                retained[name] = matched
            elif kind == 'adopter' and name.startswith('docs/product/'):
                retained[name] = []
                continue
            else:
                raise ValueError('existing framework or other adopter file')
        if name == '.github/review-gate.json':
            policy = json.loads(data)
            policy.update(repo_url='https://github.com/example/project', allowed_bases=['main'])
            data = json.dumps(policy).encode()
        writes[name] = data
    for name, data in writes.items():
        _trial_write(project, name, data)
    return retained


@pytest.mark.parametrize('name', ['AGENTS.md', '.github/pull_request_template.md'])
def test_existing_install_documents_keep_only_overlapping_owner_sections(tmp_path, name):
    project = tmp_path / 'repo'
    manifest = release_files.load_manifest(ROOT)
    entry = next(e for e in manifest['entries'] if e['destination'] == name)
    template = (ROOT / entry['source']).read_text()
    headings = re.findall(r'(?m)^## (.+)$', template)
    retained = 'How work is done' if name == 'AGENTS.md' else headings[0]
    existing = f'# Owner title\n\n## {retained}\n\nOwner text and <BUILD_COMMAND>.\n'.encode()
    _trial_write(project, name, existing)
    _trial_write(project, 'docs/product/vision.md', b'Owner vision')
    kept = _reference_first_install(ROOT, project, manifest)

    assert kept == {name: [retained], 'docs/product/vision.md': []}
    assert (project / 'docs/product/vision.md').read_bytes() == b'Owner vision'
    installed = (project / name).read_bytes()
    assert installed.startswith(existing)
    if name == 'AGENTS.md':
        assert b'\n## Working agreement\n' in installed
    tag = json.loads((ROOT / 'templates/agentic-kit.example.json').read_bytes())['version']
    assert installed.count(f'<!-- added by agentic-delivery-team {tag} -->'.encode()) == 1
    assert re.findall(rb'(?m)^## (.+)$', installed) == [
        s.encode() for s in [retained, *(h for h in headings if h != retained)]]
    snapshot = _trial_snapshot(project)
    with pytest.raises(ValueError, match='existing framework'):
        _reference_first_install(ROOT, project, manifest)
    assert _trial_snapshot(project) == snapshot


@pytest.mark.parametrize('kind,text,reason', [
    ('AGENTS.md', b'Agents may merge whenever they are ready.', 'contradictory owner or merge rule'),
    ('.github/pull_request_template.md', b'Agents push to the default branch.', 'contradictory owner or merge rule'),
    ('.github/task-lifecycle.json', b'{}', 'existing framework or other adopter file'),
    ('.github/workflows/review-gate.yml', b'name: Owner review', 'existing framework or other adopter file'),
    ('LICENSE.agentic-delivery-team', b'Local document', 'existing framework or other adopter file'),
])
def test_install_stops_on_conflict_without_writing(tmp_path, kind, text, reason):
    _trial_write(tmp_path, kind, text)
    before = _trial_snapshot(tmp_path)
    with pytest.raises(ValueError, match=reason):
        _reference_first_install(ROOT, tmp_path, release_files.load_manifest(ROOT))
    assert _trial_snapshot(tmp_path) == before


@pytest.mark.parametrize('problem', ['source-link', 'adopter-link', 'parent-link', 'traversal'])
def test_first_install_rejects_unsafe_paths(tmp_path, problem):
    release, project = tmp_path / 'release', tmp_path / 'project'
    project.mkdir()
    name = 'templates/agentic-kit.example.json'
    _trial_write(release, name, (ROOT / name).read_bytes())
    source = release / 'example.md'
    source.write_text('sample text')
    manifest = {'entries': [{'source': 'example.md', 'destination': 'docs/example.md', 'kind': 'framework'}]}
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'example.md').write_text('keep this file')
    if problem == 'source-link':
        source.unlink()
        source.symlink_to(other / 'example.md')
    elif problem == 'adopter-link':
        (project / 'docs').mkdir()
        (project / 'docs/example.md').symlink_to(other / 'example.md')
    elif problem == 'parent-link':
        (project / 'docs').symlink_to(other)
    else:
        manifest['entries'][0]['destination'] = '../other/example.md'
    with pytest.raises(ValueError, match='refuse symlink|Invalid release manifest'):
        _reference_first_install(release, project, manifest)
    assert (other / 'example.md').read_text() == 'keep this file'


def _local_install_git(repo, *args):
    return subprocess.run(
        ['git', '-c', f'core.hooksPath={os.devnull}', '-C', str(repo), *args],
        env={'PATH': os.defpath, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
             'GIT_ALLOW_PROTOCOL': 'file', 'GIT_TERMINAL_PROMPT': '0'},
        check=True, capture_output=True, text=True,
    ).stdout.rstrip()


def test_gate_only_first_install_recovers_default_and_checks_agents(tmp_path):
    project, origin, seed = (tmp_path / name for name in ('project', 'origin.git', 'seed'))
    for repo in (project, origin, seed):
        repo.mkdir()
    _local_install_git(origin, 'init', '--bare', '-b', 'main')
    _trial_git(seed, 'init', '-b', 'main')
    _trial_commit(seed)
    _local_install_git(seed, 'push', str(origin), 'main')
    _local_install_git(project, 'init', '-b', 'main')
    _local_install_git(project, 'remote', 'add', 'origin', str(origin))
    _local_install_git(project, 'fetch', 'origin')
    # Newer Git may set this on fetch. Recreate the missing-ref case either way.
    if _local_install_git(project, 'for-each-ref', 'refs/remotes/origin/HEAD'):
        _local_install_git(project, 'symbolic-ref', '--delete', 'refs/remotes/origin/HEAD')
    args = ('symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD')
    with pytest.raises(subprocess.CalledProcessError):
        _local_install_git(project, *args)
    _install_contract()
    _local_install_git(project, 'remote', 'set-head', 'origin', '--auto')
    default_ref = _local_install_git(project, *args)
    assert default_ref == 'refs/remotes/origin/main'
    _local_install_git(project, 'switch', '--no-track', '-c', 'install-agentic-kit', default_ref)
    manifest = release_files.load_manifest(ROOT)
    assert _reference_first_install(ROOT, project, manifest) == {}
    record = json.loads((project / '.github/agentic-kit.json').read_bytes())
    assert record['features'] == {feature: feature == 'review_gate' for feature in release_files.FEATURES}
    checker = [sys.executable, '-I', '-B', str(ROOT / 'scripts/verify_install.py'),
               '--repo', str(project), '--ref', 'install-agentic-kit']
    # Verification is against the committed install branch, while origin stays unchanged.
    _local_install_git(project, 'add', '.')
    _local_install_git(project, '-c', 'user.name=Example Builder', '-c', 'user.email=builder@example.invalid',
                       'commit', '-m', 'first install')
    committed = subprocess.run(checker, capture_output=True, text=True)
    assert committed.returncode == 0, committed.stdout
    count = 1 + sum(e['kind'] in {'framework', 'adopter'} for e in manifest['entries'])
    assert committed.stdout.splitlines()[-1] == f'{count} passed, 0 failed'
    _trial_write(project, 'AGENTS.md', (ROOT / 'starter-kit/AGENTS.md').read_bytes())
    _local_install_git(project, '-c', 'user.name=Example Builder', '-c', 'user.email=builder@example.invalid',
                       'commit', '-am', 'unfinished agreement')
    failure = subprocess.run(checker, capture_output=True, text=True)
    assert failure.returncode == 1
    assert any(line.startswith('AGENTS.md FAIL ') for line in failure.stdout.splitlines())
    assert failure.stdout.splitlines()[-1] == f'{count - 1} passed, 1 failed'
