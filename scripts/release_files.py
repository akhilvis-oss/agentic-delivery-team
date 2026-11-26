"""Validate the carried-file manifest and generate a tag's release hash asset."""

import argparse
import fnmatch
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FEATURES = {'review_gate', 'product_files', 'runner', 'pool', 'lifecycle', 'standup'}
TAG = re.compile(r'v\d+\.\d+\.\d+')
SHA = re.compile(r'[0-9a-f]{40}')


def relative(value):
    if (not isinstance(value, str) or not value or value.startswith('/') or '\\' in value
            or ':' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(p in {'', '.', '..'} for p in value.split('/'))):
        raise ValueError('Invalid release manifest')
    return value


def validate_manifest(manifest):
    if (not isinstance(manifest, dict) or set(manifest) != {'schema', 'entries', 'kit_only', 'features'}
            or type(manifest['schema']) is not int or manifest['schema'] != 1 or not isinstance(manifest['entries'], list)
            or not manifest['entries'] or not isinstance(manifest['kit_only'], list)):
        raise ValueError('Invalid release manifest')
    features = manifest.get('features')
    if (not isinstance(features, dict) or set(features) != FEATURES
            or any(not isinstance(deps, list) or not set(deps) <= FEATURES
                   for deps in features.values())):
        raise ValueError('Invalid release manifest')
    seen = set()
    for item in manifest['entries']:
        if not isinstance(item, dict):
            raise ValueError('Invalid release manifest')
        kind = item.get('kind')
        fields = {'source', 'destination', 'kind'}
        fields |= {'feature'} if kind == 'adopter' else set()
        fields |= {'owner_action'} if kind in {'private', 'settings'} else set()
        if set(item) != fields or kind not in {'framework', 'adopter', 'private', 'settings'}:
            raise ValueError('Invalid release manifest')
        relative(item['source'])
        relative(item['destination'])
        key = (kind, item['destination'])
        if key in seen or kind == 'adopter' and item['feature'] not in FEATURES:
            raise ValueError('Invalid release manifest')
        seen.add(key)
        if kind in {'private', 'settings'} and (
                not isinstance(item['owner_action'], str) or not item['owner_action'].strip()):
            raise ValueError('Invalid release manifest')
    for pattern in manifest['kit_only']:
        relative(pattern)
    return manifest


def load_manifest(root=ROOT):
    return validate_manifest(json.loads((Path(root) / 'release-files.json').read_text()))


def classified(manifest, paths):
    sources = {item['source'] for item in manifest['entries']}
    return all(path in sources or any(fnmatch.fnmatchcase(path, p)
                                    for p in manifest['kit_only']) for path in paths)


def archive_files(data):
    result = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
        for member in archive:
            if member.isdir():
                continue
            parts = PurePosixPath(member.name).parts
            if len(parts) < 2 or not member.isfile():
                raise ValueError('Invalid release archive')
            name = relative('/'.join(parts[1:]))
            if name in result or member.size > 16 * 1024 * 1024:
                raise ValueError('Invalid release archive')
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError('Invalid release archive')
            result[name] = handle.read()
    return result


def hash_asset(repository, tag):
    if not TAG.fullmatch(tag):
        raise ValueError('Invalid release tag')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repository), *args],
                                       stderr=subprocess.DEVNULL)
    commit = git('rev-parse', '--verify', tag + '^{commit}').decode().strip()
    if not SHA.fullmatch(commit):
        raise ValueError('Invalid release commit')
    files = archive_files(git('archive', '--format=tar', '--prefix=release/', tag))
    manifest = validate_manifest(json.loads(files['release-files.json']))
    if not classified(manifest, files):
        raise ValueError('Unclassified release file')
    sources = {e['source'] for e in manifest['entries']}
    return {'schema': 1, 'tag': tag, 'commit': commit,
            'files': {s: hashlib.sha256(files[s]).hexdigest() for s in sorted(sources)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        asset = hash_asset(Path(args.repo), args.tag)
        Path(args.output).write_text(json.dumps(asset, indent=2) + '\n')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError):
        parser.exit(1, 'Release hashes could not be generated.\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
