"""Approved-story ordering, dependency and slot boundaries, entirely offline."""

from pathlib import Path

import pytest

from lifecycle import core, service, upgrade
from test_lifecycle import configured_service, WHEN


@pytest.mark.parametrize('text,expected', [
    ('Plain story.', ()),
    ('## Dependencies\nDepends on nothing.', ()),
    ('### Dependencies\nDepends on #9, #10 and #11. Commentary.', (9, 10, 11)),
    ('## Dependencies\nDepends on #9, #10, and #11.', (9, 10, 11)),
    ('## Dependencies\nDepends on #9.\nDepends on #10.', (9,)),
    ('## Context\nDepends on #9.\n## Dependencies\nNo dependencies listed.', ()),
    ('## Dependencies\nDepends on #9.\n## Other\nDepends on #10.', (9,)),
])
def test_dependency_grammar_and_section(text, expected):
    assert service.dependency_numbers(text) == expected


@pytest.mark.parametrize('text', ['#0', '#-1', '#1,#2', '#1 or #2', 'issue #1',
                                 '#1 and #2 extra', '#1,  #2', 'Nothing'])
def test_dependency_grammar_rejects_unusable_text(text):
    with pytest.raises(ValueError):
        service.dependency_numbers('## Dependencies\nDepends on ' + text + '.')


def add(hub, number, body='Synthetic task.'):
    item = hub.issue(number, body=body)
    hub.approve(number)
    return item


def claims(cfg):
    return [core.read(p) for p in sorted((Path(cfg['state_dir']) / 'claims').glob('*.json'))]


def started(cfg):
    return [job['assignment']['lifecycle']['issue'] for job in service.jobs(cfg)]


def priority(hub, number, actor, *, remove=False):
    hub.counter += 1
    event = dict(id=hub.counter, event='unlabeled' if remove else 'labeled',
                 label={'name': 'priority:high'}, actor={'login': hub.cfg['actors'].get(actor, actor)},
                 created_at=WHEN)
    hub.events_by_issue[number].append(event)
    hub.issues[number]['labels'] = [x for x in hub.issues[number]['labels'] if x['name'] != 'priority:high']
    if not remove:
        hub.issues[number]['labels'].append({'name': 'priority:high'})


@pytest.mark.parametrize('actor,first', [('owner', 2), ('product', 2), ('runner', 1),
                                       ('stranger-fixture', 1)])
def test_priority_requires_newest_owner_or_product_event(tmp_path, monkeypatch, actor, first):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    add(hub, 2)
    priority(hub, 2, actor)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert started(cfg) == [first]


def test_non_owner_readd_cannot_reuse_earlier_priority_authority(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    add(hub, 2)
    priority(hub, 2, 'owner')
    priority(hub, 2, 'owner', remove=True)
    priority(hub, 2, 'stranger-fixture')
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert started(cfg) == [1]


def test_approval_order_not_lexical_issue_or_reapproval_order(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issues.pop(1)
    add(hub, 9)
    add(hub, 10)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert started(cfg) == [9]


def test_reapproval_takes_new_place_in_queue(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    add(hub, 2)
    hub.event(1, 'approved')
    hub.event(1, 'approved')
    hub.approve(1)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert started(cfg) == [2]


def close(hub, number, actor='owner', reason=None, listing_reason='completed'):
    hub.counter += 1
    hub.events_by_issue[number].append(dict(id=hub.counter, event='closed',
        actor={'login': hub.cfg['actors'].get(actor, actor)}, created_at=WHEN, state_reason=reason))
    hub.issues[number].update(state='closed', state_reason=listing_reason, closed_at=WHEN)


@pytest.mark.parametrize('state', ['merged', 'accepted'])
def test_final_dependency_status_releases_story(tmp_path, monkeypatch, state):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issues[1]['body'] += '\n## Dependencies\nDepends on #2.'
    hub.event(1, 'approved')
    hub.approve(1)
    hub.issue(2, state)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert started(cfg) == [1]


@pytest.mark.parametrize('actor,event_reason,listing_reason,passes', [
    ('owner', None, 'completed', True), ('owner', 'completed', 'completed', True),
    ('product', None, 'completed', False), ('owner', 'not_planned', 'completed', False),
    ('owner', None, 'not_planned', False),
])
def test_dependency_completed_close_needs_owner_and_both_reasons(
    tmp_path, monkeypatch, actor, event_reason, listing_reason, passes,
):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issues[1]['body'] += '\n## Dependencies\nDepends on #2.'
    hub.event(1, 'approved')
    hub.approve(1)
    hub.issue(2, 'draft')
    close(hub, 2, actor, event_reason, listing_reason)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert started(cfg) == ([1] if passes else [])
    if not passes:
        claim = claims(cfg)[0]
        assert claim['stage'] == 'finished' and 'error' not in claim
        records = core.records(cfg, hub.comments_by_issue[1], 1)
        assert records[-1]['reason'] == 'dependency_closed_unmerged'
        assert records[-1]['to'] == 'needs-owner'


@pytest.mark.parametrize('kind', ['pull_request', 'unknown', 'self', 'cycle', 'text'])
def test_invalid_dependency_stops_without_dispatch_or_upgrade_error(tmp_path, monkeypatch, kind):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    ref = 1 if kind == 'self' else 2
    hub.issues[1]['body'] += '\n## Dependencies\nDepends on ' + ('other text' if kind == 'text' else f'#{ref}') + '.'
    hub.event(1, 'approved')
    hub.approve(1)
    if kind in {'pull_request', 'cycle'}:
        hub.issue(2, 'draft', body='## Dependencies\nDepends on #1.' if kind == 'cycle' else 'PR')
        if kind == 'pull_request':
            hub.issues[2]['pull_request'] = {'url': 'https://example.invalid/pr/2'}
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert started(cfg) == []
    claim = claims(cfg)[0]
    assert claim['stage'] == 'finished' and 'error' not in claim
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    assert records[-1]['reason'] == 'dependency_closed_unmerged'
    assert records[-1]['to'] == 'needs-owner'
    assert 'fix the line' in hub.comments_by_issue[1][-1]['body']


def test_waiting_dependency_adds_no_reads_then_releases_with_full_admission(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issues[1]['body'] += '\n## Dependencies\nDepends on #2.'
    hub.event(1, 'approved')
    hub.approve(1)
    hub.issue(2, 'draft')
    from test_lifecycle_poll_budget import SinceClient
    client = SinceClient(hub)
    calls = []
    for name in ['events', 'comments', 'issue']:
        original = getattr(client, name)
        def wrapped(number, original=original, name=name):
            calls.append((name, number))
            return original(number)
        monkeypatch.setattr(client, name, wrapped)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 2000000000)
    assert started(cfg) == [] and claims(cfg)[0]['waiting'] == {'dependency': 2}
    calls.clear()
    service.poll(cfg, client, run_jobs=False, clock=lambda: 2000000100)
    assert not [c for c in calls if c[1] in {1, 2}]
    close(hub, 2)
    hub.issues[2]['updated_at'] = service.timestamp(2000000200)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 2000000200)
    assert started(cfg) == [1]
    assert ('issue', 1) in calls and ('comments', 1) in calls
    assert 'waiting' not in claims(cfg)[0]


def test_wait_release_rechecks_scope_before_any_dispatch(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issues[1]['body'] += '\n## Dependencies\nDepends on #2.'
    hub.event(1, 'approved')
    hub.approve(1)
    hub.issue(2, 'draft')
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    close(hub, 2)
    hub.issues[2]['updated_at'] = '2026-01-02T10:01:00+00:00'
    hub.issues[1]['body'] += '\nChanged after approval.'
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert started(cfg) == []


@pytest.mark.parametrize('status', ['blocked', 'needs_owner'])
def test_stopped_job_without_pr_frees_slot(tmp_path, monkeypatch, status):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    path = next((Path(cfg['state_dir']) / 'jobs').glob('*/runner-state/tasks/*.json'))
    job = runner.read(path)
    job['status'] = status
    runner.save(path, job)
    hub.event(1, status.replace('_', '-'), 'runner')
    add(hub, 2)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert 2 in started(cfg)


@pytest.mark.parametrize('retired', [False, True])
@pytest.mark.parametrize('pr_state', ['open', 'closed'])
def test_open_pr_holds_slot_even_for_retired_delivery(tmp_path, monkeypatch, retired, pr_state):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    path = next((Path(cfg['state_dir']) / 'jobs').glob('*/runner-state/tasks/*.json'))
    job = runner.read(path)
    job.update(status='needs_owner', delivery_retired=retired, integration={'pr': 77, 'stage': 'owner'})
    runner.save(path, job)
    hub.event(1, 'needs-owner', 'runner')
    hub.prs[77] = {'number': 77, 'state': pr_state, 'merged': pr_state == 'closed'}
    add(hub, 2)
    hub.calls.clear()
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert (2 in started(cfg)) is (pr_state == 'closed')
    assert sum(c[:2] == ('GET', '/pulls/77') for c in hub.calls) == 1
    if pr_state == 'open':
        assert next(c for c in claims(cfg) if c['issue'] == 2)['waiting'] == {'story': 1}


def test_waiting_snapshot_is_read_only_and_shown_in_status(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issues[1]['body'] += '\n## Dependencies\nDepends on #2.'
    hub.event(1, 'approved')
    hub.approve(1)
    hub.issue(2, 'draft')
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    before = {str(p): p.read_bytes() for p in Path(cfg['state_dir']).rglob('*') if p.is_file()}
    assert service.queue_waiting(cfg) == {1: {'dependency': 2}}
    assert '#1: waiting for #2' in service.poll_body(cfg, 100, [])
    assert '#1: waiting for #2' in service.status(cfg, clock=lambda: 100)
    assert before == {str(p): p.read_bytes() for p in Path(cfg['state_dir']).rglob('*') if p.is_file()}


def test_waiting_claim_does_not_block_upgrade(tmp_path, monkeypatch):
    from test_lifecycle_upgrade import configured, exporter, transport
    cfg, folder = configured(tmp_path, monkeypatch)
    remote, _ = transport(monkeypatch, cfg)
    exports = exporter(monkeypatch)
    core.save(Path(cfg['state_dir']) / 'claims' / 'waiting.json',
              {'stage': 'claimed', 'waiting': {'dependency': 2}})
    assert upgrade.attempt(cfg, upgrade.context(cfg))
    assert remote.calls and exports


@pytest.mark.parametrize('label_actor', ['owner', 'product'])
def test_priority_never_skips_dependency_or_interrupts_holder(tmp_path, monkeypatch, label_actor):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    add(hub, 2, '## Dependencies\nDepends on #3.')
    hub.issue(3, 'draft')
    priority(hub, 2, label_actor)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert started(cfg) == [1]
    assert next(c for c in claims(cfg) if c['issue'] == 2)['waiting'] == {'dependency': 3}
    close(hub, 3)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 300)
    assert started(cfg) == [1]
    assert next(c for c in claims(cfg) if c['issue'] == 2)['waiting'] == {'story': 1}


def test_same_story_reapproval_never_waits_for_own_earlier_job(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    path = next((Path(cfg['state_dir']) / 'jobs').glob('*/runner-state/tasks/*.json'))
    job = runner.read(path)
    job['status'] = 'blocked'
    runner.save(path, job)
    hub.event(1, 'blocked', 'runner')
    hub.event(1, 'approved')
    hub.approve(1)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    newest = max(claims(cfg), key=lambda c: c['event'])
    assert 'waiting' not in newest


def test_old_stop_does_not_hide_a_newer_dispatched_job(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    original = service.jobs(cfg)[0]
    stopped = dict(original, id='old-stop', status='blocked')
    old = Path(cfg['state_dir']) / 'jobs' / 'old-stop' / 'runner-state' / 'tasks' / 'old-stop.json'
    runner.save(old, stopped)
    add(hub, 2)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert next(c for c in claims(cfg) if c['issue'] == 2)['waiting'] == {'story': 1}
    assert all(j['assignment']['lifecycle']['issue'] != 2 for j in service.jobs(cfg))


def test_closed_pr_releases_even_ready_for_integration_job(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    path = next((Path(cfg['state_dir']) / 'jobs').glob('*/runner-state/tasks/*.json'))
    job = runner.read(path)
    job.update(status='ready_for_integration', integration={'pr': 77})
    runner.save(path, job)
    hub.prs[77] = {'number': 77, 'state': 'closed', 'merged': True}
    add(hub, 2)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert 2 in started(cfg)


def test_scheduled_retry_retains_story_slot(tmp_path, monkeypatch):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    path = next((Path(cfg['state_dir']) / 'jobs').glob('*/runner-state/tasks/*.json'))
    job = runner.read(path)
    job.update(status='blocked', retry={'not_before': 300})
    runner.save(path, job)
    hub.event(1, 'blocked', 'runner')
    add(hub, 2)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert next(c for c in claims(cfg) if c['issue'] == 2)['waiting'] == {'story': 1}


def test_close_event_read_once_until_next_close_generation(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issue(2, 'draft')
    close(hub, 2)
    client = hub.client()
    seen = []
    original = client.events
    monkeypatch.setattr(client, 'events', lambda n: (seen.append(n), original(n))[1])
    folder = Path(cfg['state_dir']) / 'claims'
    queue = service.StoryQueue(cfg, client, hub.issues, {2}, folder)
    assert queue.merged(2)
    hub.issues[2]['updated_at'] = '2026-01-01T10:02:00Z'
    queue = service.StoryQueue(cfg, client, hub.issues, {2}, folder)
    assert queue.merged(2) and seen == [2]
    hub.issues[2].update(state='open', closed_at=None)
    queue = service.StoryQueue(cfg, client, hub.issues, {2}, folder)
    assert not queue.merged(2)
    close(hub, 2, actor='product')
    hub.issues[2]['closed_at'] = '2026-01-01T10:03:00Z'
    queue = service.StoryQueue(cfg, client, hub.issues, {2}, folder)
    assert not queue.merged(2) and seen == [2, 2]


def test_verified_finish_record_releases_dependency(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issues[1]['body'] += '\n## Dependencies\nDepends on #2.'
    hub.event(1, 'approved')
    hub.approve(1)
    hub.issue(2, 'merged')
    record = core.public_record(cfg, 2, 'finished', 1, 'merged', event=102)
    record.update({'from': 'pr-open', 'actor': cfg['actors']['workflow']})
    hub.comments_by_issue[2].append({'user': {'login': cfg['actors']['workflow']},
                                     'body': core.record_body(record)})
    hub.issues[2]['labels'] = [{'name': 'status:approved'}]
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert started(cfg) == [1]


def test_digest_waiting_story_is_not_stale(tmp_path):
    from digest import standup
    from test_digest import AT, configuration
    from test_lifecycle import Hub
    cfg = configuration(tmp_path)
    hub = Hub(cfg['lifecycle'])
    hub.issue()
    core.save(Path(cfg['lifecycle']['state_dir']) / 'claims' / 'waiting.json',
              {'issue': 1, 'event': 101, 'stage': 'claimed', 'waiting': {'story': 2}})
    sections = standup.collect(cfg, hub.client(), AT, AT)
    text = '\n'.join(x for section in sections for x in section)
    assert '#1: waiting for the story in progress, #2' in text
    assert 'stale' not in text


def test_queue_sort_keeps_legacy_finished_claims_without_events(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    folder = Path(cfg['state_dir']) / 'claims'
    path = folder / 'legacy.json'
    core.save(path, {'issue': 1, 'stage': 'finished'})
    queue = service.StoryQueue(cfg, hub.client(), hub.issues, set(), folder)
    assert queue.order(path)[0] == 0


@pytest.mark.parametrize('phase', ['order', 'decide'])
def test_queue_unexpected_error_stops_one_story_and_dispatches_next(tmp_path, monkeypatch, phase):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    add(hub, 2)
    original = getattr(service.StoryQueue, phase)
    def fail_one(queue, item):
        claim = core.read(item) if phase == 'order' else item
        if claim['issue'] == 1:
            raise RuntimeError('synthetic queue failure')
        return original(queue, item)
    monkeypatch.setattr(service.StoryQueue, phase, fail_one)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert started(cfg) == [2]
    stopped = next(c for c in claims(cfg) if c['issue'] == 1)
    assert stopped['stage'] == 'finished' and stopped['error'] == 'unexpected_error'
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    assert sum(r.get('reason') == 'unexpected_error' for r in records) == 1
    assert service.heartbeat(cfg) == 100


@pytest.mark.parametrize('phase', ['order', 'decide'])
@pytest.mark.parametrize('kind', ['unavailable', 'integrity'])
def test_queue_transport_and_integrity_errors_still_escape(tmp_path, monkeypatch, phase, kind):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    add(hub, 2)
    error = core.Unavailable('synthetic transport') if kind == 'unavailable' else RuntimeError('synthetic integrity')
    if kind == 'integrity':
        error.integrity_error = True
    original = getattr(service.StoryQueue, phase)
    def fail_one(queue, item):
        claim = core.read(item) if phase == 'order' else item
        if claim['issue'] == 1:
            raise error
        return original(queue, item)
    monkeypatch.setattr(service.StoryQueue, phase, fail_one)
    with pytest.raises(type(error)) as caught:
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert caught.value is error
    assert started(cfg) == []
    assert not any(r.get('reason') == 'unexpected_error'
                   for r in core.records(cfg, hub.comments_by_issue[1], 1))


@pytest.mark.parametrize('listing_present', [True, False])
def test_released_pr_adds_no_reads_and_reopen_generation_is_revalidated(tmp_path, monkeypatch, listing_present):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    def bind(number, pr_number, state):
        path = next(p for p in (Path(cfg['state_dir']) / 'jobs').glob('*/runner-state/tasks/*.json')
                    if runner.read(p)['assignment']['lifecycle']['issue'] == number)
        job = runner.read(path)
        job.update(status='needs_owner', integration={'pr': pr_number})
        runner.save(path, job)
        hub.event(number, 'needs-owner', 'runner')
        hub.prs[pr_number] = {'number': pr_number, 'state': state, 'merged': False}
        if listing_present:
            row = hub.issue(pr_number, 'draft')
            row.update(pull_request={}, state=state, closed_at=WHEN if state == 'closed' else None)
    bind(1, 77, 'closed')
    add(hub, 2)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    bind(2, 88, 'open')
    add(hub, 3)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 300)
    for at in [400, 500]:
        hub.calls.clear()
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: at)
        assert [c[1] for c in hub.calls if c[0] == 'GET' and c[1].startswith('/pulls/')] == ['/pulls/88']
        assert next(c for c in claims(cfg) if c['issue'] == 3)['waiting'] == {'story': 2}
    hub.prs[77]['state'] = 'open'
    if listing_present:
        hub.issues[77].update(state='open', closed_at=None, updated_at='2026-01-02T10:00:00Z')
    else:
        hub.issues[1]['updated_at'] = '2026-01-02T10:00:00Z'
    hub.calls.clear()
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 600)
    assert sum(c[:2] == ('GET', '/pulls/77') for c in hub.calls) == 1
    assert next(c for c in claims(cfg) if c['issue'] == 3)['waiting'] == {'story': 1}
    assert 3 not in started(cfg)


@pytest.mark.parametrize('waiting_kind', ['dependency', 'story'])
@pytest.mark.parametrize('final', ['closed', 'merged', 'accepted'])
def test_terminal_waiting_story_retires_without_dispatch_or_stale_visibility(tmp_path, monkeypatch, waiting_kind, final):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    if waiting_kind == 'dependency':
        target = 1
        hub.issues[1]['body'] += '\n## Dependencies\nDepends on #2.'
        hub.event(1, 'approved')
        hub.approve(1)
        hub.issue(2, 'draft')
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    else:
        target = 2
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
        add(hub, 2)
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert target in service.queue_waiting(cfg)
    before = started(cfg)
    if final == 'closed':
        close(hub, target)
    else:
        hub.issue(target, final)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 300)
    claim = next(c for c in claims(cfg) if c['issue'] == target)
    assert claim['stage'] == 'finished' and 'waiting' not in claim
    assert started(cfg) == before and target not in service.queue_waiting(cfg)
    assert f'#{target}: waiting' not in service.status(cfg, clock=lambda: 300)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 400)
    assert f'#{target}: waiting' not in service.poll_body(cfg, 400, [])
    from digest import standup
    from test_digest import AT, configuration
    digest = configuration(tmp_path / 'digest-fixture')
    digest['lifecycle'] = cfg
    digest['runner_state_dirs'] = [cfg['state_dir']]
    text = '\n'.join(x for section in standup.collect(digest, hub.client(), AT, AT) for x in section)
    assert f'#{target}: waiting' not in text
