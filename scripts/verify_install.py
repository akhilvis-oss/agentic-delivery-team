"""Read-only verification using trusted release code and committed adopter data."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
# -I excludes caller paths; add only this trusted export, never the adopter.
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location('trusted_release_files', ROOT / 'scripts/release_files.py')
release_files = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_files)

REPOSITORY = 'akhilvis-oss/agentic-delivery-team'
FEATURES = release_files.FEATURES
PLACEHOLDER = re.compile(r'<[A-Z][^>\r\n]*>')
HEX256 = re.compile(r'[0-9a-f]{64}')
# A literal identifier grammar, not a general exemption for uppercase code spans.
ISIN_NOTATION = re.compile(r'(?<!`)`(?:<[a-z][a-z_]*>\.)+<ISIN>(?:\.<[a-z][a-z_]*>)+`(?!`)')
GATE_WORKFLOW = '.github/workflows/review-gate.yml'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'install FAIL invalid arguments\n')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_json(endpoint):
    request = urllib.request.Request(
        'https://api.github.com/repos/' + REPOSITORY + endpoint,
        headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'agentic-install-checker'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('Public read unavailable')
    return json.loads(data)


def public_archive(commit):
    if not release_files.SHA.fullmatch(commit):
        raise ValueError('Invalid release commit')
    request = urllib.request.Request(
        'https://codeload.github.com/' + REPOSITORY + '/tar.gz/' + commit,
        headers={'User-Agent': 'agentic-install-checker'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError('Public read unavailable')
    return release_files.archive_files(data)


def git(repo, *args):
    # Never inherit executable Git configuration, prompts, hooks or filters.
    env = {'PATH': os.defpath, 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull,
           'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': '',
           'GIT_NO_REPLACE_OBJECTS': '1'}
    if 'SYSTEMROOT' in os.environ:
        env['SYSTEMROOT'] = os.environ['SYSTEMROOT']
    return subprocess.check_output(
        ['git', '--literal-pathspecs', '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
         '-C', str(repo), *args], env=env, stderr=subprocess.DEVNULL, timeout=30)


def default_commit(repo):
    ref = git(repo, 'symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD').decode().strip()
    if not ref.startswith('refs/remotes/origin/') or any(c.isspace() for c in ref):
        raise ValueError('Default branch unavailable')
    sha = git(repo, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
    if not release_files.SHA.fullmatch(sha):
        raise ValueError('Default branch unavailable')
    return sha


def committed_file(repo, commit, name):
    release_files.relative(name)
    row = git(repo, 'ls-tree', '-z', commit, '--', name).split(b'\0')
    if len(row) != 2 or not row[0].startswith(b'100644 blob ') and not row[0].startswith(b'100755 blob '):
        raise ValueError('Missing or nonregular committed file')
    oid = row[0].split(b' ', 2)[2].split(b'\t', 1)[0].decode()
    if not release_files.SHA.fullmatch(oid):
        raise ValueError('Invalid committed file')
    size = int(git(repo, 'cat-file', '-s', oid))
    if size > 16 * 1024 * 1024:
        raise ValueError('Oversized committed file')
    return git(repo, 'cat-file', 'blob', oid)


def file_bytes(root, name):
    release_files.relative(name)
    if Path(root).is_symlink():
        raise ValueError('Nonregular release folder')
    root = Path(root).resolve()
    target = root / name
    if any(p.is_symlink() for p in [target, *target.parents] if p != root and root in p.parents):
        raise ValueError('Nonregular release file')
    if not target.is_file():
        raise ValueError('Missing release file')
    return target.read_bytes()


def kit(data):
    value = json.loads(data)
    if (not isinstance(value, dict) or value.get('kit') != 'agentic-delivery-team'
            or not isinstance(value.get('version'), str)
            or not release_files.TAG.fullmatch(value['version'])
            or not isinstance(value.get('features'), dict)
            or set(value['features']) != FEATURES
            or any(type(v) is not bool for v in value['features'].values())):
        raise ValueError('Invalid kit record')
    return value


def validate_adopter(name, data):
    if name == '.github/agentic-kit.json':
        kit(data)
    elif name in {'.github/review-gate.json', '.github/task-lifecycle.json'}:
        # Validators read data from a disposable file, never import adopter code.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_bytes(data)
            if name.endswith('review-gate.json'):
                spec = importlib.util.spec_from_file_location('trusted_release_gate', ROOT / 'gate/review_gate.py')
                gate = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(gate)
                return gate.load_config(path)
            else:
                from lifecycle import core
                core.load_config(path, local=False)
    elif name.endswith(('.md', '.yml', '.yaml')):
        if PLACEHOLDER.search(ISIN_NOTATION.sub('', data.decode()) if name.endswith('.md') else data.decode()):
            raise ValueError('Unfilled placeholder')
    else:
        raise ValueError('Unsupported adopter check')


def mapped_gate_workflow(repo, commit, name, asset):
    """An explicit alternate destination must preserve the trusted gate exactly."""
    release_files.relative(name)
    if not re.fullmatch(r'\.github/workflows/[A-Za-z0-9_-]+\.ya?ml', name):
        raise ValueError('Invalid gate workflow mapping')
    config = validate_adopter('.github/review-gate.json',
                              committed_file(repo, commit, '.github/review-gate.json'))
    # Require an explicit inventory, not the validator's default inventory.
    raw = json.loads(committed_file(repo, commit, '.github/review-gate.json'))
    if 'gate_paths' not in raw or name not in config['gate_paths']['workflows']:
        raise ValueError('Gate workflow not inventoried')
    data = committed_file(repo, commit, name)
    if hashlib.sha256(data).hexdigest() != asset['files']['templates/review-gate.yml']:
        raise ValueError('Gate workflow differs from trusted template')
    return data


def expected_hashes(manifest, hashes):
    version = kit(file_bytes(ROOT, 'templates/agentic-kit.example.json'))['version']
    sources = {item['source'] for item in manifest['entries']}
    computed = {s: hashlib.sha256(file_bytes(ROOT, s)).hexdigest() for s in sources}
    if hashes is None:
        return {'schema': 1, 'tag': version, 'commit': None, 'files': computed}
    value = json.loads(Path(hashes).read_text())
    if (not isinstance(value, dict) or set(value) != {'schema', 'tag', 'commit', 'files'}
            or type(value['schema']) is not int or value['schema'] != 1 or value['tag'] != version
            or not isinstance(value['commit'], str) or not release_files.SHA.fullmatch(value['commit'])
            or not isinstance(value['files'], dict) or value['files'] != computed
            or any(not HEX256.fullmatch(v) for v in value['files'].values())):
        raise ValueError('Invalid release hashes')
    return value


def current_commit(path):
    # The production service/launcher write key=value lines. Only current is data
    # needed by this checker; never validate, evaluate or emit the other values.
    values = []
    for line in Path(path).read_text().split('\n'):
        key, separator, value = line.partition('=')
        if key.strip() == 'current':
            if key != 'current' or separator != '=':
                raise ValueError('Malformed current commit')
            values.append(value)
    if len(values) != 1 or not release_files.SHA.fullmatch(values[0]):
        raise ValueError('Invalid current commit')
    return values[0]


def verify(repo, *, hashes=None, service_install=None, gate_workflow=None, emit=print):
    failed = False
    def result(item, ok, reason):
        nonlocal failed
        failed |= not ok
        emit(item + (' PASS ' if ok else ' FAIL ') + reason)
    try:
        manifest = release_files.load_manifest(ROOT)
        asset = expected_hashes(manifest, hashes)
        commit = default_commit(repo)
        record = kit(committed_file(repo, commit, '.github/agentic-kit.json'))
    except Exception:
        result('install', False, 'release or committed default branch unavailable')
        return 1
    result('.github/agentic-kit.json', record['version'] == asset['tag'], 'release version')
    for entry in manifest['entries']:
        if entry['kind'] not in {'framework', 'adopter'}:
            continue
        name = entry['destination']
        try:
            if gate_workflow is not None and name == GATE_WORKFLOW:
                data = mapped_gate_workflow(repo, commit, gate_workflow, asset)
            else:
                data = committed_file(repo, commit, name)
            if entry['kind'] == 'framework':
                ok = hashlib.sha256(data).hexdigest() == asset['files'][entry['source']]
            else:
                if name == '.github/agentic-kit.json' or record['features'][entry['feature']]:
                    validate_adopter(name, data)
                ok = True
        except Exception:
            ok = False
        reason = ('release bytes' if entry['kind'] == 'framework' else
                  'present; feature off, validation skipped'
                  if name != '.github/agentic-kit.json' and not record['features'][entry['feature']]
                  else 'adopter validation')
        result(name, ok, reason)
    if service_install is not None:
        try:
            folder = Path(service_install)
            switch = folder / 'switch'
            expected = asset['files']
            install = folder
            if switch.exists():
                if switch.is_symlink() or not switch.is_file():
                    raise ValueError('Invalid switch')
                current = current_commit(switch)
                tag = public_json('/commits/' + urllib.parse.quote(asset['tag'], safe=''))['sha']
                if (not release_files.SHA.fullmatch(tag)
                        or asset['commit'] is not None and asset['commit'] != tag):
                    raise ValueError('Invalid tag')
                comparison = public_json('/compare/' + tag + '...' + current)
                if comparison.get('status') not in {'identical', 'ahead'}:
                    raise ValueError('Older service')
                files = public_archive(current)
                current_manifest = release_files.validate_manifest(json.loads(files['release-files.json']))
                if (not release_files.classified(current_manifest, files)
                        or any(e['source'] not in files for e in current_manifest['entries'])):
                    raise ValueError('Incomplete current archive')
                expected = {s: hashlib.sha256(data).hexdigest() for s, data in files.items()}
                install = folder / current / 'install'
                if (folder / current).is_symlink() or install.is_symlink():
                    raise ValueError('Nonregular current install')
            sources = ({e['source'] for e in manifest['entries']
                        if e['kind'] in {'framework', 'private'}} if install == folder else set(expected))
            for source in sorted(sources):
                ok = hashlib.sha256(file_bytes(install, source)).hexdigest() == expected[source]
                result('service/' + source, ok, 'release bytes')
        except Exception:
            result('service', False, 'release installation unavailable or not current')
    try:
        unchanged = default_commit(repo) == commit
    except Exception:
        unchanged = False
    result('default-branch', unchanged, 'committed snapshot unchanged')
    return int(failed)


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--hashes')
    parser.add_argument('--service-install')
    parser.add_argument('--gate-workflow', help='Explicit trusted gate workflow destination')
    args = parser.parse_args(argv)
    try:
        return verify(Path(args.repo), hashes=args.hashes, service_install=args.service_install,
                      gate_workflow=args.gate_workflow)
    except Exception:
        print('install FAIL verification unavailable')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
