"""Offline error-isolation, durable-stop and reconciliation regressions."""

from pathlib import Path

import pytest

from lifecycle import core, service
from test_lifecycle import configured_service
from test_lifecycle_poll_budget import SinceClient


def claims(cfg):
    return [core.read(p) for p in sorted((Path(cfg['state_dir']) / 'claims').glob('*.json'))]


def error_files(cfg):
    return sorted((Path(cfg['state_dir']) / 'story-errors').glob('*.json'))


def stops(cfg, hub, number=1):
    return [r for r in core.records(cfg, hub.comments_by_issue[number], number)
            if r['reason'] == 'unexpected_error']


def crash(*args, **kwargs):
    raise RuntimeError('private exception text must never be published')


def test_story_error_does_not_prevent_next_dispatch_and_records_origin(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issue(2)
    hub.approve(2)
    original = service.ensure_worktree

    def fail_first(config, claim):
        if claim['issue'] == 1:
            crash()
        return original(config, claim)

    monkeypatch.setattr(service, 'ensure_worktree', fail_first)
    assert service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100) == ['issue-2-event-102']
    assert core.state(hub.issues[1]) == 'blocked'
    assert core.state(hub.issues[2]) == 'dispatched'
    record, = stops(cfg, hub)
    assert record['exception_class'] == 'RuntimeError'
    assert record['function'] == 'poll'
    assert ('An unexpected error stopped this story only, in poll. Owner: ask the operator '
            'to investigate, then re-approve.') in core.record_body(record)
    assert 'private exception text' not in str(hub.comments_by_issue)


@pytest.mark.parametrize('flush_failure', [False, True])
def test_terminal_error_never_runs_or_resaves_job_even_after_reapproval(
        tmp_path, monkeypatch, flush_failure):
    cfg, hub, runner = configured_service(tmp_path, monkeypatch)
    template = core.read(cfg['runner_config'])
    template['integration'] = {'enabled': True, 'declared_tier': 'standard'}
    core.save(cfg['runner_config'], template)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    claim, = claims(cfg)
    job_path = Path(cfg['state_dir']) / 'jobs' / claim['job'] / 'runner-state' / 'tasks' / (claim['job'] + '.json')
    before = job_path.read_bytes()
    calls = []

    def failed_run(*args, **kwargs):
        calls.append(args)
        crash()

    monkeypatch.setattr(runner, 'run_once', failed_run)
    flush = service.flush_outbox

    def failed_flush(config, client):
        if any(c.get('error') == 'unexpected_error' for c in claims(config)):
            raise core.Unavailable('offline flush')
        return flush(config, client)

    if flush_failure:
        monkeypatch.setattr(service, 'flush_outbox', failed_flush)
        with pytest.raises(core.Unavailable):
            service.poll(cfg, hub.client(), clock=lambda: 200)
        assert claims(cfg)[0]['stage'] == 'finished'
        monkeypatch.setattr(service, 'flush_outbox', flush)
    else:
        service.poll(cfg, hub.client(), clock=lambda: 200)
    service.poll(cfg, hub.client(), clock=lambda: 300)
    assert len(calls) == 1 and len(stops(cfg, hub)) == 1
    assert job_path.read_bytes() == before
    hub.event(1, 'approved', 'owner')
    hub.approve()
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 400)
    assert len(calls) == 1 and len(stops(cfg, hub)) == 1
    assert job_path.read_bytes() == before


@pytest.mark.parametrize('state', ['draft', 'pr-open', 'approved'])
def test_local_only_error_is_write_once_and_retries_incremental_listing(
        tmp_path, monkeypatch, state):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    hub.issues[1]['labels'] = [{'name': 'status:' + state}]
    hub.issues[1]['updated_at'] = '1970-01-01T00:00:00Z'
    client = SinceClient(hub)
    original = service.backfill_owner_merge
    reads = []

    def failed(config, api, issue):
        reads.append(issue['number'])
        crash()

    monkeypatch.setattr(service, 'backfill_owner_merge', failed)
    service.poll(cfg, client, clock=lambda: 1000)
    path, = error_files(cfg)
    before = path.stat().st_mtime_ns
    assert len(reads) == 1 and core.state(hub.issues[1]) == state
    assert 'Issue #1: RuntimeError in poll' in service.status(cfg)
    service.poll(cfg, client, clock=lambda: 1600)
    assert len(reads) == 2 and path.stat().st_mtime_ns == before
    assert '&since=' in client.listings[-1]
    assert not stops(cfg, hub)
    monkeypatch.setattr(service, 'backfill_owner_merge', original)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 2200)
    assert not error_files(cfg)


def test_before_event_error_retries_and_clears_only_after_all_claims_clean(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    client = SinceClient(hub)
    hub.issues[1]['updated_at'] = '1970-01-01T00:00:00Z'
    original = client.events
    monkeypatch.setattr(client, 'events', crash)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 1000)
    path, = error_files(cfg)
    assert core.state(hub.issues[1]) == 'approved' and not stops(cfg, hub)
    # On the next poll the event becomes known, but another phase still fails.
    monkeypatch.setattr(client, 'events', original)
    ensure = service.ensure_worktree
    monkeypatch.setattr(service, 'ensure_worktree', crash)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 1600)
    assert path.exists() and len(stops(cfg, hub)) == 1
    monkeypatch.setattr(service, 'ensure_worktree', ensure)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 2200)
    assert not path.exists()


@pytest.mark.parametrize('point', ['backfill', 'event', 'processing'])
def test_ambiguous_status_is_silent_including_mid_poll_change(tmp_path, monkeypatch, point):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    client = hub.client()
    if point == 'backfill':
        hub.issues[1]['labels'].append({'name': 'status:blocked'})
    elif point == 'event':
        original = client.events

        def events(number):
            result = original(number)
            hub.issues[number]['labels'].append({'name': 'status:blocked'})
            return result

        monkeypatch.setattr(client, 'events', events)
    else:
        service.poll(cfg, client, run_jobs=False, clock=lambda: 10)
        original = client.issue

        def issue(number):
            item = original(number)
            item['labels'].append({'name': 'status:blocked'})
            return item

        monkeypatch.setattr(client, 'issue', issue)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 100)
    assert not stops(cfg, hub) and not error_files(cfg)


@pytest.mark.parametrize('point', ['backfill', 'claim_creation', 'claim_processing'])
def test_unavailable_still_pauses_whole_poll(tmp_path, monkeypatch, point):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    client = hub.client()

    def unavailable(*args, **kwargs):
        raise core.Unavailable('offline')

    if point == 'backfill':
        monkeypatch.setattr(service, 'backfill_owner_merge', unavailable)
    elif point == 'claim_creation':
        monkeypatch.setattr(client, 'events', unavailable)
    else:
        monkeypatch.setattr(service, 'ensure_worktree', unavailable)
    with pytest.raises(core.Unavailable):
        service.poll(cfg, client, run_jobs=False, clock=lambda: 100)
    assert not stops(cfg, hub) and not error_files(cfg)


def test_fresh_runner_integrity_error_escapes_boundary(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    # Load from disk using the real factory, independently of fixture's runner.
    import importlib.util
    spec = importlib.util.spec_from_file_location('fresh_integrity_runner',
                                                Path('runner/task_runner.py'))
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert issubclass(fresh.IntegrityError, fresh.Blocked)

    def mismatch(*args):
        fresh.verify_assignment({}, {'assignment': {}, 'assignment_sha256': 'changed'})

    monkeypatch.setattr(service, 'ensure_worktree', mismatch)
    with pytest.raises(fresh.IntegrityError):
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    assert not stops(cfg, hub)


def test_finished_claim_error_is_local_even_on_new_approval(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    path, = (Path(cfg['state_dir']) / 'claims').glob('*.json')
    claim = core.read(path)
    claim['stage'] = 'finished'
    core.save(path, claim)
    monkeypatch.setattr(service, 'cleanup', crash)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert error_files(cfg) and not stops(cfg, hub)


def test_public_error_metadata_validates_and_roundtrips(tmp_path):
    from test_lifecycle import Hub, policy
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-progress')
    record = core.public_record(cfg, 1, 'job', 0, 'blocked', reason='unexpected_error',
                                exception_class='RuntimeError', function='poll')
    core.queue(cfg, record)
    core.flush(cfg, hub.client())
    assert stops(cfg, hub)[0]['key'] == record['key']
    for field, bad in [('exception_class', 'Bad.Name'), ('exception_class', 'x' * 81),
                       ('function', 'private/path')]:
        with pytest.raises(ValueError):
            core.public_record(cfg, 1, 'job', 0, 'blocked', reason='unexpected_error', **{field: bad})
    with pytest.raises(ValueError):
        core.public_record(cfg, 1, 'job', 0, 'blocked', reason='execution_failed', function='poll')
    assert record['key'] != core.public_record(
        cfg, 1, 'job', 0, 'blocked', reason='unexpected_error',
        exception_class='ValueError', function='poll')['key']
    assert record['key'] != core.public_record(
        cfg, 1, 'job', 0, 'blocked', reason='unexpected_error',
        exception_class='RuntimeError', function='claim_event')['key']


def test_claim_creation_error_with_known_event_gets_public_terminal_stop(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, 'continuation', crash)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    claim, = claims(cfg)
    assert claim['stage'] == 'finished' and claim['error'] == 'unexpected_error'
    assert len(stops(cfg, hub)) == 1 and not service.jobs(cfg)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert len(stops(cfg, hub)) == 1 and not service.jobs(cfg)


def test_fresh_pr_open_read_overrides_stale_listing_for_error_boundary(tmp_path, monkeypatch):
    from copy import deepcopy
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    client = hub.client()
    original = client.issue

    def fresh_issue(number):
        item = deepcopy(original(number))
        item['labels'] = [{'name': 'status:pr-open'}]
        return item

    monkeypatch.setattr(client, 'issue', fresh_issue)
    monkeypatch.setattr(client, 'comments', crash)
    service.poll(cfg, client, clock=lambda: 200)
    assert error_files(cfg) and not stops(cfg, hub)
    assert core.state(hub.issues[1]) == 'dispatched'


@pytest.mark.parametrize("prior_stage", ["finished", "enqueued"])
def test_approval_event_error_on_reapproval_is_retried_incrementally(tmp_path, monkeypatch, prior_stage):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    client = SinceClient(hub)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 1000)
    path, = (Path(cfg['state_dir']) / 'claims').glob('*.json')
    claim = core.read(path)
    claim['stage'] = prior_stage
    core.save(path, claim)
    before = path.read_bytes()
    hub.event(1, 'approved', 'owner')
    hub.approve()
    hub.issues[1]['updated_at'] = service.timestamp(1600).replace('+00:00', 'Z')
    original = client.events
    monkeypatch.setattr(client, 'events', crash)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 1600)
    assert error_files(cfg) and not stops(cfg, hub)
    assert path.read_bytes() == before
    monkeypatch.setattr(client, 'events', original)
    hub.issues[1]['updated_at'] = '1970-01-01T00:00:00Z'
    service.poll(cfg, client, run_jobs=False, clock=lambda: 2200)
    assert '&since=' in client.listings[-1]
    assert len(claims(cfg)) == 2 and not error_files(cfg)


def test_pr_open_and_suspended_unfinished_claims_only_record_locally(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 100)
    path, = (Path(cfg['state_dir']) / 'claims').glob('*.json')
    claim = core.read(path)
    claim['stage'] = 'suspended'
    core.save(path, claim)
    client = hub.client()
    monkeypatch.setattr(client, 'issue', crash)
    service.poll(cfg, client, clock=lambda: 200)
    assert error_files(cfg) and not stops(cfg, hub)
    assert core.state(hub.issues[1]) == 'dispatched'
    claim['stage'] = 'enqueued'
    core.save(path, claim)
    hub.issues[1]['labels'] = [{'name': 'status:pr-open'}]
    service.poll(cfg, client, clock=lambda: 300)
    assert error_files(cfg) and not stops(cfg, hub)
    assert core.state(hub.issues[1]) == 'pr-open'



def test_reconciliation_lookup_failure_after_full_listing_is_also_isolated(tmp_path, monkeypatch):
    cfg, hub, _ = configured_service(tmp_path, monkeypatch)
    client = hub.client()
    original = service.backfill_owner_merge
    monkeypatch.setattr(service, 'backfill_owner_merge', crash)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 100)
    monkeypatch.setattr(service, 'backfill_owner_merge', original)
    # A missing row still has a known issue number from private reconciliation.
    monkeypatch.setattr(client, 'pages', lambda path: [])
    monkeypatch.setattr(client, 'issue', crash)
    monkeypatch.setattr(service, 'publish_poll', lambda *args: None)
    service.poll(cfg, client, run_jobs=False, clock=lambda: 200, full_list=True)
    assert error_files(cfg) and not stops(cfg, hub)
    assert service.heartbeat(cfg) == 200


def test_unexpected_error_is_classified_as_needs_a_fix():
    assert core.STOP_KINDS['unexpected_error'] == 'Needs a fix'
    assert core.stop_notice('unexpected_error', 'Story stopped').startswith('Needs a fix: ')


@pytest.mark.parametrize('origin', ['core', 'fresh_runner'])
def test_continuation_integrity_failures_escape_service_boundary(tmp_path, monkeypatch, origin):
    from test_lifecycle_continuation import saved_story
    cfg, hub, _, _ = saved_story(tmp_path, monkeypatch)
    error_type = core.IntegrityError
    if origin == 'fresh_runner':
        import importlib.util
        spec = importlib.util.spec_from_file_location('fresh_continuation_integrity_runner',
                                                    Path('runner/task_runner.py'))
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        error_type = fresh.IntegrityError
    assert issubclass(error_type, ValueError)
    assert error_type.integrity_error is True
    hub.event(1, 'approved', 'owner')
    hub.approve()
    before = list(hub.comments_by_issue[1])
    called = []

    def mismatched_source(*args):
        called.append(True)
        raise error_type('synthetic recovery binding mismatch')

    monkeypatch.setattr(service, 'source_work', mismatched_source)
    with pytest.raises(error_type):
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert called
    assert hub.comments_by_issue[1] == before
    assert not stops(cfg, hub) and not error_files(cfg)


def test_continuation_still_refuses_ordinary_value_error(tmp_path, monkeypatch):
    from test_lifecycle_continuation import saved_story, newest_claim
    cfg, hub, _, _ = saved_story(tmp_path, monkeypatch)
    hub.event(1, 'approved', 'owner')
    hub.approve()

    def invalid_source(*args):
        raise ValueError('synthetic invalid source')

    monkeypatch.setattr(service, 'source_work', invalid_source)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    assert claim['error'] == 'continuation_commit_unrecorded'
    assert claim['stage'] == 'finished'
    assert not stops(cfg, hub)
