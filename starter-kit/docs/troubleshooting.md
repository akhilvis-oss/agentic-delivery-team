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

### A task says `needs-owner`, `needs-primary` or `blocked`

Read the lifecycle comment on the story. Every new stop has a fixed reason code
and an action; replies to a stand-up do not approve work. To re-approve, resolve
the stop, remove the stopped status in **Labels**, then add `status:approved` as
the configured owner. The next build reads the complete story, including
**Owner decisions**. The reason families mean:

| Stop reason | What happened and what to do |
| --- | --- |
| `owner_question` | The builder's exact question is quoted. Add your answer to the story under **Owner decisions**, then re-approve. |
| `question_withheld` | A question looked private and was withheld in full. Write "Please ask a public-safe question" under **Owner decisions**, then re-approve. Do not paste private logs into GitHub. |
| `scope_change`, `acceptance_criteria_change`, `story_approval` | Product needs an owner decision. Update the story or its decisions, then re-approve. |
| `clarification_unverified`, `product_request_unclassified`, `repository_evidence` | Repository evidence did not settle the owner decision. Record the answer under **Owner decisions**, then re-approve. |
| `worker_*_unavailable`, `prerequisite_failed`, `assignment_preflight` | Setup or a required safety check could not run. Ask the operator to fix setup, then re-approve. |
| `worker_*_unrelated_failure` | A check failed outside the builder's changes. Arrange a fix for that failure, then re-approve. |
| `worker_*_attempts_exhausted_N` | The builder used its correction attempts. Arrange further correction, then re-approve. |
| `worker_preflight`, `worker_build`, `worker_story_tests`, `worker_runner_checks`, `worker_prerequisite`, `worker_reported_blocked` | The builder stopped without a more specific cause. Ask the operator to resolve the named check, then re-approve. |
| `required_check_failed` | Runner or PR checks failed. The comment includes the failure count and up to five bare test function names when reported. Arrange correction, then re-approve. Paths, parameters and logs are never published. Older results or unknown test formats may lack names or counts. |
| `reviewer_reported_blocked` | Review needs further work or primary triage. Resolve the review on the linked PR before re-approving. |
| `execution_failed` | Execution stopped or was interrupted. Ask the operator to reconcile the interrupted work, then re-approve. |
| `publication_failed`, `publication_uncertain`, `auto_merge_disable_uncertain` | Publishing or cancelling auto-merge failed or has an unknown outcome. Inspect the PR on GitHub and ask the operator to reconcile it before retrying. |
| `stale_head`, `gate_category_higher`, `merge_conditions_missing` | The reviewed head or required evidence is no longer sufficient. Arrange a fresh review or complete the missing evidence on the PR. |
| `owner_merge_required` | Review is complete and policy reserves the merge for you. Inspect the linked PR's evidence and merge when satisfied. |
| `pr_closed_unmerged` | The PR closed without merging. Decide whether to reopen it or end the story. |
| `approved_scope_changed` | The issue changed after approval. Review the revised story, then re-approve. |
| `approval_not_attested`, `invalid_status_change`, `owner_requested_stop` | Approval was rejected, the guard restored a stopped status, or a permitted actor stopped the task. Read the preceding comment, resolve it and re-approve. |

### Continue saved work or restart fresh

**Owner.** After resolving a stop, paste the runner's block into the story exactly:

```text
### Continue from
Commit: <the full 40-character lowercase commit ID from the stop comment>
```

Replace the placeholder with the saved commit, then re-approve the complete story.
In the story form's **Saved work (optional)** field, paste both lines, including
the heading. When editing an existing issue body, insert the same complete block.
Keep the two lines adjacent: GitHub inserts a blank line after form labels, so
relying on a form label as the heading and pasting only the Commit line starts fresh.
Only runner-recorded saved work from `needs-owner` or `blocked` can continue;
integrity failures cannot. The service fetches the target branch and merges it
into the saved work before starting the builder. Review covers the full change
from that fetched target. The acceptance criteria take priority over pasted
findings. Each continuation permits a build and review plus one correction and
fresh review. A story permits at most two continuations, counted from its claims.

To restart fresh, remove the entire **Continue from** section and re-approve.
Malformed blocks are ignored and start fresh. `continuation_commit_unrecorded`
means the commit could not be verified as eligible saved work on this issue.
`continuation_conflict` lists repository-relative conflicting files and stops
before any builder runs; arrange resolution or restart fresh.
`continuation_limit` means restart fresh or split the story.
`continuation_not_applied` means the saved commit is missing from the built head;
ask the operator to reconcile history before re-approving.

### Approval could not be attested

The guard could not uniquely connect the label to an owner event and story
snapshot. It removes the `status:approved` label it cannot honour and explains
the recovery on the story. Follow that comment and re-approve. A permitted stop
and re-approval in the same second use event IDs to distinguish approval cycles;
duplicate approval events within one cycle still stop safely. Replaying an old
webhook never approves a later edit.

### A delayed or conflicting approval snapshot was ignored

The incoming webhook could not be tied to the current approval. The guard
preserves the current label and existing proof instead of rejecting a newer
approval. When approval cycles share a second, a conflicting story body is
ignored; it cannot create a second proof with different scope for that event.
An uncertain publication also keeps its original scope on retry. If the current
approval already has a proof, no action is needed. If the story still waits for
approval, review its current body, then remove and re-add `status:approved` to
re-approve. If the body was edited after a same-second re-approval, a fresh
approval is needed rather than replaying the earlier webhook.

### Is the service still polling?

Look for the pinned **Lifecycle service last poll** comment on the latest
stand-up issue. If no stand-up exists or stand-ups are disabled, open the
**Lifecycle service status** issue. The service updates one pinned comment in
place for that issue on every poll, before running a builder. The UTC time and
configured interval are public; local paths and responses are not. Builds and
reviews can delay the next poll. If the time stops advancing beyond that work,
ask the operator to restore the service. Re-approving alone cannot restart it.
Pinning uses [GitHub's issue-comment API](https://docs.github.com/en/rest/issues/comments#pin-an-issue-comment)
with the existing Issues write permission; no workflow permission change is needed.
Publication failures stop that poll; an ambiguous create is reconciled by its
trusted marker instead of posting duplicates. If GitHub has no matching object,
the operator must reconcile the publication before polling resumes.

### The status comment reports recovered polls or a restart

**Wait** for temporary GitHub failures. In `serve`, `Unavailable` and `Uncertain`
are retried after `poll_seconds`, then twice that wait on consecutive failures,
up to 600 seconds. Success resets the wait. The first successful poll and stand-up
adds “Polls first failed: …; recovered: …; failed polls: N; exception class: ….”
to the existing pinned status comment. The times are UTC. The line disappears
after the next successful cycle; it confirms recovery rather than asking for
re-approval. One-shot `run-once` and `enqueue-only` still exit on the first error.

**Operator** for unexpected errors. Other exceptions exit non-zero after recording
only their class and framework function privately. The next start's first
successful cycle adds “Service restarted after … in ….” to the same comment and
removes the private error record. The next successful cycle removes that line.
Neither status line includes messages, request paths, URLs or tokens. If restarts
repeat, investigate privately using the reported class and function; do not paste
private responses or logs into an issue.

Run `serve` under the supplied `lifecycle-serve.plist.example` launchd template
with `KeepAlive`, as described in the [lifecycle guide](../../docs/v04-lifecycle-digest.md#optional-owner-run-macos-login-setup).
It restarts the process after unexpected errors. The kit does not install it.

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

### A lifecycle stop needs an explanation
Read the issue's stop comment. Builder summaries and open review findings are
public quotes, checked for private content before posting; raw logs and receipts
stay private. Builders must explain what they tried, what failed and what they
need without paths, account names, secrets, prompt text or log excerpts. Reviewers
follow the same rules and identify the related acceptance criterion. Older
reviewers without a criterion receive “Criterion not supplied” unless their
finding already names an `AC-N`.

Examples of the public details and next actions:

- Non-question builder stop: **Builder stop summary (quoted):**
  > I tried the build. A required compiler was unavailable. I need the operator to restore it.

  Restore the prerequisite, then re-approve.
- Review stop, including `needs-primary`: **Open finding: P1 — AC-2**
  > The stop comment omits the reviewer's description.

  Arrange correction of the listed findings before re-approving.
- Thirteen open findings: the first ten appear, followed by **and 3 more**.
  Arrange further correction; only findings from the latest review are open.
- Refused commit: **Commit refusal: denied_path**, with the refused
  repository-relative path, for example `.github/workflows/example.yml`.
  This kind of change needs an **owner-delivered PR**; re-approval alone cannot
  authorize a refused path. Extremely large refusal lists use a shared size
  budget and a visible truncation marker and remaining-path count.
- Private content: a summary becomes **I tried [removed: local path]. Need a compiler.**
  The visible placeholder replaces flagged content; the remaining explanation
  still appears. Do not paste private material back into the issue.
- Over-long text: a summary ends with **[truncated]** within 1,500 characters;
  each finding description ends with the same marker within 600 characters.
  Use the visible explanation and next step; never publish the raw receipt.
