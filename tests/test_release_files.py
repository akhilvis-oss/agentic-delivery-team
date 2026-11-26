"""The release inventory is complete, safe and hashed from a tag's archive."""

import hashlib
import json
from pathlib import Path
import re
import subprocess

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
        assert 'python -I' in text
