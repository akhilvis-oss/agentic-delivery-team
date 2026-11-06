# Local task runner (v0.3, experimental)

The primary agent explicitly queues an approved text assignment. A separate CLI worker leaves task changes uncommitted. The trusted runner validates and commits them; a fresh independent reviewer checks the exact base..head range in a read-only sandbox. At most two worker/runner-commit/review rounds run. Results wait for the primary and owner to integrate. The runner never invents work, pushes, merges, deploys, installs dependencies or controls services.

A review-only assignment skips the worker and gives the primary's committed work the same independent review. `ready_for_integration` is a structural, local outcome, not authenticated human approval or permission to merge.

## Installation and configuration

Use Python 3.11+ on macOS (launchd) or another POSIX platform (manual scheduling). There are no Python runtime dependencies. The tests use the repository's pinned development tools and fake agents only.

1. Keep a trusted copy of `runner/task_runner.py` outside the worker's writable worktree, or in an operator-controlled installation. Do not run coordinator code from a branch an agent may modify.
2. Create a dedicated feature worktree and a private state directory **outside** it. Only one runner state directory may coordinate that worktree. Pause the runner and wait for its current job to finish before using an editor or agent there.
3. Copy `runner/config.example.json` and `runner/profile.example.json` to private, uncommitted locations. Replace all angle-bracket placeholders. All configured filesystem paths are absolute; no shell expansion occurs. Set actual worker/reviewer model identifiers locally, and set `commit_identity.name` and `commit_identity.email` to the author/committer identity that the runner should use. Existing configs must add this identity before work jobs can dispatch; review-only jobs do not commit. Optional `worker_start` is `new` (default), `fork` or `resume`; fork/resume also needs `worker_thread_id` pointing to a recognized inactive session in the dedicated home.
4. Set `codex_home` to a dedicated private agent home outside the worktree, separate from `state_dir`. Authenticate that CLI home yourself using your normal account workflow. Do not copy credentials into this kit. The runner does not read auth files, switch accounts, or assume an account/seat. Its agent subprocesses inherit only a small environment allowlist, use this dedicated `CODEX_HOME`, and ignore user CLI config; inherited provider keys, GitHub tokens, Git overrides, editor settings and `PYTHONPATH` are stripped. Configure accounts independently from the primary's environment.
5. Keep state, prompts, logs and results private and uncommitted. State directories have mode 0700; JSON, prompt, log and lock files have mode 0600. Atomic JSON replacement avoids partial records. Protect the config and profile with equivalent local permissions. The state contains task text and agent output; never publish it.
6. Start manually with the commands below. Only after checking the private configuration, optionally fill every `__PLACEHOLDER__` in `runner/launchd.plist.example`. Choose a unique service label, trusted script/Python paths, private config and private stdout/stderr log paths. `Umask` 63 is octal 077. Validate your filled template using `plutil -lint`, then install it in your own LaunchAgents directory and use `launchctl bootstrap gui/$(id -u) <your-plist>` if you choose. The kit does not install/start/stop a service. Do not reuse another project's service label or state directory.

The profile owns `worker_prompt`, `reviewer_prompt`, default `scope`, named `extra_scopes`, exact allowed uncommitted `coordination_files`, and optional `commit_policy` limits/additional deny patterns. Optional scopes require an explicit `--scope NAME` at enqueue, apply to the worker only, and cannot authorize bypassing sandbox/approvals or the runner's universal restrictions. Use an empty coordination list unless status-only local files really need an exception. No globs are interpreted in that list. The runner commits permitted non-coordination changes before review. Rewrite old project prompts that ask the worker to commit: the worker must never stage, commit, request `.git` write access, or change history.

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

- A nonblocking `flock` serializes the entire execution/review/correction cycle. `PAUSE` in the private state directory prevents new jobs; an existing phase continues. `CANCEL` stops the runner's current owned process group and prevents new jobs. Branch/HEAD changes block. A worker changing history, or reporting delivery without committable changes, blocks. Every successful correction gets its own runner-made commit.
- Each phase has a configured 60–3600 second timeout. The runner creates a separate process group and terminates/reaps only that newly owned group on timeout, cancellation, failure or completion, including descendants. It never signals a PID recovered from disk. SIGTERM performs owned cleanup; SIGKILL/power loss cannot. An unclean `running_*` record becomes `interrupted`, is never replayed, and blocks further dispatch until the operator reconciles it.
- After a crash, inspect private task/phase records, reconcile the worktree, and verify that the old agent and descendants have stopped. Do not use a stale recorded PID as proof of ownership. Only then run `acknowledge-interruption --id <TASK_ID>` with the same `--config`. This records manual reconciliation; it neither kills anything nor requeues the task. Queue a **new ID** if further work is authorized. Never delete records to force a retry.
- A confirmed owned timeout/cancel may clear only that worker's old busy marker, and only when no later rollout timestamp exists. Missing timestamps, unknown lifecycle data or an unconfirmed crash never clear the guard. A later resume invalidates the old stop evidence.
- The worker uses workspace-write with automatic approval review. The reviewer uses `--sandbox read-only` and `--ask-for-approval never`. No bypass flags or additional writable directories are used. Treat the CLI/OS sandbox as the write-prevention boundary; the runner is not an independent security sandbox. Native integrations and host policies need their own controls. Project instructions and operator configuration are trusted inputs.
- The worktree fingerprint checks HEAD, branch, tracked diffs, index state and every untracked/ignored file (symlinks are hashed as links) before/after review, even if the reviewer fails. Unexpected mutation rejects review. This detects content changes, not every metadata or change-and-restore operation, and does not protect the Git administrative directory. Large ignored datasets make it expensive; use a small dedicated worktree, with data/state elsewhere.
- Review results must match the complete schema. Missing fields, bad types, failed/incomplete lifecycle output or findings accompanying `ready_for_integration` can never approve. Correction rounds are bounded at two; unresolved findings stop for the primary. Filesystem errors or a damaged queue fail closed; writable storage is necessary to persist receipts.
- Busy metadata and flock coordinate cooperating processes; they cannot stop an unrelated editor, another runner using a different state directory, a malicious local user, or a daemon that deliberately escapes its process group. Do not work manually in the worktree during dispatch. No automatic production actions or merges are authorized by a successful receipt.

## Receipts

Every enqueue/state update writes `receipts/<task-id>.json`, including queued, deferred, blocked, interrupted and failed outcomes. Version 2 contains status, base/head commits (head may be null until delivery), a fixed runner-owned summary, a UTC timestamp and the private commit/refusal records described below. Full task and agent results remain private in `tasks/` and `runs/`.

GitHub receipt comments default **off**. Enabling `github_receipts.enabled` and explicitly setting `github_receipts.repository` opts into posting a terminal receipt only for a job enqueued with `--github-number <ISSUE_OR_PR_NUMBER>`. Authenticate the separate `gh` CLI locally. Its client sends an argument-vector API request, never a shell command. Only the original five public receipt fields and optional runner commit SHA/`committed_by: runner` metadata are rendered; refusal paths, task text, prompts, logs, model summaries, config and environment are never copied into comments. The public summary is a fixed runner message, not potentially sensitive agent prose.

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
| 8: an exact committed delivery is required before review (runner commits it under issue #16) | `test_unexpected_worker_commit_blocks_review`, `test_runner_commit_identity_message_and_private_journal` |
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

## Runner-owned commits: confirmed cause (issue #16, AC-1)

Before implementation on 6 November 2026, `codex --version` reported
`codex-cli 0.164.0-alpha.11.1`. `codex exec --help` describes `--approve-for-me`
as automatic approval review using the workspace-write sandbox; it lists
`--sandbox` modes and separate additional-directory and bypass options.
The [official protected-path documentation](https://learn.chatgpt.com/docs/agent-approvals-security#protected-paths-in-writable-roots)
explains that `.git` (including a resolved worktree pointer) stays read-only under
writable roots. [Sandbox documentation](https://learn.chatgpt.com/docs/sandboxing#approve-for-me)
confirms that automatic approval review retains that filesystem boundary.
The [CLI reference](https://learn.chatgpt.com/docs/cli/reference) documents the
matching flags. No sandbox widening is needed or authorized for this fix.

This confirms the documented cause of the reported `.git/index.lock` blocker:
workspace file edits do not grant Git-administrative writes. The earlier trial's
worker did not issue a recorded Git commit command, so it does not independently
prove a failed system call. Nor do these sources establish the permissions of any
reference project's resumed thread. The verified policy matches the owner's
option-B decision: the agent leaves files uncommitted, and only the trusted runner
validates and commits them. Agent permissions and the CLI version range stay unchanged.

## Commit validation and operator migration

The agent command's sandbox flags are unchanged. The worker leaves files uncommitted and reports `ready_for_review` after implementation and tests. The trusted runner then:

1. Requires a clean starting index/worktree except for explicitly allowed coordination files (ignored new files are not candidates). This prevents committing unrelated operator work. It refuses absent/placeholder runner identity before dispatch. It verifies the configured repository root, branch and expected HEAD, both after the worker returns and just before publication. Worker-made commits or branch changes block even if the worker reports blocked.
2. Collects tracked modifications/deletions and new untracked files, excluding ignored and coordination paths. Tracked files newly matched by ignore rules are excluded too; if they remain dirty, the exact-review cleanliness check will block/fail review rather than silently include them.
3. Validates repository-relative paths, protects `.git` components (including case variants), rejects parent-directory symlinks, and checks final symlink targets. Internal final symlinks may be committed as links; external/invalid targets and targets in protected/denied locations are refused. All submodules/gitlinks are conservatively unsupported, including internal ones; the runner never follows or initializes their Git pointers. Special files are unsupported. Safe regular-file reads use no-follow directory/file descriptors and bounded snapshots; final revalidation rejects changed inputs.
4. Applies the built-in deny patterns `.env*`, `*secret*`, `*credential*`, `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*`, `*.pem`, `*.key`, `*.p12`, `*.pfx` to the whole path and its components, case-insensitively. `commit_policy.deny_patterns` adds project-specific patterns; defaults cannot accidentally be removed. Defaults: `max_files: 100`, `max_file_bytes: 1048576`, `max_total_bytes: 10485760`. All limits are configurable positive integers. The example's extra deny placeholder should be replaced or removed locally.
5. Builds an isolated index from the expected parent and snapshots. It hashes raw bytes without attribute conversions, disables configured filters/hooks/signing/fsmonitor and inherited global Git config, and never stages unrelated index entries. It publishes to the explicit configured branch with an old-SHA compare-and-swap, then installs the resulting index. Existing Git lock files cause a refusal; the runner never deletes another process's locks. As elsewhere, unrelated editors/processes must not concurrently mutate the worktree; these checks do not replace the OS sandbox or cooperation.
6. Creates the fixed message `runner: job <ID> kind <KIND> round <N>`, with both author and committer taken from `commit_identity`. Each correction is a separate child commit. A new independent reviewer inspects the original base through the latest runner-made head, with the existing fingerprint/schema checks.

This changes the local worker contract and adds required configuration for work jobs. Keep runner source outside the agent's writable worktree. Git is already a runtime prerequisite; no new Python dependency, CLI range extension, permission override or automatic service installation is added. Deny/size validation cannot detect every secret in arbitrary allowed content: operator scoping, repository-specific checks and owner review remain required.

## Machine-readable commit records for the stand-up

Private `tasks/<job-id>.json` and `receipts/<job-id>.json` have `schema_version: 2` and the same two arrays:

| Array / fields | Meaning and digest use |
| --- | --- |
| `runner_commits[]`: `commit_sha`, `job_id`, `job_kind`, `round`, `file_count`, `committed_by`, `timestamp`, `state` | `committed_by` is always `runner`. Only records with `state: committed` belong in “Done since last digest”. Deduplicate by `(job_id, round, commit_sha)` and use `timestamp` for the reporting window. |
| `commit_refusals[]`: `job_id`, `job_kind`, `round`, `reason`, `paths`, `timestamp` | Refusals block the job. These belong in “Needs your decision”, deduplicated by job/round/reason/timestamp. `paths` are offending repository-relative names (or rejected malformed input); a history/identity/lock refusal may have no paths. Keep them private. |

Reason codes include `outside_or_invalid_path`, `git_metadata`, `denied_path`, `external_or_invalid_symlink`, `symlink_parent`, `denied_symlink_target`, `submodule_not_supported`, `submodule_or_directory_not_supported`, `special_file_not_supported`, `file_count_limit`, `file_size_limit`, `total_size_limit`, `history_changed`, `concurrent_change`, `preexisting_staged_changes`, `preexisting_worktree_changes`, `configure_runner_identity`, `no_committable_changes`, `git_lock_busy`, `git_operation_failed`, `git_configuration_unavailable`, `worktree_must_be_repository_root`, and `unreadable_or_changed_path`. New readers should tolerate additional reason codes and keep the job blocked.

Before ref publication, a commit's intent is persisted as `state: prepared`. After publication it becomes `committed`; a failed ref lease becomes `not_published`. A crash may leave a prepared SHA whose publication is uncertain: show it under “Needs your decision”, compare the recorded SHA with actual repository history during manual reconciliation, and never rerun or report it as done automatically. If publication succeeded but index refresh fails, the committed record survives and the job fails safely before review. JSON updates are atomic, but Git publication and receipt updates are not one filesystem transaction.

These files are a stable input contract, not an implementation of the stand-up digest. Local `status` now includes private refusal metadata; do not paste it publicly. The GitHub client receives a separately filtered object: fixed status/summary/timestamp/range plus committed SHA and `committed_by` only. Prepared records, refusals, file paths, counts, task text and agent output are never passed to it.

## Post-merge live acceptance (issue #16, AC-9)

The draft's pre-merge evidence uses fake agents and temporary repositories. AC-9 remains explicitly **NOT RUN until the owner merges this fix**. Then use the runner from that merged `main`, fill a local config/profile and identity, and run the same single greeting-file/focused-test assignment in a fresh disposable local repository. Use private state/agent home outside it, GitHub receipts off, no launchd, no other projects/services, and unchanged CLI sandbox flags. The required outcome is `ready_for_integration`, a logged `committed_by: runner` commit and a reviewer verdict. Capture job JSON, receipt and commit evidence, then delete repository, state, agent home/auth copy and control files. Do not substitute a branch-only or mocked run for this acceptance criterion. No release tag is authorized by this test.
