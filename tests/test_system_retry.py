"""System retries use only synthetic agents, transports and disposable repositories."""

from pathlib import Path

import pytest

from lifecycle import core, service
from test_lifecycle_delivery import story
from test_lifecycle_continuation import newest_claim
from test_one_continuation import reapprove
import test_runner as fixtures
import test_runner_pool as pool_fixtures

runner = fixtures.runner
setup = fixtures.setup
pool_setup = pool_fixtures.setup


@pytest.mark.parametrize('field', ['auth_exit_codes', 'quota_exit_codes'])
@pytest.mark.parametrize('value', [None, 3, '75', [True], [1], [2], [0], [-1], [256], [3.0]])
def test_exit_list_validation(setup, tmp_path, field, value):
    path, cfg = fixtures.populated_config(setup, tmp_path)
    cfg[field] = value
    runner.save(path, cfg)
    with pytest.raises(ValueError, match='exit_codes'):
        runner.load_config(path)


def test_exit_lists_disjoint_and_boundaries(setup, tmp_path):
    path, cfg = fixtures.populated_config(setup, tmp_path)
    cfg.update(auth_exit_codes=[3, 255], quota_exit_codes=[75])
    runner.save(path, cfg)
    assert runner.load_config(path)['auth_exit_codes'] == [3, 255]
    cfg['quota_exit_codes'] = [255]
    runner.save(path, cfg)
    with pytest.raises(ValueError, match='exit_codes'):
        runner.load_config(path)


@pytest.mark.parametrize('code', [0, -1, 1441, True, 1.5, '5'])
def test_schedule_validation(tmp_path, code):
    from test_lifecycle import policy
    cfg = policy(tmp_path)
    cfg['system_retry_minutes'] = [code]
    core.save(cfg['config_path'], cfg)
    with pytest.raises(ValueError, match='system_retry_minutes'):
        core.load_config(cfg['config_path'])


@pytest.mark.parametrize('value', [None, {}, 5, [1]*6, [1441]])
def test_schedule_shape_validation(tmp_path, value):
    from test_lifecycle import policy
    cfg = policy(tmp_path)
    cfg['system_retry_minutes'] = value
    core.save(cfg['config_path'], cfg)
    with pytest.raises(ValueError, match='system_retry_minutes'):
        core.load_config(cfg['config_path'])


@pytest.mark.parametrize('exit_code,expected', [(3, 'agent_sign_in_failed'),
                                              (255, 'agent_usage_limit'),
                                              (1, 'execution_failed'), (2, 'execution_failed'),
                                              (4, 'execution_failed')])
def test_standalone_detection_without_retry(setup, exit_code, expected):
    cfg, state, path, _ = setup
    cfg.update(auth_exit_codes=[3], quota_exit_codes=[255])
    fixtures.use_fake(cfg, state, f'print("untrusted provider output"); raise SystemExit({exit_code})')
    runner.run_once(cfg)
    job = runner.read(path)
    assert job['status'] in runner.TERMINAL and not job.get('retry')
    assert job.get('blocked_reason', {}).get('reason_code', 'execution_failed') == expected
    assert 'untrusted provider output' not in str(job)


def retry_story(tmp_path, monkeypatch, *, role='worker', failures=1, schedule=None, partial=False):
    cfg, hub, tool, calls, _ = story(tmp_path, monkeypatch)
    cfg['system_retry_minutes'] = [1, 2] if schedule is None else schedule
    core.save(cfg['config_path'], cfg)
    original = tool.execute
    failed = []
    attempts = []

    def execute(config, state, path, job, phase):
        attempts.append((job['id'], job['round'], phase))
        if phase == role and len(failed) < failures:
            job['status'] = 'running_' + phase
            tool.save_job(config, path, job)
            if partial:
                Path(config['worktree'], 'partial.txt').write_text('unfinished attempt\n')
            failed.append(True)
            raise tool.AgentExit('agent_sign_in_failed', retryable=True)
        if partial and phase == 'worker':
            assert Path(config['worktree'], 'partial.txt').read_text() == 'unfinished attempt\n'
        return original(config, state, path, job, phase)

    monkeypatch.setattr(tool, 'execute', execute)
    return cfg, hub, tool, attempts


@pytest.mark.parametrize('phase', ['worker', 'reviewer'])
def test_same_job_phase_durable_schedule_restart_and_exact_checks(tmp_path, monkeypatch, phase):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, role=phase, partial=phase == 'worker')
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job['status'] == 'waiting_system'
    assert job['retry'] == dict(code='agent_sign_in_failed', attempt=1, phase=phase, not_before=160)
    assert not job.get('continuation') and not job.get('stop_saving')
    claim = newest_claim(cfg)
    assert claim['stage'] == 'enqueued'
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    assert not any(r['reason'] == 'agent_sign_in_failed' for r in records)
    assert core.job_records(cfg, job) == []
    checks = list(job.get('verification_runs', []))
    body = service.poll_body(cfg, 100, [])
    assert '#1: waiting on the system (agent_sign_in_failed), retry 1 of 2 no earlier than 1970-01-01 00:02 UTC' in body
    # A fresh service config has no process-local retry data.
    cfg = core.load_config(cfg['config_path'])
    service.poll(cfg, hub.client(), clock=lambda: 159)
    assert len(attempts) == (1 if phase == 'worker' else 2)
    assert service.jobs(cfg)[0]['status'] == 'waiting_system'
    service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    finished, = service.jobs(cfg)
    assert finished['status'] == 'needs_owner'  # delivery disabled after successful review
    assert all(job_id == job['id'] and round_ == 1 for job_id, round_, _ in attempts)
    assert [p for _, _, p in attempts] == (['worker', 'worker', 'reviewer'] if phase == 'worker'
                                         else ['worker', 'reviewer', 'reviewer'])
    assert len(list((Path(cfg['state_dir']) / 'claims').glob('*.json'))) == 1
    after = core.records(cfg, hub.comments_by_issue[1], 1)
    assert sum(r['to'] == 'dispatched' for r in after) == 1
    assert sum(r['kind'] == 'transition' and r['to'] == ('in-progress' if phase == 'worker' else 'in-review') for r in after) == 1
    if phase == 'reviewer':
        assert finished['verification_runs'] == checks


@pytest.mark.parametrize('schedule', [[], [1]])
def test_final_stop_saves_continuation_with_latest_code(tmp_path, monkeypatch, schedule):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, failures=5, schedule=schedule, partial=True)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    if schedule:
        service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    job, = service.jobs(cfg)
    assert job['status'] == 'needs_owner' and job['continuation']
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    stops = [r for r in records if r['reason'] == 'agent_sign_in_failed']
    assert len(stops) == 1 and stops[0]['continuation']
    assert len(attempts) == len(schedule) + 1
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 300)
    assert newest_claim(cfg)['continued_from'] == job['head_commit']


@pytest.mark.parametrize('change', ['body', 'closed', 'approval', 'label'])
@pytest.mark.parametrize('phase', ['worker', 'reviewer'])
def test_wait_cancellation_before_new_claims(tmp_path, monkeypatch, change, phase):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, role=phase, partial=phase == 'worker')
    service.poll(cfg, hub.client(), clock=lambda: 100)
    old, = service.jobs(cfg)
    if change == 'body':
        hub.issues[1]['body'] += '\nChanged approved text'
    elif change == 'closed':
        hub.issues[1]['state'] = 'closed'
    elif change == 'approval':
        reapprove(hub)
    else:
        hub.event(1, 'needs-owner', 'owner')
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    stopped = next(j for j in service.jobs(cfg) if j['id'] == old['id'])
    assert stopped['status'] == 'needs_owner' and not stopped.get('retry')
    claims = [core.read(p) for p in (Path(cfg['state_dir']) / 'claims').glob('*.json')]
    claim = next(c for c in claims if c['job'] == old['id'])
    assert claim['stage'] == 'finished' and not claim.get('error')
    assert len(attempts) == (1 if phase == 'worker' else 2)
    stops = [r for r in core.records(cfg, hub.comments_by_issue[1], 1)
             if r['reason'] == 'agent_sign_in_failed']
    assert len(stops) == (1 if change == 'body' else 0)
    if change != 'body':
        assert not stopped.get('head_commit') and not stopped.get('continuation')
    if change == 'approval':
        assert len(claims) == 2 and not newest_claim(cfg).get('continuation_error')
        assert not newest_claim(cfg).get('continued_from')


def test_retry_phase_restart_is_interrupted_never_replayed(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    original = tool.execute

    class Crash(BaseException):
        pass

    def crash(config, state, path, job, role):
        job.update(status='running_' + role)
        job.pop('retry', None)
        tool.save_job(config, path, job)
        raise Crash

    monkeypatch.setattr(tool, 'execute', crash)
    with pytest.raises(Crash):
        service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    assert not service.jobs(cfg)[0].get('retry')
    monkeypatch.setattr(tool, 'execute', original)
    service.poll(cfg, hub.client(), clock=lambda: 220, full_list=True)
    assert service.jobs(cfg)[0]['status'] == 'interrupted'
    stops = [r for r in core.records(cfg, hub.comments_by_issue[1], 1) if r['reason'] == 'execution_failed']
    assert len(stops) == 1
    assert len(attempts) == 1


def test_admission_before_retry(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    calls = []

    def refuse(*args):
        calls.append(True)
        raise tool.Blocked('synthetic admission refusal')

    monkeypatch.setattr(tool, 'check_lifecycle_admission', refuse)
    service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    assert calls == [True] and len(attempts) == 1
    assert service.jobs(cfg)[0]['status'] != 'waiting_system'


@pytest.mark.parametrize('role', ['readiness', 'reviewer'])
def test_readonly_mutation_overrides_listed_exit(tmp_path, monkeypatch, role):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, role=role)
    if role == 'readiness':
        from test_readiness import enable
        enable(cfg)
    original = tool.execute

    def mutate(config, state, path, job, phase):
        if phase == role:
            Path(config['worktree'], 'unexpected.txt').write_text('read-only mutation')
            raise tool.AgentExit('agent_sign_in_failed', retryable=True)
        return original(config, state, path, job, phase)

    monkeypatch.setattr(tool, 'execute', mutate)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job['status'] == 'failed' and not job.get('retry')
    assert core.job_records(cfg, job)[0]['reason'] == 'execution_failed'


def test_worker_history_change_overrides_listed_exit(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)

    def mutate(config, state, path, job, phase):
        fixtures.commit(config)
        raise tool.AgentExit('agent_sign_in_failed', retryable=True)

    monkeypatch.setattr(tool, 'execute', mutate)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job['status'] == 'blocked' and not job.get('retry')
    assert job['commit_refusals'][0]['reason'] == 'history_changed'


def test_readiness_retry_questions_publish_once(tmp_path, monkeypatch):
    from test_readiness import enable, QUESTION
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, role='readiness')
    enable(cfg)
    original = tool.execute
    count = []

    def phase(config, state, path, job, role):
        if role == 'readiness' and count:
            job['status'] = 'running_readiness'
            tool.save_job(config, path, job)
            return {'status': 'questions', 'questions': [QUESTION]}
        count.append(True)
        return original(config, state, path, job, role)

    monkeypatch.setattr(tool, 'execute', phase)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    assert service.jobs(cfg)[0]['status'] == 'waiting_system'
    service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    job, = service.jobs(cfg)
    assert job['status'] == 'needs_owner' and job['readiness_sessions'] == 1
    assert not job.get('continuation')
    stops = [r for r in core.records(cfg, hub.comments_by_issue[1], 1) if r['reason'] == 'readiness_question']
    assert len(stops) == 1


@pytest.mark.parametrize('own_result,refused', [(True, False), (False, True)])
def test_no_retry_for_result_or_commit_refusal(tmp_path, monkeypatch, own_result, refused):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)

    def phase(config, state, path, job, role):
        if refused:
            job['commit_refusals'] = [{'reason': 'preexisting_staged_changes'}]
        raise tool.AgentExit('agent_sign_in_failed', retryable=not own_result)

    monkeypatch.setattr(tool, 'execute', phase)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job['status'] == 'needs_owner' and not job.get('retry')


@pytest.mark.parametrize('check', ['preflight', 'build', 'story_tests', 'runner_checks', 'prerequisite'])
def test_worker_unavailable_never_retries(tmp_path, monkeypatch, check):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)

    def phase(config, state, path, job, role):
        return dict(fixtures.result('blocked'), blocked_check=check, blocked_cause='unavailable')

    monkeypatch.setattr(tool, 'execute', phase)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job['status'] == 'needs_owner' and not job.get('retry')
    assert core.job_records(cfg, job)[0]['reason'] == 'worker_' + check + '_unavailable'


def test_count_across_codes_phases_and_failure_time(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, partial=True)
    original = tool.execute
    reviewer_failed = []

    def phase(config, state, path, job, role):
        if role == 'reviewer' and not reviewer_failed:
            reviewer_failed.append(True)
            job['status'] = 'running_reviewer'
            tool.save_job(config, path, job)
            raise tool.AgentExit('agent_usage_limit', retryable=True)
        return original(config, state, path, job, role)

    monkeypatch.setattr(tool, 'execute', phase)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    service.poll(cfg, hub.client(), clock=lambda: 200, full_list=True)
    job, = service.jobs(cfg)
    assert job['retry'] == dict(code='agent_usage_limit', attempt=2, phase='reviewer', not_before=320)
    checks = job['verification_runs']
    service.poll(cfg, hub.client(), clock=lambda: 320, full_list=True)
    job, = service.jobs(cfg)
    assert job['status'] == 'needs_owner' and job['system_retries'] == 2
    assert job['verification_runs'] == checks


def test_round_two_after_failed_required_tests_still_retries(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, failures=0)
    original = tool.execute
    verify = tool.verify_required_tests

    def tests(config, state, path, job):
        entry = verify(config, state, path, job)
        if job['round'] == 1:
            entry['status'] = 'failed'
            tool.save_job(config, path, job)
        return entry

    def phase(config, state, path, job, role):
        if role == 'worker' and job['round'] == 2 and not job.get('system_retries'):
            job['status'] = 'running_worker'
            tool.save_job(config, path, job)
            raise tool.AgentExit('agent_sign_in_failed', retryable=True)
        result = original(config, state, path, job, role)
        return fixtures.result('needs_changes', 'reviewer') if role == 'reviewer' and job['round'] == 1 else result

    monkeypatch.setattr(tool, 'execute', phase)
    monkeypatch.setattr(tool, 'verify_required_tests', tests)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job['status'] == 'waiting_system' and job['round'] == 2
    service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    job, = service.jobs(cfg)
    assert job['round'] == 2 and job['status'] == 'needs_owner'
    assert [r['status'] for r in job['verification_runs']] == ['failed', 'passed']


def test_real_phase_retry_uses_distinct_folders_and_keeps_output_private(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, failures=0)
    executable = fixtures.fake_agent(tmp_path, 'print("private quota words"); raise SystemExit(75)')
    value = core.read(cfg['runner_config'])
    value.update(agent=fixtures.agent_config(executable), auth_exit_codes=[75])
    core.save(cfg['runner_config'], value)
    monkeypatch.setattr(tool, 'execute', tool.self_module().execute_phase)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    job, = service.jobs(cfg)
    assert job['retry']['attempt'] == 2
    root = Path(cfg['state_dir']) / 'jobs' / job['id'] / 'runner-state' / 'runs' / job['id']
    assert {p.name for p in root.iterdir()} == {'round_1_worker', 'round_1_worker_retry_1'}
    assert all('private quota words' not in c['body'] for c in hub.comments_by_issue[1])


def test_pool_quota_retains_cooldown_without_worker_synthetic_result(pool_setup):
    from runner import pool
    cfg, path = pool_setup
    cfg['stories'] = cfg['stories'][:1]
    executable = fixtures.fake_agent(Path(cfg['state_dir']).parent, 'raise SystemExit(75)')
    for slot in cfg['slots']:
        slot.update(agent=fixtures.agent_config(executable), quota_exit_codes=[75])
    runner.save(path, cfg)
    engine = pool.Pool(pool.load_config(path), clock=lambda: 100)
    engine.run_once()
    assert engine.data['slots']['slot-0']['cooldown_until'] == 160
    job_path = Path(engine.runner_config(cfg['stories'][0])['state_dir']) / 'tasks/alpha.json'
    job = runner.read(job_path)
    assert job['status'] == 'blocked' and not job.get('worker') and not job.get('retry')
    assert job['blocked_reason']['reason_code'] == 'agent_usage_limit'
    assert all(slot['active'] is None for slot in engine.data['slots'].values())


def test_pool_slots_ignore_runner_auth_codes(pool_setup):
    from runner import pool
    cfg, path = pool_setup
    cfg['stories'] = cfg['stories'][:1]
    template_path = cfg['stories'][0]['runner_config']
    template = runner.read(template_path)
    template['auth_exit_codes'] = [75]
    runner.save(template_path, template)
    executable = fixtures.fake_agent(Path(cfg['state_dir']).parent, 'raise SystemExit(75)')
    for slot in cfg['slots']:
        slot['agent'] = fixtures.agent_config(executable)
    runner.save(path, cfg)
    engine = pool.Pool(pool.load_config(path))
    engine.run_once()
    job = runner.read(Path(engine.runner_config(cfg['stories'][0])['state_dir']) / 'tasks/alpha.json')
    assert job['status'] == 'failed' and not job.get('retry')
    assert job.get('blocked_reason', {}).get('reason_code') != 'agent_sign_in_failed'


def test_listed_exit_with_own_result_stops_without_parsing_it(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, failures=0)
    source = ('import pathlib,sys\n'
              "output=pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
              "output.write_text('untrusted malformed result')\nraise SystemExit(75)")
    executable = fixtures.fake_agent(tmp_path, source)
    template = core.read(cfg['runner_config'])
    template.update(agent=fixtures.agent_config(executable), auth_exit_codes=[75])
    core.save(cfg['runner_config'], template)
    monkeypatch.setattr(tool, 'execute', tool.execute_phase)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job['status'] == 'needs_owner' and not job.get('retry') and not job.get('worker')
    assert job['blocked_reason']['reason_code'] == 'agent_sign_in_failed'


def test_standup_wait_is_neither_stale_nor_attention(tmp_path, monkeypatch):
    import datetime as dt
    from digest import standup
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    at = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    sections = standup.collect(dict(lifecycle=cfg, runner_state_dirs=[cfg['state_dir']]),
                              hub.client('product'), at - dt.timedelta(days=1), at)
    assert 'stale in-progress; no automatic retry' not in str(sections)
    assert 'local job needs attention' not in str(sections)


def test_schedule_change_is_status_content_change(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    issues = list(hub.issues.values())
    service.publish_poll(cfg, hub.client(), issues, 101)
    before = core.read(Path(cfg['state_dir']) / 'public-heartbeat.json')['content']
    cfg['system_retry_minutes'] = [2, 3]
    service.publish_poll(cfg, hub.client(), issues, 102)
    journal = core.read(Path(cfg['state_dir']) / 'public-heartbeat.json')
    assert journal['content'] != before and journal['last_edit'] == 102
    assert 'retry 1 of 2' in journal['content']


def test_waiting_claim_blocks_upgrade_before_transport(tmp_path, monkeypatch):
    from lifecycle import upgrade
    from test_lifecycle_upgrade import OLD, NEW
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    folder = tmp_path / 'versions'
    folder.mkdir()
    cfg['upgrade'] = dict(track='main', smoke_agent='example-agent', versions_dir=str(folder), initial_commit=OLD)
    monkeypatch.setenv('ADT_VERSIONS_DIR', str(folder))
    monkeypatch.setattr(core, 'PublicGitHub', lambda *a: pytest.fail('active job must defer upgrade'))
    monkeypatch.setattr(core, 'GitHub', lambda *a, **kw: pytest.fail('active job must defer upgrade'))
    assert upgrade.attempt(cfg, upgrade.context(cfg)) is False
    assert not (folder / NEW).exists()


def test_unlisted_failure_on_retry_stops_immediately(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    monkeypatch.setattr(tool, 'execute', lambda *a: (_ for _ in ()).throw(RuntimeError('generic failure')))
    service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    job, = service.jobs(cfg)
    assert job['status'] == 'failed' and not job.get('retry')
    assert core.job_records(cfg, job)[0]['reason'] == 'execution_failed'



def test_schedule_is_durable_before_any_publication_or_receipt(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)
    original = tool.save
    saved_receipt = []

    class Crash(BaseException):
        pass

    def crash_after_wait_save(path, value):
        original(path, value)
        if value.get('status') == 'waiting_system':
            receipt = Path(cfg['state_dir']) / 'jobs' / value['id'] / 'runner-state' / 'receipts' / (value['id'] + '.json')
            saved_receipt.append(receipt.read_bytes())
            assert core.job_records(cfg, value) == []
            raise Crash

    monkeypatch.setattr(tool, 'save', crash_after_wait_save)
    with pytest.raises(Crash):
        service.poll(cfg, hub.client(), clock=lambda: 100)
    job, = service.jobs(cfg)
    assert job['retry']['not_before'] == 160 and newest_claim(cfg)['stage'] == 'enqueued'
    receipt = Path(cfg['state_dir']) / 'jobs' / job['id'] / 'runner-state' / 'receipts' / (job['id'] + '.json')
    monkeypatch.setattr(tool, 'save', original)
    service.poll(cfg, hub.client(), clock=lambda: 150, full_list=True)
    assert receipt.read_bytes() == saved_receipt[0] and len(attempts) == 1
    assert not any(r['reason'] == 'agent_sign_in_failed'
                   for r in core.records(cfg, hub.comments_by_issue[1], 1))
    service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    assert service.jobs(cfg)[0]['status'] == 'needs_owner'


def test_latest_code_at_exhaustion(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, schedule=[1], partial=True)
    original = tool.execute

    def phase(config, state, path, job, role):
        if job.get('system_retries'):
            raise tool.AgentExit('agent_usage_limit', retryable=True)
        return original(config, state, path, job, role)

    monkeypatch.setattr(tool, 'execute', phase)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    job, = service.jobs(cfg)
    assert job['status'] == 'needs_owner' and job['continuation']
    assert job['blocked_reason']['reason_code'] == 'agent_usage_limit'
    stops = [r for r in core.records(cfg, hub.comments_by_issue[1], 1) if r['reason'] == 'agent_usage_limit']
    assert len(stops) == 1 and stops[0]['continuation']


@pytest.mark.parametrize('change', ['approval', 'closed'])
def test_cancelled_wait_falls_back_to_older_saved_work(tmp_path, monkeypatch, change):
    from test_lifecycle_continuation import saved_story
    cfg, hub, tool, older = saved_story(tmp_path, monkeypatch)
    cfg['system_retry_minutes'] = [1]
    core.save(cfg['config_path'], cfg)
    reapprove(hub)

    def fail(config, state, path, job, role):
        job['status'] = 'running_worker'
        tool.save_job(config, path, job)
        fixtures.deliver(config)
        raise tool.AgentExit('agent_sign_in_failed', retryable=True)

    monkeypatch.setattr(tool, 'execute', fail)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    waiting = next(j for j in service.jobs(cfg) if j['status'] == 'waiting_system')
    if change == 'closed':
        hub.issues[1]['state'] = 'closed'
        service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 300)
        hub.issues[1]['state'] = 'open'
    reapprove(hub)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 400)
    claim = newest_claim(cfg)
    assert claim['continuation_source'] == older['id']
    assert claim['continued_from'] == older['head_commit']
    cancelled = next(j for j in service.jobs(cfg) if j['id'] == waiting['id'])
    assert cancelled['retry_local_end'] and not cancelled.get('head_commit')


def test_primary_exit_codes_come_from_primary_template(tmp_path, monkeypatch):
    cfg, hub, tool, calls, publications = story(tmp_path, monkeypatch)
    original = tool.execute

    def phase(config, state, path, job, role):
        result = original(config, state, path, job, role)
        return fixtures.result('needs_changes', 'reviewer') if role == 'reviewer' else result

    monkeypatch.setattr(tool, 'execute', phase)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    assert service.jobs(cfg)[0]['status'] == 'needs_primary_review'
    executable = fixtures.fake_agent(tmp_path, 'raise SystemExit(75)')
    value = core.read(cfg['primary_runner_config'])
    value.update(agent=fixtures.agent_config(executable), auth_exit_codes=[75])
    core.save(cfg['primary_runner_config'], value)
    monkeypatch.setattr(tool, 'execute', tool.execute_phase)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    claim = newest_claim(cfg)
    job = next(j for j in service.jobs(cfg) if j['id'] == claim['job'])
    assert claim['primary'] and job['status'] == 'waiting_system'
    assert job['retry']['code'] == 'agent_sign_in_failed' and job['retry']['phase'] == 'worker'


@pytest.mark.parametrize('job_status', ['waiting_system', 'running_worker'])
def test_standalone_cannot_resume_service_wait(setup, job_status):
    cfg, state, path, job = setup
    job.update(status=job_status, retry={'code': 'agent_sign_in_failed', 'attempt': 1,
                                      'phase': 'worker', 'not_before': 0})
    runner.save(path, job)
    runner.run_once(cfg)
    assert runner.read(path)['status'] == ('waiting_system' if job_status == 'waiting_system' else 'interrupted')


def test_waiting_story_older_stop_does_not_need_attention(tmp_path, monkeypatch):
    import datetime as dt
    from digest import standup
    from test_lifecycle_continuation import saved_story
    cfg, hub, tool, older = saved_story(tmp_path, monkeypatch)
    reapprove(hub)

    def phase(config, state, path, job, role):
        job['status'] = 'running_worker'
        tool.save_job(config, path, job)
        raise tool.AgentExit('agent_sign_in_failed', retryable=True)

    monkeypatch.setattr(tool, 'execute', phase)
    service.poll(cfg, hub.client(), clock=lambda: 200)
    assert any(j['status'] == 'waiting_system' for j in service.jobs(cfg))
    at = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    sections = standup.collect(dict(lifecycle=cfg, runner_state_dirs=[cfg['state_dir']]),
                              hub.client('product'), at - dt.timedelta(days=1), at)
    assert 'local job needs attention' not in str(sections)


def test_wait_scan_keeps_story_error_isolation(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    claim = newest_claim(cfg)
    # A malformed config is a story-local unexpected error, as in the claim loop.
    Path(claim['config']).write_text('{}')
    hub.issue(2)
    hub.approve(2)
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert core.state(hub.issues[1]) == 'blocked'
    assert core.state(hub.issues[2]) == 'dispatched'


def test_finished_claim_is_not_shown_as_waiting(tmp_path, monkeypatch):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    claim = newest_claim(cfg)
    Path(claim['config']).write_text('{}')
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    assert newest_claim(cfg)['stage'] == 'finished'
    assert '#1: waiting on the system' not in service.poll_body(cfg, 200, [])


@pytest.mark.parametrize('tamper', ['assignment', 'config', 'claim'])
def test_body_cancel_preserves_source_binding_integrity(tmp_path, monkeypatch, tamper):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, partial=True)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    claim = newest_claim(cfg)
    path = Path(cfg['state_dir']) / 'jobs' / claim['job'] / 'runner-state' / 'tasks' / (claim['job'] + '.json')
    if tamper == 'assignment':
        job = core.read(path)
        job['assignment']['task_sha256'] = 'f' * 64
        core.save(path, job)
    elif tamper == 'config':
        config = core.read(claim['config'])
        config['branch'] = 'another-feature'
        core.save(claim['config'], config)
    else:
        claim['event'] += 1000
        core.save(Path(cfg['state_dir']) / 'claims' / (claim['job'] + '.json'), claim)
    hub.issues[1]['body'] += '\nChanged scope'
    with pytest.raises((core.IntegrityError, tool.IntegrityError)):
        service.poll(cfg, hub.client(), clock=lambda: 200)
    assert not any(r['reason'] == 'agent_sign_in_failed'
                   for r in core.records(cfg, hub.comments_by_issue[1], 1))


@pytest.mark.parametrize('phase', ['worker', 'reviewer'])
def test_queue_retains_real_wait_and_releases_final_stop(tmp_path, monkeypatch, phase):
    from test_story_queue import add, started
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, role=phase,
                                          failures=5, schedule=[1], partial=phase == 'worker')
    service.poll(cfg, hub.client(), clock=lambda: 100)
    original, = service.jobs(cfg)
    assert original['status'] == 'waiting_system' and original['retry']['not_before'] == 160
    add(hub, 2)
    service.poll(cfg, hub.client(), clock=lambda: 159, full_list=True)
    assert started(cfg) == [1]
    assert service.queue_waiting(cfg) == {2: {'story': 1}}
    body = service.poll_body(cfg, 159, [])
    assert '#1: waiting on the system' in body and '#2: waiting for the story in progress, #1' in body
    service.poll(cfg, hub.client(), clock=lambda: 160, full_list=True)
    stopped = next(j for j in service.jobs(cfg) if j['id'] == original['id'])
    assert stopped['status'] == 'needs_owner' and not stopped.get('retry')
    assert stopped['continuation'] and stopped['head_commit']
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200, full_list=True)
    assert 2 in started(cfg)


@pytest.mark.parametrize('change', ['body', 'closed', 'approval', 'label'])
def test_queue_wait_cancellation_frees_slot_before_admission(tmp_path, monkeypatch, change):
    from test_story_queue import add, started
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, partial=True)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    old, = service.jobs(cfg)
    add(hub, 2)
    if change == 'body':
        hub.issues[1]['body'] += '\nChanged approved scope'
    elif change == 'closed':
        hub.issues[1]['state'] = 'closed'
    elif change == 'approval':
        reapprove(hub)
    else:
        hub.event(1, 'needs-owner', 'owner')
    service.poll(cfg, hub.client(), run_jobs=False, clock=lambda: 200)
    stopped = next(j for j in service.jobs(cfg) if j['id'] == old['id'])
    assert stopped['status'] == 'needs_owner' and not stopped.get('retry')
    assert 2 in started(cfg)
    if change == 'body':
        assert stopped['continuation'] and stopped['head_commit']
    else:
        assert not stopped.get('continuation') and not stopped.get('head_commit')
    if change == 'approval':
        claims = [core.read(p) for p in (Path(cfg['state_dir']) / 'claims').glob('*.json')]
        current = max((c for c in claims if c['issue'] == 1), key=lambda c: c['event'])
        assert not current.get('continuation_error') and not current.get('continued_from')


@pytest.mark.parametrize('pr_state', [None, 'open', 'closed'])
def test_queue_terminal_error_claim_does_not_retain_unowned_wait(tmp_path, monkeypatch, pr_state):
    from test_story_queue import add
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch)
    service.poll(cfg, hub.client(), clock=lambda: 100)
    claim = newest_claim(cfg)
    claim.update(stage='finished', error='unexpected_error')
    folder = Path(cfg['state_dir']) / 'claims'
    core.save(folder / (claim['job'] + '.json'), claim)
    path = Path(cfg['state_dir']) / 'jobs' / claim['job'] / 'runner-state' / 'tasks' / (claim['job'] + '.json')
    job = core.read(path)
    assert job['status'] == 'waiting_system' and job['retry']
    if pr_state:
        job['integration'] = {'pr': 77}
        core.save(path, job)
        hub.prs[77] = {'number': 77, 'state': pr_state, 'merged': pr_state == 'closed'}
    before = path.read_bytes()
    add(hub, 2)
    next_claim = {'issue': 2}
    queue = service.StoryQueue(cfg, hub.client(), hub.issues, set(), folder)
    expected = 1 if pr_state == 'open' else None
    assert queue.holder(next_claim) == expected
    assert queue.holder(next_claim) == expected
    reads = sum(call[:2] == ('GET', '/pulls/77') for call in hub.calls)
    assert reads == int(pr_state is not None)
    # A later poll reuses a closed PR's stable release generation, but rechecks
    # an open PR once. The finished claim never causes a runner-job resave.
    later = service.StoryQueue(cfg, hub.client(), hub.issues, set(), folder)
    assert later.holder(next_claim) == expected
    assert sum(call[:2] == ('GET', '/pulls/77') for call in hub.calls) == (2 if pr_state == 'open' else reads)
    assert path.read_bytes() == before


@pytest.mark.parametrize('window,change', [
    (window, change) for window in ['after_scan', 'later_issue']
    for change in ['body', 'label', 'approval', 'closed', 'dispatch']
] + [('later_history', 'dispatch')])
@pytest.mark.parametrize('phase', ['worker', 'reviewer'])
def test_late_wait_admission_change_finishes_without_suspending(
        tmp_path, monkeypatch, window, change, phase):
    cfg, hub, tool, attempts = retry_story(tmp_path, monkeypatch, role=phase,
                                          partial=phase == 'worker')
    service.poll(cfg, hub.client(), clock=lambda: 100)
    old, = service.jobs(cfg)
    claim = newest_claim(cfg)
    original_scan = service.cancel_waiting
    client = hub.client()
    original_issue = client.issue
    original_comments = client.comments
    gate = {'armed': False, 'changed': False, 'reads': 0, 'histories': 0}

    def mutate():
        if gate['changed']:
            return
        gate['changed'] = True
        if change == 'body':
            hub.issues[1]['body'] += '\nLate approved-text change'
        elif change == 'label':
            hub.event(1, 'needs-owner', 'owner')
        elif change == 'approval':
            reapprove(hub)
        elif change == 'closed':
            hub.issues[1]['state'] = 'closed'
        else:
            record = core.public_record(cfg, 1, 'new-dispatch', 0, 'dispatched',
                                        event=claim['event'])
            record.update({'from': 'approved', 'actor': cfg['actors']['runner']})
            hub.comments_by_issue[1].append({'user': {'login': cfg['actors']['runner']},
                                           'body': core.record_body(record)})

    def scan(*args, **kwargs):
        result = original_scan(*args, **kwargs)
        gate['armed'] = True
        if window == 'after_scan':
            mutate()
        return result

    def issue(number):
        if gate['armed'] and number == 1:
            gate['reads'] += 1
            if window == 'later_issue' and gate['reads'] == 2:
                mutate()
        return original_issue(number)

    def comments(number):
        if gate['armed'] and number == 1:
            gate['histories'] += 1
            if window == 'later_history' and gate['histories'] == 2:
                mutate()
        return original_comments(number)

    monkeypatch.setattr(service, 'cancel_waiting', scan)
    monkeypatch.setattr(client, 'issue', issue)
    monkeypatch.setattr(client, 'comments', comments)
    before = list(attempts)
    service.poll(cfg, client, clock=lambda: 200)
    assert gate['changed']
    stopped = next(j for j in service.jobs(cfg) if j['id'] == old['id'])
    final = core.read(Path(cfg['state_dir']) / 'claims' / (claim['job'] + '.json'))
    assert final['stage'] == 'finished' and not final.get('error')
    assert stopped['status'] == 'needs_owner' and not stopped.get('retry')
    assert attempts == before
    records = core.records(cfg, hub.comments_by_issue[1], 1)
    held = [r for r in records if r['job'] == old['id'] and r['reason'] == 'agent_sign_in_failed']
    if change == 'body':
        assert stopped['continuation'] and stopped['head_commit']
        assert len(held) == 1
    else:
        assert not stopped.get('continuation') and not stopped.get('head_commit')
        assert held == []
    queue = service.StoryQueue(cfg, client, hub.issues, set(), Path(cfg['state_dir']) / 'claims')
    assert queue.holder({'issue': 2}) is None
    service.poll(cfg, client, run_jobs=False, clock=lambda: 300)
    after = core.records(cfg, hub.comments_by_issue[1], 1)
    assert len([r for r in after if r['job'] == old['id'] and r['reason'] == 'agent_sign_in_failed']) == len(held)
    assert attempts == before
