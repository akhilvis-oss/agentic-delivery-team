"""Integrity stops use a distinct public code while retaining private refusal evidence."""

import datetime as dt
from pathlib import Path
import sys

from digest import standup
from lifecycle import core, service, smoke
from test_digest import AT, configuration
from test_lifecycle import Hub, configured_service
from test_lifecycle_continuation import approve, newest_claim
import test_runner as fixtures

CODE = 'checks_worktree_changed'
TEXT = ('The worktree changed while the required checks ran. '
        'Owner: re-approve to build the story again.')


def test_integrity_stop_code_through_job_receipt_notice_without_review_or_replay(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    verify = runner.verify_required_tests
    calls = []

    def execute(config, state, path, job, role):
        calls.append(role)
        assert role == 'worker'
        fixtures.deliver(config)
        return fixtures.result('ready_for_review')

    def changed(config, state, path, job):
        target = Path(config['worktree']) / 'delivery.txt'
        config['verification'] = dict(commands=[[
            sys.executable, '-B', '-c',
            'from pathlib import Path; Path(' + repr(str(target)) + ").write_text('changed\\n')"]],
            timeout_seconds=3)
        return verify(config, state, path, job)

    monkeypatch.setattr(runner, 'execute', execute)
    monkeypatch.setattr(runner, 'verify_required_tests', changed)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job['status'] == 'blocked'
    assert job['blocked_reason'] == dict(reason_code=CODE, reason=TEXT)
    evidence, = job['verification_runs']
    assert evidence['reason'] == 'reviewed_worktree_changed'
    assert evidence['source_unchanged'] is False
    # Read the configured runner's receipt, never guess its private directory layout.
    runner_cfg = runner.load_config(Path(cfg['state_dir']) / 'jobs' / job['id'] / 'config.json')
    receipt = core.read(Path(runner_cfg['state_dir']) / 'receipts' / (job['id'] + '.json'))
    assert runner.public_receipt(receipt)['blocked_reason'] == job['blocked_reason']
    notices = [c['body'] for c in hub.comments_by_issue[1] if CODE in c['body']]
    assert len(notices) == 1
    assert notices[0].startswith('Needs a fix:') and TEXT in notices[0]
    assert str(tmp_path) not in notices[0]
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    assert records[-1]['reason'] == CODE
    service.poll(cfg, hub.client(), clock=lambda: 150)
    assert calls == ['worker']
    assert len([c for c in hub.comments_by_issue[1] if CODE in c['body']]) == 1
    approve(hub, job['head_commit'])
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert newest_claim(cfg)['error'] == 'continuation_commit_unrecorded'
    assert calls == ['worker']


def test_new_integrity_code_is_allowed_in_standup_and_smoke(tmp_path):
    cfg = configuration(tmp_path)
    hub = Hub(cfg['lifecycle'])
    hub.issue(state='blocked')
    reason = dict(reason_code=CODE, reason=TEXT)
    core.save(Path(cfg['runner_state_dirs'][0]) / 'tasks/integrity.json',
              dict(id='integrity', status='blocked', updated_at=AT.isoformat(),
                   base_commit='b' * 40, github_number=1, assignment={}, blocked_reason=reason))
    sections = standup.collect(cfg, hub.client('product'), AT - dt.timedelta(days=1), AT)
    body = '\n'.join(line for section in sections for line in section)
    assert CODE in body and TEXT in body
    assert f'stop={CODE}.' in smoke.description('fixture', False, 2, CODE)
