# Local task runner (v0.3, experimental)

The primary agent explicitly queues an approved text assignment. A separate CLI worker leaves task changes uncommitted. The trusted runner validates and commits them; a fresh independent reviewer checks the exact base..head range in the configured read-only mode. At most two worker/runner-commit/review rounds run. Results wait for the primary and owner to integrate. The runner never invents work, pushes, merges, deploys, installs dependencies or controls services.

A review-only assignment skips the worker and gives the primary's committed work the same independent review. `ready_for_integration` is a structural, local outcome, not authenticated human approval or permission to merge.

## Installation and configuration

Use Python 3.11+ on macOS (launchd) or another POSIX platform (manual scheduling). There are no Python runtime dependencies. The tests use the repository's pinned development tools and fake agents only.

1. Keep a trusted copy of `runner/task_runner.py` outside the worker's writable worktree, or in an operator-controlled installation. Do not run coordinator code from a branch an agent may modify.
2. Create a dedicated feature worktree and a private state directory **outside** it. Only one runner state directory may coordinate that worktree. Pause the runner and wait for its current job to finish before using an editor or agent there.
3. Copy `runner/config.example.json` and `runner/profile.example.json` to private, uncommitted locations. Replace all angle-bracket placeholders. All configured filesystem paths are absolute; no shell expansion occurs. Set actual worker/reviewer model identifiers locally, and set `commit_identity.name` and `commit_identity.email` to the author/committer identity that the runner should use. Existing configs must add this identity before work jobs can dispatch; review-only jobs do not commit. All phases use fresh sessions, including correction rounds and later assignments. Omit `worker_start` or set it to `new`. Resume/fork and `worker_thread_id` are rejected; follow-up [#20](https://github.com/akhilvis-oss/agentic-delivery-team/issues/20) tracks a documented coordination design.
4. Set `agent_home` to a dedicated private agent home outside the worktree, separate from `state_dir`. Authenticate that CLI home yourself using your normal account workflow. Do not copy credentials into this kit. The runner does not read auth files, switch accounts, or assume an account/seat. Its agent subprocesses inherit only a small environment allowlist, apply only the explicitly configured `agent.environment`; the shipped Codex example selects this dedicated `CODEX_HOME` and ignores user CLI config; inherited provider keys, GitHub tokens, Git overrides, editor settings and `PYTHONPATH` are stripped. Configure accounts independently from the primary's environment.
5. Keep state, prompts, logs and results private and uncommitted. State directories have mode 0700; JSON, prompt, log and lock files have mode 0600. Atomic JSON replacement avoids partial records. Protect the config and profile with equivalent local permissions. The state contains task text and agent output; never publish it.
6. Start manually with the commands below. Only after checking the private configuration, optionally fill every `__PLACEHOLDER__` in `runner/launchd.plist.example`. Choose a unique service label, trusted script/Python paths, private config and private stdout/stderr log paths. `Umask` 63 is octal 077. Validate your filled template using `plutil -lint`, then install it in your own LaunchAgents directory and use `launchctl bootstrap gui/$(id -u) <your-plist>` if you choose. The kit does not install/start/stop a service. Do not reuse another project's service label or state directory.

The profile owns `worker_prompt`, `reviewer_prompt`, default `scope`, named `extra_scopes`, exact allowed uncommitted `coordination_files`, and optional `commit_policy` limits/additional deny patterns. Optional scopes require an explicit `--scope NAME` at enqueue, apply to the worker only, and cannot authorize bypassing sandbox/approvals or the runner's universal restrictions. Use an empty coordination list unless status-only local files really need an exception. No globs are interpreted in that list. The runner commits permitted non-coordination changes before review. Remove commit instructions from project profiles and task briefs, including instructions to leave the final delivery uncommitted. The runner supplies the phase-specific handoff policy itself; see the fixed lifecycle notes below.

The queue saves the full profile, selected scopes, task text, its SHA-256, original base commit, job kind, optional review head, dependency, worktree and branch in an integrity-bound assignment. Later task-document/profile edits cannot change queued work. The hash detects accidental edits; it is not a signature against a malicious local operator who can rewrite state. A dependent job keeps its original assignment baseline and records its exact reviewed predecessor baseline separately.

## Fixed phase lifecycle notes

The runner always appends a fixed lifecycle note to every phase prompt, after all profile/scope/task text, correction findings and worker claims. Profiles cannot remove, configure or override this runner policy; project instructions appear alongside it. The note is generic across agents and models:

- **Worker:** leave all task changes uncommitted; do not stage, commit or change history or Git administrative files. The trusted runner validates paths and commits after the worker exits. This is a worker handoff rule, not a final delivery requirement.
- **Reviewer:** for work jobs, the runner has committed the worker's changes before review. A runner-made commit and clean worktree are expected inputs, not findings. Judge the exact base..head range against the assignment. A review-only job supplies existing commits and runs no worker phase.

These notes are included in the first round, every correction round, and review-only prompts. Adopters should describe implementation, tests, review checks and project scope in profiles and tasks **without repeating commit instructions**. The example profile's prompt placeholders need no lifecycle wording. Older saved assignments retain their text, but receive the current fixed note when dispatched; remove conflicting wording from new briefs rather than asking an agent to alter history. The note makes instruction precedence explicit; prompt assembly cannot guarantee model obedience. The runner's commit validation, exact-range checks and reviewer fingerprint remain the enforcement mechanisms.

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

## Agent contract and configuration

Any agent can be configured if it can run non-interactively in a given folder, accept a prompt file or stdin, write a final JSON answer to a runner-named file, and exit nonzero on failure. A phase succeeds only after **zero process exit, a final answer passing the runner's complete schema, and the existing Git checks**. The runner saves stdout/stderr as private opaque logs; extra text, unknown events, or missing event streams never determine success. There are no per-agent parsers or code paths. Each job records `agent-file-v1` and the full matched version output.

The `agent` object contains:

| Field | Contract |
| --- | --- |
| `command` | Nonempty argv array; first argument is an absolute executable path. Never a shell command. |
| `version_command` | Nonempty argv array probing the version without a model call; absolute executable, 10-second timeout. |
| `version_pattern` | Regex matched against the **entire stripped stdout** of a successful version probe. Empty, mismatched or unavailable output blocks before dispatch. |
| `worker_flags`, `reviewer_flags` | Optional argv arrays inserted at one standalone `{role_flags}` argument. Reviewer flags must select the agent's read-only mode when available. |
| `environment` | Optional explicit mapping for agent home/auth selection. Inherited provider keys are stripped. No automatic credential discovery, reading or forwarding. Standard HOME/PATH, Python path and Git/loader overrides cannot be replaced here. |

Command/flag placeholders are `{worktree}`, `{agent_home}`, `{model}`, `{prompt_file}`, `{output_file}`, and `{schema_file}`. Version/environment templates accept only `{worktree}` and `{agent_home}`. Formatting, conversion, attribute access and unknown placeholders are rejected. Arguments stay separate even with spaces or shell metacharacters. Literal braces must be doubled. Stdin always contains the prompt; a file-based agent may use `{prompt_file}` instead. The prompt also names the final-answer and schema files, so agents without native schema flags can still implement the contract. The runner validates independently.

`runner/config.example.json` ships a Codex definition using `exec`, stdin, `--output-schema`, `--output-last-message`, automatic worker approval review and read-only reviewer flags. Its version pattern accepts only **codex-cli 0.164.0-alpha.11.1**, preserving the prior compatibility envelope; this is not an expansion of the accepted CLI range. No model identifier is supplied by the kit. Other agents require locally configured commands/permissions/version patterns and their own verification; no compatibility with an untested agent is claimed.

### Official interface findings (issue #18, AC-1)

On 7 November 2026, the installed CLI reported `codex-cli 0.164.0-alpha.11.1`. Before code changes, the [official non-interactive guide](https://learn.chatgpt.com/docs/non-interactive-mode) and [CLI reference](https://learn.chatgpt.com/docs/cli/reference) were checked alongside `codex exec --help`, `codex exec resume --help` and `codex exec fork --help`. The public documentation describes JSONL stdout from `--json` (including thread/turn events), stdin prompts, the working directory/model options, `--output-last-message`, `--output-schema` and resume commands. It mentions persisted rollout files and `--ephemeral`, but no supported persisted-file schema or reliable busy-marker contract was found for `session_meta`, `task_started`, `task_complete` or timestamps. A full numeric exit-code taxonomy was not established; this runner only uses the generic zero/nonzero contract, which an agent must meet. No particular error code is interpreted.

Implementation paused at that gap. The [owner's approved scope decision](https://github.com/akhilvis-oss/agentic-delivery-team/issues/18#issuecomment-6702641296) defers resume/fork AC-3 to [#20](https://github.com/akhilvis-oss/agentic-delivery-team/issues/20). **The runner never opens or enumerates agent session files**, and never reads legacy `worker_thread.json`. Only its own lock, process ownership/wait and crash records coordinate work. Codex JSON completion events are optional output, not required signals under amended AC-4. The shipped example therefore does not request `--json`.

### Migration and CLI upgrades

Stop the runner and reconcile any active/interrupted job before updating. Replace old `codex_bin`/`codex_home` settings with `agent` and `agent_home` from the example, populate private executable paths and model/identity placeholders, remove `worker_thread_id`, and remove or set `worker_start: new`. Legacy configurations are rejected rather than silently reinterpreted. Do not delete job/receipt records; old managed-thread files are simply unused. This configuration migration and removal of resume/fork are approved scope for #18.

Whenever the agent CLI version changes, first review its official flags and failure/output contract, then run **one throwaway live job and independently inspect its review, runner-made commit, final job JSON and receipt before resuming real work**. Use private state/home outside the disposable repo, GitHub receipts off, no launchd and no other projects/services. Require `ready_for_integration` and a reviewer verdict; capture evidence and delete the disposable data. A version regex match or synthetic tests alone are insufficient. Version-policy evolution remains follow-up #15.

## Safety, recovery and limits

Before dispatch, the runner checks that configured agent, version-probe and
verification programs exist and are executable. Verification preflight resolves
bare program names through the runner's PATH; absolute executable paths are
recommended for reproducibility. Repository-relative programs are checked against
the assigned worktree and retain their relative spelling in the disposable test
checkout. This preflight does not execute test commands or prove that modules,
arguments or dependencies are available. The existing CLI version probe still runs.

Commands are JSON argument arrays, never shell strings. A path with spaces is one
argument, without embedded shell quotes, for example:

```json
{"verification": {"commands": [["/opt/example tools/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]]}}
```

Workers receive clearly labelled private trusted verification argv before
implementation, including the configured executable (or its PATH resolution).
The runner uses the same resolution for its checks. Keep argv out of public
evidence. Missing tools block safely; the runner never installs dependencies.

The built-in worker notes require stopping on failed or unavailable required
checks: preflight, build, story tests and runner checks. Exploratory searches,
file lookups and listings may fail normally; recover and continue. Avoid the
unqualified instruction **"stop on any failure"** in task text: it can cause a
worker to stop on a harmless lookup. Required-check failure behavior is unchanged.

- A nonblocking `flock` serializes the entire execution/review/correction cycle. `PAUSE` in the private state directory prevents new jobs; an existing phase continues. `CANCEL` stops the runner's current owned process group and prevents new jobs. Branch/HEAD changes block. A worker changing history, or reporting delivery without committable changes, blocks. Every successful correction gets its own runner-made commit.
- Each phase has a configured 60–3600 second timeout. The runner creates a separate process group and terminates/reaps only that newly owned group on timeout, cancellation, failure or completion, including descendants. It never signals a PID recovered from disk. SIGTERM performs owned cleanup; SIGKILL/power loss cannot. An unclean `running_*` record becomes `interrupted`, is never replayed, and blocks further dispatch until the operator reconciles it.
- After a crash, inspect private task/phase records, reconcile the worktree, and verify that the old agent and descendants have stopped. Do not use a stale recorded PID as proof of ownership. Only then run `acknowledge-interruption --id <TASK_ID>` with the same `--config`. This records manual reconciliation; it neither kills anything nor requeues the task. Queue a **new ID** if further work is authorized. Never delete records to force a retry.
- The shipped Codex worker uses workspace-write with automatic approval review. The reviewer uses `--sandbox read-only` and `--ask-for-approval never`. No bypass flags or additional writable directories are used. Treat the CLI/OS sandbox as the write-prevention boundary; the runner is not an independent security sandbox. Native integrations and host policies need their own controls. Project instructions and operator configuration are trusted inputs.
- The worktree fingerprint checks HEAD, branch, tracked diffs, index state and every untracked/ignored file (symlinks are hashed as links) before/after review, even if the reviewer fails. Unexpected mutation rejects review. This detects content changes inside the worktree, not effects elsewhere on the machine, every metadata or change-and-restore operation, or changes to the Git administrative directory. Isolation varies by agent. Use a dedicated macOS user for agent automation to limit its access to unrelated files, credentials and services; configure equivalent OS isolation on other platforms. Large ignored datasets make it expensive; use a small dedicated worktree, with data/state elsewhere.
- Review results must match the complete schema. Missing/malformed final answers, bad field types, nonzero exit or findings accompanying `ready_for_integration` can never approve. Correction rounds are bounded at two; unresolved findings stop for the primary. Filesystem errors or a damaged queue fail closed; writable storage is necessary to persist receipts.
- The runner lock and process ownership coordinate cooperating processes; they cannot stop an unrelated editor, another runner using a different state directory, a malicious local user, or a daemon that deliberately escapes its process group. Do not work manually in the worktree during dispatch. No automatic production actions or merges are authorized by a successful receipt.

## Receipts

Every enqueue/state update writes `receipts/<task-id>.json`, including queued, deferred, blocked, interrupted and failed outcomes. Version 2 contains status, base/head commits (head may be null until delivery), a fixed runner-owned summary, a UTC timestamp and the private commit/refusal records described below. Full task and agent results remain private in `tasks/` and `runs/`.

GitHub receipt comments default **off**. Enabling `github_receipts.enabled` and explicitly setting `github_receipts.repository` opts into posting a terminal receipt only for a job enqueued with `--github-number <ISSUE_OR_PR_NUMBER>`. Authenticate the separate `gh` CLI locally. Its client sends an argument-vector API request, never a shell command. Only the original five public receipt fields, optional runner commit SHA/`committed_by: runner` metadata and the safe blocked reason described below are rendered; refusal paths, task text, prompts, logs, model summaries, config and environment are never copied into comments. The public summary is a fixed runner message, not potentially sensitive agent prose.

Blocked receipts add `blocked_reason` with a machine-readable `reason_code` and a
short `reason`. Codes include `prerequisite_failed`, `worker_reported_blocked` and
`reviewer_reported_blocked`. Runner-authored reasons identify command preflight
(including the configured check's one-based index), agent compatibility,
assignment safety or runner required checks. Worker/reviewer reports use fixed
messages; their detailed check names and explanations remain private and are
never inferred from arbitrary worker text. Public receipts and the factual
stand-up validate reasons against the runner's safe vocabulary, falling back to
a generic safety-preflight reason for unknown or altered data. This change does
not enable scheduled stand-up.

If a blocked worker leaves uncommitted changes, preserve its assignment, private
results and receipts. The owner inspects the changes and the failing prerequisite
before authorizing any further round with a new job ID and explicit scope. Use a
separately exported approved release as the tool. A new work job requires a clean
worktree: the owner must explicitly decide whether to preserve reviewed changes
on a separate branch/commit or discard them after retaining evidence. The runner
does not adopt dirty changes, rewrite blocked results, reset jobs to queued or
silently replay workers. Review-only recovery below applies when a preserved
worker commit already exists; it cannot adopt an uncommitted worker diff.

A failed/uncertain comment does not change the local execution result or erase its receipt. Delivery is marked attempted **before** sending and is not retried automatically, preventing duplicate comments after a crash/ambiguous response. Inspect GitHub before any manual retry. The fake-client tests verify payload boundaries, default-off behavior and failures; this PR does not post a live receipt or install launchd.

## Synthetic verification

`tests/test_runner.py` covers bounded corrections, delays, exact reviewed dependencies, review-only jobs, immutable assignments, private receipts, cancellation/timeouts with descendant cleanup, stale phase output, crash reconciliation, exclusive locks and reviewer mutation detection. `tests/test_runner_commits.py` covers runner-owned commit/refusal behavior, concurrent changes and safe Git operations.

Issue #18 replaces earlier rollout-parser tests with assertions that fresh jobs **never open or enumerate agent storage**, including malformed synthetic session files and stale managed-thread files. A non-Codex fake CLI exercises prompt-file input, a custom version command/pattern, different role flags, two fresh correction/review rounds and runner-made commits without any agent-specific code. Extra/missing/malformed stdout is opaque. Missing/malformed/incomplete final answers, nonzero exit even with a valid answer, contradictory status, invalid configuration and resume/fork requests cannot approve. Existing Git integrity, recovery and concurrency tests remain in place.

Run `python -m pytest -q tests/test_runner.py tests/test_runner_commits.py` for the focused suite, or the repository's full checks from README. Fixtures are synthetic; no test launches a model, uses real credentials or accesses another project's state or services. Historic session/fork test correspondence from #13 is superseded by the owner's fresh-only decision; that behavior is not claimed as supported.

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
validates and commits them. The sandbox boundary and example CLI version acceptance remain unchanged by the subsequent #18 configuration migration.

## Commit validation and operator migration

The agent command's sandbox flags are unchanged. The worker leaves files uncommitted and reports `ready_for_review` after implementation and tests. The trusted runner then:

1. Requires a clean starting index/worktree except for explicitly allowed coordination files (ignored new files are not candidates). This prevents committing unrelated operator work. It refuses absent/placeholder runner identity before dispatch. It verifies the configured repository root, branch and expected HEAD, both after the worker returns and just before publication. Worker-made commits or branch changes block even if the worker reports blocked.
2. Collects tracked modifications/deletions and new untracked files, excluding ignored and coordination paths. Tracked files newly matched by ignore rules are excluded too; if they remain dirty, the exact-review cleanliness check will block/fail review rather than silently include them.
3. Validates repository-relative paths, protects `.git` components (including case variants), rejects parent-directory symlinks, and checks final symlink targets. Internal final symlinks may be committed as links; external/invalid targets and targets in protected/denied locations are refused. All submodules/gitlinks are conservatively unsupported, including internal ones; the runner never follows or initializes their Git pointers. Special files are unsupported. Safe regular-file reads use no-follow directory/file descriptors and bounded snapshots; final revalidation rejects changed inputs.
4. Applies the built-in deny patterns `.env*`, `*secret*`, `*credential*`, `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*`, `*.pem`, `*.key`, `*.p12`, `*.pfx` to the whole path and its components, case-insensitively. `commit_policy.deny_patterns` adds project-specific patterns; defaults cannot accidentally be removed. Defaults: `max_files: 100`, `max_file_bytes: 1048576`, `max_total_bytes: 10485760`. All limits are configurable positive integers. The example's extra deny placeholder should be replaced or removed locally.
5. Builds an isolated index from the expected parent and snapshots. It hashes raw bytes without attribute conversions, disables include-defined filters, external diff/textconv drivers, hooks/signing/fsmonitor and inherited global executable Git config, and never stages unrelated index entries. It publishes to the explicit configured branch with an old-SHA compare-and-swap, then installs the resulting index. Existing Git lock files cause a refusal; the runner never deletes another process's locks. As elsewhere, unrelated editors/processes must not concurrently mutate the worktree; these checks do not replace the OS sandbox or cooperation.
6. Creates the fixed message `runner: job <ID> kind <KIND> round <N>`, with both author and committer taken from `commit_identity`. Each correction is a separate child commit. A new independent reviewer inspects the original base through the latest runner-made head, with the existing fingerprint/schema checks.

This changes the local worker contract and adds required configuration for work jobs. Keep runner source outside the agent's writable worktree. The effective `core.excludesFile` is resolved through a data-only config read (with local/include precedence and the default XDG/HOME ignore path preserved), then explicitly passed to sanitized Git operations. Executable global config is not re-enabled. Git is already a runtime prerequisite; no new Python dependency, CLI range extension, permission override or automatic service installation is added. Deny/size validation cannot detect every secret in arbitrary allowed content: operator scoping, repository-specific checks and owner review remain required.

## Machine-readable commit records for the stand-up

Private `tasks/<job-id>.json` and `receipts/<job-id>.json` have `schema_version: 2` and the same two arrays:

| Array / fields | Meaning and digest use |
| --- | --- |
| `runner_commits[]`: `commit_sha`, `job_id`, `job_kind`, `round`, `file_count`, `committed_by`, `timestamp`, `state` | `committed_by` is always `runner`. Only records with `state: committed` belong in “Done since last digest”. Deduplicate by `(job_id, round, commit_sha)` and use `timestamp` for the reporting window. |
| `commit_refusals[]`: `job_id`, `job_kind`, `round`, `reason`, `paths`, `timestamp` | Refusals block the job. These belong in “Needs your decision”, deduplicated by job/round/reason/timestamp. `paths` are offending repository-relative names (or rejected malformed input); a history/identity/lock refusal may have no paths. Keep them private. |

Reason codes include `outside_or_invalid_path`, `git_metadata`, `denied_path`, `external_or_invalid_symlink`, `symlink_parent`, `denied_symlink_target`, `submodule_not_supported`, `submodule_or_directory_not_supported`, `special_file_not_supported`, `file_count_limit`, `file_size_limit`, `total_size_limit`, `history_changed`, `concurrent_change`, `preexisting_staged_changes`, `preexisting_worktree_changes`, `configure_runner_identity`, `no_committable_changes`, `git_lock_busy`, `git_operation_failed`, `git_configuration_unavailable`, `worktree_must_be_repository_root`, and `unreadable_or_changed_path`. New readers should tolerate additional reason codes and keep the job blocked.

Before ref publication, a commit's intent is persisted as `state: prepared`. After publication it becomes `committed`; a failed ref lease becomes `not_published`. A crash may leave a prepared SHA whose publication is uncertain: show it under “Needs your decision”, compare the recorded SHA with actual repository history during manual reconciliation, and never rerun or report it as done automatically. If publication succeeded but index refresh fails, the committed record survives and the job fails safely before review. JSON updates are atomic, but Git publication and receipt updates are not one filesystem transaction.

These files are a stable input contract, not an implementation of the stand-up digest. Local `status` now includes private refusal metadata; do not paste it publicly. The GitHub client receives a separately filtered object: fixed status/summary/timestamp/range plus committed SHA and `committed_by` only. Prepared records, refusals, file paths, counts, task text and agent output are never passed to it.

## Post-merge live acceptance (issue #18, AC-8; issue #16, AC-9)

The draft's pre-merge evidence uses fake agents and temporary repositories. AC-8 (also completing #16 AC-9) remains explicitly **NOT RUN until the owner merges #18**. Then use the runner from that merged `main`, fill a local config/profile and identity, and run the same single greeting-file/focused-test assignment in a fresh disposable local repository. Use private state/agent home outside it, GitHub receipts off, no launchd, no other projects/services, and unchanged CLI sandbox flags. The required outcome is `ready_for_integration`, a logged `committed_by: runner` commit and a reviewer verdict. Capture job JSON, receipt and commit evidence, then delete repository, state, agent home/auth copy and control files. Do not substitute a branch-only or mocked run for this acceptance criterion. No release tag is authorized by this test.

## Post-merge lifecycle trial (issue #23, AC-5)

After the owner merges #23, run the trusted runner from that merged `main` in a new disposable local repository. Use a minimal profile and task whose text contains **no commit instructions or even commit wording**; only the built-in notes should explain the handoff. Enqueue one small function with one focused standard-library test. Require `ready_for_integration`, a recorded runner-made commit and a passing fresh review. Keep private state and agent home outside the worktree, GitHub receipts off, no launchd, and no other projects/services. Capture the task/profile, generated phase prompts, job JSON, queued/final receipts, commit metadata and reviewer verdict as evidence. Then delete the throwaway repo, state, agent home/auth copy and control files. This live trial is NOT RUN until the owner merges; synthetic prompt tests do not replace it.

## Optional v0.4 issue lifecycle

See [lifecycle and digest setup](v04-lifecycle-digest.md). Adding a private absolute `lifecycle_config` opts a runner into event-bound service dispatch, canonical P0/P1/P2 findings, private review history and synchronized metadata-only issue records. Standalone v0.3 dispatch remains available without this option. New lifecycle jobs allow an initial delivery plus at most two correction rounds; review-only jobs still never run a correction worker. The lifecycle worker schema adds a nullable structured `disagreement`; the default standalone worker schema is unchanged. All review outputs now use the canonical severity enum. Historical critical/high/medium/low/minor map to P0/P1/P1/P2/P2; unknown severity requires primary review rather than silent downgrading. Do not rewrite old receipts or reinterpret an old verdict as approval of a new head.

## Runner-owned required tests

For standalone jobs, optional private config `verification` enables runner-owned
checks before every reviewer dispatch. Existing configs without this key retain
their behavior; the pool always supplies its required `test_commands`. For example:

```json
{"verification": {"commands": [["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider"]], "timeout_seconds": 1800}}
```

Commands are explicit argv arrays (no shell interpolation); use an absolute executable
when needed. Do not put secrets in argv or output. Each review attempt gets a private,
disposable independent Git clone checked out at the exact reviewed SHA. It has its own
index and copied objects, no origin, user hooks/config or alternate object database,
and excludes untracked and ignored source files. Tests can create fixtures, cache and
bytecode in this copy without granting the reviewer writes. Submodules and symlinks
escaping the copy are unsupported and block. Locally installed dependencies must be
available through the configured executable: untracked virtual environments and ignored
artifacts are not copied or installed automatically.

The runner sets temporary-directory variables to private scratch and removes agent
credentials and user Git configuration from the environment. Default scratch is
`verification` under private job state. Optional `verification.scratch_dir` must be an
existing private directory owned by the current user, outside registered worktrees and
agent home. A missing explicit directory is never created or replaced with a fallback.
Unavailable space, invalid scratch, launch failure, cancellation, timeout (1–3,600
seconds per command) and cleanup failure yield `blocked`. The runner stops its owned
process group and cleans scratch after each attempt. Interrupted attempts remain
recorded and require owner reconciliation; they are never silently replayed.

Required tests execute **builder-written code**, including tests, `conftest.py` and
imported modules, **outside the agent sandbox**, with the runner OS user's full
access and network. This is the same trust as running an AI-written pull request's
tests on your own machine. Approving `pytest` does not constrain the code it loads.
Environment filtering and disposable copies do not prevent access to files or
Keychain items readable by that user (including through `/usr/bin/security`), or
network use. The supported pool setup uses a dedicated OS user with no Keychain
items or GitHub credentials; the lifecycle service's App keys stay under a different
user inaccessible to the pool user. See [pool setup](agent-pool.md#private-setup).
An optional test sandbox is [follow-up #73](https://github.com/akhilvis-oss/agentic-delivery-team/issues/73), not part of this change.
The runner snapshots actual source bytes/types/modes and relevant Git index, HEAD
and configuration before and after tests, without trusting index flags or Git diff.
Unrelated branch refs can advance concurrently. A change blocks without resetting or hiding it. This protects normal fixture/cache
writes by isolation and detects checkout drift, rather than containing hostile code.
It cannot undo disclosure or other host/network effects.

Private job JSON and local receipts contain `verification_runs`: head, round, command
set hash, timestamps, per-command argv, status/exit code, output hash and bounded output
tail, plus reported pytest counts or unittest total. Other count formats remain null;
the runner never infers zero tests or independent coverage from an unknown summary.
Full output is kept privately in `test-runs`. The strictly read-only reviewer receives
the corresponding evidence as data and must assess sufficiency and failures without
rerunning required tests. A reviewer PASS cannot override a failed required command.
Review findings can request the existing bounded correction cycle, which runs new
checks on the new head. Verification is never inferred from worker claims.

Raw verification records are excluded from automatic public GitHub receipts: argv and
output can contain private paths. Publish a manually sanitized receipt with paths and
private output removed. Main-refresh reviews have separate state; their actual test
records also appear in the original job receipt after successful refresh, alongside
the earlier head records. Readiness still requires the fresh cross-slot review and
current-main/admission checks. No reviewer permission flags change for this feature.

### Recovery of blocked pool reviews

Preserve the old blocked job, immutable assignment, receipts and worker commit. After
an owner merges and releases this fix, export that new tagged tool separately and
record its release SHA; never patch the pinned old export or use the source worktree
as the tool. In a new private pool/job state, explicitly enqueue a **review-only**
assignment (`--kind review --base <ORIGINAL_BASE_SHA> --head <PRESERVED_WORKER_SHA>`)
against the clean preserved worktree. Configure the pool's base to that original base
for this initial exact-range review and retain the recorded builder slot via
`force_slot`; lead-and-team assigns its independent reviewer to the other slot.
Do not reset the old job to queued, remove its blocked receipt, or replay its worker.
The new receipt must show zero worker phases/commits, required-test evidence at the
preserved head and the independent verdict. After that review passes, advance the
configured local base to current main and use normal main-refresh/review before
integration. If the pool cannot honor these constraints, stop and report the limitation.
