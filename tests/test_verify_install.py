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
                for rule in value['tiers']['rules']:
                    if rule['tier'] == 'domain-critical':
                        rule['reason'] = 'Project domain logic'
                data = json.dumps(value).encode()
            elif entry['destination'] == '.github/task-lifecycle.json':
                value = json.loads((ROOT / '.github/task-lifecycle.json').read_bytes())
                value['trusted_gate_workflow'] = '.github/workflows/review-gate.yml'
                data = json.dumps(value).encode()
            elif entry['destination'].endswith('.md'):
                data = re.sub(rb'<[^<>\r\n]+>', b'example', data)
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
    tag = 'b' * 40
    current = tag if comparison == 'identical' else 'a' * 40
    install = folder / current / 'install'
    install.mkdir(parents=True)
    files = {'release-files.json': (ROOT / 'release-files.json').read_bytes()}
    for item in checker.release_files.load_manifest(ROOT)['entries']:
        files[item['source']] = (ROOT / item['source']).read_bytes()
    for name, data in files.items():
        path = install / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    from lifecycle import upgrade
    record = upgrade.empty()
    record['current'] = current
    upgrade.write(folder, record)
    # Exercise the real producer's key=value protocol; ignore all unrelated fields.
    with (folder / 'switch').open('a') as handle:
        handle.write('ignored=CANARY-SWITCH-FIELD\n')
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
    from lifecycle import upgrade
    record = upgrade.empty()
    upgrade.write(folder, record)
    assert verify(adopter, service_install=folder)[0] == 1
    record['current'] = 'a' * 40
    upgrade.write(folder, record)
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
    (adopter / 'docs/product/vision.md').write_text('<One or two sentences: the problem and why it matters now.>')
    commit(adopter)
    code, output = verify(adopter)
    assert code == 0 and 'docs/product/vision.md PASS present; feature off, validation skipped' in output
    (adopter / 'docs/product/vision.md').unlink()
    commit(adopter)
    assert verify(adopter)[0] == 1
    (adopter / 'docs/product/vision.md').write_text('<One or two sentences: the problem and why it matters now.>')
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
    from lifecycle import upgrade
    record = upgrade.empty()
    record['current'] = 'a' * 40
    upgrade.write(folder, record)
    monkeypatch.setattr(checker, 'public_json', lambda endpoint: {'sha': 'b' * 40, 'status': 'ahead'})
    code, output = verify(adopter, hashes=hashes, service_install=folder)
    assert code == 1 and 'service FAIL ' in output


@pytest.mark.parametrize('text', [
    'current=\n', 'current\n', 'previous=' + 'a' * 40 + '\n',
    'current=' + 'a' * 39 + '\n', 'current=' + 'A' * 40 + '\n',
    'current=' + 'a' * 40 + '\ncurrent=' + 'a' * 40 + '\n',
    'current=' + 'a' * 40 + '\ncurrent=' + 'b' * 40 + '\n',
    ' current=' + 'a' * 40 + '\n',
    'current =' + 'a' * 40 + '\ncurrent=' + 'a' * 40 + '\n',
    '{"current": "' + 'a' * 40 + '"}\n',
])
def test_main_track_switch_refuses_empty_malformed_and_duplicate_current(adopter, tmp_path, monkeypatch, text):
    folder = tmp_path / 'versions'
    folder.mkdir()
    (folder / 'switch').write_text(text + 'notice=CANARY-UNINSPECTED-FIELD\n')
    calls = []
    monkeypatch.setattr(checker, 'public_json', lambda endpoint: calls.append(endpoint))
    code, output = verify(adopter, service_install=folder)
    assert code == 1 and 'service FAIL ' in output
    assert calls == []
    assert 'CANARY-UNINSPECTED-FIELD' not in output


def test_install_setup_labels_uses_only_trusted_export(adopter, tmp_path):
    import os
    import shlex
    import sys
    matches = re.findall(r'`(python[^`]*setup-labels)`', (ROOT / 'INSTALL.md').read_text())
    assert len(matches) == 1
    marker = adopter / 'shadow-module-executed'
    (adopter / 'json.py').write_text(
        "from pathlib import Path\nPath('shadow-module-executed').touch()\nraise SystemExit(71)\n")
    args = shlex.split(matches[0])
    args[0] = sys.executable
    args = [arg.replace('$EXPORT', str(ROOT)).replace('$PRIVATE_CONFIG', str(tmp_path / 'absent-settings'))
            for arg in args]
    # Help reaches imports/entry parsing, but cannot load config, authenticate or write labels.
    result = subprocess.run([*args, '--help'], cwd=adopter, capture_output=True, text=True,
                            env={'PATH': os.defpath, 'PYTHONPATH': str(adopter),
                                 'PYTHONDONTWRITEBYTECODE': '1'})
    assert not marker.exists()
    assert result.returncode == 0


@pytest.mark.parametrize('replacement', ['commit', 'blob'])
def test_local_git_replacements_cannot_hide_changed_default_branch_bytes(adopter, replacement):
    # Healthy data still passes; replacement refs must not change what "committed" means.
    assert verify(adopter)[0] == 0
    name = 'lifecycle/core.py'
    good_commit = command(adopter, 'rev-parse', 'HEAD').decode().strip()
    good_blob = command(adopter, 'rev-parse', good_commit + ':' + name).decode().strip()
    bad_bytes = b'changed committed framework bytes\n'
    (adopter / name).write_bytes(bad_bytes)
    commit(adopter)
    bad_commit = command(adopter, 'rev-parse', 'HEAD').decode().strip()
    bad_blob = command(adopter, 'rev-parse', bad_commit + ':' + name).decode().strip()
    original, substitute = ((bad_commit, good_commit) if replacement == 'commit'
                            else (bad_blob, good_blob))
    command(adopter, 'replace', original, substitute)
    assert command(adopter, '--no-replace-objects', 'show', bad_commit + ':' + name) == bad_bytes
    assert checker.default_commit(adopter) == bad_commit
    before = {str(p.relative_to(adopter)): p.read_bytes()
              for p in adopter.rglob('*') if p.is_file()}
    code, output = verify(adopter)
    after = {str(p.relative_to(adopter)): p.read_bytes()
             for p in adopter.rglob('*') if p.is_file()}
    assert before == after
    assert checker.default_commit(adopter) == bad_commit
    assert code == 1
    assert name + ' FAIL release bytes' in output
    assert checker.committed_file(adopter, bad_commit, name) == bad_bytes


def edit_json(repo, name, edit):
    path = repo / name
    value = json.loads(path.read_text())
    edit(value)
    path.write_text(json.dumps(value, indent=2) + '\n')


@pytest.mark.parametrize('mode', ['renamed', 'standard', 'missing-configured', 'missing-standard'])
def test_gate_path_is_configured_or_fixed_without_guessing(adopter, mode):
    standard = adopter / '.github/workflows/review-gate.yml'
    configured = '.github/workflows/private-gate.yml'
    if mode in {'renamed', 'missing-configured'}:
        edit_json(adopter, '.github/task-lifecycle.json',
                  lambda value: value.update(trusted_gate_workflow=configured))
        if mode == 'renamed':
            standard.rename(adopter / configured)
    else:
        edit_json(adopter, '.github/task-lifecycle.json', lambda value: value.pop('trusted_gate_workflow'))
        if mode == 'missing-standard':
            standard.rename(standard.with_name('another-gate.yml'))
    # An existing selected file needs no recognizable gate invocation.
    if mode == 'standard':
        standard.write_text('name: ordinary workflow\n')
    commit(adopter)
    code, output = verify(adopter)
    assert code == int(mode.startswith('missing'))
    assert output.count('gate-workflow ') == 1
    assert configured not in output
    if code:
        assert 'gate-workflow FAIL gate workflow not found; set `trusted_gate_workflow`' in output


@pytest.mark.parametrize('configured', [False, True])
@pytest.mark.parametrize('enabled', [False, True])
def test_gate_workflow_checks_only_committed_metadata(adopter, monkeypatch, configured, enabled):
    name = '.github/workflows/review-gate.yml'
    if configured:
        configured_name = '.github/workflows/CANARY-PRIVATE-GATE.yml'
        edit_json(adopter, '.github/task-lifecycle.json',
                  lambda v: v.update(trusted_gate_workflow=configured_name))
        if enabled:
            (adopter / name).rename(adopter / configured_name)
            name = configured_name
    else:
        edit_json(adopter, '.github/task-lifecycle.json', lambda v: v.pop('trusted_gate_workflow'))
    edit_json(adopter, '.github/agentic-kit.json', lambda v: v['features'].update(review_gate=enabled))
    # Even same-template tokens are outside the workflow's metadata-only contract.
    (adopter / name).write_bytes(b'<PROJECT_NAME>\nCANARY-PRIVATE-WORKFLOW\xff')
    commit(adopter)
    oid = command(adopter, 'rev-parse', 'HEAD:' + name).decode().strip()
    asset = checker.expected_hashes(checker.release_files.load_manifest(ROOT), None)
    monkeypatch.setattr(checker, 'expected_hashes', lambda manifest, hashes: asset)
    original_git, original_bytes = checker.git, checker.file_bytes
    calls = []
    def metadata_git(repo, *args):
        calls.append(args)
        assert args != ('cat-file', 'blob', oid), 'workflow payload opened'
        return original_git(repo, *args)
    def trusted_bytes(root, source):
        assert source != 'templates/review-gate.yml', 'workflow placeholder oracle opened'
        return original_bytes(root, source)
    monkeypatch.setattr(checker, 'git', metadata_git)
    monkeypatch.setattr(checker, 'file_bytes', trusted_bytes)
    code, output = verify(adopter)
    assert code == 0
    assert ('cat-file', '-s', oid) in calls
    assert ('cat-file', 'blob', oid) not in calls
    assert output.count('gate-workflow PASS ') == 1
    assert 'CANARY-PRIVATE' not in output


@pytest.mark.parametrize('field', ['repo_url', 'tiers', 'rules'])
def test_json_placeholder_line_uses_effective_duplicate_field(adopter, field):
    path = adopter / '.github/review-gate.json'
    value = json.loads(path.read_text())
    if field == 'repo_url':
        bad = json.loads((ROOT / 'templates/review-gate.example.json').read_text())['repo_url']
    elif field == 'rules':
        bad = [checker.SAMPLE_DOMAIN_RULE]
    else:
        bad = dict(value['tiers'], rules=[checker.SAMPLE_DOMAIN_RULE])
    text = json.dumps(value)
    container = value['tiers'] if field == 'rules' else value
    # Build duplicates without changing any other parsed fields or policy checks.
    needle = json.dumps(field) + ': ' + json.dumps(container[field])
    replacement = needle + ',\n' + json.dumps(field) + ':\n' + json.dumps(bad)
    text = text.replace(needle, replacement, 1)
    path.write_text(text)
    commit(adopter)
    line = text.count('\n', 0, text.index(json.dumps(bad), text.index(',\n'))) + 1
    code, output = verify(adopter)
    assert code == 1
    assert f'.github/review-gate.json FAIL placeholder still unfilled; line {line}' in output


def test_unchanged_shipped_pr_template_passes_adopter_validation(adopter):
    name = '.github/pull_request_template.md'
    shipped = (ROOT / 'templates/pull_request_template.md').read_bytes()
    (adopter / name).write_bytes(shipped)
    commit(adopter)
    assert command(adopter, 'show', 'HEAD:' + name) == shipped
    code, output = verify(adopter)
    assert code == 0, output
    assert name + ' PASS adopter validation' in output.splitlines()


def test_roadmap_comment_does_not_hide_unfilled_date(adopter):
    name = 'docs/product/roadmap.md'
    edit_json(adopter, '.github/agentic-kit.json',
              lambda value: value['features'].update(product_files=True))
    (adopter / name).write_text('<!-- roadmap note -->\n<date>\n')
    commit(adopter)
    code, output = verify(adopter)
    assert code == 1
    assert name + ' FAIL placeholder still unfilled; line 2' in output.splitlines()


@pytest.mark.parametrize(('name', 'text', 'expected'), [
    ('AGENTS.md', '<country>.<ISIN>.<exchange>', 0),
    ('AGENTS.md', '<date>', 0),  # Token belongs only to the roadmap.
    ('AGENTS.md', 'filled\n<PROJECT_NAME>', 1),
    ('docs/product/roadmap.md', 'filled\n<date>', 1),
    ('docs/product/roadmap.md', 'filled\n<#issue>', 1),
])
def test_only_exact_same_template_tokens_fail(adopter, name, text, expected):
    if name == 'docs/product/roadmap.md':
        edit_json(adopter, '.github/agentic-kit.json',
                  lambda value: value['features'].update(product_files=True))
    (adopter / name).write_text(text)
    commit(adopter)
    code, output = verify(adopter)
    assert code == expected
    if expected:
        assert name + ' FAIL placeholder still unfilled; line 2' in output
        assert text not in output


@pytest.mark.parametrize('text', ['filled\n<date>', 'filled\n<#issue>'])
def test_unfilled_roadmap_is_skipped_in_gate_only_install(adopter, text):
    name = 'docs/product/roadmap.md'
    (adopter / name).write_text(text)
    commit(adopter)
    code, output = verify(adopter)
    assert code == 0
    assert name + ' PASS present; feature off, validation skipped' in output
    assert text not in output


@pytest.mark.parametrize('sentinel', ['repo-url', 'sample-rule', 'filled-owner-url'])
def test_gate_json_whole_sentinels_and_exact_locations(adopter, sentinel):
    source = json.loads((ROOT / 'templates/review-gate.example.json').read_text())
    def edit(value):
        # Decoys must not affect the location of the actual JSON value.
        value['_comment'] = 'repo_url tiers rules CANARY-PRIVATE'
        if sentinel == 'repo-url':
            value['repo_url'] = source['repo_url']
        elif sentinel == 'sample-rule':
            value['tiers']['rules'].insert(0, checker.SAMPLE_DOMAIN_RULE.copy())
        else:
            value['repo_url'] = 'https://github.com/OWNER-tools/REPO-library'
    edit_json(adopter, '.github/review-gate.json', edit)
    commit(adopter)
    code, output = verify(adopter)
    assert code == (sentinel != 'filled-owner-url')
    if code:
        text = (adopter / '.github/review-gate.json').read_text()
        needle = '"repo_url": ' if sentinel == 'repo-url' else '"rules": [\n'
        pos = text.index(needle) + len(needle)
        line = text.count('\n', 0, pos) + 1
        assert f'.github/review-gate.json FAIL placeholder still unfilled; line {line}' in output
    assert 'CANARY-PRIVATE' not in output and 'OWNER-tools' not in output


def test_historical_sample_survives_current_template_migration(adopter, monkeypatch):
    edit_json(adopter, '.github/review-gate.json',
              lambda value: value['tiers']['rules'].append({
                  'tier': 'domain-critical',
                  'reason': "Logic whose correctness users rely on (replace with your project's core calculations)",
                  'patterns': ['src/pricing/*', 'tests/test_pricing*'],
              }))
    commit(adopter)
    original = checker.file_bytes
    def migrated(root, name):
        data = original(root, name)
        if name == 'templates/review-gate.example.json':
            value = json.loads(data)
            value['tiers']['rules'] = [r for r in value['tiers']['rules'] if r != checker.SAMPLE_DOMAIN_RULE]
            return json.dumps(value).encode()
        return data
    monkeypatch.setattr(checker, 'file_bytes', migrated)
    assert command(adopter, 'tag', '--list') == b''
    assert '.github/review-gate.json FAIL placeholder still unfilled;' in verify(adopter)[1]


def test_agents_activation_follows_manifest_mapping(adopter, monkeypatch):
    manifest = checker.release_files.load_manifest(ROOT)
    entry = next(e for e in manifest['entries'] if e['destination'] == 'AGENTS.md')
    entry['feature'] = 'review_gate'  # Local #266 mapping fixture, no production exception.
    monkeypatch.setattr(checker.release_files, 'load_manifest', lambda root: manifest)
    edit_json(adopter, '.github/agentic-kit.json', lambda v: v['features'].update(product_files=False))
    (adopter / 'AGENTS.md').write_text('<PROJECT_NAME>')
    commit(adopter)
    assert 'AGENTS.md FAIL placeholder still unfilled; line 1' in verify(adopter)[1]


@pytest.mark.parametrize('bad', ['wrong-tag', 'bad-tag', 'bad-hash', 'inventory', 'unreadable', 'json'])
def test_hash_asset_diagnostics_check_tag_before_inventory(adopter, tmp_path, bad):
    asset = checker.expected_hashes(checker.release_files.load_manifest(ROOT), None)
    asset['commit'] = 'a' * 40
    path = tmp_path / 'hashes.json'
    if bad == 'wrong-tag':
        asset.update(tag='v999.0.0', files={'other-release.py': 'a' * 64})
    elif bad == 'bad-tag':
        asset['tag'] = 'CANARY-PRIVATE-TAG'
    elif bad == 'bad-hash':
        asset['files'] = {'other-release.py': 123}
    elif bad == 'inventory':
        asset['files'] = {'other-release.py': 'a' * 64}
    if bad != 'unreadable':
        path.write_text('{' if bad == 'json' else json.dumps(asset))
    code, output = verify(adopter, hashes=path)
    reason = 'hash file for another tag' if bad == 'wrong-tag' else 'manifest or hash file unreadable or malformed'
    assert code == 1 and output == f'install FAIL {reason}\n0 passed, 1 failed'
    assert 'CANARY' not in output


@pytest.mark.parametrize('fault', ['absent', 'nongit', 'no-head', 'manifest'])
def test_distinct_preflight_reasons(adopter, tmp_path, monkeypatch, fault):
    repo = adopter
    if fault == 'absent':
        repo = tmp_path / 'absent'
        reason = 'repository path not found'
    elif fault == 'nongit':
        repo = tmp_path
        reason = 'not a git repository'
    elif fault == 'no-head':
        command(adopter, 'symbolic-ref', '--delete', 'refs/remotes/origin/HEAD')
        reason = 'no origin/HEAD (run git remote set-head origin --auto)'
    else:
        def broken(root):
            raise ValueError('CANARY-PRIVATE-MANIFEST')
        monkeypatch.setattr(checker.release_files, 'load_manifest', broken)
        reason = 'manifest or hash file unreadable or malformed'
    assert verify(repo) == (1, f'install FAIL {reason}\n0 passed, 1 failed')


@pytest.mark.parametrize(('name', 'fault', 'reason'), [
    ('.github/agentic-kit.json', 'missing', 'kit file missing'),
    ('.github/agentic-kit.json', 'version', 'kit version not of the form vX.Y.Z'),
    ('.github/agentic-kit.json', 'invalid', 'public policy invalid'),
    ('lifecycle/core.py', 'missing', 'committed file missing'),
    ('lifecycle/core.py', 'symlink', 'committed file not a regular file'),
    ('lifecycle/core.py', 'large', 'committed file too large'),
    ('lifecycle/core.py', 'changed', 'release bytes'),
    ('.github/review-gate.json', 'invalid', 'public policy invalid'),
    ('.github/task-lifecycle.json', 'invalid', 'public policy invalid'),
    ('.github/workflows/review-gate.yml', 'symlink', 'committed file not a regular file'),
    ('.github/workflows/review-gate.yml', 'large', 'committed file too large'),
])
def test_file_faults_have_distinct_fixed_item_reasons(adopter, name, fault, reason):
    path = adopter / name
    if fault == 'missing':
        path.unlink()
    elif fault == 'symlink':
        path.unlink()
        path.symlink_to('CANARY-PRIVATE-TARGET')
    elif fault == 'large':
        path.write_bytes(b'x' * (16 * 1024 * 1024 + 1))
    elif fault == 'version':
        edit_json(adopter, name, lambda value: value.update(version='CANARY-PRIVATE-VERSION'))
    else:
        path.write_text('CANARY-PRIVATE-INVALID')
    if name == '.github/task-lifecycle.json':
        edit_json(adopter, '.github/agentic-kit.json', lambda v: v['features'].update(lifecycle=True))
    commit(adopter)
    code, output = verify(adopter)
    item = 'gate-workflow' if name.endswith('review-gate.yml') else name
    assert code == 1 and item + ' FAIL ' + reason in output
    assert 'CANARY-PRIVATE' not in output


@pytest.mark.parametrize('broken', [False, True])
def test_summary_reports_each_item_once(adopter, broken):
    if broken:
        (adopter / 'lifecycle/core.py').write_text('changed')
        commit(adopter)
    code, output = verify(adopter)
    *items, summary = output.splitlines()
    assert code == broken
    assert len({line.split()[0] for line in items}) == len(items)
    assert summary == f'{len(items) - int(broken)} passed, {int(broken)} failed'
    assert sum(' FAIL ' in line for line in items) == broken


def test_local_unpushed_branch_checks_committed_bytes_and_cli(adopter, capsys):
    command(adopter, 'switch', '-c', 'install-candidate')
    (adopter / 'lifecycle/core.py').write_text('changed')
    command(adopter, 'add', '.')
    command(adopter, 'commit', '-m', 'local branch')
    assert verify(adopter)[0] == 0
    assert checker.main(['--repo', str(adopter), '--ref', 'install-candidate']) == 1
    assert 'lifecycle/core.py FAIL release bytes' in capsys.readouterr().out


@pytest.mark.parametrize('branch', ['absent', 'main~0', 'main^{commit}', 'refs/heads/main', '../main', '--all'])
def test_ref_rejects_revision_expressions_and_nonlocal_refs(adopter, branch):
    assert verify(adopter, ref=branch) == (1, 'install FAIL --ref not found\n0 passed, 1 failed')


def test_ref_does_not_resolve_tag_suffix_of_branch(adopter):
    command(adopter, 'tag', 'refs/heads/absent', 'HEAD')
    assert verify(adopter, ref='absent') == (1, 'install FAIL --ref not found\n0 passed, 1 failed')


def test_local_ref_must_point_to_commit_object(adopter):
    blob = command(adopter, 'rev-parse', 'HEAD:AGENTS.md').decode().strip()
    # Loose malformed ref emulates corruption; Git's update-ref refuses it.
    (adopter / '.git/refs/heads/broken').write_text(blob + '\n')
    assert verify(adopter, ref='broken') == (1, 'install FAIL --ref not found\n0 passed, 1 failed')


@pytest.mark.parametrize('local', [False, True])
def test_snapshot_drift_is_rechecked(adopter, local):
    initial = command(adopter, 'rev-parse', 'HEAD').decode().strip()
    command(adopter, 'commit', '--allow-empty', '-m', 'next snapshot')
    newer = command(adopter, 'rev-parse', 'HEAD').decode().strip()
    command(adopter, 'update-ref', 'refs/heads/candidate', initial)
    lines = []
    def emit(line):
        lines.append(line)
        if len(lines) == 1:
            command(adopter, 'update-ref', 'refs/heads/candidate' if local else 'refs/remotes/origin/main', newer)
    assert checker.verify(adopter, ref='candidate' if local else None, emit=emit) == 1
    item = 'ref' if local else 'default-branch'
    assert item + ' FAIL committed snapshot unchanged' in lines
    assert lines[-1].endswith(', 1 failed')


@pytest.mark.parametrize('bytecode_flag', [False, True])
def test_export_is_unchanged_even_with_scratch_environment_in_export(adopter, tmp_path, bytecode_flag):
    import os
    import sys
    export = tmp_path / 'export'
    shutil.copytree(ROOT, export, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', '.ruff_cache'))
    def snapshot():
        return {str(p.relative_to(export)): p.read_bytes() if p.is_file() else None for p in export.rglob('*')}
    before = snapshot()
    result = subprocess.run([sys.executable, '-I', *(['-B'] if bytecode_flag else []),
                             str(export / 'scripts/verify_install.py'), '--repo', str(adopter)],
                            capture_output=True, text=True,
                            env={'PATH': os.defpath, 'TMPDIR': str(export), 'TEMP': str(export), 'TMP': str(export)})
    assert result.returncode == 0, result.stdout
    assert snapshot() == before
    assert not list(export.rglob('__pycache__'))


def test_validator_scratch_stays_outside_export_and_checkout(adopter, monkeypatch):
    original = checker.tempfile.TemporaryDirectory
    seen = []
    def temporary(*args, **kwargs):
        scratch = Path(kwargs['dir']).resolve()
        assert not scratch.is_relative_to(ROOT)
        assert not scratch.is_relative_to(adopter)
        seen.append(scratch)
        return original(*args, **kwargs)
    monkeypatch.setattr(checker.tempfile, 'tempdir', str(adopter))
    monkeypatch.setenv('TMPDIR', str(ROOT))
    monkeypatch.setattr(checker.tempfile, 'TemporaryDirectory', temporary)
    assert verify(adopter)[0] == 0
    assert seen


@pytest.mark.parametrize('field', ['tier', 'reason', 'patterns', 'extra'])
def test_only_the_complete_unchanged_sample_rule_is_flagged(adopter, field):
    sample = {
        'tier': 'domain-critical',
        'reason': "Logic whose correctness users rely on (replace with your project's core calculations)",
        'patterns': ['src/pricing/*', 'tests/test_pricing*'],
    }
    sample[field] = {'tier': 'standard', 'reason': 'Project calculations',
                     'patterns': ['src/domain/*'], 'extra': 'project note'}[field]
    edit_json(adopter, '.github/review-gate.json', lambda v: v['tiers']['rules'].append(sample))
    commit(adopter)
    assert verify(adopter)[0] == 0


def test_configured_workflow_path_is_committed_data(adopter):
    edit_json(adopter, '.github/task-lifecycle.json',
              lambda value: value.update(trusted_gate_workflow='.github/workflows/absent.yml'))
    assert verify(adopter)[0] == 0
    commit(adopter)
    assert 'gate-workflow FAIL gate workflow not found;' in verify(adopter)[1]


def test_kit_regular_file_controls_are_not_called_missing(adopter):
    path = adopter / '.github/agentic-kit.json'
    path.unlink()
    path.symlink_to('CANARY-PRIVATE-KIT')
    commit(adopter)
    assert verify(adopter) == (1, '.github/agentic-kit.json FAIL committed file not a regular file\n0 passed, 1 failed')


def test_wrong_tag_is_diagnosed_before_loading_current_export_inventory(adopter, tmp_path, monkeypatch):
    path = tmp_path / 'hashes.json'
    path.write_text(json.dumps({'schema': 1, 'tag': 'v999.0.0', 'commit': 'a' * 40,
                                'files': {'previous-release.py': 'b' * 64}}))
    original = checker.file_bytes
    def unavailable(root, name):
        if name == 'lifecycle/core.py':
            raise FileNotFoundError('current inventory must not be needed')
        return original(root, name)
    monkeypatch.setattr(checker, 'file_bytes', unavailable)
    assert verify(adopter, hashes=path) == (1, 'install FAIL hash file for another tag\n0 passed, 1 failed')
