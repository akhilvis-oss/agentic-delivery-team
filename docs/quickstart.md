# Quickstart

Ask your agent to follow `INSTALL.md` (or `UPGRADE.md`) at the release tag, then run the checker.

Read the [install runbook](../INSTALL.md) for a new project and the [upgrade runbook](../UPGRADE.md)
for an existing one. Both use the release's [carried-file manifest](../release-files.json),
install every carried file, and preserve adopter-owned settings. Features that are off
remain installed but asleep. The trusted lifecycle switch is checked after checkout;
`false` skips the guard, `true` or a missing kit record keeps it on, and invalid data
fails closed. The other switches remain conventions: agents are told to read and respect it.

## Manual setup

A manual installation follows the same ordered `INSTALL.md` steps and the same checker.
There is no separate file list or level-based setup path. The owner handles GitHub Apps,
interactive Keychain entry, settings imports and any private service authorization.
Keep the MIT notice with the code. See [review categories](review-categories.md),
[runner setup](task-runner.md), [pool setup](agent-pool.md) and
[lifecycle and stand-up](v04-lifecycle-digest.md) for the feature-specific fixed steps.

## Upgrading

Use `UPGRADE.md` at the target release; verify its tag and the `Release commit:` line
in its notes, update every framework file from that tag, and apply only the exact
adopter migrations the notes list. The checker runs from a fresh target export and
reads the committed default branch. Private installs and repository copies are both
required; a main-track service does not update the adopting repository's files.

For a public repository, the owner imports `templates/actions-policy-pull-request-target.json`
from the release at **Settings → Actions → Policies → New policy → Import a ruleset**.
It allows `issues` and `pull_request_target` only for the trusted review-gate and lifecycle
workflow paths. Never allow those events for arbitrary workflows. Required checks need
a public repository or a paid GitHub plan. Keep this policy step in every upgrade plan
when the release notes require it.

A framework service or an adopting project can opt into automatic `main`
upgrades; release-tag upgrades are not yet supported. For a service whose own
repository is the framework, add a private `upgrade` object (omit it to stay off):

```json
{"track": "main", "smoke_agent": "example-agent", "versions_dir": "/absolute/private/versions", "initial_commit": "0123456789abcdef0123456789abcdef01234567"}
```

Use the launcher's exact versions folder; symlinks and trailing slashes resolve
to the same folder. `initial_commit` is the last manually pinned commit and stays
the running baseline until the first confirmation. With neither framework field
set, reads and exports use the configured service repository.
Only `serve` checks for upgrades. In this mode an idle poll requires the newest main commit's
successful `smoke/<smoke_agent>` status from `actors.runner`, read using a separate
runner status-only token, and a Product-token comparison proving it is ahead.
Active claims, finished claims with reconciliation errors and pending switches
all delay upgrades. Read/export outages retry while claims continue normally.
In both modes, checks start at least ten minutes apart, measured from the start
of the last check that reached a GitHub read, whatever its outcome. Local skips
for claims or a pending switch do not start that interval; restarting the service
allows an earlier check.

The service exports into `<versions_dir>/<commit>/install`, removes the download,
and runs `upgrade-check` in an isolated Python child with a 120-second limit.
That action imports code, loads config and mints configured App-role tokens;
it never starts an agent. Temporary GitHub failures (exit 75) retry. Unsafe
archives, definite check failures and check timeouts refuse that commit.
Passing checks save an atomic pending switch and exit cleanly for launchd.
The candidate ignores the saved cursor and confirms after reads and status
publication succeed, before any claim or job. Failed publication runs no jobs.
Upgrade and rollback notices persist in the status comment.

Install the launcher and replacement plist once as described in the
[lifecycle guide](v04-lifecycle-digest.md#optional-owner-run-macos-login-setup).
The launcher rolls back at the fourth unconfirmed start, or a start at least ten
minutes after the first. Under `serve`, a pending version starts one daemon timer
at its first poll, after validation, state binding and upgrade-context reads.
It exits with code 70 at `pending_since` plus 600 whole wall-clock seconds, without
writing `service-error.json`; an empty start time gets the full ten minutes from
timer startup. An already-expired deadline exits in the polling thread before
the rest of that poll. Confirmation cancels the timer only after the atomic
switch write, before claims or jobs. launchd's `KeepAlive` restarts the launcher,
which rolls back and records "Rolled back from <commit>: did not confirm".
A GitHub outage lasting ten minutes straight after an upgrade can therefore
roll back and refuse a good commit; investigate, then retry with `upgrade-clear`.
Hangs before the timer starts, including validation, state binding and context
reads, or native code holding the interpreter lock still need an owner restart
after the stale-heartbeat alert; the launcher then rolls back. The launcher does
not supervise the process. Keep watchdog and smoke-watch
jobs on a fixed approved install, rarely updated by hand. An unreadable switch
runs the first install and turns upgrades off until repaired or removed. Folder
mismatches also disable upgrades, with an explanatory status-comment notice.

For an adopting project, add both framework fields together (or omit both):

```json
{"track": "main", "smoke_agent": "<AGENT_NAME>", "versions_dir": "/absolute/private/versions", "initial_commit": "0123456789abcdef0123456789abcdef01234567", "framework_repository": "<FRAMEWORK_OWNER>/<FRAMEWORK_REPOSITORY>", "framework_runner": "<FRAMEWORK_RUNNER_BOT_LOGIN>"}
```

These placeholders match `runner/smoke.example.json`. `framework_repository` may
be any public repository, including the service's own. `smoke_agent` is the
framework smoke watcher's agent name, not the adopting project's agent.
`initial_commit` is the framework commit used for the adopting project's first
install, at or after the merge that introduced this adopter mode. Install the
launcher as described above. Main, smoke-status and comparison reads use a new
GET-only public client with no token; the successful status must come from
`framework_runner`, and the export comes from `framework_repository`. Nothing
is written to that repository. HTTP 403 and 429 are unavailable reads, retried
at the next check without refusing the commit. Each check uses at most three
requests, plus one for each further 100 statuses, within GitHub's unauthenticated
limit of 60 requests per hour.

Only the service follows `main`. The adopting project's workflows, guard copy,
gate and rules stay as last copied; the owner updates them by hand when a
framework change needs it. Until then, a new `main` commit may run against older
copies of those files.

