"""Exact approval instants and one bounded scratch-story recovery."""

import copy
import io
from types import SimpleNamespace

import pytest

from lifecycle import core, smoke
from test_lifecycle import Hub, WHEN, policy
from test_smoke import run_setup, smoke_evidence, smoke_guard_idle, smoke_guard_snapshot


def approval(tmp_path, snapshot=WHEN, latest=WHEN):
    cfg = policy(tmp_path)
    hub = Hub(cfg)
    issue = hub.issue()
    hub.events_by_issue[1][-1]['created_at'] = latest
    issue['updated_at'] = snapshot
    event = dict(action='labeled', label={'name': 'status:approved'},
                 sender={'login': cfg['actors']['owner']}, issue=copy.deepcopy(issue))
    return cfg, hub, event


@pytest.mark.parametrize('snapshot,latest,seconds', [
    ('2026-01-02T10:00:00.001Z', WHEN, 1),
    ('2026-01-02T09:59:47.9Z', WHEN, 13),
    ('2026-01-02T10:00:12+00:00', WHEN, 12),
])
def test_ignored_snapshot_reports_only_rounded_absolute_gap(tmp_path, snapshot, latest, seconds):
    cfg, hub, event = approval(tmp_path, snapshot, latest)
    client = hub.client('workflow')
    with pytest.raises(core.Unavailable):
        core.attest_approval(cfg, client, event)
    body = client.comments(1)[-1]['body']
    assert f'Snapshot time differs from the latest approval event by {seconds} seconds.\n' in body
    assert not core.records(cfg, client.comments(1), 1)
    assert not any(method == 'DELETE' for method, *_ in hub.calls)


@pytest.mark.parametrize('bad', [None, 7, {}, [], '', 'bad', '2026-01-02T10:00:00'])
@pytest.mark.parametrize('side', ['both', 'snapshot', 'latest'])
def test_invalid_approval_times_never_attest_or_diagnose(tmp_path, bad, side):
    cfg, hub, event = approval(tmp_path, bad if side != 'latest' else WHEN,
                               bad if side != 'snapshot' else WHEN)
    client = hub.client('workflow')
    with pytest.raises(core.Unavailable):
        core.attest_approval(cfg, client, event)
    assert not core.records(cfg, client.comments(1), 1)
    assert all('Snapshot time differs' not in row['body'] for row in client.comments(1))


def test_equivalent_aware_instants_attest_without_gap(tmp_path):
    cfg, hub, event = approval(tmp_path, '2026-01-02T15:30:00+05:30', WHEN)
    client = hub.client('workflow')
    core.attest_approval(cfg, client, event)
    assert len(core.records(cfg, client.comments(1), 1)) == 1
    assert all('Snapshot time differs' not in row['body'] for row in client.comments(1))


def test_smoke_reapproves_once_in_order_with_fresh_grace(tmp_path, monkeypatch):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (0, False, 'pending'))
    monkeypatch.setattr(smoke, 'evidence', smoke_evidence)
    client.comments = lambda _: []
    client.issue = lambda _: {'labels': ['status:approved']}
    elapsed, chain = [0], []
    monkeypatch.setattr(smoke, 'guard_snapshot', lambda *a, **kw: chain.append(('snapshot', elapsed[0])) or 9)
    monkeypatch.setattr(smoke, 'guard_idle', lambda c, previous, **kw: chain.append(('idle', elapsed[0], previous)) or True)
    request = client.request

    def send(method, path, data=None):
        if path == '/issues/1/labels/status%3Aapproved':
            chain.append(('delete', elapsed[0]))
        if path == '/issues/1/labels':
            chain.append(('approve', elapsed[0]))
        return request(method, path, data)

    client.request = send
    assert smoke.run(cfg, 'main', clock=lambda: elapsed[0],
                     sleep=lambda s: elapsed.__setitem__(0, elapsed[0] + s)) == 1
    assert chain == [('snapshot', 0), ('idle', 0, 9), ('approve', 0),
                     ('snapshot', 180), ('delete', 180), ('idle', 180, 9), ('approve', 180)]
    assert elapsed[0] == 360


@pytest.mark.parametrize('phase', ['expired', 'snapshot', 'idle'])
def test_smoke_recovery_never_approves_after_deadline(tmp_path, monkeypatch, phase):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (0, False, 'approval_unrecorded'))
    cfg['timeout_seconds'] = 180 if phase == 'expired' else 185
    elapsed, snapshots, idle = [0], [], []

    def snapshot(*args, **kwargs):
        snapshots.append(kwargs)
        if len(snapshots) > 1:
            elapsed[0] = 185 if phase == 'snapshot' else elapsed[0]
        return 9

    def drained(*args, **kwargs):
        idle.append(kwargs)
        if len(idle) > 1 and phase == 'idle':
            elapsed[0] = 185
        return True

    monkeypatch.setattr(smoke, 'guard_snapshot', snapshot)
    monkeypatch.setattr(smoke, 'guard_idle', drained)
    assert smoke.run(cfg, 'main', clock=lambda: elapsed[0],
                     sleep=lambda s: elapsed.__setitem__(0, elapsed[0] + s)) == 1
    assert sum(path == '/issues/1/labels' for _, path, _ in client.calls) == 1
    assert 'stop=timeout' in client.calls[-1][2]['description']
    if phase != 'expired':
        assert snapshots[-1]['deadline'] == 185
    if phase == 'idle':
        assert idle[-1]['deadline'] == 185


@pytest.mark.parametrize('wait', ['snapshot', 'idle'])
def test_recovery_guard_waits_respect_overall_deadline(wait):
    elapsed, timeouts = [180], []

    def runs(*args, timeout):
        timeouts.append(timeout)
        elapsed[0] += timeout
        raise core.Unavailable('synthetic')

    client = SimpleNamespace(guard_runs=runs)
    kwargs = dict(clock=lambda: elapsed[0], sleep=lambda s: elapsed.__setitem__(0, elapsed[0] + s), deadline=183)
    if wait == 'snapshot':
        assert smoke_guard_snapshot(client, **kwargs) is None
    else:
        assert smoke_guard_idle(client, 9, **kwargs) is False
    assert elapsed[0] == 183
    assert timeouts == [3]


def test_smoke_label_delete_is_bound_to_current_issue():
    cfg = {'repository': 'example/scratch', 'actors': {'runner': 'runner', 'owner': 'owner'}}
    client = smoke.SmokeGitHub(cfg, token='synthetic')
    client.identity_verified = True
    client.smoke_issue_number = 7
    calls = []
    client.opener = SimpleNamespace(open=lambda req, **kw: calls.append(req) or io.BytesIO(b''))
    assert client.request('DELETE', '/issues/7/labels/status%3Aapproved') is None
    for path in ['/issues/8/labels/status%3Aapproved', '/issues/7/labels/status%3Adraft',
                 '/issues/7/labels/status:approved', '/issues/7', '/git/refs/heads/main']:
        with pytest.raises(ValueError):
            client.request('DELETE', path)
    assert len(calls) == 1


@pytest.mark.parametrize('confirmed', [True, False])
def test_reapproval_uncertain_post_reads_back_without_repeating(tmp_path, monkeypatch, confirmed):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (0, False, 'approval_unrecorded'))
    elapsed, attempts, reads = [0], [], []
    request = client.request

    def send(method, path, data=None):
        if method == 'POST' and path == '/issues/1/labels':
            attempts.append(elapsed[0])
            if len(attempts) == 2:
                raise core.Uncertain('synthetic lost response')
        return request(method, path, data)

    def issue(number):
        reads.append(number)
        return {'labels': ['status:approved'] if confirmed else []}

    client.request, client.issue = send, issue
    assert smoke.run(cfg, 'main', clock=lambda: elapsed[0],
                     sleep=lambda s: elapsed.__setitem__(0, elapsed[0] + s)) == 1
    assert attempts == [0, 180]
    assert reads == [1]
    assert elapsed[0] == (360 if confirmed else 180)
    assert ('stop=approval_unrecorded' if confirmed else 'stop=unavailable') in client.calls[-1][2]['description']


@pytest.mark.parametrize('failed_step', ['snapshot', 'idle', 'delete'])
def test_unproven_recovery_does_not_reapprove(tmp_path, monkeypatch, failed_step):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (0, False, 'approval_unrecorded'))
    elapsed, snapshots, idles = [0], [], []

    def snapshot(*args, **kwargs):
        snapshots.append(1)
        return None if failed_step == 'snapshot' and len(snapshots) > 1 else 9

    def idle(*args, **kwargs):
        idles.append(1)
        return not (failed_step == 'idle' and len(idles) > 1)

    monkeypatch.setattr(smoke, 'guard_snapshot', snapshot)
    monkeypatch.setattr(smoke, 'guard_idle', idle)
    request = client.request

    def send(method, path, data=None):
        if path == '/issues/1/labels/status%3Aapproved' and failed_step == 'delete':
            raise core.Unavailable('synthetic delete outage')
        return request(method, path, data)

    client.request = send
    assert smoke.run(cfg, 'main', clock=lambda: elapsed[0],
                     sleep=lambda s: elapsed.__setitem__(0, elapsed[0] + s)) == 1
    assert sum(path == '/issues/1/labels' for _, path, _ in client.calls) == 1
    assert elapsed[0] == 180


@pytest.mark.parametrize('side', ['both', 'snapshot', 'latest'])
def test_missing_timestamp_key_is_ignored(tmp_path, side):
    cfg, hub, event = approval(tmp_path)
    if side != 'latest':
        event['issue'].pop('updated_at')
    if side != 'snapshot':
        hub.events_by_issue[1][-1].pop('created_at')
    client = hub.client('workflow')
    with pytest.raises(core.Unavailable):
        core.attest_approval(cfg, client, event)
    assert not core.records(cfg, client.comments(1), 1)
    assert all('Snapshot time differs' not in row['body'] for row in client.comments(1))


def test_reapproval_real_guard_waits_observe_removal_run(tmp_path, monkeypatch):
    cfg, client, _, _ = run_setup(tmp_path, monkeypatch, (0, False, 'pending'))
    monkeypatch.setattr(smoke, 'evidence', smoke_evidence)
    monkeypatch.setattr(smoke, 'guard_snapshot', smoke_guard_snapshot)
    monkeypatch.setattr(smoke, 'guard_idle', smoke_guard_idle)
    client.comments = lambda _: []
    client.issue = lambda _: {'labels': ['status:approved']}
    elapsed, guard_id, reads, approvals = [0], [0], [], []
    request = client.request

    def send(method, path, data=None):
        if path == '/issues':
            guard_id[0] = 1
        elif path == '/issues/1/labels/status%3Aapproved':
            guard_id[0] = 2
        elif path == '/issues/1/labels':
            approvals.append((elapsed[0], guard_id[0]))
        return request(method, path, data)

    def runs(query=None, *, timeout):
        reads.append((elapsed[0], query, timeout))
        count = int(query in (None, 'success') and guard_id[0] > 0)
        return {'total_count': count, 'workflow_runs': [{'id': guard_id[0]}] if count else []}

    client.request, client.guard_runs = send, runs
    assert smoke.run(cfg, 'main', clock=lambda: elapsed[0],
                     sleep=lambda s: elapsed.__setitem__(0, elapsed[0] + s)) == 1
    assert approvals == [(0, 1), (180, 2)]
    assert [query for when, query, _ in reads if when == 180] == [
        None, 'success', 'queued', 'in_progress', 'waiting', 'pending', 'requested']
    assert elapsed[0] == 360
