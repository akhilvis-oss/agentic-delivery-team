"""Runner-owned checks use independent, disposable Git copies, never reviewer writes."""
from pathlib import Path
import sys

import pytest
import test_runner as fixtures

runner = fixtures.runner
setup = fixtures.setup


def configure(cfg, source):
    cfg['verification'] = {'commands': [[sys.executable, '-c', source]], 'timeout_seconds': 3}


def prepared(setup):
    cfg, state, path, job = setup
    job.update(head_commit=runner.git(cfg, 'rev-parse', 'HEAD'), round=1)
    return cfg, state, path, job


def test_pass_receipt_prompt_and_reviewed_worktree_unchanged(setup, monkeypatch):
    cfg, state, path, _ = setup
    repo = Path(cfg['worktree'])
    (repo / 'ignored.txt').write_text('private fixture\n')
    (repo / '.git/info/exclude').write_text('ignored.txt\n')
    git_config = (repo / '.git/config').read_bytes()
    configure(cfg, '''import pathlib, subprocess, tempfile, unittest
assert pathlib.Path('delivery.txt').read_text() == 'delivery\\n'
assert not pathlib.Path('ignored.txt').exists()
assert not pathlib.Path('.git/objects/info/alternates').exists()
subprocess.run(['git', 'config', 'trial.only', 'copy'], check=True)
pathlib.Path('delivery.txt').write_text('test mutation')
with tempfile.TemporaryDirectory() as tmp:
    pathlib.Path(tmp, 'test.txt').write_text('temporary fixture')
unittest.TextTestRunner().run(unittest.TestSuite([unittest.FunctionTestCase(lambda: None)]))
''')
    calls = []
    def execute(config, state, path, job, role):
        calls.append(role)
        if role == 'reviewer':
            evidence = job['verification_runs'][-1]
            assert evidence['head'] == job['head_commit']
            assert evidence['status'] == 'passed'
            assert evidence['results'][0]['reported_counts'] == {'total': 1}
            prompt = runner.prompt_for(config, job, role)
            assert 'RUNNER-OWNED REQUIRED TEST EVIDENCE' in prompt
            assert 'Do not rerun' in prompt
            assert (repo / 'delivery.txt').read_text() == 'delivery\n'
        return fixtures.execute_delivery(config, state, path, job, role)
    monkeypatch.setattr(runner, 'execute', execute)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job['status'] == 'ready_for_integration'
    assert calls == ['worker', 'reviewer']
    assert (repo / '.git/config').read_bytes() == git_config
    receipt = runner.read(state / 'receipts/task.json')
    result = receipt['verification_runs'][0]['results'][0]
    assert result['argv'] == cfg['verification']['commands'][0]
    assert result['exit_code'] == 0 and result['output_sha256']
    assert 'Ran 1 test' in result['output_tail']
    assert not list((state / 'verification').iterdir())
    assert 'verification_runs' not in runner.public_receipt(receipt)


def test_exact_head_copy_has_independent_index_and_no_untracked_files(setup):
    cfg, state, path, job = prepared(setup)
    repo = Path(cfg['worktree'])
    (repo / 'tracked.txt').write_text('tracked')
    runner.git(cfg, 'add', 'tracked.txt')
    job['head_commit'] = fixtures.commit(cfg)
    (repo / 'untracked.txt').write_text('not in reviewed commit')
    before = runner.fingerprint(cfg)
    index = (repo / '.git/index').read_bytes()
    configure(cfg, "import pathlib, subprocess; assert pathlib.Path('tracked.txt').read_text() == 'tracked'; "
              "assert not pathlib.Path('untracked.txt').exists(); "
              "assert subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip() == " + repr(job['head_commit']) + "; "
              "pathlib.Path('tracked.txt').write_text('changed'); subprocess.run(['git','add','tracked.txt'], check=True)")
    assert runner.verify_required_tests(cfg, state, path, job)['status'] == 'passed'
    assert runner.fingerprint(cfg) == before
    assert (repo / '.git/index').read_bytes() == index


def test_failed_tests_passed_to_reviewer_cannot_be_approved(setup, monkeypatch):
    cfg, state, path, _ = setup
    configure(cfg, "print('1 failed in 0.01s'); raise SystemExit(1)")
    roles = []
    def execute(config, state, path, job, role):
        roles.append(role)
        if role == 'reviewer':
            assert job['verification_runs'][-1]['status'] == 'failed'
        return fixtures.execute_delivery(config, state, path, job, role)
    monkeypatch.setattr(runner, 'execute', execute)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job['status'] == 'blocked'
    assert roles == ['worker', 'reviewer']
    result = job['verification_runs'][0]['results'][0]
    assert result['status'] == 'failed' and result['exit_code'] == 1
    assert result['reported_counts'] == {'failed': 1}


@pytest.mark.parametrize('missing', ['directory', 'capacity'])
def test_unavailable_scratch_blocks_before_reviewer(setup, monkeypatch, missing):
    cfg, state, path, _ = setup
    configure(cfg, 'pass')
    if missing == 'directory':
        cfg['verification']['scratch_dir'] = str(state / 'missing')
    else:
        def unavailable(*args, **kwargs):
            raise OSError('synthetic no space')
        monkeypatch.setattr(runner.tempfile, 'mkdtemp', unavailable)
    def execute(config, state, path, job, role):
        assert role == 'worker'
        return fixtures.execute_delivery(config, state, path, job, role)
    monkeypatch.setattr(runner, 'execute', execute)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job['status'] == 'blocked'
    assert job['verification_runs'][0]['status'] == 'blocked'
    assert not (state / 'missing').exists()


@pytest.mark.parametrize('mode', ['timeout', 'cancel', 'launch'])
def test_execution_unavailable_is_blocked_and_copy_is_removed(setup, mode):
    cfg, state, path, job = prepared(setup)
    configure(cfg, 'import time; time.sleep(20)')
    cfg['verification']['timeout_seconds'] = 1
    if mode == 'cancel':
        (state / 'CANCEL').touch()
    if mode == 'launch':
        cfg['verification']['commands'] = [['/not-an-executable']]
    before = runner.fingerprint(cfg)
    with pytest.raises(runner.Blocked):
        runner.verify_required_tests(cfg, state, path, job)
    assert job['verification_runs'][0]['status'] == 'blocked'
    assert runner.fingerprint(cfg) == before
    assert not list((state / 'verification').iterdir())


def test_interrupted_test_attempt_not_replayed(setup):
    cfg, state, path, job = prepared(setup)
    configure(cfg, 'pass')
    job['verification_runs'] = [{'status': 'running'}]
    with pytest.raises(runner.Blocked, match='reconciliation'):
        runner.verify_required_tests(cfg, state, path, job)
    assert len(job['verification_runs']) == 1


def test_changed_reviewed_worktree_blocks_without_restoring_it(setup):
    cfg, state, path, job = prepared(setup)
    target = Path(cfg['worktree']) / 'unapproved.txt'
    configure(cfg, 'from pathlib import Path; Path(' + repr(str(target)) + ").write_text('mutation')")
    with pytest.raises(runner.Blocked, match='worktree changed'):
        runner.verify_required_tests(cfg, state, path, job)
    assert target.read_text() == 'mutation'
    assert job['verification_runs'][0]['reason'] == 'reviewed_worktree_changed'


def test_environment_does_not_forward_agent_credentials(setup, monkeypatch):
    cfg, state, path, job = prepared(setup)
    monkeypatch.setenv('GH_TOKEN', 'synthetic-secret')
    monkeypatch.setenv('API_KEY', 'synthetic-secret')
    configure(cfg, "import os; assert 'GH_TOKEN' not in os.environ; assert 'API_KEY' not in os.environ; assert 'HOME' not in os.environ")
    assert runner.verify_required_tests(cfg, state, path, job)['status'] == 'passed'


@pytest.mark.parametrize('location', ['worktree', 'public'])
def test_unsafe_scratch_rejected_without_changing_permissions(setup, tmp_path, location):
    cfg, state, path, job = prepared(setup)
    scratch = Path(cfg['worktree']) / 'scratch' if location == 'worktree' else tmp_path / 'public'
    scratch.mkdir(mode=0o700 if location == 'worktree' else 0o755)
    scratch.chmod(0o700 if location == 'worktree' else 0o755)
    configure(cfg, 'pass')
    cfg['verification']['scratch_dir'] = str(scratch)
    original_mode = scratch.stat().st_mode
    with pytest.raises(runner.Blocked):
        runner.verify_required_tests(cfg, state, path, job)
    assert scratch.stat().st_mode == original_mode


def test_escaping_symlink_is_not_executed(setup):
    cfg, state, path, job = prepared(setup)
    (Path(cfg['worktree']) / 'escape').symlink_to(state)
    runner.git(cfg, 'add', 'escape')
    job['head_commit'] = fixtures.commit(cfg)
    configure(cfg, 'pass')
    with pytest.raises(runner.Blocked):
        runner.verify_required_tests(cfg, state, path, job)
    assert 'escaping symlink' in job['verification_runs'][0]['reason']


@pytest.mark.parametrize(('output', 'counts'), [('2 passed, 1 skipped in 0.10s', {'passed': 2, 'skipped': 1}),
                                               ('Ran 2 tests in 0.1s\nOK', {'total': 2}),
                                               ('all fine', None)])
def test_counts_are_reported_or_unavailable(output, counts):
    assert runner.verification_counts(output) == counts


def test_review_only_verifies_preserved_head_without_worker_replay(setup, monkeypatch):
    cfg, state, _, _ = setup
    head = runner.git(cfg, 'rev-parse', 'HEAD')
    configure(cfg, 'print("1 passed in 0.01s")')
    review_path = runner.enqueue(cfg, 'review-only', 'Review existing work', kind='review', base=head, head=head)
    def review(config, state, path, job, role):
        assert role == 'reviewer'
        assert job['verification_runs'][0]['head'] == head
        return fixtures.result('ready_for_integration', role)
    monkeypatch.setattr(runner, 'execute', review)
    before = runner.fingerprint(cfg)
    runner.run_once(cfg, task_id='review-only')
    assert runner.read(review_path)['status'] == 'ready_for_integration'
    assert runner.read(review_path).get('runner_commits', []) == []
    assert runner.fingerprint(cfg) == before


def test_failed_test_correction_gets_fresh_head_bound_evidence(setup, monkeypatch):
    cfg, state, path, _ = setup
    configure(cfg, "from pathlib import Path; import sys; sys.exit(Path('delivery.txt').read_text().count('delivery') != 2)")
    def execute(config, state, path, job, role):
        if role == 'reviewer':
            status = 'needs_changes' if job['verification_runs'][-1]['status'] == 'failed' else 'ready_for_integration'
            return fixtures.result(status, role)
        return fixtures.execute_delivery(config, state, path, job, role)
    monkeypatch.setattr(runner, 'execute', execute)
    runner.run_once(cfg)
    job = runner.read(path)
    assert job['status'] == 'ready_for_integration'
    first, second = job['verification_runs']
    assert first['status'] == 'failed' and second['status'] == 'passed'
    assert first['head'] != second['head'] == job['reviewed_head']


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
def test_index_flags_cannot_hide_original_source_mutation(setup, flag):
    cfg, state, path, job = prepared(setup)
    repo = Path(cfg['worktree'])
    tracked = repo / 'tracked.txt'
    tracked.write_text('original')
    runner.git(cfg, 'add', 'tracked.txt')
    job['head_commit'] = fixtures.commit(cfg)
    configure(cfg, "import subprocess; from pathlib import Path; subprocess.run(['git','-C'," + repr(str(repo)) +
              ",'update-index'," + repr(flag) + ",'tracked.txt'], check=True); Path(" + repr(str(tracked)) + ").write_text('mutation')")
    with pytest.raises(runner.Blocked, match='worktree changed'):
        runner.verify_required_tests(cfg, state, path, job)
    assert tracked.read_text() == 'mutation'  # preserve evidence, never reset
    assert job['verification_runs'][0]['source_unchanged'] is False


def test_original_git_config_mutation_blocks_without_post_test_git_execution(setup, monkeypatch):
    cfg, state, path, job = prepared(setup)
    config_file = Path(cfg['worktree']) / '.git/config'
    configure(cfg, 'from pathlib import Path; p=Path(' + repr(str(config_file)) + "); p.write_text(p.read_text() + '\\n[trial]\\nchanged = true\\n')")
    original_snapshot = runner.verification_snapshot
    calls = []
    def snapshot(boundary):
        calls.append(True)
        if len(calls) == 2:
            # Boundary comparison must not consult executable Git configuration.
            monkeypatch.setattr(runner, 'trusted_git', lambda *a, **kw: pytest.fail('no Git after tests before detecting drift'))
        return original_snapshot(boundary)
    monkeypatch.setattr(runner, 'verification_snapshot', snapshot)
    with pytest.raises(runner.Blocked, match='worktree changed'):
        runner.verify_required_tests(cfg, state, path, job)
    assert 'changed = true' in config_file.read_text()


def test_original_index_only_mutation_is_detected(setup):
    cfg, state, path, job = prepared(setup)
    repo = Path(cfg['worktree'])
    (repo / 'tracked.txt').write_text('original')
    runner.git(cfg, 'add', 'tracked.txt')
    job['head_commit'] = fixtures.commit(cfg)
    configure(cfg, "import subprocess; subprocess.run(['git','-C'," + repr(str(repo)) + ",'update-index','--skip-worktree','tracked.txt'], check=True)")
    with pytest.raises(runner.Blocked, match='worktree changed'):
        runner.verify_required_tests(cfg, state, path, job)


def test_cleanup_failure_blocks_and_preserves_scratch_for_reconciliation(setup, monkeypatch):
    cfg, state, path, job = prepared(setup)
    configure(cfg, 'pass')
    real_cleanup = runner.shutil.rmtree
    retained = []
    def unavailable(folder):
        retained.append(folder)
        raise OSError('synthetic cleanup unavailable')
    monkeypatch.setattr(runner.shutil, 'rmtree', unavailable)
    try:
        with pytest.raises(runner.Blocked, match='cleanup unavailable'):
            runner.verify_required_tests(cfg, state, path, job)
        assert job['verification_runs'][0]['reason'] == 'verification_cleanup_unavailable'
        assert retained[0].is_dir()
    finally:
        for folder in retained:
            real_cleanup(folder)


def test_unrelated_branch_advance_does_not_invalidate_source_boundary(setup):
    cfg, state, path, job = prepared(setup)
    runner.git(cfg, 'branch', 'peer', job['head_commit'])
    job['head_commit'] = fixtures.commit(cfg)
    configure(cfg, "import subprocess; subprocess.run(['git','-C'," + repr(cfg['worktree']) +
              ",'update-ref','refs/heads/peer'," + repr(job['head_commit']) + "], check=True)")
    assert runner.verify_required_tests(cfg, state, path, job)['status'] == 'passed'
    assert runner.git(cfg, 'rev-parse', 'peer') == job['head_commit']


@pytest.mark.parametrize('mode', ['missing', 'nonexecutable', 'shell_string'])
def test_command_preflight_blocks_before_worker(setup, monkeypatch, tmp_path, mode):
    cfg, state, path, _ = setup
    tool = tmp_path / 'private tool'
    if mode == 'nonexecutable':
        tool.write_text('not executable')
        tool.chmod(0o600)
    cfg['verification'] = {'commands': [[str(tool)]]}
    if mode == 'shell_string':
        cfg['verification']['commands'] = [str(tool) + ' --check']
    monkeypatch.setattr(runner, 'execute', lambda *a: pytest.fail('no worker dispatch'))
    runner.run_once(cfg)
    job = runner.read(path)
    assert job['status'] in {'blocked', 'failed'}
    if mode != 'shell_string':
        assert job['status'] == 'blocked'
        receipt = runner.public_receipt(runner.read(state / 'receipts/task.json'))
        assert receipt['blocked_reason'] == {
            'reason_code': 'prerequisite_failed',
            'reason': 'Configured required check 1 program missing or not executable'}
        assert str(tmp_path) not in str(receipt)
    assert not job.get('runner_commits')


def test_private_argv_space_path_preflight_does_not_execute(setup, tmp_path, monkeypatch):
    cfg, _, _, job = setup
    tool = tmp_path / 'tool directory' / 'python'
    tool.parent.mkdir()
    tool.symlink_to(sys.executable)
    marker = tmp_path / 'must-not-exist'
    args = [str(tool), '-c', 'from pathlib import Path; Path(' + repr(str(marker)) + ').touch()']
    cfg['verification'] = {'commands': [args]}
    monkeypatch.setenv('PATH', '/unavailable-path')
    assert runner.verification_commands(cfg) == [args]
    prompt = runner.prompt_for(cfg, job, 'worker')
    assert 'TRUSTED VERIFICATION CONFIGURATION' in prompt
    assert runner.json.dumps([args]) in prompt
    assert not marker.exists()
    assert 'Exploratory commands' in prompt and 'recover and continue' in prompt
    assert 'Required checks (preflight, build, story tests and runner checks)' in prompt


def test_worker_block_preserves_dirty_work_and_receipt_without_replay(setup, monkeypatch):
    cfg, state, path, _ = setup
    calls = []
    def blocked(config, state, path, job, role):
        calls.append(role)
        fixtures.deliver(config)
        value = fixtures.result('blocked')
        value['summary'] = '/private/secret/check failed with credentials'
        return value
    monkeypatch.setattr(runner, 'execute', blocked)
    runner.run_once(cfg)
    receipt_path = state / 'receipts/task.json'
    receipt = receipt_path.read_bytes()
    public = runner.public_receipt(runner.read(receipt_path))
    assert public['blocked_reason'] == runner.block_reason('worker_reported_blocked')
    assert '/private/secret' not in str(public)
    assert 'delivery.txt' in runner.dirty_paths(cfg)
    runner.run_once(cfg)
    assert receipt_path.read_bytes() == receipt
    assert calls == ['worker']
    assert not runner.read(path).get('runner_commits')


def test_bare_program_resolved_using_path(setup, tmp_path, monkeypatch):
    cfg, _, _, job = setup
    tool = tmp_path / 'synthetic-python'
    tool.symlink_to(sys.executable)
    monkeypatch.setenv('PATH', str(tmp_path))
    cfg['verification'] = {'commands': [['synthetic-python', '-m', 'pytest']]}
    assert runner.verification_commands(cfg) == [[str(tool), '-m', 'pytest']]
    assert str(tool) in runner.prompt_for(cfg, job, 'worker')


def test_program_access_error_blocks_safely(setup, monkeypatch):
    cfg, _, _, _ = setup
    cfg['verification'] = {'commands': [[sys.executable, '-m', 'pytest']]}
    def unavailable(*args):
        raise PermissionError('synthetic private access detail')
    monkeypatch.setattr(runner.os, 'access', unavailable)
    with pytest.raises(runner.CommandUnavailable, match='required check 1') as error:
        runner.verification_commands(cfg)
    assert 'synthetic private access detail' not in str(error.value.blocked_reason)
