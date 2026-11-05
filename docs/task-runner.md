# Local task runner (v0.3, experimental)

The primary agent explicitly queues an approved text assignment. A separate CLI worker makes local commits; a fresh independent reviewer checks the exact base..head range in a read-only sandbox. At most two worker/review rounds run. Results wait for the primary and owner to integrate. The runner never invents work, pushes, merges, deploys, installs dependencies or controls services.

A review-only assignment skips the worker and gives the primary's committed work the same independent review. `ready_for_integration` is a structural, local outcome, not authenticated human approval or permission to merge.

## Installation and configuration

Use Python 3.11+ on macOS (launchd) or another POSIX platform (manual scheduling). There are no Python runtime dependencies. The tests use the repository's pinned development tools and fake agents only.

1. Keep a trusted copy of `runner/task_runner.py` outside the worker's writable worktree, or in an operator-controlled installation. Do not run coordinator code from a branch an agent may modify.
2. Create a dedicated feature worktree and a private state directory **outside** it. Only one runner state directory may coordinate that worktree. Pause the runner and wait for its current job to finish before using an editor or agent there.
3. Copy `runner/config.example.json` and `runner/profile.example.json` to private, uncommitted locations. Replace all angle-bracket placeholders. All configured filesystem paths are absolute; no shell expansion occurs. Set actual worker/reviewer model identifiers locally. Optional `worker_start` is `new` (default), `fork` or `resume`; fork/resume also needs `worker_thread_id` pointing to a recognized inactive session in the dedicated home.
4. Set `codex_home` to a dedicated private agent home outside the worktree, separate from `state_dir`. Authenticate that CLI home yourself using your normal account workflow. Do not copy credentials into this kit. The runner does not read auth files, switch accounts, or assume an account/seat. Its agent subprocesses inherit only a small environment allowlist, use this dedicated `CODEX_HOME`, and ignore user CLI config; inherited provider keys, GitHub tokens, Git overrides, editor settings and `PYTHONPATH` are stripped. Configure accounts independently from the primary's environment.
5. Keep state, prompts, logs and results private and uncommitted. State directories have mode 0700; JSON, prompt, log and lock files have mode 0600. Atomic JSON replacement avoids partial records. Protect the config and profile with equivalent local permissions. The state contains task text and agent output; never publish it.
6. Start manually with the commands below. Only after checking the private configuration, optionally fill every `__PLACEHOLDER__` in `runner/launchd.plist.example`. Choose a unique service label, trusted script/Python paths, private config and private stdout/stderr log paths. `Umask` 63 is octal 077. Validate your filled template using `plutil -lint`, then install it in your own LaunchAgents directory and use `launchctl bootstrap gui/$(id -u) <your-plist>` if you choose. The kit does not install/start/stop a service. Do not reuse another project's service label or state directory.

The profile owns `worker_prompt`, `reviewer_prompt`, default `scope`, named `extra_scopes` and exact allowed uncommitted `coordination_files`. Optional scopes require an explicit `--scope NAME` at enqueue, apply to the worker only, and cannot authorize bypassing sandbox/approvals or the runner's universal restrictions. Use an empty coordination list unless status-only local files really need an exception. No globs are interpreted in that list. Non-coordination changes must be committed before review.

The queue saves the full profile, selected scopes, task text, its SHA-256, original base commit, job kind, optional review head, dependency, worktree and branch in an integrity-bound assignment. Later task-document/profile edits cannot change queued work. The hash detects accidental edits; it is not a signature against a malicious local operator who can rewrite state. A dependent job keeps its original assignment baseline and records its exact reviewed predecessor baseline separately.

## Commands

Substitute private absolute paths and full 40-character commit SHAs. Commands here are examples, not automatic actions.

```sh
python runner/task_runner.py --config '<PRIVATE_CONFIG_JSON>' enqueue \
  --id small-feature --task '<APPROVED_TASK_TEXT_FILE>'

python runner/task_runner.py --config '<PRIVATE_CONFIG_JSON>' enqueue \
  --id later-feature --task '<APPROVED_TASK_TEXT_FILE>' \
  --delay-seconds 3600 --after-task small-feature --scope optional-check

python runner/task_runner.py --config '<PRIVATE_CONFIG_JSON>' enqueue \
  --id primary-review --kind review --task '<REVIEW_BRIEF_TEXT_FILE>' \
  --base '<FULL_BASE_SHA>' --head '<FULL_HEAD_SHA>'

python runner/task_runner.py --config '<PRIVATE_CONFIG_JSON>' run-once
python runner/task_runner.py --config '<PRIVATE_CONFIG_JSON>' status
```

A review-only job requires HEAD at the supplied review head, with base an ancestor. It never runs a worker or correction worker, and does not accept optional worker scopes or dependencies. Findings end as `needs_primary_review`; a fresh explicit assignment is needed after corrections.

IDs are unique lowercase letters/digits/hyphen/underscore, at most 80 characters. Reusing an ID is rejected, even for failed jobs. Delays are 0–86400 seconds and make no model calls (not even a CLI probe) before eligibility. The queue is deterministic by ID; waiting dependencies do not automatically re-order work or launch unrelated tasks. A dependency must be `ready_for_integration`, carry a valid successful review of its recorded head, and match the current **clean** worktree exactly. There is no automatic rebase or cherry-pick.

## Compatibility and busy detection

The adapter records `codex-jsonl-v1` and the observed CLI version in the job. Its deliberately narrow supported range is **0.164.0-alpha.11.1 inclusive to 0.164.0-alpha.11.2 exclusive**. Flags were checked using local `--version` / `exec --help` / `exec resume --help` / `exec fork --help`; protocol and recovery tests use synthetic fixtures. No real model run or live account compatibility is claimed by these tests. Extending the range requires reviewing CLI flags, updating lifecycle fixtures and independently reviewing the change; do not widen it just to skip a block.

A due job first probes `--version` without a prompt/model call. Unsupported, malformed or unavailable versions block with a reason in the private task JSON. Busy detection reads only session/lifecycle metadata in the dedicated home. Recognized rollouts require timezone-aware timestamps, session identity, matching supported `cli_version`, a worktree and known lifecycle records. The first session metadata identifies a fork; appended parent metadata does not replace it. Active tasks defer. Partial JSON, unknown lifecycle records or timestamps fail closed and block **before** starting an agent. Check the task JSON's `error`; `status` deliberately shows safe receipts rather than private logs.

A process can emit an unexpected format only after it starts; such output blocks/fails that phase and can never approve or start the next phase. This is separate from detecting pre-existing unknown session data before a model call. CLI JSON event order must identify a thread, start a turn and complete it, with no failed/unknown events. Worker identities are recorded for explicit future resume; seed/managed sessions must exist in the dedicated home's recognized metadata for that worktree. Fresh reviewers never resume or fork a worker context.

Interface references: [official non-interactive CLI documentation](https://learn.chatgpt.com/docs/non-interactive-mode) and [CLI option reference](https://learn.chatgpt.com/docs/cli/reference). These describe the public interface, not a promise that internal rollout formats are stable.

## Safety, recovery and limits

- A nonblocking `flock` serializes the entire execution/review/correction cycle. `PAUSE` in the private state directory prevents new jobs; an existing phase continues. `CANCEL` stops the runner's current owned process group and prevents new jobs. Branch/HEAD changes block; delivery without a new local commit fails. Every correction must add a new commit.
- Each phase has a configured 60–3600 second timeout. The runner creates a separate process group and terminates/reaps only that newly owned group on timeout, cancellation, failure or completion, including descendants. It never signals a PID recovered from disk. SIGTERM performs owned cleanup; SIGKILL/power loss cannot. An unclean `running_*` record becomes `interrupted`, is never replayed, and blocks further dispatch until the operator reconciles it.
- After a crash, inspect private task/phase records, reconcile the worktree, and verify that the old agent and descendants have stopped. Do not use a stale recorded PID as proof of ownership. Only then run `acknowledge-interruption --id <TASK_ID>` with the same `--config`. This records manual reconciliation; it neither kills anything nor requeues the task. Queue a **new ID** if further work is authorized. Never delete records to force a retry.
- A confirmed owned timeout/cancel may clear only that worker's old busy marker, and only when no later rollout timestamp exists. Missing timestamps, unknown lifecycle data or an unconfirmed crash never clear the guard. A later resume invalidates the old stop evidence.
- The worker uses workspace-write with automatic approval review. The reviewer uses `--sandbox read-only` and `--ask-for-approval never`. No bypass flags or additional writable directories are used. Treat the CLI/OS sandbox as the write-prevention boundary; the runner is not an independent security sandbox. Native integrations and host policies need their own controls. Project instructions and operator configuration are trusted inputs.
- The worktree fingerprint checks HEAD, branch, tracked diffs, index state and every untracked/ignored file (symlinks are hashed as links) before/after review, even if the reviewer fails. Unexpected mutation rejects review. This detects content changes, not every metadata or change-and-restore operation, and does not protect the Git administrative directory. Large ignored datasets make it expensive; use a small dedicated worktree, with data/state elsewhere.
- Review results must match the complete schema. Missing fields, bad types, failed/incomplete lifecycle output or findings accompanying `ready_for_integration` can never approve. Correction rounds are bounded at two; unresolved findings stop for the primary. Filesystem errors or a damaged queue fail closed; writable storage is necessary to persist receipts.
- Busy metadata and flock coordinate cooperating processes; they cannot stop an unrelated editor, another runner using a different state directory, a malicious local user, or a daemon that deliberately escapes its process group. Do not work manually in the worktree during dispatch. No automatic production actions or merges are authorized by a successful receipt.

## Receipts

Every enqueue/state update writes `receipts/<task-id>.json`, including queued, deferred, blocked, interrupted and failed outcomes. It contains only status, base/head commits (head may be null until delivery), a fixed runner-owned summary and a UTC timestamp. Full task and agent results remain private in `tasks/` and `runs/`.

GitHub receipt comments default **off**. Enabling `github_receipts.enabled` and explicitly setting `github_receipts.repository` opts into posting a terminal receipt only for a job enqueued with `--github-number <ISSUE_OR_PR_NUMBER>`. Authenticate the separate `gh` CLI locally. Its client sends an argument-vector API request, never a shell command. Only the same five receipt fields are rendered; task text, prompts, logs, model summaries, config and environment are never copied into comments. The public summary is a fixed runner message, not potentially sensitive agent prose.

A failed/uncertain comment does not change the local execution result or erase its receipt. Delivery is marked attempted **before** sending and is not retried automatically, preventing duplicate comments after a crash/ambiguous response. Inspect GitHub before any manual retry. The fake-client tests verify payload boundaries, default-off behavior and failures; this PR does not post a live receipt or install launchd.

## Test correspondence

The following maps all **20 reference test functions (23 current parameterized cases)**, in source order, to their generalized equivalents in `tests/test_runner.py`. Ordinals and safety concerns identify reference tests without copying reference-project domain terminology or names. Parameterized cases are preserved or strengthened (unknown rollout data now blocks rather than being treated as idle).

| Reference ordinal and guarantee | Ported test |
| --- | --- |
| 1: bounded repair, independent review, no duplicate | `test_bounded_repair_then_independent_review_and_no_duplicate` |
| 2: delayed eligibility without calls | `test_delayed_job_waits_without_spending_model_calls` |
| 3: correction limit | `test_retry_limit` |
| 4: busy thread deferral | `test_busy_thread_defers_task` |
| 5: changed HEAD blocks | `test_head_change_blocks_not_silent_rebase` |
| 6: crash is not replayed | `test_crashed_run_not_replayed` |
| 7: exclusive runner, pause | `test_single_runner_and_pause` |
| 8: uncommitted code blocks review | `test_uncommitted_code_blocks_review` |
| 9: review-time mutation detected | `test_review_mutation_detected` |
| 10: isolated auth and command permissions | `test_auth_isolation_and_permissions` |
| 11: real subprocess, fake agent structured protocol | `test_real_subprocess_structured_protocol` |
| 12: timeout/cancel stop owned process | `test_timeout_and_cancel_stop_owned_process` |
| 13: missing completion fails | `test_missing_completion_event_is_failure` |
| 14: invalid review cannot approve | `test_invalid_review_cannot_approve` |
| 15: optional scope explicit and worker-only | `test_optional_scope_is_explicit_and_worker_only` |
| 16: dependency waits for review | `test_dependent_task_waits_for_reviewed_predecessor` |
| 17: dependency uses exact reviewed clean head | `test_dependency_uses_only_exact_reviewed_clean_head` |
| 18: dependency rejects unrelated changes | `test_dependency_rejects_unrelated_worktree_changes` |
| 19: fork identity and confirmed owned-stop evidence | `test_fork_metadata_and_reaped_timeout_do_not_block_next_task` |
| 20: unknown lifecycle timestamps never clear busy guard | `test_unknown_lifecycle_timestamps_never_clear_busy_guard` |

The issue calls this the “21 reference tests”; the supplied file currently has **20 functions and 23 parameterized cases**. The table enumerates the actual functions rather than inventing a 21st function. The timeout/cancel test maps both reference cases to the same two ported parameter cases; additional parametrizations preserve the other stop/dependency variants. New tests additionally cover immutable assignment/profile/IDs, review-only behavior, CLI/version/session guards, sanitized receipts/fake posting, private permissions, descendants, stale results, ignored/index fingerprints, setup placeholders and CLI/config validation.

Run `python -m pytest -q tests/test_runner.py` for the focused suite, or the repository's full checks from README. No test launches an actual model, uses real credentials, or accesses any reference-project service/state/worktree.
