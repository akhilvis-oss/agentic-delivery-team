"""Unexpected stops retain normal, evidence-bound cleanup without job replay."""

from pathlib import Path

import pytest

from lifecycle import core, service
from test_lifecycle import configured_service
from test_lifecycle_owner_finish import FinishClient, owner_label
from test_lifecycle_resilience import stops


@pytest.mark.parametrize('ending', [
    'owner-close', 'other-close', 'reopened', 'untrusted-state',
    'finish-merged', 'finish-unmerged',
])
def test_unexpected_stop_keeps_authorized_cleanup_without_replay(tmp_path, monkeypatch, ending):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    template = core.read(cfg['runner_config'])
    template['integration'] = {'enabled': True, 'declared_tier': 'standard'}
    core.save(cfg['runner_config'], template)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    path, = (Path(cfg['state_dir']) / 'claims').glob('*.json')
    claim = core.read(path)
    if ending == 'finish-unmerged':
        service.git(cfg, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '--allow-empty', '-m', 'synthetic saved work',
                    worktree=claim['worktree'])
    head = service.git(cfg, 'rev-parse', 'HEAD', worktree=claim['worktree']).stdout.strip()
    ref = 'refs/agentic/continue/' + claim['job']
    branch = 'refs/heads/' + claim['branch']
    service.git(cfg, 'update-ref', ref, head)
    job_path = (Path(cfg['state_dir']) / 'jobs' / claim['job'] / 'runner-state'
                / 'tasks' / (claim['job'] + '.json'))
    job_before = job_path.read_bytes()
    calls = []

    def failed_run(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('synthetic runner failure')

    monkeypatch.setattr(runner, 'run_once', failed_run)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert core.read(path)['error'] == 'unexpected_error'
    # Exercise the finished-claim delivery path while the story is still open.
    service.poll(cfg, hub.client(), clock=lambda: 300)
    assert len(calls) == 1
    if ending.startswith('finish-'):
        owner_label(hub)
        core.guard_issue(cfg, FinishClient(hub, 'workflow'), 1)
    else:
        hub.issues[1]['state'] = 'closed'
        hub.counter += 1
        hub.events_by_issue[1].append(dict(
            id=hub.counter, event='closed',
            actor={'login': cfg['actors']['runner' if ending == 'other-close' else 'owner']},
        ))
        if ending == 'reopened':
            hub.counter += 1
            hub.events_by_issue[1].append(dict(
                id=hub.counter, event='reopened', actor={'login': cfg['actors']['owner']},
            ))
        if ending == 'untrusted-state':
            hub.issues[1]['labels'] = [{'name': 'status:merged'}]
    for at in (400, 500):
        service.poll(cfg, hub.client(), clock=lambda: at)
    after = core.read(path)
    assert after['stage'] == 'finished' and after['error'] == 'unexpected_error'
    assert len(calls) == 1
    assert job_path.read_bytes() == job_before
    assert len(stops(cfg, hub)) == 1
    retained = ending in {'other-close', 'reopened', 'untrusted-state', 'finish-unmerged'}
    assert (service.git(cfg, 'show-ref', '--verify', ref, check=False).returncode == 0) == retained
    assert Path(claim['worktree']).exists() == (not ending.startswith('finish-'))
    assert (service.git(cfg, 'show-ref', '--verify', branch, check=False).returncode == 0) == (
        ending != 'finish-merged')
    if ending == 'finish-unmerged':
        assert after['owner_finish_kept_unmerged'] and after['cleaned']
        assert 'Issue #1: owner finish kept unmerged work' in service.status(cfg)
    elif ending == 'finish-merged':
        assert after['cleaned']
    else:
        assert after['cleanup_pending'] == (
            'untrusted_close_state' if ending == 'untrusted-state'
            else 'closed_without_merge_requires_owner')
