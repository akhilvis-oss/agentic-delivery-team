"""Compatibility aliases are read without changing stored records or policy."""

import copy
import json
from pathlib import Path

import pytest

from lifecycle import core, service, smoke
from test_lifecycle import Hub, policy
from test_lifecycle_continuation import approve, newest_claim, saved_story


def alias(monkeypatch, name, target):
    monkeypatch.setitem(core.STATUS_NAMES, name, target)


def stored_alias(record, name):
    """Construct an old stored target and its authentic original hash."""
    record = copy.deepcopy(record)
    record['to'] = name
    fields = ['issue', 'job', 'round', 'to', 'kind', 'event', 'outcome', 'reason',
              'commit', 'pr', 'gate', 'scope_sha256']
    record['key'] = core.key([record[f] for f in fields])
    if 'owner_question' in record or 'failed_tests' in record:
        record['key'] = core.key([record['key'], record.get('owner_question'), record.get('failed_tests')])
    if 'continuation' in record or 'stop_save_failed' in record:
        record['key'] = core.key([record['key'], record['continuation'], record['stop_save_failed']])
    if 'conflict_files' in record:
        record['key'] = core.key([record['key'], record['conflict_files']])
    if 'stop_details' in record:
        record['key'] = core.key([record['key'], record['stop_details']])
    if 'exception_class' in record or 'function' in record:
        record['key'] = core.key([record['key'], record.get('exception_class'), record.get('function')])
    return record


def comment(record):
    return {'user': {'login': record['actor']},
            'body': core.PREFIX + json.dumps(record) + core.SUFFIX}


def test_identity_table_and_all_named_readers_accept_alias(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    assert len(core.STATES) == 13
    assert core.STATUS_NAMES == {name: name for name in core.STATES}
    alias(monkeypatch, 'legacy-blocked', 'needs-owner')
    hub = Hub(cfg)
    issue = hub.issue(state='needs-owner')
    event = hub.events_by_issue[1][-1]
    event['label']['name'] = 'status:legacy-blocked'
    issue['labels'] = [{'name': 'status:legacy-blocked'}]
    original = copy.deepcopy(event)
    assert core.state(issue) == 'needs-owner'
    assert core.canonical_state(cfg, hub.events_by_issue[1]) == ('needs-owner', event)
    assert core.permitted(cfg, 'approved', 'legacy-blocked', cfg['actors']['runner'])
    assert core.permitted(cfg, 'legacy-blocked', 'approved', cfg['actors']['owner'])
    finish = hub.event(1, 'merged', 'owner')
    assert core.owner_finish_event(cfg, hub.events_by_issue[1]) == ('needs-owner', finish)
    assert event == original


def test_records_validate_raw_hash_before_returning_mapped_copy(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    alias(monkeypatch, 'legacy-blocked', 'needs-owner')
    alias(monkeypatch, 'legacy-approved', 'approved')
    raw = core.public_record(cfg, 1, 'old-job', 0, 'needs-owner', reason='owner_merge_required')
    raw.update(actor=cfg['actors']['runner'], **{'from': 'legacy-approved'})
    raw = stored_alias(raw, 'legacy-blocked')
    c = comment(raw)
    before = copy.deepcopy(c)
    mapped, = core.records(cfg, [c], 1)
    assert mapped == dict(raw, to='needs-owner', **{'from': 'approved'})
    assert c == before
    assert mapped['key'] == raw['key']
    canonical_key = core.public_record(cfg, 1, 'old-job', 0, 'needs-owner', reason='owner_merge_required')['key']
    assert canonical_key != raw['key']
    tampered = dict(raw, key=canonical_key)
    assert core.records(cfg, [comment(tampered)], 1) == []
    with pytest.raises(ValueError):
        core.public_record(cfg, 1, 'new-job', 0, 'legacy-blocked')


@pytest.mark.parametrize('damage', ['actor', 'from', 'reason', 'issue', 'fields', 'approval'])
def test_alias_keeps_record_and_actor_checks(tmp_path, monkeypatch, damage):
    cfg = policy(tmp_path)
    alias(monkeypatch, 'legacy-blocked', 'needs-owner')
    raw = core.public_record(cfg, 1, 'old-job', 0, 'needs-owner')
    raw.update(actor=cfg['actors']['runner'], **{'from': 'approved'})
    raw = stored_alias(raw, 'legacy-blocked')
    if damage == 'actor':
        raw['actor'] = cfg['actors']['product']
    elif damage == 'from':
        raw['from'] = 'unknown-state'
    elif damage == 'reason':
        raw['reason'] = 'invented_reason'
        raw = stored_alias(raw, 'legacy-blocked')
    elif damage == 'issue':
        raw['issue'] = 2
        raw = stored_alias(raw, 'legacy-blocked')
    elif damage == 'fields':
        raw['unexpected'] = True
    else:
        raw['kind'] = 'approval'
        raw = stored_alias(raw, 'legacy-blocked')
    c = comment(raw)
    c['user']['login'] = cfg['actors']['runner']
    assert core.records(cfg, [c], 1) == []


def test_alias_cannot_grant_owner_approval(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    alias(monkeypatch, 'legacy-approved', 'approved')
    assert not core.permitted(cfg, 'draft', 'legacy-approved', cfg['actors']['runner'])
    assert core.permitted(cfg, 'draft', 'legacy-approved', cfg['actors']['owner'])


def test_smoke_alias_label_requires_matching_mapped_stop_record(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    alias(monkeypatch, 'legacy-blocked', 'needs-owner')
    hub = Hub(cfg)
    hub.issue(state='approved')
    hub.approve()
    hub.issues[1]['labels'] = [{'name': 'status:legacy-blocked'}]
    assert smoke.evidence(cfg, hub.client(), 1) == (1, False, 'stop_unrecorded')
    raw = core.public_record(cfg, 1, 'old-job', 0, 'needs-owner', reason='owner_merge_required')
    raw.update(actor=cfg['actors']['runner'], **{'from': 'approved'})
    hub.comments_by_issue[1].append(comment(stored_alias(raw, 'legacy-blocked')))
    assert smoke.evidence(cfg, hub.client(), 1) == (1, True, 'owner_merge_required')


def test_claim_event_maps_primary_admission_and_event(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    alias(monkeypatch, 'legacy-primary', 'needs-primary')
    hub = Hub(cfg)
    issue = hub.issue(state='needs-primary')
    event = hub.events_by_issue[1][-1]
    event['label']['name'] = 'status:legacy-primary'
    issue['labels'] = [{'name': 'status:legacy-primary'}]
    assert service.claim_event(cfg, issue, hub.events_by_issue[1]) == (event, True)


@pytest.mark.parametrize('open_pr', [False, True])
def test_continuation_and_open_pr_reapproval_read_aliases(tmp_path, monkeypatch, open_pr):
    cfg, hub, runner, old = saved_story(tmp_path, monkeypatch)
    alias(monkeypatch, 'legacy-blocked', 'needs-owner')
    alias(monkeypatch, 'legacy-approved', 'approved')
    for c in hub.comments_by_issue[1]:
        if core.PREFIX not in c['body']:
            continue
        raw = json.loads(c['body'].split(core.PREFIX)[1].split(core.SUFFIX)[0])
        if raw['to'] == 'needs-owner':
            c['body'] = comment(stored_alias(raw, 'legacy-blocked'))['body']
    if open_pr:
        old['integration'] = {'pr': 77, 'stage': 'owner'}
        core.save(Path(cfg['state_dir']) / 'jobs' / old['id'] / 'runner-state' / 'tasks' / (old['id'] + '.json'), old)
        hub.prs[77] = {'number': 77, 'state': 'open'}
    approve(hub, old['head_commit'])
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    claim = newest_claim(cfg)
    if open_pr:
        assert claim['error'] == 'pr_open_reapproval'
        assert len(service.jobs(cfg)) == 1
    else:
        assert 'continued_from' in claim, claim
        assert claim['continued_from'] == old['head_commit']
        assert claim['previous_stop_reason'] == 'worker_reported_blocked'
        assert len(service.jobs(cfg)) == 2


def test_alias_restoration_record_still_excluded(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    alias(monkeypatch, 'legacy-blocked', 'needs-owner')
    record = core.public_record(cfg, 1, 'guard', 0, 'needs-owner',
                                kind='guard', reason='invalid_status_change')
    record.update(actor=cfg['actors']['workflow'], **{'from': 'legacy-blocked'})
    raw = stored_alias(record, 'legacy-blocked')
    assert core.records(cfg, [comment(raw)], 1) == []
