# Troubleshooting

The product agent reads this file when the owner asks about a problem. Each entry says what the message means, **who fixes it**, and how.

**Who fixes it**
- **Agent:** the coding agent that owns the pull request fixes it. The owner doesn't need to act unless it fails twice.
- **Owner:** only the owner can do it (settings, approvals, merges, releases, keys). The steps are written for clicking, not typing.
- **Wait:** caused by GitHub or a provider, not by the project. Wait, then re-run.

The owner should never need to run Git commands. If a step needs the terminal, run one command at a time and copy only the command, not any text after a `#`.

## Pull request checks

### `review-gate` fails: "Tested head must equal the current full PR head SHA"
**Agent.** The pull request changed after its tests were recorded, often because someone clicked **Update branch**. The agent re-runs the tests on the new head, confirms its own changes are unchanged, and updates "Tested head" in the description.

### `review-gate` fails: "Missing section/content: …"
**Agent.** The pull request description lacks a section its review category needs (for example acceptance criteria, evidence or a security review). The agent completes the description from the pull request template. The failure message names the missing sections.

### `review-gate` fails: Checker "Reviewed head" doesn't match
**Agent (the Checker).** The independent Checker recorded its verdict on an older head. The Checker re-verifies the current head and updates its record. Nobody else may edit the Checker's record.

### "Required to update: Update your branch with the base branch before merging"
**Agent.** Another pull request merged first, so this one is out of date. The agent updates the branch, re-runs the checks and refreshes "Tested head". If the owner clicks **Update branch** instead, the gate fails as described above until the agent refreshes the description.

### Auto-merge refused: "Pull request is in clean status"
**Agent.** GitHub won't queue auto-merge on a pull request that is already ready. Under the merge policy, the agent may merge it directly after re-checking every condition on the exact head. Control-critical pull requests are always merged by the owner.

### A check shows "startup failure", or "Expected — waiting for status to be reported"
**Wait**, or **Owner** if it keeps happening. The trusted workflow didn't start. Common causes:
- GitHub blocked `pull_request_target` workflows during a scheduled test window, or from 2 December 2026 because the repository has no allowance for them. If it happens outside a test window, the owner adds the allowance (see the quickstart).
- A GitHub outage.

To re-trigger, the agent makes a small edit to the pull request description, or re-runs the workflow from the **Actions** tab.

### A check shows ✕ "cancelled" next to a green run of the same name
**Nobody.** Two events started the same check a few seconds apart (for example a push and a description edit), and GitHub cancelled the older run when the newer one started. The newer run is the one that counts. If GitHub still won't merge, open the cancelled run and choose **Re-run all jobs**.

### `lifecycle` check fails: "PR head has no trusted successful task review"
**Agent.** The pull request closes a story that the runner manages, but the runner's review of this exact head is missing. The story goes back through review. Pull requests that don't close a runner-managed story skip this check.

## Issues and labels

### An issue was labelled `status:draft` automatically
Expected. Every issue carries one status label, and a new issue starts as a draft. It means "not yet approved for work".

### Approval failed or the story did not start
**Owner.** To approve a draft, sign in as the configured owner, open the issue's **Labels** picker and add `status:approved`. You can leave `status:draft` selected: the guard removes it for a permitted draft → approved transition and records approval of the body at that event. Another identity cannot approve, and other combinations of status labels fail closed.

If approval failed, read the guard comment and the lifecycle run in **Actions**, and fix the reported cause first (for example, a label-removal permission error). In **Labels**, first remove every other status label, including `status:draft`. Then, as the configured owner, remove `status:approved` if present and add it again, leaving it as the only status label. The transition must still be allowed from the last permitted status in the issue history. A failed cleanup can already have advanced that history to approved, so retaining draft on a retry can fail again. Wait for the guard to record approval before expecting work to start. Conflicting labels, a missing or ambiguous event, or an edit after approval require a fresh owner approval event; re-running an old workflow does not approve the edited body.

### A status label changed back on its own
Expected. The guard workflow reverts status changes made by anyone who isn't allowed to make them. Only the owner can approve or accept work. If the owner's own change was reverted, report it as a bug.

### An approved story is waiting with `target_branch_fetch_failed`
**Owner** for App installation/permissions or branch configuration; **Wait** for
a GitHub/network outage. Before starting a story, the lifecycle service fetches
the configured `base_branch` (default `main`) using the Runner App, including for
private repositories. The App installation needs Contents read and Issues write;
delivery-enabled installations retain their existing delivery permissions.
Check that the App is installed on this repository, its updated permissions were
accepted, and the configured branch exists on GitHub. The story stays approved
and retries on a later poll without starting a job. No owner credential or stale
local branch is used as a fallback. The private claim records the fixed failure
reason; `status` lists the waiting claim. Do not publish token or Git transport
output while investigating.

An older dispatched claim may lack `start_commit` even though its worktree or
queued job already exists. Recovery validates and retains that work's original
start without a fetch, so a later remote merge or fetch outage does not change
it. Conflicting or missing worktree/job evidence stops recovery for manual
reconciliation; preserve the existing receipts and history when investigating.

## Bot identities and keys

### The bot gets "404" when looking up its installation
**Owner.** The GitHub App isn't installed on that repository. Open the App's settings, then **Install App**, choose **Only select repositories**, and add the repository.

### The bot can't get a token, or gets a permission error
**Owner.** The App asks for a permission it wasn't given. Open the App's **Permissions & events**, add the permission named in the error, save, and then **accept the updated permissions** on the installation (GitHub sends an email or shows a banner).

### Keychain says "The specified item already exists"
**Owner.** A key with that name is already stored. Add `-U` to the `security add-generic-password` command to replace it.

### Terminal says "operation not permitted" when reading Downloads
**Owner.** macOS hasn't allowed Terminal to read that folder. Open **System Settings → Privacy & Security → Files & Folders → Terminal**, switch on **Downloads**, then reopen Terminal.

### Push fails: not authenticated
**Owner**, once. Sign in for the account the agents run under:
```sh
gh auth login
```
```sh
gh auth setup-git
```
```sh
gh auth status
```

## Task runner

### A runner receipt says `blocked`
**Agent first.** Read the fixed reason and, for lifecycle jobs, the issue's stop line
and owner's next step. A worker corrects failing tests, lint or builds caused by
its own changes within its run, up to `worker_check_attempts` total attempts
(default 3, including the first attempt). It stops immediately if a required
environment, dependency or tool is unavailable, or stops when attempts run out.
`blocked_check` identifies preflight, build, story tests, runner checks or a
prerequisite. `blocked_cause` distinguishes unavailable prerequisites from
exhausted corrections and unrelated failures. The lifecycle issue line says
which occurred; exhaustion includes the configured attempt count. Restore the
prerequisite for an unavailable check, or arrange further correction for exhausted
attempts, before authorizing a new task. Older clients without a cause receive an
explicitly unspecified explanation; inspect their private result. A missing
category from an older client receives a generic reason.
The runner still runs its own required checks after delivery; worker self-checks
do not replace them. Never install dependencies, replace configured tools,
change test configuration to pass, delete tests or skip them as a repair.
Common causes:
- a prerequisite failed (dependency missing, preflight not passed): fix the story or its dependencies;
- a configured command failed (wrong path, missing program): fix the configuration;
- the worker stopped on a harmless exploratory command because its instructions said "stop on any failure": stop rules should cover required checks only.

Preserve the blocked run's changes and private receipt for inspection. The owner
resolves unavailable prerequisites or reviews exhausted corrections before
authorizing a new task ID. Never reset the blocked job to queued or replay it.

### An agent says "Not logged in"
**Owner.** The runner gives each agent its own private home, which needs its own sign-in or API key. Sign in using the command the coding agent gives you, then check with its status command.

## Notifications

### Too many emails from bots
**Owner.** In GitHub **Settings → Notifications**, send notifications to a separate address, or filter on the mailing-list header for the repository. Everything that needs the owner also appears in the stand-up.
