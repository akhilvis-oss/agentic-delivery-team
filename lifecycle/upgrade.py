"""Opt-in main-track upgrades, with a launcher-readable atomic switch record."""

import os
from pathlib import Path
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error

from lifecycle import app_tokens, core, smoke, smoke_watch

KEYS = ('current', 'previous', 'pending', 'pending_since', 'pending_starts', 'refused', 'notice')
SWITCH = object()
DEFER = []
MISMATCH = "Upgrades are off: versions_dir does not match the launcher's folder."
UNREADABLE = "Upgrades are off: the switch record is unreadable."
_pending_timer = None


class PendingTimer(threading.Timer):
    """One daemon thread, checking the launcher's whole-second wall clock."""

    def __init__(self, deadline, clock, exit_func):
        super().__init__(0, self.watch)
        self.daemon = True
        self.deadline = deadline
        self.clock = clock
        self.exit_func = exit_func

    def watch(self):
        while not self.finished.is_set():
            remaining = self.deadline - int(self.clock())
            if remaining <= 0:
                self.exit_func(70)
                return
            self.finished.wait(min(remaining, 1))


def start_pending_timer(ctx, *, clock=time.time, limit=600, exit_func=os._exit):
    """Called only by serve's poll hook; an expired deadline exits synchronously."""
    global _pending_timer
    if _pending_timer is not None:
        return
    since = ctx[1]['pending_since']
    deadline = (int(since) if since else int(clock())) + limit
    if int(clock()) >= deadline:
        exit_func(70)
        return
    _pending_timer = PendingTimer(deadline, clock, exit_func)
    _pending_timer.start()


def cancel_pending_timer():
    if _pending_timer is not None:
        _pending_timer.cancel()


def validate(settings):
    if (not isinstance(settings, dict)
            or set(settings) not in (
                {'track', 'smoke_agent', 'versions_dir', 'initial_commit'},
                {'track', 'smoke_agent', 'versions_dir', 'initial_commit',
                 'framework_repository', 'framework_runner'})
            or settings['track'] != 'main'
            or not isinstance(settings['versions_dir'], str)
            or not Path(settings['versions_dir']).is_absolute()
            or not isinstance(settings['initial_commit'], str)
            or not core.SHA.fullmatch(settings['initial_commit'])
            or not isinstance(settings['smoke_agent'], str)
            or not core.ID.fullmatch(settings['smoke_agent'])):
        raise ValueError('Invalid private upgrade settings')
    if 'framework_repository' in settings:
        if (not isinstance(settings['framework_repository'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*',
                                    settings['framework_repository'])
                or not isinstance(settings['framework_runner'], str)
                or not core.LOGIN.fullmatch(settings['framework_runner'])):
            raise ValueError('Invalid private upgrade settings')


def empty():
    return dict.fromkeys(KEYS, '')


def validate_record(record):
    if set(record) != set(KEYS):
        raise ValueError('Invalid switch fields')
    for key in ('current', 'previous', 'pending'):
        if record[key] and not core.SHA.fullmatch(record[key]):
            raise ValueError('Invalid switch commit')
    for key in ('pending_since', 'pending_starts'):
        if record[key] and not re.fullmatch(r'[0-9]{1,12}', record[key]):
            raise ValueError('Invalid switch counter')
    if record['refused'] and not re.fullmatch(r'[0-9a-f]{40}( [0-9a-f]{40})*', record['refused']):
        raise ValueError('Invalid refused commits')
    if record['notice'] and not re.fullmatch(
            r'(Upgraded from [0-9a-f]{40} to [0-9a-f]{40}|'
            r'Rolled back from [0-9a-f]{40}: did not confirm)', record['notice']):
        raise ValueError('Invalid switch notice')
    return record


def read(folder):
    path = Path(folder) / 'switch'
    try:
        text = path.read_bytes().decode('utf-8')
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError('Unreadable switch record') from None
        return empty()
    record = {}
    lines = text.split('\n')
    if lines[-1] == '':
        lines.pop()
    for line in lines:
        key, separator, value = line.partition('=')
        if not separator or key in record:
            raise ValueError('Malformed switch record')
        record[key] = value
    return validate_record(record)


def write(folder, record):
    validate_record(record)
    folder = core.private(folder)
    fd, name = tempfile.mkstemp(prefix='.switch-', dir=folder)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(''.join(f'{key}={record[key]}\n' for key in KEYS))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, folder / 'switch')
    finally:
        Path(name).unlink(missing_ok=True)


def context(cfg):
    """Resolve only local state. Called inside the poll lock."""
    cfg.pop('_upgrade_notice', None)
    if 'upgrade' not in cfg:
        return None
    settings = cfg['upgrade']
    validate(settings)
    folder = Path(settings['versions_dir']).resolve()
    exported = os.environ.get('ADT_VERSIONS_DIR')
    if not exported or folder != Path(exported).resolve():
        cfg['_upgrade_notice'] = MISMATCH
        return None
    try:
        record = read(folder)
    except (OSError, ValueError, UnicodeError):
        cfg['_upgrade_notice'] = UNREADABLE
        return None
    cfg['_upgrade_notice'] = record['notice']
    return folder, record


def is_pending(ctx, install=None):
    if ctx is None:
        return False
    folder, record = ctx
    install = Path(install or Path(__file__).resolve().parents[1]).resolve()
    return bool(record['pending'] and install == (folder / record['pending'] / 'install').resolve())


def confirm(cfg, ctx):
    folder, record = ctx
    old = record['current'] or cfg['upgrade']['initial_commit']
    new = record['pending']
    record.update(previous=record['current'], current=new, pending='', pending_since='',
                  pending_starts='', notice=f'Upgraded from {old} to {new}')
    write(folder, record)
    cancel_pending_timer()
    cfg['_upgrade_notice'] = record['notice']


def refuse(folder, record, commit):
    record['refused'] = ' '.join(dict.fromkeys(record['refused'].split() + [commit]))
    write(folder, record)


def start_check(install, cfg):
    return subprocess.run(
        [sys.executable, '-I', str(install / 'lifecycle/service.py'),
         '--config', cfg['config_path'], 'upgrade-check'],
        cwd=install, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=120, check=False,
    ).returncode


def attempt(cfg, ctx, on_read=None):
    """Upgrade errors are advisory: every failure leaves normal polling available."""
    if ctx is None:
        return False
    folder, record = ctx
    try:
        claims = [core.read(p) for p in (Path(cfg['state_dir']) / 'claims').glob('*.json')]
        if record['pending'] or any((c['stage'] != 'finished' and not (c['stage'] == 'claimed'
                                      and c.get('waiting'))) or c.get('error')
                                    or c.get('cleanup_pending') for c in claims):
            return False
        settings = cfg['upgrade']
        repository = settings.get('framework_repository', cfg['repository'])
        public = 'framework_repository' in settings
        product = core.PublicGitHub(repository) if public else core.GitHub(cfg, 'product')
        if on_read is not None:
            on_read()
        commit = product.request('GET', '/commits/main')['sha']
        if not isinstance(commit, str) or not core.SHA.fullmatch(commit):
            return False
        running = record['current'] or cfg['upgrade']['initial_commit']
        if commit in {running, record['previous']} or commit in record['refused'].split():
            return False
        if public:
            statuses = product
        else:
            statuses = core.app_client(cfg, status_only=True)
        status = smoke_watch.smoke_status(statuses, commit, cfg['upgrade']['smoke_agent'],
                                          settings.get('framework_runner', cfg['actors']['runner']))
        if not status or status.get('state') != 'success':
            return False
        if product.request('GET', f'/compare/{running}...{commit}').get('status') != 'ahead':
            return False
        root = folder / commit
        if root.is_symlink():
            root.unlink()
        elif root.exists():
            shutil.rmtree(root)
        core.private(root)
        try:
            install, exported = smoke.export_candidate(
                {'framework_repository': repository}, commit, root, smoke.environment(core.private(root / 'home')))
        except core.Unavailable as error:
            if str(error) == 'Unsafe candidate archive':
                refuse(folder, record, commit)
            return False
        if exported != commit:
            return False
        shutil.rmtree(root / 'download')
        try:
            code = start_check(install, cfg)
        except subprocess.TimeoutExpired:
            code = 1
        except OSError:
            return False
        if code == 0:
            record.update(pending=commit, pending_starts='0', pending_since='', notice='')
            write(folder, record)
            return True
        if code != 75:
            refuse(folder, record, commit)
    except Exception:
        # Token minting, GitHub reads, export and local writes all retry next poll.
        return False
    return False


def clear(cfg, commit):
    if not core.SHA.fullmatch(commit):
        raise ValueError('Expected a 40-hex commit')
    if 'upgrade' not in cfg:
        raise ValueError('Upgrade settings required')
    with core.lock(Path(cfg['state_dir']) / 'service.lock') as acquired:
        if not acquired:
            raise core.Unavailable('A poll is active; retry upgrade-clear later')
        folder = Path(cfg['upgrade']['versions_dir']).resolve()
        record = read(folder)
        record['refused'] = ' '.join(c for c in record['refused'].split() if c != commit)
        write(folder, record)


def check(config_path):
    """Isolated child: import, load config and mint, without dispatching any work."""
    def timed_out(signum, frame):
        raise TimeoutError('Upgrade check GitHub deadline')

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(110)
    try:
        from lifecycle import service
        from digest import standup  # noqa: F401
        service.runner_module()
        cfg = core.load_config(config_path)
        service.validate_local(cfg)
        for role in cfg.get('app_auth', {}):
            app_tokens.get(cfg, role)
        return 0
    except app_tokens.AuthenticationError as error:
        message = str(error)
        transient = ('transport' in message or 'TLS' in message
                     or re.search(r'HTTP (?:5[0-9]{2}|429)\b', message))
        return 75 if transient else 1
    except urllib.error.HTTPError as error:
        return 75 if error.code == 429 or 500 <= error.code < 600 else 1
    except (TimeoutError, urllib.error.URLError, ssl.SSLError):
        return 75
    except Exception:
        return 1
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
