"""Restoration notices are informational and cannot acquire state authority."""

import copy
import json

import pytest

from lifecycle import core
from test_lifecycle import Hub, WHEN, policy


def restoration(cfg, event=106, job='guard'):
    item = core.public_record(cfg, 1, job, 0, 'needs-owner', event=event,
                              kind='guard', reason='invalid_status_change')
    item.update(actor=cfg['actors']['workflow'], **{'from': 'needs-owner'})
    return item


def comment(cfg, item):
    return dict(user={'login': cfg['actors']['workflow']}, body=core.record_body(item),
                created_at=WHEN, updated_at=WHEN)


def test_real_shaped_restoration_is_explicitly_excluded(tmp_path):
    cfg = policy(tmp_path)
    item = restoration(cfg)
    assert core.records(cfg, [comment(cfg, item)], 1) == []


def test_label_stop_preserves_current_parsing(tmp_path):
    cfg = policy(tmp_path)
    item = core.public_record(cfg, 1, 'label-stop', 0, 'needs-owner', event=106,
                              kind='guard', reason='owner_requested_stop')
    item.update(actor=cfg['actors']['workflow'], **{'from': 'needs-owner'})
    assert core.records(cfg, [comment(cfg, item)], 1) == [item]


def test_restoration_followed_by_guard_is_one_notice(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='needs-owner')
    hub.event(1, 'approved', 'runner')  # An impermissible change.
    core.guard_issue(cfg, hub.client('workflow'), 1)
    notice, = hub.comments_by_issue[1]
    notice.update(created_at=WHEN, updated_at=WHEN)
    restored_event = hub.events_by_issue[1][-1]['id']
    data = json.loads(notice['body'].split(core.PREFIX)[1].split(core.SUFFIX)[0])
    assert data['event'] == restored_event
    core.guard_issue(cfg, hub.client('workflow'), 1)
    assert len(hub.comments_by_issue[1]) == 1
    assert core.records(cfg, hub.comments_by_issue[1], 1) == []


@pytest.mark.parametrize('change', ['none', 'actor', 'edited', 'missing_time', 'event',
                                   'job', 'kind', 'reason', 'issue', 'repository', 'text_only',
                                   'two_blocks', 'boolean_event'])
def test_only_authenticated_exact_restoration_suppresses_workflow_stop(tmp_path, change):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    event = hub.event(1, 'needs-owner', 'workflow')
    item = restoration(cfg, event['id'])
    # Model today's malformed restoration: it does not pass records().
    item['from'] = 'approved'
    notice = comment(cfg, item)
    if change in {'job', 'kind', 'reason', 'issue', 'repository', 'event', 'boolean_event'}:
        field = 'event' if change == 'boolean_event' else change
        item[field] = {'job': 'another-job', 'kind': 'transition', 'reason': 'owner_requested_stop',
                       'issue': 2, 'repository': 'example/other', 'event': event['id'] - 1,
                       'boolean_event': True}[change]
        notice['body'] = core.record_body(item)
    if change == 'actor':
        notice['user']['login'] = cfg['actors']['owner']
    if change == 'edited':
        notice['updated_at'] = '2026-01-02T11:00:00+00:00'
    if change == 'missing_time':
        del notice['updated_at']
    if change == 'text_only':
        notice['body'] = 'Needs a fix: The guard restored the last permitted status.'
    if change == 'two_blocks':
        notice['body'] *= 2
    hub.comments_by_issue[1].append(notice)
    core.guard_issue(cfg, hub.client('workflow'), 1)
    assert len(hub.comments_by_issue[1]) == (1 if change == 'none' else 2)
    if change != 'none':
        assert core.records(cfg, hub.comments_by_issue[1][-1:], 1)[0]['job'] == 'label-stop'


def test_guard_records_do_not_change_dispatch_or_owner_merge_evidence(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    for target, kind, outcome, reason in [
            ('dispatched', 'transition', None, None),
            ('in-review', 'review', 'ready_for_integration', None),
            ('needs-owner', 'transition', None, 'owner_merge_required')]:
        item = core.public_record(cfg, 1, 'original', 1, target, event=101,
                                  kind=kind, outcome=outcome, reason=reason, commit='a' * 40, pr=9)
        item.update(actor=cfg['actors']['runner'], **{'from': 'committed' if kind == 'review' else 'in-review' if target != 'dispatched' else 'approved'})
        hub.comments_by_issue[1].append(dict(user={'login': item['actor']},
                                             body=core.record_body(item)))
    before = core.records(cfg, hub.comments_by_issue[1], 1)
    pr = dict(number=9, merged=True, base={'ref': 'main'},
              head={'sha': 'a' * 40, 'repo': {'full_name': cfg['repository']}},
              user={'login': cfg['actors']['runner']}, body='Closes #1')
    assert core.owner_merge_verified(cfg, 1, before, pr)
    hub.comments_by_issue[1].append(comment(cfg, restoration(cfg)))
    after = core.records(cfg, hub.comments_by_issue[1], 1)
    assert after == before
    assert core.owner_merge_verified(cfg, 1, after, pr)
    assert [r['job'] for r in after if r['to'] == 'dispatched'] == ['original']
    assert copy.deepcopy(before) == after


def test_missing_restoration_event_readback_claims_no_event(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='needs-owner')
    hub.event(1, 'approved', 'runner')
    client = hub.client('workflow')
    original = client.events
    old = original(1)
    calls = []

    def events(number):
        calls.append(number)
        return old if len(calls) == 2 else original(number)

    monkeypatch.setattr(client, 'events', events)
    core.guard_issue(cfg, client, 1)
    notice, = hub.comments_by_issue[1]
    notice.update(created_at=WHEN, updated_at=WHEN)
    item = json.loads(notice['body'].split(core.PREFIX)[1].split(core.SUFFIX)[0])
    assert item['event'] is None
    core.guard_issue(cfg, client, 1)
    core.guard_issue(cfg, client, 1)
    assert len(hub.comments_by_issue[1]) == 2  # Safe fallback: one label-stop notice.
    assert core.records(cfg, hub.comments_by_issue[1], 1)[0]['job'] == 'label-stop'


@pytest.mark.parametrize('payload', [[], [1], None, True, 17, 'plain text'])
def test_non_object_data_block_cannot_crash_or_suppress_stop(tmp_path, payload):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    hub.event(1, 'needs-owner', 'workflow')
    malformed = dict(user={'login': cfg['actors']['workflow']},
                     body=core.PREFIX + json.dumps(payload) + core.SUFFIX,
                     created_at=WHEN, updated_at=WHEN)
    hub.comments_by_issue[1].append(malformed)
    assert core.records(cfg, [malformed], 1) == []
    core.guard_issue(cfg, hub.client('workflow'), 1)
    assert len(hub.comments_by_issue[1]) == 2
    assert core.records(cfg, hub.comments_by_issue[1], 1)[0]['reason'] == 'owner_requested_stop'


@pytest.mark.parametrize('fields', [('created_at',), ('updated_at',),
                                    ('created_at', 'updated_at')])
@pytest.mark.parametrize('value', [None, 0, False, [], {}, 'missing'])
def test_malformed_comment_timestamps_cannot_crash_or_suppress_stop(tmp_path, fields, value):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    event = hub.event(1, 'needs-owner', 'workflow')
    notice = comment(cfg, restoration(cfg, event['id']))
    for field in fields:
        if value == 'missing':
            del notice[field]
        else:
            notice[field] = value
    hub.comments_by_issue[1].append(notice)
    assert core.restoration_noticed(cfg, [notice], 1, event, 'needs-owner') is False
    core.guard_issue(cfg, hub.client('workflow'), 1)
    assert len(hub.comments_by_issue[1]) == 2
    assert core.records(cfg, hub.comments_by_issue[1][-1:], 1)[0]['job'] == 'label-stop'
