"""Synthetic local repositories and agents; no credentials or model calls."""
import copy
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from runner import pool
from runner import task_runner as runner
import test_runner as fixtures


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    repo, worktrees = tmp_path / 'repo', tmp_path / 'worktrees'
    repo.mkdir()
    worktrees.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-qm', 'base')
    profile = tmp_path / 'profile.json'
    runner.save(profile, dict(worker_prompt='Implement the task.', reviewer_prompt='Review the task.',
                              scope='Synthetic work only.', extra_scopes={}, coordination_files=[]))
    slots = [dict(id=f'slot-{i}', agent_home=str(tmp_path / f'home-{i}'),
                  agent=fixtures.agent_config('/must-not-launch'), worker_model='fixture', reviewer_model='fixture',
                  budget=100, phase_reserve=5, small_threshold=20, small_max_ac=2, cooldown_seconds=60)
             for i in range(2)]
    stories = []
    for name in ('alpha', 'beta'):
        worktree = worktrees / name
        git(repo, 'worktree', 'add', '-qb', name, str(worktree), 'main')
        config = dict(worktree=str(worktree), branch=name, state_dir=str(tmp_path / (name + '-state')),
                      agent_home=slots[0]['agent_home'], agent=slots[0]['agent'], worker_model='fixture', reviewer_model='fixture',
                      profile=str(profile), phase_timeout_seconds=60, max_rounds=2,
                      commit_identity={'name': 'Fixture Runner', 'email': 'runner@example.invalid'})
        config_path = tmp_path / (name + '.json')
        runner.save(config_path, config)
        runner.enqueue(runner.load_config(config_path), name, f'Add {name}.txt with one focused test.')
        stories.append(dict(id=name, approved=True, priority=1, ac_count=2, roadmap_section=name,
                            expected_files=[name + '.txt'], depends_on=[], runner_config=str(config_path)))
    cfg = dict(repository_root=str(repo), worktree_root=str(worktrees), state_dir=str(tmp_path / 'pool-state'),
               mode='pool', lead='slot-0', max_concurrency=2, paused=False, slots=slots, stories=stories,
               test_commands=[[sys.executable, '-c', 'pass']])
    path = tmp_path / 'pool.json'
    runner.save(path, cfg)
    monkeypatch.setattr(runner, 'check_cli', lambda _: 'fixture-cli')
    return cfg, path


def delivery(config, state, path, job, role):
    if role == 'worker':
        (Path(config['worktree']) / (job['id'] + '.txt')).write_text('hello\n')
    return fixtures.result('ready_for_review' if role == 'worker' else 'ready_for_integration', role)


def test_concurrent_assignment_and_cross_review_without_deadlock(setup, monkeypatch):
    cfg, path = setup
    barrier = threading.Barrier(2)
    calls = []
    guard = threading.Lock()
    active = set()

    def execute(config, state, job_path, job, role):
        slot = config['pool_slot']
        if role == 'reviewer':
            assert job['verification_runs'][-1]['status'] == 'passed'
            assert job['verification_runs'][-1]['head'] == job['head_commit']
        with guard:
            assert slot not in active
            active.add(slot)
            calls.append((job['id'], role, slot))
        try:
            if role == 'worker':
                barrier.wait(timeout=10)
            return delivery(config, state, job_path, job, role)
        finally:
            with guard:
                active.remove(slot)

    monkeypatch.setattr(runner, 'execute_phase', execute)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert len(calls) == 4
    for name in ('alpha', 'beta'):
        worker = next(s for n, r, s in calls if n == name and r == 'worker')
        reviewer = next(s for n, r, s in calls if n == name and r == 'reviewer')
        assert worker != reviewer
        assert engine.data['stories'][name]['status'] == 'ready_for_integration'
    assert all(s['active'] is None and s['unknown_phases'] == 2 and s['used'] == 10 for s in engine.data['slots'].values())
    engine.run_once()
    assert len(calls) == 4  # no replay of a completed job


def test_lead_reviews_delegated_work_and_never_own(setup, monkeypatch):
    cfg, path = setup
    cfg['mode'] = 'lead-and-team'
    cfg['stories'][0]['force_slot'] = 'slot-0'
    cfg['stories'][1]['force_slot'] = 'slot-1'
    runner.save(path, cfg)
    calls = []

    def execute(config, state, job_path, job, role):
        calls.append((job['id'], role, config['pool_slot']))
        return delivery(config, state, job_path, job, role)

    monkeypatch.setattr(runner, 'execute_phase', execute)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert ('alpha', 'reviewer', 'slot-1') in calls
    assert ('beta', 'reviewer', 'slot-0') in calls


@pytest.mark.parametrize('case,reason', [('files', 'roadmap_or_files_overlap'), ('roadmap', 'roadmap_or_files_overlap'),
                                      ('dependency', 'dependency_not_merged'), ('cap', 'concurrency_cap'),
                                      ('approval', 'owner_approval_required')])
def test_waiting_reasons(setup, monkeypatch, case, reason):
    cfg, path = setup
    if case == 'files':
        cfg['stories'][1]['expected_files'] = ['alpha.txt']
    elif case == 'roadmap':
        cfg['stories'][1]['roadmap_section'] = 'alpha'
    elif case == 'dependency':
        cfg['stories'][1]['depends_on'] = ['alpha']
    elif case == 'cap':
        cfg['max_concurrency'] = 1
    else:
        cfg['stories'][1]['approved'] = False
    runner.save(path, cfg)
    monkeypatch.setattr(runner, 'execute_phase', delivery)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert engine.data['stories']['beta']['waiting'] == reason
    assert engine.data['stories']['alpha']['status'] == 'ready_for_integration'


def test_budget_size_and_cooldown_routing(setup):
    cfg, path = setup
    engine = pool.Pool(pool.load_config(path), clock=lambda: 100)
    engine.data['slots']['slot-0']['used'] = 85
    large = dict(cfg['stories'][0], ac_count=5)
    assert engine.select(large) == 'slot-1'
    assert engine.select(large, 'worker') == 'slot-1'
    engine.data['slots']['slot-1']['cooldown_until'] = 120
    assert engine.select(large) is None
    assert engine.select(dict(large, ac_count=1)) == 'slot-0'
    engine.data['slots']['slot-0']['used'] = 100
    assert engine.select(dict(large, ac_count=1)) is None


def test_reported_usage_and_quota_mark_slot_cooling_down(setup, monkeypatch):
    cfg, path = setup
    cfg['stories'] = [cfg['stories'][0]]
    runner.save(path, cfg)

    def quota(config, state, job_path, job, role):
        answer = fixtures.result('blocked')
        answer.update(rate_limited=True, usage_units=7)
        return answer

    monkeypatch.setattr(runner, 'execute_phase', quota)
    engine = pool.Pool(pool.load_config(path), clock=lambda: 100)
    engine.run_once()
    assert engine.data['slots']['slot-0']['cooldown_until'] == 160
    assert engine.data['slots']['slot-0']['used'] == 7
    assert engine.data['stories']['alpha']['status'] == 'blocked'


def test_single_slot_fresh_review_fallback(setup, monkeypatch):
    cfg, path = setup
    cfg.update(slots=cfg['slots'][:1], max_concurrency=1, stories=cfg['stories'][:1])
    runner.save(path, cfg)
    calls = []

    def execute(config, state, job_path, job, role):
        calls.append((role, config['pool_slot']))
        return delivery(config, state, job_path, job, role)

    monkeypatch.setattr(runner, 'execute_phase', execute)
    pool.Pool(pool.load_config(path)).run_once()
    assert calls == [('worker', 'slot-0'), ('reviewer', 'slot-0')]


def test_pause_and_crash_recovery_never_replay_active_slot(setup, monkeypatch):
    cfg, path = setup
    cfg['paused'] = True
    runner.save(path, cfg)
    monkeypatch.setattr(runner, 'execute_phase', lambda *a: pytest.fail('must not launch'))
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert not engine.data['stories']
    cfg['paused'] = False
    runner.save(path, cfg)
    engine = pool.Pool(pool.load_config(path))
    engine.data['slots']['slot-0']['active'] = {'story': 'alpha', 'role': 'worker'}
    engine.save()
    with pytest.raises(runner.Blocked, match='reconcile'):
        pool.Pool(pool.load_config(path)).run_once()


@pytest.mark.parametrize('case', ['same_home', 'home_in_worktree', 'zero_budget', 'unknown_force', 'missing_files', 'duplicate_id'])
def test_reject_unsafe_configuration(setup, case):
    cfg, path = setup
    if case == 'same_home':
        cfg['slots'][1]['agent_home'] = cfg['slots'][0]['agent_home']
    elif case == 'home_in_worktree':
        cfg['slots'][0]['agent_home'] = cfg['worktree_root'] + '/alpha/home'
    elif case == 'zero_budget':
        cfg['slots'][0]['budget'] = 0
    elif case == 'unknown_force':
        cfg['stories'][0]['force_slot'] = 'absent'
    elif case == 'missing_files':
        cfg['stories'][0]['expected_files'] = []
    else:
        cfg['stories'][1]['id'] = 'alpha'
    runner.save(path, cfg)
    with pytest.raises(ValueError):
        pool.load_config(path)


def test_new_main_invalidates_tests_and_review_then_refreshes(setup, monkeypatch):
    cfg, path = setup
    cfg['stories'] = cfg['stories'][:1]
    runner.save(path, cfg)
    calls = []

    def execute(config, state, job_path, job, role):
        calls.append(role)
        return delivery(config, state, job_path, job, role)

    monkeypatch.setattr(runner, 'execute_phase', execute)
    pool.Pool(pool.load_config(path)).run_once()
    repo = Path(cfg['repository_root'])
    (repo / 'main.txt').write_text('new main\n')
    git(repo, 'add', 'main.txt')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'main update')
    engine = pool.Pool(pool.load_config(path))
    assert engine.snapshot()['stories']['alpha']['status'] == 'refresh_required'
    engine.run_once()
    record = engine.data['stories']['alpha']
    assert record['status'] == 'ready_for_integration'
    assert record['validated_main'] == git(repo, 'rev-parse', 'main')
    assert calls == ['worker', 'reviewer', 'reviewer']
    refreshed = runner.read(record['refreshed_review'])
    assert refreshed['reviewed_head'] == record['head']


def test_post_merge_conflict_blocks_without_resolution(setup, monkeypatch):
    cfg, path = setup
    cfg['stories'] = cfg['stories'][:1]
    runner.save(path, cfg)
    monkeypatch.setattr(runner, 'execute_phase', delivery)
    pool.Pool(pool.load_config(path)).run_once()
    repo = Path(cfg['repository_root'])
    (repo / 'alpha.txt').write_text('conflicting main\n')
    git(repo, 'add', 'alpha.txt')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'main conflicting update')
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert engine.data['stories']['alpha']['waiting'] == 'rebase_conflict_requires_owner'
    worktree = Path(cfg['worktree_root']) / 'alpha'
    assert git(worktree, 'diff', '--name-only', '--diff-filter=U') == 'alpha.txt'


def test_overlap_is_conservative_and_scope_change_rejected(setup, monkeypatch):
    cfg, path = setup
    a, b = copy.deepcopy(cfg['stories'])
    a['expected_files'] = ['src/*']
    assert pool.overlap(a, b)
    monkeypatch.setattr(runner, 'execute_phase', delivery)
    pool.Pool(pool.load_config(path)).run_once()
    cfg['stories'][0]['expected_files'] = ['unrelated.txt']
    runner.save(path, cfg)
    with pytest.raises(ValueError, match='binding changed'):
        pool.Pool(pool.load_config(path))


def test_owner_approval_priority_and_concurrency_drain(setup, monkeypatch):
    cfg, path = setup
    cfg['max_concurrency'] = 1
    cfg['stories'][1]['approved'] = False
    runner.save(path, cfg)
    monkeypatch.setattr(runner, 'execute_phase', delivery)
    pool.Pool(pool.load_config(path)).run_once()
    cfg['stories'][1].update(approved=True, priority=0, force_slot='slot-1')
    runner.save(path, cfg)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert engine.data['stories']['beta']['status'] == 'ready_for_integration'
    assert engine.data['stories']['beta']['worker_slot'] == 'slot-1'


def test_refresh_test_failure_keeps_original_approval_invalid(setup, monkeypatch):
    cfg, path = setup
    cfg['stories'] = cfg['stories'][:1]
    runner.save(path, cfg)
    monkeypatch.setattr(runner, 'execute_phase', delivery)
    pool.Pool(pool.load_config(path)).run_once()
    cfg['test_commands'] = [[sys.executable, '-c', 'raise SystemExit(1)']]
    runner.save(path, cfg)
    repo = Path(cfg['repository_root'])
    (repo / 'main.txt').write_text('new main\n')
    git(repo, 'add', 'main.txt')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'main update')
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert engine.data['stories']['alpha']['status'] == 'blocked'
    original = runner.read(Path(runner.load_config(cfg['stories'][0]['runner_config'])['state_dir']) / 'tasks/alpha.json')
    assert original['reviewed_head'] is None
    assert original['status'] == 'blocked'


def test_declared_scope_is_enforced_before_commit(setup, monkeypatch):
    cfg, path = setup
    cfg['stories'] = cfg['stories'][:1]
    runner.save(path, cfg)

    def outside_scope(config, state, job_path, job, role):
        assert role == 'worker'
        (Path(config['worktree']) / 'unexpected.txt').write_text('no\n')
        return fixtures.result('ready_for_review')

    monkeypatch.setattr(runner, 'execute_phase', outside_scope)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert engine.data['stories']['alpha']['status'] == 'blocked'


def test_global_slot_lease_prevents_independent_pool_collision(setup, monkeypatch):
    cfg, path = setup
    cfg['stories'] = cfg['stories'][:1]
    runner.save(path, cfg)
    monkeypatch.setattr(runner, 'execute_phase', lambda *a: pytest.fail('leased slot'))
    with runner.lock(Path(cfg['slots'][0]['agent_home']) / '.agentic-pool-slot.lock') as acquired:
        assert acquired
        engine = pool.Pool(pool.load_config(path))
        engine.run_once()
    assert engine.data['stories']['alpha']['status'] == 'blocked'


def test_stale_coordinator_object_cannot_overwrite_budget_ledger(setup, monkeypatch):
    cfg, path = setup
    cfg['stories'] = cfg['stories'][:1]
    runner.save(path, cfg)
    stale = pool.Pool(pool.load_config(path))
    monkeypatch.setattr(runner, 'execute_phase', delivery)
    pool.Pool(pool.load_config(path)).run_once()
    stale.run_once()
    assert sum(s['used'] for s in stale.data['slots'].values()) == 10


def test_digest_exposes_only_fixed_pool_status_and_budget(setup, tmp_path, monkeypatch):
    import test_digest as digest_fixtures
    from digest import standup

    cfg, path = setup
    cfg['stories'][1]['depends_on'] = ['alpha']
    runner.save(path, cfg)
    monkeypatch.setattr(runner, 'execute_phase', delivery)
    pool.Pool(pool.load_config(path)).run_once()
    digest_cfg = digest_fixtures.configuration(tmp_path / 'digest-fixture')
    digest_cfg['pool_configs'] = [str(path)]
    hub = digest_fixtures.Hub(digest_cfg['lifecycle'])
    sections = standup.collect(digest_cfg, hub.client('product'), digest_fixtures.AT, digest_fixtures.AT)
    text = '\n'.join(item for section in sections for item in section)
    assert 'Pool slot slot-0: idle' in text
    assert 'budget used 5/100 owner units' in text
    assert 'Pool story beta: dependency_not_merged' in text
    assert str(tmp_path) not in text and '/must-not-launch' not in text


@pytest.mark.parametrize('value', [-1, True, 1.5])
def test_invalid_usage_never_counts_as_success(value):
    result = fixtures.result('ready_for_review')
    result['usage_units'] = value
    with pytest.raises(ValueError):
        runner.validate_result(result, 'worker')


def test_pool_schema_does_not_change_standalone_agent_contract():
    assert 'usage_units' not in runner.schema('worker')['properties']
    schema = runner.schema('worker', pool=True)
    assert set(schema['properties']) == set(schema['required'])
    result = fixtures.result('ready_for_review')
    result.update(usage_units=None, rate_limited=False)
    runner.validate_result(result, 'worker')


def test_waiting_queued_branch_refreshes_after_dependency_merge(setup, monkeypatch):
    cfg, path = setup
    cfg['stories'][1]['depends_on'] = ['alpha']
    runner.save(path, cfg)
    monkeypatch.setattr(runner, 'execute_phase', delivery)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    repo = Path(cfg['repository_root'])
    git(repo, 'merge', '--ff-only', 'alpha')
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    record = engine.data['stories']['beta']
    assert engine.data['stories']['alpha']['status'] == 'merged'
    assert record['status'] == 'ready_for_integration'
    assert record['queued_refresh'] is True
    assert record['validated_main'] == git(repo, 'rev-parse', 'main')
    cfg = runner.load_config(record['active_config'])
    job = runner.read(Path(cfg['state_dir']) / 'tasks/beta.json')
    assert job['base_commit'] == record['validated_main']
    assert job['reviewed_head'] == record['head']


def advance_main(cfg, name):
    repo = Path(cfg['repository_root'])
    (repo / name).write_text(name + '\n')
    git(repo, 'add', name)
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', name)


def test_builder_identity_survives_serial_builds_then_concurrent_refresh(setup, monkeypatch):
    cfg, path = setup
    cfg['max_concurrency'] = 1
    runner.save(path, cfg)
    calls = []

    def execute(config, state, job_path, job, role):
        calls.append((job['id'], role, config['pool_slot']))
        result = delivery(config, state, job_path, job, role)
        result['usage_units'] = 0  # both initial builders choose slot-0
        return result

    monkeypatch.setattr(runner, 'execute_phase', execute)
    pool.Pool(pool.load_config(path)).run_once()
    pool.Pool(pool.load_config(path)).run_once()
    builders = {n: s for n, role, s in calls if role == 'worker'}
    assert builders == {'alpha': 'slot-0', 'beta': 'slot-0'}
    advance_main(cfg, 'main-one.txt')
    cfg['max_concurrency'] = 2
    runner.save(path, cfg)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert all(r['status'] == 'ready_for_integration' for r in engine.data['stories'].values())
    assert all(s != builders[n] for n, role, s in calls if role == 'reviewer')
    assert {n: r['worker_slot'] for n, r in engine.data['stories'].items()} == builders


def test_multiple_main_updates_refresh_same_object_and_receipts(setup, monkeypatch):
    cfg, path = setup
    cfg['stories'] = cfg['stories'][:1]
    runner.save(path, cfg)
    monkeypatch.setattr(runner, 'execute_phase', delivery)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    for name in ('main-one.txt', 'main-two.txt'):
        advance_main(cfg, name)
        assert engine.snapshot()['stories']['alpha']['status'] == 'refresh_required'
        engine.run_once()
        record = engine.data['stories']['alpha']
        assert record['status'] == 'ready_for_integration'
        assert record['validated_main'] == git(Path(cfg['repository_root']), 'rev-parse', 'main')
        assert runner.read(record['refreshed_review'])['reviewed_head'] == record['head']


def test_normalized_quota_exit_code_cools_down_without_log_parsing(setup):
    cfg, path = setup
    cfg['stories'] = cfg['stories'][:1]
    root = Path(cfg['state_dir']).parent
    executable = fixtures.fake_agent(root, 'raise SystemExit(75)')
    for slot in cfg['slots']:
        slot['agent'] = fixtures.agent_config(executable)
        slot['quota_exit_codes'] = [75]
    runner.save(path, cfg)
    engine = pool.Pool(pool.load_config(path), clock=lambda: 100)
    engine.run_once()
    assert engine.data['slots']['slot-0']['cooldown_until'] == 160
    assert engine.data['stories']['alpha']['status'] == 'blocked'
    assert all(s['active'] is None for s in engine.data['slots'].values())


def test_unlisted_queued_job_is_never_dispatched_by_pool(setup, monkeypatch):
    cfg, path = setup
    cfg['stories'] = cfg['stories'][:1]
    runner.save(path, cfg)
    config = runner.load_config(cfg['stories'][0]['runner_config'])
    other = runner.enqueue(config, '000-unlisted', 'Not authorized by the pool')
    seen = []

    def execute(config, state, job_path, job, role):
        seen.append(job['id'])
        assert job['id'] == 'alpha'
        return delivery(config, state, job_path, job, role)

    monkeypatch.setattr(runner, 'execute_phase', execute)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert seen == ['alpha', 'alpha']
    assert runner.read(other)['status'] == 'queued'
    assert engine.data['stories']['alpha']['status'] == 'ready_for_integration'


def test_enqueue_only_never_runs_optional_digest_model(tmp_path, monkeypatch):
    from lifecycle import service
    from digest import standup

    calls = []
    monkeypatch.setattr(service.core, 'load_config', lambda _: {'digest_config': 'must-not-load'})
    monkeypatch.setattr(service.core, 'GitHub', lambda _: object())
    monkeypatch.setattr(service, 'poll', lambda cfg, client, *, run_jobs: calls.append(run_jobs))
    monkeypatch.setattr(standup, 'load_config', lambda _: pytest.fail('queue-only must not call digest/model'))
    assert service.main(['--config', str(tmp_path / 'unused.json'), 'enqueue-only']) == 0
    assert calls == [False]


def lifecycle_story(setup, tmp_path, monkeypatch):
    """Real admission/outbox/record policy with an in-memory GitHub transport."""
    from lifecycle import core
    from test_lifecycle import Hub, policy

    cfg, path = setup
    cfg['stories'] = cfg['stories'][:1]
    runner.save(path, cfg)
    story = cfg['stories'][0]
    config_path = Path(story['runner_config'])
    config = runner.read(config_path)
    task_path = Path(config['state_dir']) / 'tasks/alpha.json'
    task_text = runner.read(task_path)['assignment']['task_text']
    policy_cfg = policy(tmp_path / 'lifecycle-fixture')
    hub = Hub(policy_cfg)
    hub.issue(body=task_text)
    approval = hub.approve()
    monkeypatch.setattr(runner, '_LIFECYCLE', core)
    monkeypatch.setattr(core, 'GitHub', lambda cfg, role='runner': hub.client(role))
    dispatch = core.public_record(policy_cfg, 1, 'alpha', 0, 'dispatched', event=approval['id'])
    core.queue(policy_cfg, dispatch)
    core.flush(policy_cfg, hub.client())
    assert core.state(hub.issues[1]) == 'dispatched'
    config['lifecycle_config'] = policy_cfg['config_path']
    runner.save(config_path, config)
    task_path.unlink()
    runner.enqueue(runner.load_config(config_path), 'alpha', task_text, github_number=1,
                   lifecycle={'issue': 1, 'event': approval['id'], 'primary': False})
    phases = []

    def execute(config, state, job_path, job, role):
        phases.append((role, config['pool_slot']))
        # Match the production phase boundary so the actual policy/outbox sees
        # worker -> committed -> reviewer instead of bypassing lifecycle writes.
        job.update(status='running_' + role, updated_at=runner.now())
        runner.save_job(config, job_path, job)
        return delivery(config, state, job_path, job, role)

    monkeypatch.setattr(runner, 'execute_phase', execute)
    return cfg, path, policy_cfg, hub, phases


def test_lifecycle_queued_refresh_preserves_admission_and_publishes_review(setup, tmp_path, monkeypatch):
    from lifecycle import core

    cfg, path, policy_cfg, hub, phases = lifecycle_story(setup, tmp_path, monkeypatch)
    advance_main(cfg, 'main-update.txt')
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    record = engine.data['stories']['alpha']
    assert record['status'] == 'ready_for_integration'
    assert [r for r, _ in phases] == ['worker', 'reviewer']
    assert phases[0][1] != phases[1][1]
    assert core.state(hub.issues[1]) == 'in-review'
    history = core.records(policy_cfg, hub.client().comments(1), 1)
    assert any(r['kind'] == 'review' and r['outcome'] == 'ready_for_integration'
               and r['commit'] == record['head'] for r in history)
    assert not any(call == ('label', 1, 'blocked') for call in hub.calls)
    original = runner.read(Path(runner.read(cfg['stories'][0]['runner_config'])['state_dir']) / 'tasks/alpha.json')
    assert original['status'] == 'blocked'  # private superseded assignment only


@pytest.mark.parametrize('revocation', ['blocked', 'closed', 'scope', 'dispatch'])
def test_lifecycle_queued_refresh_cannot_override_revocation(setup, tmp_path, monkeypatch, revocation):
    from lifecycle import core

    cfg, path, policy_cfg, hub, phases = lifecycle_story(setup, tmp_path, monkeypatch)
    if revocation == 'blocked':
        hub.event(1, 'blocked', 'runner')
    elif revocation == 'closed':
        hub.issues[1]['state'] = 'closed'
    elif revocation == 'scope':
        hub.issues[1]['body'] = 'Owner changed the scope'
    else:
        other = core.public_record(policy_cfg, 1, 'other-task', 0, 'dispatched', event=101)
        other.update({'from': 'approved', 'actor': policy_cfg['actors']['runner']})
        hub.comments_by_issue[1].append({'user': {'login': policy_cfg['actors']['runner']},
                                       'body': core.record_body(other)})
    advance_main(cfg, 'main-update.txt')
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    assert not phases
    assert engine.data['stories']['alpha']['status'] == 'blocked'
    assert not any(r['kind'] == 'review' for r in core.records(policy_cfg, hub.client().comments(1), 1))


def test_lifecycle_completed_refresh_publishes_new_head_for_each_merge(setup, tmp_path, monkeypatch):
    from lifecycle import core

    cfg, path, policy_cfg, hub, phases = lifecycle_story(setup, tmp_path, monkeypatch)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    builder = phases[0][1]
    for name in ('main-one.txt', 'main-two.txt'):
        advance_main(cfg, name)
        engine.run_once()
        record = engine.data['stories']['alpha']
        assert record['status'] == 'ready_for_integration'
        assert core.state(hub.issues[1]) == 'in-review'
        history = core.records(policy_cfg, hub.client().comments(1), 1)
        assert history[-1]['kind'] == 'review'
        assert history[-1]['commit'] == record['head']
        assert history[-1]['outcome'] == 'ready_for_integration'
        assert phases[-1] == ('reviewer', 'slot-1' if builder == 'slot-0' else 'slot-0')
    assert not any(call == ('label', 1, 'blocked') for call in hub.calls)


def test_completed_refresh_checks_revocation_before_and_after_review(setup, tmp_path, monkeypatch):
    from lifecycle import core

    cfg, path, policy_cfg, hub, phases = lifecycle_story(setup, tmp_path, monkeypatch)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    old_head = engine.data['stories']['alpha']['head']
    advance_main(cfg, 'main-one.txt')
    execute = runner.execute_phase

    def revoke_during_review(config, state, job_path, job, role):
        result = execute(config, state, job_path, job, role)
        hub.issues[1]['body'] = 'Revoked during review'
        return result

    monkeypatch.setattr(runner, 'execute_phase', revoke_during_review)
    engine.run_once()
    assert engine.data['stories']['alpha']['status'] == 'blocked'
    history = core.records(policy_cfg, hub.client().comments(1), 1)
    assert all(r['commit'] == old_head for r in history if r['kind'] == 'review')
    # A later attempt with admission already revoked cannot launch a model.
    assert len(phases) == 3


@pytest.mark.parametrize('revocation', ['blocked', 'closed', 'scope'])
def test_completed_refresh_stops_before_model_on_revocation(setup, tmp_path, monkeypatch, revocation):
    from lifecycle import core

    cfg, path, policy_cfg, hub, phases = lifecycle_story(setup, tmp_path, monkeypatch)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    if revocation == 'blocked':
        hub.event(1, 'blocked', 'runner')
    elif revocation == 'closed':
        hub.issues[1]['state'] = 'closed'
    else:
        hub.issues[1]['body'] = 'Owner changed the scope'
    old_comments = hub.client().comments(1)
    advance_main(cfg, 'main-one.txt')
    engine.run_once()
    assert len(phases) == 2
    assert engine.data['stories']['alpha']['status'] == 'blocked'
    assert hub.client().comments(1) == old_comments
    assert core.records(policy_cfg, old_comments, 1)[-1]['kind'] == 'review'
