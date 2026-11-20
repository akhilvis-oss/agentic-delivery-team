"""Public findings are sanitized at send time without changing record data."""

import json

import pytest

from lifecycle import core
from test_lifecycle import Hub, WHEN, policy


def review_job(findings, stop=False):
    return dict(id='findings-job', round=1,
                status='needs_primary_review' if stop else 'ready_for_integration',
                head_commit='a' * 40,
                assignment={'lifecycle': {'issue': 1, 'event': 101}},
                review={'status': 'needs_changes' if stop else 'ready_for_integration',
                        'findings': findings})


def findings(count):
    return [dict(severity='P2', criterion=f'AC-{i + 1}', detail=f'Finding detail {i + 1}.')
            for i in range(count)]


@pytest.mark.parametrize('count', [1, 10, 12])
@pytest.mark.parametrize('stop', [False, True])
def test_review_findings_visible_once_with_limit_and_unchanged_data(tmp_path, monkeypatch, count, stop):
    monkeypatch.setattr(core, "now", lambda: WHEN)
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    job = review_job(findings(count), stop)
    expected, = core.job_records(cfg, job)
    core.sync_job(cfg['config_path'], job, hub.client())
    comment, = hub.comments_by_issue[1]
    prose, data = comment['body'].split(core.PREFIX)
    for i in range(min(count, 10)):
        assert prose.count(f'> Finding detail {i + 1}.') == 1
        assert f'P2 — AC-{i + 1}' in prose
    assert ('and 2 more' in prose) == (count == 12)
    assert 'Finding detail 11.' not in prose
    actual = json.loads(data.split(core.SUFFIX)[0])
    expected.update(actor=cfg['actors']['runner'], **{'from': 'in-review'})
    assert actual == expected
    assert core.records(cfg, [comment], 1) == [expected]
    assert 'notice_context' not in actual


def test_non_stop_review_maximum_findings_fits_github_limit_and_removes_private_content(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    private = '/private/synthetic-fixture/secret.txt'
    job = review_job([dict(severity='P2', criterion='c' * 100,
                           detail=private + '\n' + '"\\😀\n' * 10000) for _ in range(10)])
    core.sync_job(cfg['config_path'], job, hub.client())
    body = hub.comments_by_issue[1][0]['body']
    assert len(body) < 65536
    assert private not in body
    assert body.count('Open finding: P2') == 10
    assert body.count('[truncated]') == 10
    assert '[removed: local path]' in body


def test_minor_followups_show_each_own_sanitized_finding_before_marker(tmp_path):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='in-review')
    items = findings(2)
    items[0]['detail'] += ' /private/synthetic-fixture/secret.txt'
    items[1]['criterion'] = 'a' * 120
    items[1]['detail'] = 'b' * 800
    job = review_job(items)
    job['review_history'] = [dict(round=1, head='a' * 40, result={'findings': items})]
    core.followups(cfg, job, hub.client('product'))
    for index, issue in enumerate([hub.issues[2], hub.issues[3]]):
        prose, _ = issue['body'].split('<!-- agentic-followup-v1:')
        finding = items[index]
        assert 'P2 — ' + core.public_quote(cfg, finding['criterion'], 100) in prose
        assert core.public_quote(cfg, finding['detail'], 600) in prose
        assert '/private/synthetic-fixture' not in prose
    assert 'Finding detail 2.' not in hub.issues[2]['body']
    assert 'Finding detail 1.' not in hub.issues[3]['body']
    core.followups(cfg, job, hub.client('product'))
    assert len(hub.issues) == 3


def test_review_context_is_rendered_with_current_public_filter_at_send(tmp_path, monkeypatch):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    hub.issue(state='committed')
    record = core.public_record(cfg, 1, 'render-later', 1, 'in-review', event=101,
                                kind='review', outcome='ready_for_integration')
    path = core.queue(cfg, record, notice_context={'findings': [dict(
        severity='P2', criterion='AC-1', detail='Ask newly-private-fixture for the fix.')]})
    quote = core.public_quote
    calls = []

    def current_filter(policy, text, limit):
        calls.append(text)
        return quote(policy, text, limit).replace('newly-private-fixture', '[removed: account name]')

    monkeypatch.setattr(core, 'public_quote', current_filter)
    assert calls == []
    core.flush(cfg, hub.client())
    body = hub.comments_by_issue[1][0]['body']
    assert 'newly-private-fixture' not in body
    assert 'Ask [removed: account name] for the fix.' in body
    assert core.read(path)['record'].keys() == record.keys() | {'from', 'actor'}
