"""Opt-in, private local agent pool. No daemon, provider balance scraping or merges."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time

from runner import task_runner as runner


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def outside(path, roots):
    p = Path(path).resolve()
    return Path(path).is_absolute() and all(p != r and r not in p.parents for r in roots)


def load_config(path):
    cfg = runner.read(path)
    roots = [Path(cfg['repository_root']).resolve(), Path(cfg['worktree_root']).resolve()]
    if not all(Path(cfg[k]).is_absolute() and '<' not in cfg[k]
               for k in ('repository_root', 'worktree_root', 'state_dir')):
        raise ValueError('Populate absolute private pool paths')
    if not outside(cfg['state_dir'], roots) or not outside(cfg['worktree_root'], roots[:1]):
        raise ValueError('Pool state and worktrees must be outside source repository')
    if not runner.ID.fullmatch(cfg.get('base_branch', 'main')):
        raise ValueError('Safe local base branch required')
    slots = cfg['slots']
    if not isinstance(slots, list) or not slots or len(slots) > 32:
        raise ValueError('Configure between one and 32 slots')
    ids, homes = set(), []
    for slot in slots:
        if not runner.ID.fullmatch(slot['id']) or slot['id'] in ids:
            raise ValueError('Unique safe slot IDs required')
        ids.add(slot['id'])
        if not outside(slot['agent_home'], roots + [Path(cfg['state_dir']).resolve()]):
            raise ValueError('Slot home must be outside all worktrees and pool state')
        home = Path(slot['agent_home']).resolve()
        if any(home == h or home in h.parents or h in home.parents for h in homes):
            raise ValueError('Slot homes must be isolated')
        homes.append(home)
        for key in ('budget', 'phase_reserve', 'small_threshold', 'small_max_ac', 'cooldown_seconds'):
            if not integer(slot[key], 1 if key in {'budget', 'phase_reserve', 'cooldown_seconds'} else 0):
                raise ValueError('Invalid owner-set slot budget')
        if slot['phase_reserve'] > slot['budget'] or slot['small_threshold'] > slot['budget']:
            raise ValueError('Budget thresholds exceed budget')
        probe = dict(worktree=cfg['repository_root'], agent_home=slot['agent_home'], agent=slot['agent'])
        runner.validate_agent(probe)
        if any(not integer(n, 1) for n in slot.get('quota_exit_codes', [])):
            raise ValueError('Quota exit codes must be explicit positive integers')
    if cfg['mode'] not in {'pool', 'lead-and-team'} or cfg.get('lead') not in ids:
        raise ValueError('Select a mode and a configured lead')
    if not integer(cfg['max_concurrency'], 1) or cfg['max_concurrency'] > len(slots) or type(cfg['paused']) is not bool:
        raise ValueError('Invalid concurrency or pause setting')
    verification = {'commands': cfg.get('test_commands'),
                    'timeout_seconds': cfg.get('verification_timeout_seconds', 1800)}
    if 'verification_scratch_dir' in cfg:
        verification['scratch_dir'] = cfg['verification_scratch_dir']
    runner.verification_options({'verification': verification})
    seen = set()
    for story in cfg.get('stories', []):
        if not runner.ID.fullmatch(story['id']) or story['id'] in seen:
            raise ValueError('Unique story IDs required')
        seen.add(story['id'])
        if type(story['approved']) is not bool or not integer(story['priority']) or not integer(story['ac_count'], 1):
            raise ValueError('Explicit approval, priority and AC count required')
        if not isinstance(story['roadmap_section'], str) or not story['roadmap_section'].strip():
            raise ValueError('Declared roadmap section required')
        if not isinstance(story['expected_files'], list) or not story['expected_files']:
            raise ValueError('Explicit expected paths required')
        for name in story['expected_files']:
            runner.path_name(name)
        if not isinstance(story['depends_on'], list) or any(not runner.ID.fullmatch(d) or d == story['id'] for d in story['depends_on']):
            raise ValueError('Invalid dependencies')
        if story.get('force_slot') is not None and story['force_slot'] not in ids:
            raise ValueError('Forced slot does not exist')
        if not Path(story['runner_config']).is_absolute():
            raise ValueError('Absolute runner config required')
    if any(d not in seen for s in cfg.get('stories', []) for d in s['depends_on']):
        raise ValueError('Unknown dependency')
    return cfg


def overlap(a, b):
    if a['roadmap_section'] == b['roadmap_section']:
        return True
    # Globs cannot generally prove disjointness. Conservatively serialize any
    # wildcard declarations; exact files/directories use ancestor comparison.
    for x in a['expected_files']:
        for y in b['expected_files']:
            if any(c in x + y for c in '*?[') or x == y or x.startswith(y.rstrip('/') + '/') or y.startswith(x.rstrip('/') + '/'):
                return True
    return False


class Pool:
    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.root = runner.private_dir(cfg['state_dir'])
        self.path = self.root / 'pool.json'
        self.condition = threading.Condition()
        self.data = runner.read(self.path) if self.path.exists() else {'slots': {}, 'stories': {}}
        for slot in cfg['slots']:
            self.data['slots'].setdefault(slot['id'], {'used': 0, 'unknown_phases': 0, 'cooldown_until': 0, 'active': None})
        self.slots = {s['id']: s for s in cfg['slots']}
        self.stories = {s['id']: s for s in cfg.get('stories', [])}
        for name, record in self.data['stories'].items():
            if name not in self.stories or record.get('binding') != runner.digest({k: v for k, v in self.stories[name].items() if k not in {'force_slot', 'approved', 'priority'}}):
                raise ValueError('Existing story scope/config binding changed; reconcile privately')
        source = dict(worktree=cfg['repository_root'], agent_home=cfg['state_dir'], agent={'environment': {}})
        registered = [Path(row.removeprefix('worktree ')).resolve()
                      for row in runner.trusted_git(source, 'worktree', 'list', '--porcelain', '-z').decode().split('\0')
                      if row.startswith('worktree ')]
        if any(not outside(s['agent_home'], registered) for s in cfg['slots']):
            raise ValueError('Slot homes must be outside every registered worktree')
        locations = set()
        for story in self.stories.values():
            config = self.runner_config(story)
            for field in ('worktree', 'state_dir'):
                location = str(Path(config[field]).resolve())
                if location in locations or not outside(config['state_dir'], [Path(cfg['repository_root']).resolve(), Path(cfg['worktree_root']).resolve()]):
                    raise ValueError('Stories require distinct worktrees and private runner states')
                locations.add(location)
        self.main = self.git('rev-parse', cfg.get('base_branch', 'main')).strip()
        if not runner.SHA.fullmatch(self.main):
            raise ValueError('Local main revision unavailable')

    def save(self):
        self.data['updated_at'] = runner.now()
        runner.save(self.path, self.data)

    def git(self, *args, config=None):
        cfg = config or dict(worktree=self.cfg['repository_root'], agent_home=self.cfg['state_dir'], agent={'environment': {}})
        return runner.git(cfg, *args)

    def remaining(self, slot):
        return self.slots[slot]['budget'] - self.data['slots'][slot]['used']

    def available(self, slot, size):
        state, definition = self.data['slots'][slot], self.slots[slot]
        return (not state['active'] and self.clock() >= state['cooldown_until']
                and self.remaining(slot) >= definition['phase_reserve']
                and (self.remaining(slot) >= definition['small_threshold'] or size <= definition['small_max_ac']))

    def select(self, story, role='worker', worker=None):
        candidates = list(self.slots)
        if role == 'reviewer':
            if len(candidates) > 1:
                candidates.remove(worker)
            if self.cfg['mode'] == 'lead-and-team' and worker != self.cfg['lead']:
                candidates = [self.cfg['lead']]
        elif story.get('force_slot'):
            candidates = [story['force_slot']]
        candidates = [s for s in candidates if self.available(s, story['ac_count'])]
        # At equal budget the lead takes the first story and delegates the rest.
        return sorted(candidates, key=lambda s: (-self.remaining(s), s != self.cfg['lead'], s))[0] if candidates else None

    def runner_config(self, story):
        active_config = self.data['stories'].get(story['id'], {}).get('active_config', story['runner_config'])
        cfg = runner.load_config(active_config)
        self.configure_verification(cfg)
        path = Path(cfg['worktree']).resolve()
        if path.parent != Path(self.cfg['worktree_root']).resolve():
            raise ValueError('Pool job must use a dedicated owned worktree')
        if self.git('rev-parse', '--git-common-dir', config=cfg):
            # Resolve through Git itself: all pool jobs must belong to this repo.
            common = runner.trusted_git(cfg, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
            source = runner.trusted_git(dict(worktree=self.cfg['repository_root'], agent_home=self.cfg['state_dir'], agent={'environment': {}}), 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
            if common != source:
                raise ValueError('Pool job belongs to another repository')
        return cfg

    def configure_verification(self, cfg):
        cfg['verification'] = {'commands': self.cfg['test_commands'],
                               'timeout_seconds': self.cfg.get('verification_timeout_seconds', cfg.get('phase_timeout_seconds', 1800))}
        cfg['_verification_agent_homes'] = [slot['agent_home'] for slot in self.cfg['slots']]
        if 'verification_scratch_dir' in self.cfg:
            cfg['verification']['scratch_dir'] = self.cfg['verification_scratch_dir']
        runner.verification_options(cfg)

    @contextmanager
    def phase(self, story, config, job, role, admission=None):
        name = story['id']
        if job['id'] != name:
            raise runner.Blocked('Phase job is not the approved pool story')
        with self.condition:
            record = self.data['stories'][name]
            worker = record['worker_slot']
            while True:
                if self.cfg['paused'] or (self.root / 'PAUSE').exists():
                    raise runner.Blocked('Pool paused before phase')
                selected = worker if role == 'worker' and self.available(worker, story['ac_count']) else self.select(story, role, worker) if role == 'reviewer' else None
                if selected:
                    break
                # Only wait for a running phase to release an eligible slot.
                # No cooldown/budget spin and no worker lease held during wait.
                if not any(s['active'] for s in self.data['slots'].values()):
                    raise runner.Blocked('No eligible slot: budget or cooldown')
                self.condition.wait(timeout=0.2)
            definition, state = self.slots[selected], self.data['slots'][selected]
            with runner.lock(Path(definition['agent_home']) / '.agentic-pool-slot.lock') as acquired:
                if not acquired:
                    raise runner.Blocked('Slot lease is held by another coordinator')
                state['active'] = {'story': name, 'role': role}
                state['used'] += definition['phase_reserve']  # reserve before model call
                record['current_slot'] = selected
                phase_record = {'story': name, 'role': role, 'slot': selected,
                                'started_at': runner.now(), 'head': runner.git(config, 'rev-parse', 'HEAD')}
                self.data.setdefault('phases', []).append(phase_record)
                self.save()
                phase_cfg = {**config, 'agent': definition['agent'], 'agent_home': definition['agent_home'],
                             'worker_model': definition['worker_model'], 'reviewer_model': definition['reviewer_model'], 'pool_slot': selected,
                             'quota_exit_codes': definition.get('quota_exit_codes', []),
                             'pool_expected_files': story['expected_files']}
                # Release the in-process mutex during the actual model phase,
                # but retain the OS lease until the process has stopped.
                self.condition.release()
                result = None
                try:
                    if admission is not None:
                        runner.check_lifecycle_admission(*admission)
                    yield phase_cfg
                    result = None
                finally:
                    self.condition.acquire()
                    # execute returns before run_once assigns its result; an
                    # explicit phase report hook below supplies usage instead.
                    report = job.pop('_pool_report', result) or {}
                    used = report.get('usage_units')
                    if integer(used):
                        state['used'] += used - definition['phase_reserve']
                    else:
                        state['unknown_phases'] += 1
                    if report.get('rate_limited'):
                        state['cooldown_until'] = self.clock() + definition['cooldown_seconds']
                    phase_record.update(finished_at=runner.now(), usage_units=used if integer(used) else None,
                                        rate_limited=bool(report.get('rate_limited')))
                    state['active'] = None
                    record['current_slot'] = None
                    self.save()
                    self.condition.notify_all()

    def run_story(self, story, worker):
        name = story['id']
        try:
            cfg = self.runner_config(story)
            slot = self.slots[worker]
            cfg['pool_expected_files'] = story['expected_files']
            cfg.update(agent=slot['agent'], agent_home=slot['agent_home'],
                       worker_model=slot['worker_model'], reviewer_model=slot['reviewer_model'])
            task = Path(cfg['state_dir']) / 'tasks' / (name + '.json')
            job = runner.read(task)
            if job['status'] == 'queued' and job['base_commit'] != self.main:
                cfg, task, job = self.refresh_queued(story, cfg, task, job)
                self.configure_verification(cfg)
                cfg.update(agent=slot['agent'], agent_home=slot['agent_home'],
                           worker_model=slot['worker_model'], reviewer_model=slot['reviewer_model'],
                           pool_expected_files=story['expected_files'])
            runner.verify_assignment(cfg, job)
            record = self.data['stories'][name]
            if job['status'] == 'blocked' and record.get('refreshed_review'):
                reviewed = runner.read(record['refreshed_review'])
                if (reviewed['status'] != 'ready_for_integration' or reviewed.get('reviewed_head') != record['head']
                        or reviewed.get('head_commit') != record['head']):
                    raise runner.Blocked('Refreshed receipt binding changed')
                job = {**job, 'status': 'ready_for_integration', 'head_commit': record['head'],
                       'reviewed_head': record['head'], 'review': reviewed['review']}
            if job['status'].startswith('running_'):
                raise runner.Blocked('Interrupted pool phase requires owner reconciliation')
            if job['status'] not in {'queued', 'ready_for_integration'}:
                raise runner.Blocked('Job is not ready for pool execution')
            if job['status'] == 'queued':
                cfg['phase_context'] = lambda c, j, r: self.phase(story, c, j, r)
                runner.run_once(cfg, task_id=name)
                job = runner.read(task)
            with self.condition:
                record = self.data['stories'][name]
                record.update(status=job['status'], head=job.get('head_commit'))
                if job['status'] != 'ready_for_integration':
                    record['waiting'] = 'job_requires_owner'
                self.save()
            if job['status'] == 'ready_for_integration':
                self.refresh(story, cfg, task, job)
        except Exception:
            with self.condition:
                self.data['stories'][name].update(status='blocked', waiting='execution_requires_owner')
                self.save()

    def rebase(self, cfg, main):
        identity = runner.commit_identity(cfg)
        runner.trusted_git(cfg, '-c', 'rerere.enabled=false', '-c', 'rerere.autoupdate=false',
                           '-c', 'rebase.autoStash=false', '-c', 'rebase.updateRefs=false',
                           'rebase', '--no-autosquash', '--no-rebase-merges', main, extra_env={
            'GIT_COMMITTER_NAME': identity['name'], 'GIT_COMMITTER_EMAIL': identity['email']})

    def invalidate(self, cfg, path, job):
        # A main update invalidates local integration evidence, not owner
        # admission. Publishing an execution failure here revokes our own job.
        local = dict(cfg)
        local.pop('lifecycle_config', None)
        job.update(status='blocked', reviewed_head=None, updated_at=runner.now())
        runner.save_job(local, path, job)

    def refresh_queued(self, story, cfg, path, job):
        runner.verify_assignment(cfg, job)
        if cfg.get('lifecycle_config'):
            runner.check_lifecycle_admission(cfg, job)
        if runner.dirty_paths(cfg) or self.git('rev-parse', 'HEAD', config=cfg) != job['base_commit']:
            raise runner.Blocked('Queued branch changed; owner reconciliation required')
        self.invalidate(cfg, path, job)
        self.rebase(cfg, self.main)
        folder = self.root / 'rebased' / story['id'] / self.main
        config_path = folder / 'config.json'
        raw = runner.read(story['runner_config'])
        raw.update(state_dir=str(folder / 'runner-state'))
        runner.save(config_path, raw)
        fresh = runner.load_config(config_path)
        assignment = job['assignment']
        path = runner.enqueue(fresh, story['id'], assignment['task_text'],
                              github_number=job.get('github_number'), lifecycle=assignment.get('lifecycle'),
                              scopes=assignment['scopes'])
        with self.condition:
            self.data['stories'][story['id']].update(active_config=str(config_path), start_main=self.main,
                                                    queued_refresh=True)
            self.save()
        return fresh, path, runner.read(path)

    def refresh(self, story, cfg, path, job):
        current = self.git('rev-parse', self.cfg.get('base_branch', 'main')).strip()
        name = story['id']
        record = self.data['stories'][name]
        if record.get('validated_main') == current:
            return
        original_base = record['start_main']
        if current == original_base:
            with self.condition:
                record['validated_main'] = current
                self.save()
            return
        if cfg.get('lifecycle_config'):
            runner.check_lifecycle_admission(cfg, job)
        # Persist invalidation before any Git/test operation. Never let an old
        # exact-head approval survive an integration-base change or crash.
        self.invalidate(cfg, path, job)
        with self.condition:
            record.update(status='refreshing', waiting='main_changed_tests_and_review_required')
            self.save()
        if runner.dirty_paths(cfg) or self.git('rev-parse', 'HEAD', config=cfg) != record['head']:
            raise runner.Blocked('Dirty or changed branch; owner reconciliation required')
        try:
            self.rebase(cfg, current)
        except Exception:
            # Leave Git conflict state intact for the owner; never resolve/abort
            # or discard local history automatically.
            with self.condition:
                record.update(status='blocked', waiting='rebase_conflict_requires_owner')
                self.save()
            return
        new_head = self.git('rev-parse', 'HEAD', config=cfg)
        # Separate runner state and a new review-only job keep original frozen
        # assignments immutable. No lifecycle side effects from stale evidence.
        fresh_cfg = {**cfg, 'state_dir': str(self.root / 'refresh' / name / current)}
        fresh_cfg.pop('lifecycle_config', None)
        fresh_cfg['github_receipts'] = {'enabled': False}
        admission = (cfg, job) if cfg.get('lifecycle_config') else None
        fresh_cfg['phase_context'] = lambda c, j, r: self.phase(story, c, j, r, admission)
        review_path = runner.enqueue(fresh_cfg, name, job['assignment']['task_text'], kind='review', base=current, head=new_head)
        runner.run_once(fresh_cfg, task_id=name)
        review = runner.read(review_path)
        if review['status'] != 'ready_for_integration' or review.get('reviewed_head') != new_head:
            raise runner.Blocked('Post-merge independent review did not pass')
        if self.git('rev-parse', self.cfg.get('base_branch', 'main')).strip() != current:
            raise runner.Blocked('Main changed during refresh; owner reconciliation required')
        if admission is not None:
            runner.check_lifecycle_admission(*admission)
        # Project only the actual new-head review, retaining the original
        # frozen assignment. Queued/intermediate refresh state stays private.
        job.update(status='ready_for_integration', head_commit=new_head, reviewed_head=new_head,
                   review=review['review'], updated_at=runner.now())
        job.setdefault('verification_runs', []).extend(review.get('verification_runs', []))
        job.setdefault('review_history', []).append({
            'round': job['round'], 'head': new_head, 'result': review['review']})
        runner.save_job(cfg, path, job)
        with self.condition:
            record.update(status='ready_for_integration', head=new_head, validated_main=current,
                          refreshed_review=str(review_path), waiting=None)
            self.save()

    def merged(self, head, config):
        try:
            self.git("merge-base", "--is-ancestor", head, self.main, config=config)
            return True
        except runner.CommitRefused:
            return False

    def snapshot(self):
        self.main = self.git('rev-parse', self.cfg.get('base_branch', 'main')).strip()
        data = json.loads(json.dumps(self.data))
        for record in data['stories'].values():
            if record.get('status') == 'ready_for_integration' and record.get('validated_main') != self.main:
                record.update(status='refresh_required', waiting='main_changed_tests_and_review_required')
        for name, record in data['stories'].items():
            if record.get('status') == 'ready_for_integration':
                config = self.runner_config(self.stories[name])
                if self.git('rev-parse', 'HEAD', config=config) != record.get('head') or runner.dirty_paths(config):
                    record.update(status='blocked', waiting='branch_changed_requires_owner')
        return data

    def run_once(self):
        with runner.lock(self.root / 'pool.lock') as acquired:
            if not acquired:
                return
            self.main = self.git('rev-parse', self.cfg.get('base_branch', 'main')).strip()
            if self.path.exists():
                latest = runner.read(self.path)
                for name, record in latest['stories'].items():
                    if name not in self.stories or record.get('binding') != runner.digest({k: v for k, v in self.stories[name].items() if k not in {'force_slot', 'approved', 'priority'}}):
                        raise ValueError('Concurrent story binding change requires reconciliation')
                self.data = latest
                for slot in self.slots:
                    self.data['slots'].setdefault(slot, {'used': 0, 'unknown_phases': 0, 'cooldown_until': 0, 'active': None})
            if any(s['active'] for s in self.data['slots'].values()):
                raise runner.Blocked('Persisted active slot; reconcile after interrupted coordinator')
            if self.cfg['paused'] or (self.root / 'PAUSE').exists():
                self.save()
                return
            chosen, occupied = [], set()
            # Keep all unmerged assignments in the conflict set, including
            # finished branches waiting for integration and failed dirty work.
            active = [self.stories[n] for n, r in self.data['stories'].items()
                      if n in self.stories and r['status'] != 'merged' and r.get('worker_slot')]
            for story in sorted(self.stories.values(), key=lambda s: (s['priority'], -s['ac_count'], s['id'])):
                name = story['id']
                old = self.data['stories'].get(name)
                if old and old['status'] == 'ready_for_integration':
                    cfg = self.runner_config(story)
                    if self.merged(old['head'], cfg):
                        old.update(status='merged', waiting=None)
                        active = [s for s in active if s['id'] != name]
                        continue
                    if old.get('validated_main') == self.main:
                        continue
                if old and old['status'] not in {'waiting', 'ready_for_integration'}:
                    continue
                reason = None
                if not story['approved']:
                    reason = 'owner_approval_required'
                elif any(self.data['stories'].get(d, {}).get('status') != 'merged' for d in story['depends_on']):
                    reason = 'dependency_not_merged'
                elif any(s['id'] != name and overlap(story, s) for s in active):
                    reason = 'roadmap_or_files_overlap'
                elif len(chosen) >= self.cfg['max_concurrency']:
                    reason = 'concurrency_cap'
                refreshing = bool(old and old['status'] == 'ready_for_integration')
                worker = old.get('worker_slot') if old and old.get('worker_slot') else self.select(story)
                if not reason and not refreshing and (not worker or worker in occupied):
                    candidates = [s for s in self.slots if s not in occupied and self.available(s, story['ac_count'])
                                  and (not story.get('force_slot') or s == story['force_slot'])]
                    worker = sorted(candidates, key=lambda s: -self.remaining(s))[0] if candidates else None
                    if not worker:
                        reason = 'slot_budget_cooldown_or_busy'
                record = old or {'status': 'waiting', 'start_main': self.main,
                                 'binding': runner.digest({k: v for k, v in story.items() if k not in {'force_slot', 'approved', 'priority'}})}
                self.data['stories'][name] = record
                if reason:
                    record['waiting'] = reason
                    continue
                record.update(worker_slot=worker, status='scheduled', waiting=None)
                chosen.append((story, worker))
                if not refreshing:
                    occupied.add(worker)
                active.append(story)
            self.save()
            with ThreadPoolExecutor(max_workers=self.cfg['max_concurrency']) as executor:
                futures = [executor.submit(self.run_story, story, slot) for story, slot in chosen]
                for future in futures:
                    future.result()
            self.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('action', choices=['run-once', 'status'])
    args = parser.parse_args(argv)
    pool = Pool(load_config(args.config))
    if args.action == 'run-once':
        pool.run_once()
    else:
        print(json.dumps(pool.snapshot(), indent=2))


if __name__ == '__main__':
    main()
