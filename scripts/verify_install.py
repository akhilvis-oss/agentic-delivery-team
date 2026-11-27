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

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
# -I excludes caller paths; add only this trusted export, never the adopter.
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location('trusted_release_files', ROOT / 'scripts/release_files.py')
release_files = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_files)

REPOSITORY = 'akhilvis-oss/agentic-delivery-team'
FEATURES = release_files.FEATURES
PLACEHOLDER = re.compile(r'<[^<>\r\n]+>')
HEX256 = re.compile(r'[0-9a-f]{64}')
# Complete historical v0.6.0 sample, independent of later template migrations.
SAMPLE_DOMAIN_RULE = {
    'tier': 'domain-critical',
    'reason': "Logic whose correctness users rely on (replace with your project's core calculations)",
    'patterns': ['src/pricing/*', 'tests/test_pricing*'],
}


class Fault(ValueError):
    """Fixed diagnostics only; never include untrusted exception text."""


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


def local_commit(repo, branch):
    ref = 'refs/heads/' + branch
    git(repo, 'check-ref-format', ref)
    sha = git(repo, 'show-ref', '--verify', '--hash', ref).decode().strip()
    if (not release_files.SHA.fullmatch(sha)
            or git(repo, 'cat-file', '-t', sha).strip() != b'commit'):
        raise ValueError('Invalid local branch')
    return sha


def committed_file(repo, commit, name, *, metadata_only=False):
    release_files.relative(name)
    row = git(repo, 'ls-tree', '-z', commit, '--', name).split(b'\0')
    if row == [b'']:
        raise Fault('committed file missing')
    if len(row) != 2 or not row[0].startswith(b'100644 blob ') and not row[0].startswith(b'100755 blob '):
        raise Fault('committed file not a regular file')
    oid = row[0].split(b' ', 2)[2].split(b'\t', 1)[0].decode()
    if not release_files.SHA.fullmatch(oid):
        raise ValueError('Invalid committed file')
    size = int(git(repo, 'cat-file', '-s', oid))
    if size > 16 * 1024 * 1024:
        raise Fault('committed file too large')
    return b'' if metadata_only else git(repo, 'cat-file', 'blob', oid)


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


def json_line(text, keys):
    """Locate the effective JSON value, including last-wins duplicate fields."""
    decoder = json.JSONDecoder()
    pos = 0
    for key in keys:
        pos += re.search(r'\S', text[pos:]).start()
        opening, pos, index, selected = text[pos], pos + 1, 0, None
        while True:
            pos += re.search(r'\S', text[pos:]).start()
            if opening == '{':
                field, pos = decoder.raw_decode(text, pos)
                pos = text.index(':', pos) + 1
                pos += re.search(r'\S', text[pos:]).start()
            else:
                field = index
            if field == key:
                selected = pos
            _, pos = decoder.raw_decode(text, pos)
            pos += re.search(r'\S', text[pos:]).start()
            if text[pos] != ',':
                break
            pos += 1
            index += 1
        pos = selected
    return text.count('\n', 0, pos) + 1


def validate_adopter(name, data, template=b'', *, repo=None):
    text = data.decode()
    tokens = set(PLACEHOLDER.findall(template.decode()))
    for number, line in enumerate(text.splitlines(), 1):
        if any(token in line for token in tokens):
            raise Fault(f'placeholder still unfilled; line {number}')
    if name == '.github/review-gate.json':
        value = json.loads(data)
        rules = value.get('tiers', {}).get('rules', [])
        keys = None
        if value.get('repo_url') == json.loads(template).get('repo_url'):
            keys = ['repo_url']
        elif SAMPLE_DOMAIN_RULE in rules:
            keys = ['tiers', 'rules', rules.index(SAMPLE_DOMAIN_RULE)]
        if keys is not None:
            raise Fault(f'placeholder still unfilled; line {json_line(text, keys)}')
    if name == '.github/agentic-kit.json':
        kit(data)
    elif name in {'.github/review-gate.json', '.github/task-lifecycle.json'}:
        # Validators read data from a disposable file, never import adopter code.
        # Never trust TMPDIR to be outside the export or adopter checkout.
        roots = [ROOT.resolve(), Path(repo).resolve()] if repo is not None else [ROOT.resolve()]
        candidates = [tempfile.tempdir, *(os.environ.get(k) for k in ('TMPDIR', 'TEMP', 'TMP')),
                      '/tmp', '/var/tmp']
        scratch = next(Path(p) for p in candidates if p and Path(p).is_dir() and all(
            not Path(p).resolve().is_relative_to(root) for root in roots))
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            path = Path(folder) / 'settings.json'
            path.write_bytes(data)
            if name.endswith('review-gate.json'):
                spec = importlib.util.spec_from_file_location('trusted_release_gate', ROOT / 'gate/review_gate.py')
                gate = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(gate)
                gate.load_config(path)
            else:
                from lifecycle import core
                core.load_config(path, local=False)
    elif name.endswith(('.md', '.yml', '.yaml')):
        pass
    else:
        raise ValueError('Unsupported adopter check')


def expected_hashes(manifest, hashes):
    version = kit(file_bytes(ROOT, 'templates/agentic-kit.example.json'))['version']
    def computed():
        return {e['source']: hashlib.sha256(file_bytes(ROOT, e['source'])).hexdigest()
                for e in manifest['entries']}
    if hashes is None:
        return {'schema': 1, 'tag': version, 'commit': None, 'files': computed()}
    value = json.loads(Path(hashes).read_text())
    if (not isinstance(value, dict) or set(value) != {'schema', 'tag', 'commit', 'files'}
            or type(value['schema']) is not int or value['schema'] != 1
            or not isinstance(value['tag'], str) or not release_files.TAG.fullmatch(value['tag'])
            or not isinstance(value['commit'], str) or not release_files.SHA.fullmatch(value['commit'])
            or not isinstance(value['files'], dict)
            or any(not isinstance(v, str) or not HEX256.fullmatch(v) for v in value['files'].values())):
        raise ValueError('Invalid release hashes')
    for name in value['files']:
        release_files.relative(name)
    if value['tag'] != version:
        raise Fault('hash file for another tag')
    if value['files'] != computed():
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


def verify(repo, *, hashes=None, service_install=None, ref=None, emit=print):
    passed = failed = 0
    def result(item, ok, reason):
        nonlocal passed, failed
        passed += bool(ok)
        failed += not ok
        emit(item + (' PASS ' if ok else ' FAIL ') + reason)
    def finish():
        emit(f'{passed} passed, {failed} failed')
        return int(bool(failed))
    def stop(item, reason):
        result(item, False, reason)
        return finish()
    if not Path(repo).exists():
        return stop('install', 'repository path not found')
    try:
        git(repo, 'rev-parse', '--git-dir')
    except Exception:
        return stop('install', 'not a git repository')
    try:
        manifest = release_files.load_manifest(ROOT)
        asset = expected_hashes(manifest, hashes)
    except Fault as exc:
        return stop('install', str(exc))
    except Exception:
        return stop('install', 'manifest or hash file unreadable or malformed')
    resolve = (lambda: local_commit(repo, ref)) if ref is not None else lambda: default_commit(repo)
    try:
        commit = resolve()
    except Exception:
        return stop('install', '--ref not found' if ref is not None else
                    'no origin/HEAD (run git remote set-head origin --auto)')
    kit_name = '.github/agentic-kit.json'
    try:
        raw = committed_file(repo, commit, kit_name)
    except Fault as exc:
        return stop(kit_name, 'kit file missing' if str(exc) == 'committed file missing' else str(exc))
    except Exception:
        return stop(kit_name, 'committed file missing')
    try:
        value = json.loads(raw)
        if isinstance(value, dict) and not release_files.TAG.fullmatch(str(value.get('version', ''))):
            return stop(kit_name, 'kit version not of the form vX.Y.Z')
        record = kit(raw)
    except Exception:
        return stop(kit_name, 'public policy invalid')
    result(kit_name, record['version'] == asset['tag'], 'release version')
    for entry in manifest['entries']:
        if entry['kind'] not in {'framework', 'adopter'} or entry['destination'] == kit_name:
            continue
        name = item = entry['destination']
        active = entry['kind'] == 'framework' or record['features'][entry['feature']]
        workflow = entry['source'] == 'templates/review-gate.yml'
        if workflow:
            item = 'gate-workflow'
            if active:
                try:
                    policy = json.loads(committed_file(repo, commit, '.github/task-lifecycle.json'))
                except Exception:
                    policy = {}  # Disabled policy contents need not be populated.
                name = policy.get('trusted_gate_workflow', '.github/workflows/review-gate.yml') if isinstance(policy, dict) else name
        reason = 'release bytes' if entry['kind'] == 'framework' else 'adopter validation'
        try:
            data = committed_file(repo, commit, name, metadata_only=workflow)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, Fault) else 'committed file missing'
            if workflow and reason == 'committed file missing':
                reason = 'gate workflow not found; set `trusted_gate_workflow`'
            result(item, False, reason)
            continue
        try:
            if entry['kind'] == 'framework':
                ok = hashlib.sha256(data).hexdigest() == asset['files'][entry['source']]
            else:
                if active and not workflow:
                    validate_adopter(name, data, file_bytes(ROOT, entry['source']), repo=repo)
                elif not active:
                    reason = 'present; feature off, validation skipped'
                ok = True
        except Fault as exc:
            ok, reason = False, str(exc)
        except Exception:
            ok, reason = False, 'public policy invalid'
        result(item, ok, reason)
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
        unchanged = resolve() == commit
    except Exception:
        unchanged = False
    result('default-branch' if ref is None else 'ref', unchanged, 'committed snapshot unchanged')
    return finish()


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--hashes')
    parser.add_argument('--ref')
    parser.add_argument('--service-install')
    args = parser.parse_args(argv)
    try:
        return verify(Path(args.repo), hashes=args.hashes, service_install=args.service_install, ref=args.ref)
    except Exception:
        print('install FAIL verification unavailable\n0 passed, 1 failed')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
