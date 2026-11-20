"""Stop identity and matching, using only synthetic lifecycle transports."""

from pathlib import Path

import pytest

from lifecycle import core, product
from test_lifecycle import Hub, WHEN, policy
from test_runner_integration import delivery, setup  # noqa: F401


def delivery_records(delivery_case, monkeypatch):
    """Give only notice tests a real lifecycle policy and record transport."""
    import test_runner_integration as delivery_tests

    config, _, job, client = delivery_case
    api = delivery_tests.runner.lifecycle_api()
    cfg = policy(Path(config['state_dir']) / 'notice-policy')
    cfg['repository'] = 'example/kit'
    cfg['actors']['runner'] = 'runner-bot'
    cfg['identity_roles']['runner-bot'] = ['runner']
    hub = Hub(cfg)
    hub.issue(state='in-review', body=job['assignment']['task_text'])
    transport = hub.client()
    original = client.request

    def request(method, path, data=None):
        if path == '/issues/1/comments' and method == 'POST':
            response = original(method, path, data)
            transport.request(method, path, data)
            return response
        return original(method, path, data)

    monkeypatch.setattr(client, 'request', request)
    comments = client.comments
    monkeypatch.setattr(client, 'comments', lambda number: transport.comments(number)
                        if number == 1 else comments(number))
    monkeypatch.setattr(client, 'events', transport.events, raising=False)
    monkeypatch.setattr(client, 'set_state', transport.set_state, raising=False)
    monkeypatch.setattr(client, 'issue', transport.issue)
    monkeypatch.setattr(client, 'role', 'runner', raising=False)
    monkeypatch.setattr(core, 'load_config', lambda _: cfg)
    monkeypatch.setattr(core, 'GitHub', lambda *args: client)
    monkeypatch.setattr(api, 'load_config', lambda _: cfg)
    monkeypatch.setattr(api, 'GitHub', lambda *args: client)
    monkeypatch.setattr(api, 'sync_job', core.sync_job)
    return cfg, hub


def test_product_stop_has_one_notice_and_separate_requests_remain_separate(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='draft')
    for identity in ['request-one', 'request-two']:
        product.answer(cfg, dict(id=identity, issue=1, kind='scope_change'), hub.client('product'))
        core.guard_issue(cfg, hub.client('workflow'), 1)
    recs = core.records(cfg, hub.client().comments(1), 1)
    assert len(hub.comments_by_issue[1]) == 2
    assert [r['job'] for r in recs] == ['request-one', 'request-two']
    assert all(r['event'] is None for r in recs)


@pytest.mark.parametrize('extra_label', [False, True])
def test_owner_stop_even_with_product_role_has_one_record_notice(tmp_path, extra_label):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    event = hub.event(1, 'needs-owner', 'owner')
    if extra_label:
        hub.issues[1]['labels'].append({'name': 'status:in-review'})
    core.guard_issue(cfg, hub.client('workflow'), 1)
    core.guard_issue(cfg, hub.client('workflow'), 1)
    comment, = hub.comments_by_issue[1]
    record, = core.records(cfg, [comment], 1)
    assert record['reason'] == 'owner_requested_stop'
    assert record['event'] == event['id']


@pytest.mark.parametrize('variant', ['none', 'old', 'wrong_actor', 'wrong_status',
                                     'clarification', 'rejection', 'older_restoration', 'matching'])
def test_workflow_stop_requires_current_matching_record(tmp_path, variant):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    hub.events_by_issue[1][-1]['created_at'] = '2026-01-02T09:00:00+00:00'
    hub.event(1, 'needs-owner', 'workflow')
    if variant != 'none':
        actor = 'product' if variant in {'wrong_actor', 'clarification'} else 'workflow'
        record = core.public_record(cfg, 1, 'other', 0,
                                    'blocked' if variant == 'wrong_status' else 'needs-owner',
                                    event=101, reason='approval_not_attested' if variant == 'rejection'
                                    else 'invalid_status_change' if variant == 'older_restoration'
                                    else 'owner_requested_stop',
                                    kind='clarification' if variant == 'clarification' else 'guard')
        record.update(actor=cfg['actors'][actor], **{'from': 'needs-owner'})
        record['timestamp'] = '2026-01-01T00:00:00+00:00' if variant in {'old', 'older_restoration'} else WHEN
        hub.comments_by_issue[1].append(dict(user={'login': record['actor']}, body=core.record_body(record)))
    before = len(hub.comments_by_issue[1])
    core.guard_issue(cfg, hub.client('workflow'), 1)
    new = hub.comments_by_issue[1][before:]
    assert len(new) == (0 if variant == 'matching' else 1)
    if new:
        assert core.records(cfg, new, 1)[0]['reason'] == 'owner_requested_stop'


def test_guard_rereads_product_comment_after_label(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='draft')
    hub.event(1, 'needs-owner', 'product')
    client = hub.client('workflow')
    original = client.comments
    record = core.public_record(cfg, 1, 'late-product', 0, 'needs-owner', reason='scope_change')
    record.update(actor=cfg['actors']['product'], **{'from': 'draft'})
    reads = []

    def comments(number):
        reads.append(number)
        return original(number) + [dict(user={'login': record['actor']}, body=core.record_body(record))]

    monkeypatch.setattr(client, 'comments', comments)
    core.guard_issue(cfg, client, 1)
    assert reads
    assert not hub.comments_by_issue[1]


def test_runner_login_stop_with_late_comment_does_not_add_guard_record(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    hub.event(1, 'needs-owner', 'runner')
    core.guard_issue(cfg, hub.client('workflow'), 1)
    assert hub.comments_by_issue[1] == []
    record = core.public_record(cfg, 1, 'late-runner', 1, 'needs-owner',
                                reason='owner_merge_required', event=101, pr=9)
    core.queue(cfg, record)
    core.flush(cfg, hub.client())
    core.guard_issue(cfg, hub.client('workflow'), 1)
    assert len(hub.comments_by_issue[1]) == 1
    assert core.records(cfg, hub.comments_by_issue[1], 1)[0]['reason'] == 'owner_merge_required'


def test_delivery_stop_posts_only_its_record_notice(delivery, monkeypatch):  # noqa: F811
    from test_runner_integration import advance

    cfg, hub = delivery_records(delivery, monkeypatch)
    _, _, job, client = delivery
    client.allow_auto_merge = False
    advance(delivery)
    notices = [c['body'] for c in hub.comments_by_issue[1]
               if 'Reviewed head:' in c['body'] or 'owner_merge_required' in c['body']]
    assert len(notices) == 1
    assert core.PREFIX in notices[0]
    assert 'https://github.com/example/kit/pull/9' in notices[0]
    assert job['reviewed_head'] in notices[0]
    assert core.records(cfg, hub.comments_by_issue[1], 1)[-1]['reason'] == 'owner_merge_required'
