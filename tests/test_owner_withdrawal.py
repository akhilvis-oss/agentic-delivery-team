"""Owner withdrawal: trusted synthetic events and disposable work only."""
from pathlib import Path

import pytest

from lifecycle import core, service
from test_lifecycle import Hub, policy, configured_service
from test_lifecycle_continuation import saved_story, newest_claim
from test_lifecycle_delivery import story


def stops(cfg, hub):
    return [r for r in core.records(cfg, hub.comments_by_issue[1], 1)
            if r.get('reason') == 'owner_withdrew']


@pytest.mark.parametrize('state', sorted(core.STATES - {'draft', 'merged', 'accepted'}))
def test_owner_can_withdraw_every_active_state(tmp_path, state):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state=state)
    event = hub.event(1, 'draft')
    hub.issues[1]['labels'].append({'name': 'status:' + state})
    assert core.canonical_state(cfg, hub.events_by_issue[1]) == ('draft', event)
    assert core.guard_issue(cfg, hub.client('workflow'), 1)
    assert hub.issues[1]['labels'] == [{'name': 'status:draft'}]
    assert core.canonical_state(cfg, hub.events_by_issue[1])[0] == 'draft'
    assert hub.comments_by_issue[1] == []


def test_only_exact_owner_newest_status_event_withdraws(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    approval = hub.events_by_issue[1][-1]['id']
    hub.event(1, 'draft', 'product')
    assert core.withdrawal(cfg, hub.client(), hub.issues[1], approval) is None
    core.guard_issue(cfg, hub.client('workflow'), 1)
    assert core.state(hub.issues[1]) == 'approved'
    event = hub.event(1, 'draft')
    assert core.withdrawal(cfg, hub.client(), hub.issues[1], approval) == event
    # Runner/workflow events cannot supersede the owner's status decision.
    hub.events_by_issue[1].append(dict(id=hub.counter + 1, event='labeled',
        label={'name': 'status:approved'}, actor={'login': cfg['actors']['workflow']}))
    assert core.withdrawal(cfg, hub.client(), hub.issues[1], approval) == event
    assert core.withdrawal(cfg, hub.client(), hub.issues[1], event['id']) is None
    hub.event(1, 'approved')
    hub.issues[1]['labels'].append({'name': 'status:draft'})
    assert core.withdrawal(cfg, hub.client(), hub.issues[1], approval) is None


def test_no_draft_reads_no_withdrawal_events(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    client = hub.client()
    monkeypatch.setattr(client, 'events', lambda n: pytest.fail('unbudgeted events'))
    assert core.withdrawal(cfg, client, hub.issues[1], 1) is None


@pytest.mark.parametrize('continuation', [True, False])
def test_withdrawal_record_round_trips_and_explains_restart(tmp_path, continuation):
    cfg = policy(tmp_path)
    record = core.withdrawal_record(cfg, 1, 'job', 101, head='a' * 40,
                                    continuation=continuation)
    assert record['from'] == record['to'] == 'draft'
    assert bool(record.get('continuation')) is continuation
    body = core.record_body(record)
    assert ('Its work is saved.' in body) is continuation
    assert ('continue from it.' if continuation else 'start it again.') in body
    record['actor'] = cfg['actors']['runner']
    assert core.records(cfg, [{'user': {'login': record['actor']}, 'body': body}], 1)


@pytest.mark.parametrize('stage', ['claimed', 'dispatched', 'enqueued'])
def test_claim_withdrawal_finishes_once_without_dispatch_or_error(tmp_path, monkeypatch, stage):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    claim = newest_claim(cfg)
    path = Path(cfg['state_dir']) / 'claims' / (claim['job'] + '.json')
    claim['stage'] = stage
    core.save(path, claim)
    hub.event(1, 'draft')
    before = len(hub.comments_by_issue[1])
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 300)
    claim = core.read(path)
    assert claim['stage'] == 'finished' and not claim.get('error')
    job, = service.jobs(cfg)
    assert job['status'] in runner.TERMINAL
    assert len(stops(cfg, hub)) == 1
    assert all('rejected' not in c['body'] for c in hub.comments_by_issue[1][before:])
    assert core.state(hub.issues[1]) == 'draft'


def test_no_claim_withdrawal_has_no_notice(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    hub.event(1, 'draft')
    before = list(hub.comments_by_issue[1])
    service.poll(cfg, hub.client(), clock=lambda: 100)
    assert hub.comments_by_issue[1] == before
    assert not service.jobs(cfg)


@pytest.mark.parametrize('failed', [False, True])
def test_running_stop_withdrawal_wins_and_reapproval_uses_saved_head(tmp_path, monkeypatch, failed):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    execute = runner.execute

    def withdraw(config, state, path, job, role):
        result = execute(config, state, path, job, role)
        if role == 'worker':
            hub.event(1, 'draft')
            if failed:
                raise RuntimeError('synthetic failure')
            result.update(status='blocked', blocked_check='story_tests')
        return result

    monkeypatch.setattr(runner, 'execute', withdraw)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    record, = stops(cfg, hub)
    assert bool(record.get('continuation')) is (not failed)
    assert core.state(hub.issues[1]) == 'draft'
    assert [r for r in core.records(cfg, hub.comments_by_issue[1], 1) if r.get('reason')] == [record]
    assert not publications
    assert all('rejected' not in c['body'] for c in hub.comments_by_issue[1])
    if not failed:
        monkeypatch.setattr(runner, 'execute', execute)
        hub.event(1, 'approved')
        hub.approve()
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
        assert newest_claim(cfg)['continued_from'] == job['head_commit']


@pytest.mark.parametrize('variant', ['valid', 'no_continuation', 'other_job', 'forged_actor',
                                     'wrong_head', 'wrong_reason', 'wrong_from'])
def test_saved_withdrawal_matcher_retains_identity_guards(tmp_path, monkeypatch, variant):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    stop = next(r for r in records if r.get('continuation'))
    record = core.withdrawal_record(cfg, 1, 'other' if variant == 'other_job' else old['id'],
                                    stop['event'], head='b' * 40 if variant == 'wrong_head' else old['head_commit'],
                                    continuation=variant != 'no_continuation')
    if variant == 'forged_actor':
        record['actor'] = cfg['actors']['product']
    if variant == 'wrong_reason':
        record = core.public_record(cfg, 1, old['id'], 0, 'draft', event=stop['event'],
                                    commit=old['head_commit'], reason='owner_question')
        record.update(actor=cfg['actors']['runner'], **{'from': 'draft'})
    if variant == 'wrong_from':
        record['from'] = 'needs-owner'
    for comment in hub.comments_by_issue[1]:
        if stop['key'] in comment['body']:
            comment.update(body=core.record_body(record), user={'login': record['actor']})
    hub.event(1, 'approved')
    hub.approve()
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    assert bool(claim.get('continued_from')) is (variant == 'valid')
    if variant != 'valid':
        assert claim['error'] == 'continuation_commit_unrecorded'


@pytest.mark.parametrize('target', ['dispatched', 'in-progress', 'committed', 'in-review',
                                    'changes-requested', 'blocked', 'needs-owner'])
def test_queued_progress_and_phase_stops_stay_local_after_withdrawal(tmp_path, target):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    record = core.public_record(cfg, 1, 'old', 1, target, event=101)
    record.update(actor=cfg['actors']['runner'], **{'from': 'in-review'})
    path = core.queue(cfg, record)
    hub.event(1, 'draft')
    core.flush(cfg, hub.client())
    assert core.read(path)['status'] == 'superseded'
    assert hub.comments_by_issue[1] == []
    assert core.state(hub.issues[1]) == 'draft'


def test_withdrawal_after_approval_before_dispatch(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    original = service.continuation

    def withdraw(*args, **kwargs):
        result = original(*args, **kwargs)
        hub.event(1, 'draft')
        return result

    monkeypatch.setattr(service, 'continuation', withdraw)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert newest_claim(cfg)['stage'] == 'finished'
    assert not service.jobs(cfg)
    record, = stops(cfg, hub)
    assert not record.get('continuation')
    assert not any(r['to'] == 'dispatched' for r in core.records(cfg, hub.comments_by_issue[1], 1))


def test_withdrawal_still_wins_after_a_later_runner_stop_event(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    draft = hub.event(1, 'draft')
    hub.events_by_issue[1].append(dict(id=hub.counter + 1, event='labeled',
        label={'name': 'status:blocked'}, actor={'login': cfg['actors']['runner']}))
    assert core.withdrawal(cfg, hub.client(), hub.issues[1], 101) == draft


def test_outbox_read_outage_remains_retryable(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    path = core.queue(cfg, core.public_record(cfg, 1, 'job', 0, 'dispatched', event=101))
    hub.fail_read = True
    core.flush(cfg, hub.client())
    assert core.read(path)['status'] == 'unsynced'
    hub.fail_read = False
    core.flush(cfg, hub.client())
    assert core.read(path)['status'] == 'synced'


@pytest.mark.parametrize('variant', ['no_head', 'save_failed', 'failed'])
def test_unsaved_or_ineligible_job_has_only_start_again_text(tmp_path, variant):
    cfg = policy(tmp_path)
    job = dict(id='job', status='needs_owner', head_commit='a' * 40, continuation=True,
               assignment={'lifecycle': {'issue': 1, 'event': 101}})
    if variant == 'no_head':
        job.pop('head_commit')
    elif variant == 'save_failed':
        job['stop_save_failed'] = True
    else:
        job['status'] = 'failed'
    record = core.job_withdrawal_record(cfg, job)
    assert not record.get('continuation')
    assert 'start it again.' in core.record_body(record)
    assert 'continue from it.' not in core.record_body(record)


def test_running_claim_is_left_for_next_runner_check(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    claim = newest_claim(cfg)
    config, job = service.source_work(cfg, claim['job'])
    job['status'] = 'running_worker'
    path = Path(config['state_dir']) / 'tasks' / (job['id'] + '.json')
    core.save(path, job)
    hub.event(1, 'draft')
    monkeypatch.setattr(runner, 'run_once', lambda *a, **k: pytest.fail('immediate stop'))
    with runner.lock(Path(config['state_dir']) / 'runner.lock') as acquired:
        assert acquired
        service.poll(cfg, hub.client(), clock=lambda: 200)
    assert core.read(path)['status'] == 'running_worker'
    assert newest_claim(cfg)['stage'] == 'enqueued'
    assert not stops(cfg, hub)


@pytest.mark.parametrize('failure', [False, True, 'recovery'])
def test_open_pr_withdrawal_disables_merge_once_and_keeps_notice_parseable(tmp_path, monkeypatch, failure):
    cfg, hub, tool, calls, publications = story(tmp_path, monkeypatch, enabled=True)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    path = Path(cfg['state_dir']) / 'jobs' / job['id'] / 'runner-state' / 'tasks' / (job['id'] + '.json')
    job['integration']['stage'] = 'auto_merge_enabled'
    if failure == 'recovery':
        job['withdrawal_merge_attempted'] = True
    core.save(path, job)
    hub.prs[9] = dict(state='open', node_id='synthetic', auto_merge={'enabled': True})
    disabled = []
    client = hub.client()

    def disable(pr):
        disabled.append(pr['node_id'])
        if failure:
            raise core.Uncertain('synthetic cancellation response lost')
        return True

    client.disable_auto_merge = disable
    monkeypatch.setattr(tool.integration_api(), 'client_for', lambda *a: client)
    hub.event(1, 'draft')
    before = len(publications)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    service.poll(cfg, hub.client(), clock=lambda: 300)
    record, = stops(cfg, hub)
    assert record.get('continuation')
    assert len(disabled) == (0 if failure == 'recovery' else 1)
    notice, = [c['body'] for c in hub.comments_by_issue[1] if record['key'] in c['body']]
    assert ('Auto-merge may still be on.' in notice) is bool(failure)
    if failure:
        assert notice.index('Auto-merge may still be on.') < notice.index(core.PREFIX)
    assert 'Reviewed head:' not in notice
    assert len(publications) == before and hub.prs[9]['state'] == 'open'
    hub.event(1, 'approved')
    hub.approve()
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 400)
    assert newest_claim(cfg)['error'] == 'pr_open_reapproval'


@pytest.mark.parametrize('corruption', ['continuation_not_applied', 'history_changed', 'concurrent_change',
                                       'reviewed_worktree_changed', 'source_unchanged', 'assignment',
                                       'missing_commit'])
def test_withdrawn_source_keeps_every_saved_work_integrity_guard(tmp_path, monkeypatch, corruption):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    stop = next(r for r in core.records(cfg, hub.comments_by_issue[1], 1) if r.get('continuation'))
    head = 'b' * 40 if corruption == 'missing_commit' else old['head_commit']
    record = core.withdrawal_record(cfg, 1, old['id'], stop['event'], head=head, continuation=True)
    for comment in hub.comments_by_issue[1]:
        if stop['key'] in comment['body']:
            comment['body'] = core.record_body(record)
    if corruption == 'continuation_not_applied':
        old['blocked_reason'] = runner.block_reason(corruption)
    elif corruption in {'history_changed', 'concurrent_change'}:
        old['commit_refusals'] = [{'reason': corruption}]
    elif corruption == 'reviewed_worktree_changed':
        old['verification_runs'] = [{'reason': corruption}, {'source_unchanged': True}]
    elif corruption == 'source_unchanged':
        old['verification_runs'] = [{'source_unchanged': False}, {'source_unchanged': True}]
    elif corruption == 'assignment':
        old['assignment']['task_text'] += 'Tampered'
    else:
        old['head_commit'] = head
    path = Path(cfg['state_dir']) / 'jobs' / old['id'] / 'runner-state' / 'tasks' / (old['id'] + '.json')
    core.save(path, old)
    hub.event(1, 'approved')
    hub.approve()
    if corruption == 'assignment':
        with pytest.raises(runner.IntegrityError):
            service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    else:
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
        assert newest_claim(cfg)['error'] == 'continuation_commit_unrecorded'
        assert not newest_claim(cfg).get('continued_from')


@pytest.mark.parametrize('boundary', ['checks', 'review'])
def test_withdrawal_at_checks_or_review_stops_before_next_phase(tmp_path, monkeypatch, boundary):
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    if boundary == 'checks':
        original = runner.verify_required_tests

        def verify(*args):
            result = original(*args)
            hub.event(1, 'draft')
            return result

        monkeypatch.setattr(runner, 'verify_required_tests', verify)
    else:
        original = runner.execute

        def execute(*args):
            result = original(*args)
            if args[-1] == 'reviewer':
                hub.event(1, 'draft')
            return result

        monkeypatch.setattr(runner, 'execute', execute)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    record, = stops(cfg, hub)
    assert record.get('continuation')
    assert core.state(hub.issues[1]) == 'draft' and not publications
    assert [role for _, role in calls] == (['worker'] if boundary == 'checks' else ['worker', 'reviewer'])


@pytest.mark.parametrize('reason', ['continuation_conflict', 'pr_open_reapproval'])
def test_service_stop_checks_withdrawal_before_publishing_reason(tmp_path, reason):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue()
    hub.event(1, 'draft')
    claim = dict(job='job', issue=1, event=101, stage='claimed')
    path = Path(cfg['state_dir']) / 'claims' / 'job.json'
    service.stop_continuation(cfg, hub.client(), path, claim, reason,
                              ['file.py'] if reason == 'continuation_conflict' else None)
    record, = stops(cfg, hub)
    assert record['from'] == record['to'] == 'draft'
    assert core.read(path)['stage'] == 'finished' and not core.read(path).get('error')
    assert len(hub.comments_by_issue[1]) == 1


def test_service_unexpected_stop_checks_withdrawal_before_notice(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    issue = hub.issue()
    claim = dict(job='job', issue=1, event=101, stage='claimed')
    failed, skipped = set(), set()
    with service.story_boundary(cfg, hub.client(), issue, failed, skipped, [claim], claim=claim):
        hub.event(1, 'draft')
        raise RuntimeError('synthetic service stop')
    record, = stops(cfg, hub)
    assert record['reason'] == 'owner_withdrew'
    assert len(hub.comments_by_issue[1]) == 1


def test_withdrawal_between_required_check_commands_skips_the_next_check(tmp_path, monkeypatch):
    from test_lifecycle_delivery import settings

    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    settings(cfg, 'runner_config', {'enabled': False, 'declared_tier': 'standard'})
    config = core.read(cfg['runner_config'])
    config['verification']['commands'] *= 2
    core.save(cfg['runner_config'], config)
    original = runner.verification_counts

    def counted(output):
        result = original(output)
        hub.event(1, 'draft')
        return result

    monkeypatch.setattr(runner, 'verification_counts', counted)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert len(job['verification_runs'][0]['results']) == 1
    assert [role for _, role in calls] == ['worker']
    assert len(stops(cfg, hub)) == 1 and not publications


def test_withdrawn_job_does_not_publish_a_second_receipt_notice(tmp_path, monkeypatch):
    from types import SimpleNamespace

    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    config = core.read(cfg['runner_config'])
    config['github_receipts'] = {'enabled': True, 'repository': cfg['repository']}
    core.save(cfg['runner_config'], config)
    posted = []
    monkeypatch.setattr(runner, 'GitHubClient', lambda config: SimpleNamespace(post=lambda *args: posted.append(args)))
    original = runner.execute

    def withdraw(*args):
        result = original(*args)
        hub.event(1, 'draft')
        result.update(status='blocked', blocked_check='story_tests')
        return result

    monkeypatch.setattr(runner, 'execute', withdraw)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    assert len(stops(cfg, hub)) == 1
    assert posted == []


def test_withdrawn_local_receipt_retains_its_fixed_reason(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    hub.event(1, 'draft')
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    job, = service.jobs(cfg)
    config, _ = service.source_work(cfg, job['id'])
    receipt = core.read(Path(config['state_dir']) / 'receipts' / (job['id'] + '.json'))
    assert receipt['blocked_reason']['reason_code'] == 'owner_withdrew'


@pytest.mark.parametrize('phase', ['worker', 'reviewer'])
@pytest.mark.parametrize('timing', ['before_scan', 'after_scan', 'later_issue', 'later_events'])
@pytest.mark.parametrize('code', ['agent_sign_in_failed', 'agent_usage_limit'])
def test_waiting_retry_withdrawal_saves_once_and_reapproves_latest_head(
        tmp_path, monkeypatch, phase, timing, code):
    from test_system_retry import retry_story
    cfg, hub, runner, attempts = retry_story(tmp_path, monkeypatch, role=phase,
                                            partial=phase == 'worker')
    execute = runner.execute

    def listed_exit(*args, **kwargs):
        try:
            return execute(*args, **kwargs)
        except runner.AgentExit:
            raise runner.AgentExit(code, retryable=True) from None

    monkeypatch.setattr(runner, 'execute', listed_exit)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job['status'] == 'waiting_system' and job['retry']['code'] == code
    claim = newest_claim(cfg)
    original_scan = service.cancel_waiting
    client = hub.client()
    issue = client.issue
    events = client.events
    state = {'armed': False, 'changed': False, 'reads': 0}

    def withdraw():
        if not state['changed']:
            state['changed'] = True
            hub.event(1, 'draft')

    def scan(*args, **kwargs):
        result = original_scan(*args, **kwargs)
        state['armed'] = True
        if timing == 'after_scan':
            withdraw()
        return result

    def fresh(number):
        if state['armed'] and number == 1:
            state['reads'] += 1
            if timing == 'later_issue' and state['reads'] == 2:
                withdraw()
        return issue(number)

    def fresh_events(number):
        if state['armed'] and number == 1 and timing == 'later_events':
            withdraw()
        return events(number)

    monkeypatch.setattr(service, 'cancel_waiting', scan)
    monkeypatch.setattr(client, 'issue', fresh)
    monkeypatch.setattr(client, 'events', fresh_events)
    if timing == 'before_scan':
        withdraw()
    before = list(attempts)
    checks = job.get('verification_runs', [])
    service.poll(cfg, client, clock=lambda: 200)
    ended = next(j for j in service.jobs(cfg) if j['id'] == job['id'])
    final = core.read(Path(cfg['state_dir']) / 'claims' / (claim['job'] + '.json'))
    assert state['changed'] and final['stage'] == 'finished' and not final.get('error')
    assert ended['status'] == 'needs_owner' and not ended.get('retry')
    assert ended['owner_withdrew'] and ended['continuation'] and ended['head_commit']
    record, = stops(cfg, hub)
    assert record['commit'] == ended['head_commit'] and record['continuation']
    assert record['from'] == record['to'] == 'draft'
    assert core.state(hub.issues[1]) == 'draft' and attempts == before
    assert ended.get('verification_runs', []) == checks
    assert not any(r.get('reason') in {'agent_sign_in_failed', 'agent_usage_limit'}
                   for r in core.records(cfg, hub.comments_by_issue[1], 1))
    queue = service.StoryQueue(cfg, client, hub.issues, set(), Path(cfg['state_dir']) / 'claims')
    assert queue.holder({'issue': 2}) is None
    service.poll(cfg, client, run_jobs=False, clock=lambda: 300)
    assert len(stops(cfg, hub)) == 1 and attempts == before
    hub.event(1, 'approved')
    hub.approve()
    service.poll(cfg, client, run_jobs=False, clock=lambda: 400)
    assert newest_claim(cfg)['continued_from'] == ended['head_commit']


@pytest.mark.parametrize('tamper', ['assignment', 'config', 'claim'])
def test_waiting_withdrawal_retains_source_and_claim_integrity(tmp_path, monkeypatch, tamper):
    from test_system_retry import retry_story
    cfg, hub, runner, attempts = retry_story(tmp_path, monkeypatch, partial=True)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    claim = newest_claim(cfg)
    path = Path(cfg['state_dir']) / 'jobs' / claim['job'] / 'runner-state' / 'tasks' / (claim['job'] + '.json')
    if tamper == 'assignment':
        job = core.read(path)
        job['assignment']['task_sha256'] = 'f' * 64
        core.save(path, job)
    elif tamper == 'config':
        config = core.read(claim['config'])
        config['branch'] = 'unbound-feature'
        core.save(claim['config'], config)
    else:
        claim['worktree'] = str(tmp_path / 'unbound-worktree')
        core.save(Path(cfg['state_dir']) / 'claims' / (claim['job'] + '.json'), claim)
    hub.event(1, 'draft')
    before = path.read_bytes()
    with pytest.raises((core.IntegrityError, runner.IntegrityError)):
        service.poll(cfg, hub.client(), clock=lambda: 200)
    assert path.read_bytes() == before and not stops(cfg, hub)


@pytest.mark.parametrize('order', ['non_owner', 'reapproved', 'later_runner'])
def test_waiting_withdrawal_uses_newest_owner_event_only(tmp_path, monkeypatch, order):
    from test_system_retry import retry_story
    cfg, hub, runner, attempts = retry_story(tmp_path, monkeypatch, partial=True)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    old, = service.jobs(cfg)
    if order == 'non_owner':
        hub.event(1, 'draft', 'product')
    else:
        hub.event(1, 'draft')
        if order == 'reapproved':
            hub.event(1, 'approved')
            hub.approve()
            hub.issues[1]['labels'].append({'name': 'status:draft'})
        else:
            hub.event(1, 'blocked', 'runner')
    core.guard_issue(cfg, hub.client('workflow'), 1)
    before = list(attempts)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 150, full_list=True)
    ended = next(j for j in service.jobs(cfg) if j['id'] == old['id'])
    if order == 'later_runner':
        record, = stops(cfg, hub)
        assert ended['owner_withdrew'] and ended['continuation'] and not ended.get('retry')
        assert record['continuation'] and record['commit'] == ended['head_commit']
        assert core.state(hub.issues[1]) == 'draft'
    else:
        assert not stops(cfg, hub) and not ended.get('owner_withdrew')
        if order == 'non_owner':
            assert ended['status'] == 'waiting_system' and ended['retry']
        else:
            assert ended['retry_local_end'] and not ended.get('continuation')
    assert attempts == before


def test_waiting_withdrawal_reapproval_prefers_new_saved_work_to_older_stop(tmp_path, monkeypatch):
    from test_one_continuation import reapprove
    cfg, hub, runner, older = saved_story(tmp_path, monkeypatch)
    cfg['system_retry_minutes'] = [1]
    core.save(cfg['config_path'], cfg)
    reapprove(hub)

    def fail(config, state, path, job, role):
        job['status'] = 'running_worker'
        runner.save_job(config, path, job)
        Path(config['worktree'], 'withdrawn-wait.txt').write_text('new saved work\n')
        raise runner.AgentExit('agent_sign_in_failed', retryable=True)

    monkeypatch.setattr(runner, 'execute', fail)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    waiting = next(j for j in service.jobs(cfg) if j['status'] == 'waiting_system')
    hub.event(1, 'draft')
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 300)
    saved = next(j for j in service.jobs(cfg) if j['id'] == waiting['id'])
    assert saved['owner_withdrew'] and saved['continuation'] and not saved.get('retry')
    assert saved['head_commit'] != older['head_commit']
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 400)
    claim = newest_claim(cfg)
    assert claim['continuation_source'] == waiting['id']
    assert claim['continued_from'] == saved['head_commit']



def test_owner_withdrawal_wins_admitted_stop_write_guard_race(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    claim = newest_claim(cfg)
    record = core.public_record(cfg, 1, claim['job'], 0, 'blocked',
                                event=claim['event'], reason='execution_failed')
    entry = core.queue(cfg, record)
    client = hub.client()
    replace = client.set_state
    injected = []

    def race(number, target):
        if target == 'blocked' and not injected:
            injected.append(hub.event(number, 'draft'))
        return replace(number, target)

    monkeypatch.setattr(client, 'set_state', race)
    core.flush(cfg, client)
    assert injected and core.read(entry)['status'] == 'synced'
    remote = core.records(cfg, hub.comments_by_issue[1], 1)
    assert any(r['key'] == record['key'] and r['reason'] == 'execution_failed' for r in remote)
    # This used the admitted runner write and a valid producer record, rather
    # than directly inventing its blocked label event or bypassing the outbox.
    core.guard_issue(cfg, hub.client('workflow'), 1)
    assert core.state(hub.issues[1]) == 'draft'



def test_workflow_pr_close_after_withdrawal_still_needs_owner(tmp_path):
    from copy import deepcopy
    from test_lifecycle import pr_fixture
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    pr = pr_fixture(cfg, hub)
    hub.event(1, 'pr-open', 'workflow')
    draft = hub.event(1, 'draft')
    assert core.canonical_state(cfg, hub.events_by_issue[1]) == ('draft', draft)
    pr.update(state='closed', merged=False)
    core.pr_event(cfg, hub.client('workflow'), {'action': 'closed', 'pull_request': deepcopy(pr)})
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    record, = [r for r in records if r['reason'] == 'pr_closed_unmerged']
    assert record['actor'] == cfg['actors']['workflow'] and record['to'] == 'needs-owner'
    assert core.state(hub.issues[1]) == 'needs-owner'
    assert core.canonical_state(cfg, hub.events_by_issue[1])[0] == 'needs-owner'
    # The permitted workflow move does not clear the owner's runner-only hold.
    hub.event(1, 'blocked', 'runner')
    core.guard_issue(cfg, hub.client('workflow'), 1)
    assert core.state(hub.issues[1]) == 'needs-owner'


def test_owner_reapproval_clears_runner_only_withdrawal_hold(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-progress')
    draft = hub.event(1, 'draft')
    hub.event(1, 'blocked', 'runner')
    assert core.canonical_state(cfg, hub.events_by_issue[1]) == ('draft', draft)
    approval = hub.event(1, 'approved')
    hub.approve()
    assert core.canonical_state(cfg, hub.events_by_issue[1]) == ('approved', approval)
    core.guard_issue(cfg, hub.client('workflow'), 1)
    assert core.state(hub.issues[1]) == 'approved'
    dispatched = hub.event(1, 'dispatched', 'runner')
    assert core.canonical_state(cfg, hub.events_by_issue[1]) == ('dispatched', dispatched)


@pytest.mark.parametrize('phase', ['worker', 'reviewer'])
@pytest.mark.parametrize('live', [False, True])
def test_withdrawal_after_phase_crash_recovers_unlocked_job_only(tmp_path, monkeypatch, phase, live):
    from contextlib import nullcontext
    cfg, hub, runner, calls, publications = story(tmp_path, monkeypatch)
    execute = runner.execute

    def crash(config, state, path, job, role):
        if role == phase:
            job.update(status='running_' + role, updated_at=runner.now())
            runner.save_job(config, path, job)
            if role == 'worker':
                Path(config['worktree'], 'crash-partial.txt').write_text('preserved partial work\n')
            raise SystemExit('synthetic runner crash')
        return execute(config, state, path, job, role)

    monkeypatch.setattr(runner, 'execute', crash)
    with pytest.raises(SystemExit, match='synthetic runner crash'):
        service.poll(cfg, hub.client(), clock=lambda: 100)
    claim = newest_claim(cfg)
    config, old = service.source_work(cfg, claim['job'])
    path = Path(config['state_dir']) / 'tasks' / (old['id'] + '.json')
    assert old['status'] == 'running_' + phase
    hub.event(1, 'draft')
    before = list(calls)
    monkeypatch.setattr(runner, 'execute', lambda *a, **k: pytest.fail('crashed phase replayed'))
    context = runner.lock(Path(config['state_dir']) / 'runner.lock') if live else nullcontext(True)
    with context as acquired:
        assert acquired
        service.poll(cfg, hub.client(), clock=lambda: 200)
        current = core.read(path)
        final = core.read(Path(cfg['state_dir']) / 'claims' / (claim['job'] + '.json'))
        if live:
            assert current['status'] == 'running_' + phase
            assert final['stage'] == 'enqueued' and not stops(cfg, hub)
        else:
            assert current['status'] == 'needs_owner'
            assert current['interrupted_phase'] == 'running_' + phase
            assert current['continuation'] and current['head_commit']
            assert final['stage'] == 'finished' and not final.get('error')
            record, = stops(cfg, hub)
            assert record['from'] == record['to'] == 'draft'
            assert record['continuation'] and record['commit'] == current['head_commit']
            assert core.state(hub.issues[1]) == 'draft'
            queue = service.StoryQueue(cfg, hub.client(), hub.issues, set(), Path(cfg['state_dir']) / 'claims')
            assert queue.holder({'issue': 2}) is None
    assert calls == before and not publications
    if phase == 'worker':
        assert Path(config['worktree'], 'crash-partial.txt').read_text() == 'preserved partial work\n'
    if not live:
        service.poll(cfg, hub.client(), clock=lambda: 300)
        assert len(stops(cfg, hub)) == 1 and calls == before
        hub.event(1, 'approved')
        hub.approve()
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 400)
        assert newest_claim(cfg)['continued_from'] == current['head_commit']


@pytest.mark.parametrize('action', ['reapproval', 'newer_job'])
def test_withdrawal_rereads_owner_and_job_after_lock_acquisition(tmp_path, monkeypatch, action):
    from contextlib import contextmanager
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    claim = newest_claim(cfg)
    claim_path = Path(cfg['state_dir']) / 'claims' / (claim['job'] + '.json')
    config, job = service.source_work(cfg, claim['job'])
    path = Path(config['state_dir']) / 'tasks' / (job['id'] + '.json')
    job['status'] = 'running_worker'
    core.save(path, job)
    Path(config['worktree'], 'updated-under-lock.txt').write_text('new preserved work\n')
    hub.event(1, 'draft')
    observed = hub.client().issue(1)
    lock = runner.lock
    changed = []
    expected = {}

    @contextmanager
    def acquire(path_to_lock):
        with lock(path_to_lock) as acquired:
            if acquired and not changed:
                changed.append(True)
                current = core.read(path)
                current['fresh_marker'] = 'newer state'
                if action == 'reapproval':
                    core.save(path, current)
                    hub.event(1, 'approved')
                    hub.approve()
                    expected['job_bytes'] = path.read_bytes()
                    expected['claim_bytes'] = claim_path.read_bytes()
                else:
                    # The old runner completed before this service acquired
                    # its lock. Keep the newly saved head and metadata.
                    runner.withdrawal_stop(config, path, current)
                    expected['head'] = current['head_commit']
                    expected['commits'] = current['runner_commits']
            yield acquired

    monkeypatch.setattr(runner, 'lock', acquire)
    result = service.withdraw_claim(cfg, hub.client(), claim_path, claim, observed)
    assert changed
    current = core.read(path)
    assert current['fresh_marker'] == 'newer state'
    if action == 'reapproval':
        assert result is False and path.read_bytes() == expected['job_bytes']
        assert claim_path.read_bytes() == expected['claim_bytes'] and not stops(cfg, hub)
        assert core.state(hub.issues[1]) == 'approved'
    else:
        assert result is True and current['head_commit'] == expected['head']
        assert current['runner_commits'] == expected['commits']
        assert 'interrupted_phase' not in current
        record, = stops(cfg, hub)
        assert record['continuation'] and record['commit'] == expected['head']
        assert core.read(claim_path)['stage'] == 'finished'



def test_withdrawal_liveness_lock_unavailable_preserves_state(tmp_path, monkeypatch):
    from contextlib import contextmanager
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    claim = newest_claim(cfg)
    config, job = service.source_work(cfg, claim['job'])
    path = Path(config['state_dir']) / 'tasks' / (job['id'] + '.json')
    claim_path = Path(cfg['state_dir']) / 'claims' / (claim['job'] + '.json')
    job['status'] = 'running_worker'
    core.save(path, job)
    hub.event(1, 'draft')
    before = (path.read_bytes(), claim_path.read_bytes())

    @contextmanager
    def unavailable(*args):
        raise OSError('synthetic lock unavailable')
        yield

    monkeypatch.setattr(runner, 'lock', unavailable)
    with pytest.raises(core.Unavailable, match='Runner liveness lock unavailable'):
        service.withdraw_claim(cfg, hub.client(), claim_path, claim, hub.client().issue(1))
    assert (path.read_bytes(), claim_path.read_bytes()) == before and not stops(cfg, hub)
