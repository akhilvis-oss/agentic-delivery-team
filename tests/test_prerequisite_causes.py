"""Fixed prerequisite notices and private CLI diagnostics; synthetic tools only."""

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys

import pytest

from lifecycle import core, service
from test_lifecycle import Hub, policy
from test_lifecycle_continuation import saved_story, newest_claim
from test_one_continuation import reapprove
import test_runner as fixtures

setup = fixtures.setup
runner = fixtures.runner
CHECK_CLI = runner.check_cli
HINT = ("The agent CLI's version check failed: the installed CLI may have changed, "
        "or its version command didn't run. Compare it with the runner's pinned `version_pattern`.")
FIXED = [runner.block_reason(name)['reason'] for name in
         ('command_preflight', 'agent_compatibility', 'assignment_preflight', 'required_checks')]
MISSING = ['Configured command program missing or not executable',
           'Configured version_command program missing or not executable',
           'Configured required check 12 program missing or not executable']


def stop_job(cause, *, continuation=False):
    return dict(id='prerequisite', round=1, status='blocked',
                assignment={'lifecycle': {'issue': 1, 'event': 101}},
                head_commit='b' * 40, continuation=continuation,
                blocked_reason={'reason_code': 'prerequisite_failed', 'reason': cause})


def sent_notice(tmp_path, cause, *, continuation=False):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-progress')
    job = stop_job(cause, continuation=continuation)
    core.sync_job(cfg['config_path'], job, hub.client())
    record, = core.records(cfg, hub.comments_by_issue[1], 1)
    notice, = [c for c in hub.comments_by_issue[1] if record['key'] in c['body']]
    return cfg, hub, job, record, notice['body']


@pytest.mark.parametrize('cause', FIXED + MISSING)
def test_fixed_prerequisite_cause_reaches_transport_context_once(tmp_path, cause):
    cfg, hub, job, record, body = sent_notice(tmp_path, cause)
    assert body.startswith('Needs a fix: ' + cause + '\n')
    assert body.count(cause) == 1
    assert body.index(cause) < body.index('Task lifecycle record')
    assert cause not in json.dumps(record)
    entry, = [core.read(p) for p in (Path(cfg['state_dir']) / 'outbox').glob('*.json')]
    assert entry['notice_context']['prerequisite_cause'] == cause
    before = list(hub.comments_by_issue[1])
    core.sync_job(cfg['config_path'], job, hub.client())
    assert hub.comments_by_issue[1] == before


@pytest.mark.parametrize('saved', [False, True])
def test_prerequisite_text_uses_record_saved_work_signal(tmp_path, saved):
    _, _, _, record, body = sent_notice(tmp_path, FIXED[0], continuation=saved)
    expected = "A required tool or safety check couldn't run or failed. Owner: fix it, then re-approve"
    assert expected + ('.' if saved else ' to start again.') in body
    saved_line = ('Saved work is available. Re-approve to continue it, or add '
                  'Restart: fresh under Owner decisions and re-approve to restart fresh.')
    assert body.count(saved_line) == int(saved)
    assert record.get('continuation', False) is saved
    assert 'authorize a new task' not in body


@pytest.mark.parametrize('cause', [None, [], {}, 'UNTRUSTED-DIAGNOSTIC',
    'Agent compatibility check failed.\nUNTRUSTED-DIAGNOSTIC',
    'Configured required check 0 program missing or not executable',
    'Configured required check 01 program missing or not executable',
    'Configured required check 1000000 program missing or not executable',
    'Configured command program missing or not executable extra'])
def test_transport_rejects_unrecognized_prerequisite_context(tmp_path, cause):
    cfg = policy(tmp_path)
    record = core.public_record(cfg, 1, 'fixed', 1, 'blocked', event=101,
                                reason='prerequisite_failed')
    body = core.notice_body(cfg, None, {'record': record,
                                      'notice_context': {'prerequisite_cause': cause}})
    assert body == core.stop_notice('prerequisite_failed', core.record_body(record))
    assert 'UNTRUSTED-DIAGNOSTIC' not in body


def test_agent_compatibility_notice_adds_only_fixed_hint(tmp_path):
    _, _, _, _, body = sent_notice(tmp_path, FIXED[1])
    assert HINT in body
    _, _, _, _, other = sent_notice(tmp_path / 'other', FIXED[0])
    assert HINT not in other


def cli_config(cfg, code):
    cfg['agent'] = fixtures.agent_config(sys.executable)
    cfg['agent']['version_command'] = [sys.executable, '-c', code]
    cfg['agent']['version_pattern'] = r'fixture-cli 1'
    return cfg


@pytest.mark.parametrize('version', ['unexpected-cli 2', ''])
def test_check_cli_classifies_its_own_mismatch(setup, version):
    cfg, _, _, _ = setup
    cli_config(cfg, 'print(' + repr(version) + ')')
    with pytest.raises(runner.Blocked) as error:
        CHECK_CLI(cfg)
    assert error.value.blocked_reason == runner.block_reason('agent_compatibility')
    assert json.dumps(version) in str(error.value)


@pytest.mark.parametrize('failure', ['nonzero', 'timeout', 'oserror', 'decode'])
def test_failed_version_probe_has_compatibility_cause(setup, monkeypatch, failure):
    cfg, _, _, _ = setup
    cli_config(cfg, "print('unused')")
    def fail(*args, **kwargs):
        if failure == 'nonzero':
            raise subprocess.CalledProcessError(3, ['synthetic'], output='failed-cli 2\n')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(['synthetic'], 10, output=b'partial-cli 2\n')
        if failure == 'decode':
            raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'synthetic invalid version bytes')
        raise OSError('synthetic unavailable probe')
    monkeypatch.setattr(runner.subprocess, 'run', fail)
    with pytest.raises(runner.Blocked) as error:
        CHECK_CLI(cfg)
    assert error.value.blocked_reason == runner.block_reason('agent_compatibility')
    assert 'synthetic unavailable probe' not in str(error.value)
    found = {'nonzero': 'failed-cli 2', 'timeout': 'partial-cli 2', 'oserror': '', 'decode': ''}[failure]
    assert json.dumps(found) in str(error.value)


def test_found_version_is_quoted_capped_private_and_absent_from_notice(setup, tmp_path, monkeypatch):
    cfg, state, path, _ = setup
    version = 'PRIVATE-VERSION-MARKER\n' + 'x' * 240 + 'tail'
    cli_config(cfg, 'print(' + repr(version) + ')')
    monkeypatch.setattr(runner, 'check_cli', CHECK_CLI)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job['status'] == 'blocked'
    assert job['blocked_reason'] == runner.block_reason('agent_compatibility')
    quoted = job['error'].split('Found version: ', 1)[1]
    assert json.loads(quoted) == version[:200]
    public = runner.public_receipt(runner.read(state / 'receipts/task.json'))
    assert 'PRIVATE-VERSION-MARKER' not in json.dumps(public)
    _, _, _, _, body = sent_notice(tmp_path / 'notice', job['blocked_reason']['reason'])
    assert HINT in body
    assert 'PRIVATE-VERSION-MARKER' not in body
    assert 'x' * 40 not in body


def test_pool_phase_compatibility_failure_keeps_its_cause(setup, monkeypatch, tmp_path):
    cfg, state, path, _ = setup
    selected = cli_config(dict(cfg), "print('pool-cli 2')")
    selected['pool_slot'] = 'fixture-slot'
    @contextmanager
    def phase(config, job, role):
        yield selected
    cfg['phase_context'] = phase
    monkeypatch.setattr(runner, 'check_cli',
                        lambda config: CHECK_CLI(config) if config.get('pool_slot') else 'fixture-cli 1')
    runner.run_once(cfg)
    job = runner.read(path)
    assert job['phase_slots'][0]['slot'] == 'fixture-slot'
    assert job['status'] == 'blocked'
    assert job['blocked_reason'] == runner.block_reason('agent_compatibility')
    assert 'pool-cli 2' in job['error']
    _, _, _, _, body = sent_notice(tmp_path / 'notice', job['blocked_reason']['reason'])
    assert HINT in body and 'pool-cli 2' not in body
    assert 'pool-cli 2' not in json.dumps(runner.public_receipt(runner.read(state / 'receipts/task.json')))


def test_prerequisite_reapproval_continues_existing_saved_head(tmp_path, monkeypatch):
    cfg, hub, _, old = saved_story(tmp_path, monkeypatch)
    old.update(status='needs_owner', blocked_reason=runner.block_reason('required_checks'))
    path = (Path(cfg['state_dir']) / 'jobs' / old['id'] / 'runner-state'
            / 'tasks' / (old['id'] + '.json'))
    core.save(path, old)
    stop = next(r for r in core.records(cfg, hub.comments_by_issue[1], 1) if r.get('continuation'))
    replacement = core.public_record(cfg, 1, old['id'], stop['round'], stop['to'],
        commit=old['head_commit'], event=stop['event'], reason='prerequisite_failed', continuation=True)
    replacement.update(actor=stop['actor'], **{'from': stop['from']})
    for comment in hub.comments_by_issue[1]:
        if stop['key'] in comment['body']:
            comment['body'] = core.record_body(replacement)
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    assert claim['continued_from'] == old['head_commit']
    assert claim['previous_stop_reason'] == 'prerequisite_failed'
    assert Path(claim['worktree'], 'delivery.txt').exists()


def test_saved_prerequisite_notice_keeps_findings_and_failed_checks(tmp_path):
    cfg = policy(tmp_path)
    record = core.public_record(cfg, 1, 'with-details', 1, 'blocked', event=101,
        reason='prerequisite_failed', commit='b' * 40, continuation=True,
        failed_tests={'count': 1, 'names': ['test_required_check']},
        stop_details={'summary': 'The required check could not finish.', 'findings': [
            {'severity': 'P1', 'criterion': 'AC-5', 'detail': 'Required evidence is missing.'}]})
    body = core.record_body(record)
    assert 'Failing tests: 1.' in body
    assert 'test_required_check' in body
    assert 'The required check could not finish.' in body
    assert 'Open finding: P1 — AC-5' in body
    assert 'Required evidence is missing.' in body
    assert 'Saved work is available.' in body
