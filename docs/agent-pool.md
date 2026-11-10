# Local agent pool (v0.5, experimental)

The pool is an opt-in coordinator over the existing bounded runner. Each approved
story has a dedicated feature branch, registered worktree, runner config and state.
Pool mode accepts any command-line agent implementing the runner's version and
structured-result contract. Lead-and-team uses the same engine: the lead builds
some stories, delegates others, reviews delegated work, and receives independent
review from another slot for its own work. Pool and lead-and-team were [live-tested with two Codex slots](agent-pool-live-trial.md);
other vendors are supported by configuration, not a compatibility claim.

**Lifecycle refresh:** after an owner-authorized correction round, main changes invalidate integration evidence locally without publishing a false execution failure. The existing lifecycle admission guard still runs before rebasing queued work, immediately before refreshed review and before publishing its result. Real-policy integration tests cover queued replacements, repeated completed refreshes and revoked admission. This conditional path was tested with a synthetic GitHub transport, not live App credentials.

## Private setup

**Supported setup: a dedicated OS user for the pool.** Limit that user's access to
the project and the tool, agent, state and scratch files it needs. It must have no
Keychain items and no GitHub credentials (including `gh` login, tokens, SSH keys or
credential-helper entries). Keep the lifecycle service and its App private keys
under a **different OS user**; the pool user must not be able to read that user's
files or Keychain. Configure filesystem permissions accordingly. Separate agent
homes and bot identities do not provide this OS-user separation. Account creation
and permission changes are owner-operated; the pool does not provision them.

Required tests execute **builder-written code**, including tests, `conftest.py` and
imported modules, **outside the agent sandbox**, with the runner OS user's full
access and network connectivity. This is the same trust as running an AI-written
pull request's tests on your own machine. Approving a command such as `pytest`
does not approve or constrain all the code it loads. A filtered environment and a
disposable copy do not prevent access to files, credentials or Keychain items that
the runner user can read, including through `/usr/bin/security`, or network use.
Source-integrity checks detect changes after execution; they cannot undo disclosure
or other host/network effects. An optional test sandbox is tracked in [#73](https://github.com/akhilvis-oss/agentic-delivery-team/issues/73)
and is not provided by this change. See also [Security](../SECURITY.md#using-the-kit-safely).

Copy `runner/pool.example.json` outside every repository and worktree. Keep its
directory 0700 and files 0600. Fill in placeholders locally. It contains commands,
agent homes and budgets and must never be committed. Existing standalone runner
configs keep working without this file; no migration, daemon or service is installed.

Each slot has a unique ID, agent definition, models and isolated agent home outside
**every registered worktree**, the source repository and pool state. Supply accepted
version patterns and genuinely read-only reviewer flags for your CLI. Every phase
starts a fresh process; no session files, resume or fork are used. A slot is a local
execution resource, not proof of a separate human account or model independence.

For unattended Codex automation, prefer API keys with owner-controlled provider
spending limits. The [official authentication documentation](https://learn.chatgpt.com/docs/auth?surface=cli)
recommends API keys for automation. If using per-person subscriptions, use one seat
per real person; this kit does not recommend one person buying multiple seats for
agents. Authenticate each dedicated home yourself; never put credentials in this
example, a task, a command argument, or public evidence. The kit never discovers
provider balances from dashboards, sessions or undocumented endpoints.

Create one dedicated worktree and private runner config per story using the normal
[runner setup](task-runner.md), then explicitly enqueue its task under the same ID
as the pool story. `approved: true` is the owner's explicit local authorization.
Set it only after approval; task text cannot change this setting. For GitHub lifecycle
approval, first use `python -m lifecycle.service --config '<PRIVATE_LIFECYCLE_CONFIG>'
enqueue-only` to make authenticated event-bound claims and jobs without launching
them. Point each pool story at its generated private runner config and claim/job ID.
The pool cannot replace or override lifecycle admission. Stop standalone job runners
for these same configs before enabling the pool; no existing service is stopped for
you. Enqueue-only and pool commands are manual.

Declare priority (smaller first), AC count, roadmap section, expected repository
paths and dependency IDs. Unknown dependencies are rejected; cyclic dependencies
remain waiting for owner correction. Expected files are also enforced by the trusted
runner before committing. Wildcards conservatively conflict with every other story;
prefer exact files or directory prefixes. Same-section or overlapping-file stories
wait until the earlier work is merged. A failed/dirty assignment still reserves its
scope until private owner reconciliation; approval is never inferred from labels.
Do not edit an admitted story's scope/config in place; use a new explicit assignment.

## Run and control

From the kit's trusted source checkout:

```sh
python -m runner.pool --config '<PRIVATE_POOL_CONFIG>' run-once
python -m runner.pool --config '<PRIVATE_POOL_CONFIG>' status
```

One invocation selects a bounded batch in priority order and runs it concurrently.
Run again to drain more work. `mode` is `pool` or `lead-and-team`; `lead` names a
configured slot. Set `max_concurrency` from 1 to the number of slots, `paused: true`
or create `PAUSE` in pool state, and optionally set a story's `force_slot`. Config
changes apply to the next invocation; PAUSE is checked again before each phase.
A pause prevents new phases; it does not kill an existing phase. Use the existing
per-job CANCEL mechanism for bounded cancellation. Forcing never bypasses approval,
conflicts, dependencies, budget, cooldown, review independence or the concurrency cap.

OS locks enforce one active model phase/job per slot and one coordinator per pool.
Worker slot assignment is recorded. A worker releases its slot before waiting for a
reviewer, so two simultaneous builds can exchange reviewer slots without deadlock.
With multiple slots the reviewer must use a different slot; with one slot it uses
the existing fresh read-only fallback. Lead-and-team delegates reviews to the lead
except that the lead's own work is reviewed by another slot. A correction reacquires
the original worker slot; the existing bounded correction policy stays in force.
Never point two independent pool state directories at the same slot homes.

## Budgets and availability

All budget values are owner-defined integer units, such as micro-dollars or usage
allowance units. They are not a live provider-credit balance or a billing guarantee.
`budget` is a cap for this pool ledger's lifetime; `phase_reserve` is charged before
each phase. Use a conservative reserve and provider-enforced spend limits. When an
agent reports `usage_units`, the ledger replaces the reservation with that reported
value, including an overrun. Otherwise it retains the reserve and increments an
explicit unknown-usage count. No usage report means **unavailable**, not zero cost.
Structured reporting is untrusted agent evidence, not an independent billing audit.

Large stories prefer slots with more remaining budget. Below `small_threshold`, a
slot only takes stories at or below `small_max_ac`. A phase needs enough remaining
budget for its reservation. The same limits apply to review. A structured blocked
result with `rate_limited: true` makes the slot unavailable for `cooldown_seconds`.
CLI adapters may normalize a documented quota error to an explicitly configured
`quota_exit_codes` value. Generic process failures are not guessed to be quota errors
and raw stderr is never published. The failed job requires explicit reconciliation;
other ready assignments can use the slot once cooldown expires. No automatic replay
of a failed/interrupted model phase occurs.

## Main changes and integration

The owner must update the **local** configured main after a merge; the pool never
fetches, pushes, merges PRs or changes remote refs. `status` invalidates readiness
when local main differs from `validated_main`. A subsequent run rebases a clean,
unchanged completed branch, reruns every explicit `test_commands` argument array in a disposable copy,
and runs a fresh cross-slot read-only review of the new exact main-to-head range.
Clean queued branches are rebased and receive new immutable assignments in separate state; original frozen assignments and review receipts are retained. Refreshed review jobs
use separate private state. The pool's `validated_main`, current head and refreshed
review receipt are the integration evidence; an old original-job approval is invalid.

Before **every** review (initial, correction, review-only and main refresh), the runner executes
`test_commands` in a fresh independent Git checkout of the reviewed head. The reviewer
retains its configured read-only sandbox and receives the runner's recorded evidence; it
does not rerun required tests in the reviewed checkout. Private job and receipt records
include the exact head, argv, exit status, timestamps, output hash/tail and reported test
counts. Unrecognized counts remain unavailable. Failed required tests cannot become
integration readiness through a reviewer PASS.

Tests receive a filtered environment that omits agent credentials and user Git
configuration variables; this does not restrict filesystem, Keychain or network access.
The timeout defaults to the job phase timeout (1,800 seconds); optional pool keys
`verification_timeout_seconds` (1–3,600) and `verification_scratch_dir` configure it.
By default scratch is created under each private runner state directory. An explicit
scratch directory must already exist, be owned by the current user, have mode 0700,
and be outside every registered worktree and agent home. Unavailable scratch, inability
to launch tests, cancellation, timeout or failed cleanup returns `blocked`, without
launching the reviewer. See [runner verification and recovery](task-runner.md#runner-owned-required-tests).
These commands execute builder-written code with the runner user's access outside
the agent sandbox. Use the dedicated OS-user setup above; the copy is not an OS
sandbox. Never give test commands credentials. A rebase conflict is left intact
and blocks for the owner: no resolution, reset, abort, force-push or dirty-worktree
discard is automatic. Test failure, branch drift, unavailable reviewers, quota and
another main change during refresh also block readiness. Integration detection uses Git ancestry; squashed or cherry-picked work needs owner reconciliation. Check main again immediately
before integration. This is procedural evidence; it does not add a new required
GitHub check or grant merge authority. After tests and a fresh review pass, the coordinator re-checks admission and current main, then updates the original job/receipt and publishes the trusted exact-head review through the existing lifecycle outbox. Intermediate invalidation stays local. A revocation prevents that publication. Check the resulting evidence before integration.

## Stand-up and recovery

Add absolute private pool config paths to the digest's optional `pool_configs` list.
The factual stand-up includes each safe slot ID, busy/idle/cooldown state, current
story, budget used and unknown usage count, plus fixed waiting reasons for blocked
stories. It publishes no commands, homes, config paths, logs or provider responses.
The digest performs no model calls for pool status and never changes pool scheduling.

After a coordinator crash, a persisted active lease or interrupted job blocks rather
than replaying. Inspect live processes yourself, reconcile worktree/job/slot state and
retain the ledger before any new assignment. No process ID from disk is signalled.
The pool never removes a story or worktree automatically. After a trial, verify all
phases stopped, retain sanitized evidence, then remove only its throwaway repository,
worktrees, homes and private state; keep unrelated credentials and services intact.
