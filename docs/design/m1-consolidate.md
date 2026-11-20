# M1, consolidate the lifecycle: design note

Status: proposed for owner approval, 16 November 2026. Part of the lifecycle plan, after the safety net. Merging this note lifts the freeze on M1 stories.

## Why

Between 10 and 14 November, most stops, stalls and outages came from two causes in the lifecycle's own design:

- **A story's state lives in several places,** and several pieces of code can change it: the label and records on GitHub, the service's local claim, the runner's job record and the outbox. When they disagree, the service stalls, reopens a finished story (#75, #110) or repeats work until it hits GitHub's limits.
- **Each fix added a path instead of strengthening one.** Today there are 13 statuses, 98 stop codes, three ways to continue work, and two templates that must both carry delivery settings. Each new path broke in a case its siblings already handled. For example, a continuation without delivery settings was parked silently (#110), and a continuation after an ordinary failure was refused (#115).

M1 doesn't rebuild the lifecycle. It works in the existing code: it cuts each outcome down to one path, adds three guards, and deletes what it replaces.

## What stays

These rules are unchanged:
- Approval is tied to the story text's hash.
- Review is independent and runs in a fresh session.
- The runner can't change `.github/`.
- Control-critical work waits for the owner's merge.
- Saved work is kept under named references.
- Stop explanations are cleaned before posting.

## What changes

### 1. Seven statuses

| New status | Replaces | Who sets it |
| --- | --- | --- |
| `draft` | `draft` | product, owner (to withdraw an approval), workflow |
| `approved` | `approved` | owner only |
| `working` | `dispatched`, `in-progress`, `committed`, `changes-requested`, `needs-primary` | runner |
| `in-review` | `in-review` | runner |
| `needs-owner` | `needs-owner`, `blocked` | runner, workflow, product |
| `pr-open` | `pr-open` | workflow |
| `merged` | `merged`, `accepted` | workflow, runner, owner (attested finish) |

- **Progress moves out of the labels.** Build and review progress (build round, correction round, continuation) moves into the records and the status comment.
- **The owner only ever adds a label.** The system removes the old status label itself.
- **Owner labels:**
  - `status:approved` approves.
  - `status:draft` withdraws (part 7).
  - `status:merged` attests a finish done by hand (part 8).
- **The rules shrink:** transitions go from 13 states to 7. The rules file is `.github/task-lifecycle.json`, so it's changed in an owner-delivered PR alongside the story.
- **Migration:** a `lifecycle migrate-labels` command maps each open issue's old label to the new one. It has a dry run, and it never touches closed issues. Old records keep their old status names and still parse: names are mapped when read, never rewritten.

### 2. One way to continue

- **Every stop saves the work,** as today, including failures, agent start failures and system stops.
- **A re-approval continues by default** from the latest saved work for that story, merged with the latest target branch.
- **To start over,** write `Restart: fresh` under "Owner decisions".
- **No new build when the work is current.** If the saved work is the reviewed head and still current against the target, the runner only delivers it.
- **One function serves every continuation:** the owner's re-approval, the automatic agent-to-agent continuation that replaces `needs-primary`, and delivery-only resume. Delivery settings, limits and checks therefore can't differ between them.
- **One template for delivery settings.** The continuation agent can still be a different agent, but delivery settings come only from the main template.
- **Limits are unchanged:** at most two continuations per story, each with one correction round.
- **Deleted:**
  - the pasted "Continue from" block and its parser;
  - the separate continuation lookup that excluded failed jobs;
  - the separate delivery-only resume path.
  
  A story that still carries a "Continue from" block is continued from its latest saved work, and the notice says so.

### 3. Four kinds of stop, one notice each

Every stop is one of these four kinds:

| Kind | Examples | What happens |
| --- | --- | --- |
| **Needs your decision** | The builder's or the readiness pass's question | Quoted; you answer under "Owner decisions" and re-approve |
| **Needs your merge** | Control-critical work; a repository without auto-merge | You merge; the merge is recorded |
| **Needs a fix** | Open findings, failing tests, files the runner refused | Quoted and cleaned; you revise the story or re-approve to continue |
| **Waiting on the system** | Agent usage limit, sign-in, GitHub unavailable, setup | Retries by itself; never asks for a re-approval while retrying |

- **Retries for "waiting on the system":** by default up to three, after 5, 20 and 60 minutes (configurable).
  - While retrying, the story stays `working`, and the status comment says why and when the next retry is.
  - Each retry re-checks the approval hash and the target branch first.
  - After the last retry, the story stops at `needs-owner`, with the reason.
- **One notice per stop,** keyed by the stop. Today the guard and the runner can each post one; the second is removed.
- **Stop codes stay fixed for automation, with one cause per code.**
  - The notice leads with the kind.
  - The record adds the exception type and the framework function where the stop came from, both public-safe names.
  - Exactly one code, `unexpected_error`, covers failures nobody anticipated. It always stops safely, as "needs a fix".

### 4. Three guards

1. **A finished story never reopens.** A story that is closed, or has a `merged` record, has no actions. Every path that can start work (dispatch, continuation, recovery and backfill) asks one function first, and that function refuses finished stories.
2. **One story's error can't stop another's.** Each story's work in a poll runs inside its own error boundary: an error stops that story as "waiting on the system" or "needs a fix", and the poll moves on. Only service-wide conditions pause every story: GitHub unavailable, a rate limit, or bad configuration.
3. **Only one function changes a status.**
   - `core.transition()` is the only code that writes a status label or removes one. Today labels are written from the outbox flush, from two places in the guard, and by three separate label removals.
   - The guard, the runner and the service all call it.
   - A test fails if any other code writes to an issue's labels.

### 5. Records

- **Known versions only.** Each record carries an explicit JSON integer `version`, excluding booleans and floating-point values. Only version 1 currently parses; missing or unsupported versions are ignored. Supported versions are listed in one place in code. The version is not part of the record key.
- **Validity comes from the unique data block, its author and a known version,** never from text before or after it, including continuation instructions. Existing data, key and author-role checks still apply. Rewording a notice can't orphan earlier records.
- **Edited records:** when a comment's `updated_at` is later than its `created_at`, it is ignored for state, including stop records. Edited `merged` and `accepted` records retain finality so an edit cannot reopen a story. A comment missing either timestamp counts as unedited.
- **Follow-ups:** naming ignored records in the status comment and stopping on inconsistent state after a record deletion are separate work. The existing guard-record construction defect is also a separate story (#192).

### 6. Fewer API calls per poll

- **Fewer reads.** Each poll lists issues once and fetches events only for issues that changed since the last poll, or that have active work.
- **Token caching.** An App token is reused until shortly before it expires, instead of being minted on every job save.
- **Status comment.** It's edited only when its content changes, and at most every 10 minutes while idle. The local watchdog reads the local heartbeat, not the comment, so alerts are unaffected.
- **Target:** an idle poll makes at most three GitHub requests. A test counts the requests against recorded responses.

### 7. Withdrawing an approval

Adding `status:draft` to a story that is `approved`, `working` or `in-review`:
- **Queued:** it leaves the queue.
- **Running:** it stops at the end of the current agent session and saves the work.
- **At `pr-open`:** the runner turns auto-merge off and leaves the PR open for you.

Approving again continues from the saved work.

### 8. Owner-attested finish

- **What it does:** the owner adds `status:merged` from any status except `merged`, leaving the previous label selected. The guard removes the old status, publishes one `finish` record per owner label event with `reason: finished_by_owner` and its event ID, then closes the issue only after confirming the record. A later issue event recovers a dropped run, including an edit with both labels still present. A closed story without a merged record or label can be finished too; a history that already reached merged gets no second finish.
- **How it's attested:** publication verifies the label event actor against owner history. Readers accept only strict workflow-authored finish records carrying that ID, as with approval attestations. Owner-account comments never count as merged records, including for an owner with the Product role. The workflow gains no transition rights; other actors retain existing policy handling.
- **Cleanup:** remove a clean managed worktree. Remove its branch and saved-work reference only when merged into the target; otherwise keep both, mark the claim cleaned and show `Issue #N: owner finish kept unmerged work` until the owner removes the branch. An owner close before finishing already removes the saved-work reference.
- **Hand-built PRs:** use `Refs #N`. Close any open runner PR before finishing; closing it afterwards fails the lifecycle check once and changes nothing. The check is unchanged and discovers issues only through `Closes #N`. This replaces #49 without changing that check.
- **Limits:** a finish is final; a mistake needs a new story. Removing `accepted` and `delegate_acceptance` belongs to the later status change. No hand-signed record path exists to delete. The rules and example allow owner finishes; the `.github/` policy change is owner-delivered.

### 9. A minimal readiness pass

- **When:** after the approval is attested and before any build.
- **Who:** a fresh session with no write access.
- **What it reads:** the story, its criteria, `AGENTS.md` and the product documents the story names.
- **What it returns:** either `ready` or a list of questions, in a strict-mode output format.
- **If it has questions:** they stop the story as "needs your decision" and count as the story's first round.
- **What it checks in M1:**
  - criteria that contradict each other or the product documents;
  - criteria a read-only reviewer can't check;
  - paths the runner can't commit, unless the story says the owner delivers them.
- **Cost:** one extra agent session per approval.
- **Later:** the full checklist and an owner override come in M5. Answering questions from the product documents before asking you (#100) also stays in M5.

### 10. A queue with dependencies

- **Order:** approved stories dispatch in approval order.
- **Reordering:** adding the `queue:next` label moves a story to the front. The system removes the label once it's used.
- **Dependencies:**
  - `Depends on #N` in the story makes it wait until #N is merged.
  - If #N closes without merging, the story stops as "needs your decision".
- **One story at a time per repository** by default. The next story waits until the previous PR is merged or closed.
- **A PR that falls behind its target branch** is refreshed automatically:
  - the runner merges the target into the branch (GitHub's "Update branch" does the same, and no force-push is needed);
  - it runs the tests and a fresh review;
  - then it delivers again.

### 11. Smaller fixes folded in

- **Public text.** Every review finding and follow-up issue carries public text.
- **Worktrees.** A worktree left over from an earlier run never blocks the next run of the same story.
- **Frozen trees.** Checks run on a frozen tree, and a file edit during a check is a clear stop.
- **PR descriptions.** A runner PR quotes the story's "Owner decisions" along with its acceptance criteria (seen on #147, where the PR quoted a criterion that had been replaced).
- **Closed PRs.** On a closed pull request, the guard records the gate as not checked instead of "unavailable" (seen while recording fixtures for #140). The trusted gate can't bind a run once its pull request closes.

## Out of scope

- **Left for M3:** delivery policy in one place, the code-owner rule, per-criterion evidence, protected tests and verified direct merges.
- **Left for M4:** separate worker processes and owner commands on the status issue.
- **Left for M5:** the full readiness check.
- **Left for M6:** one-command adoption.

## Exit test

- **Less code.** Each replaced path is deleted in the same PR that replaces it. Together, `lifecycle/` and `runner/` have fewer lines than at this note's base commit: 6,817 at `651068f`. They're measured together so that code doesn't just move between them.
- **No pasted commit IDs.** No owner step anywhere in the docs asks for a commit ID.
- **Finished stories stay finished.** Replaying the #75 and #110 reopen cases leaves both stories closed.
- **Old records still parse.** Every record comment in this repository still parses, except restoration records with job `guard`, kind `guard` and reason `invalid_status_change`: these remain informational and are explicitly excluded from state.
- **The migration runs cleanly.** `migrate-labels --dry-run` completes on a copy of an adopting project's issues.
- **Readiness catches #113.** Replaying #113's criteria through the readiness pass produces the builder's question before any build.
- **Smoke test.** Every M1 PR passes the live smoke test on its own code before merge.

## Stories, in order

Each story has five criteria or fewer, and the runner builds them. The smoke command (safety net, story 2) must be merged first, because every M1 PR has to pass it.

1. **Three guards** (part 4).
2. **Records and fewer API calls** (parts 5 and 6).
3. **Seven statuses, withdrawal and owner-attested finish** (parts 1, 7 and 8), with an owner-delivered PR for `.github/task-lifecycle.json`.
4. **One way to continue** (part 2).
5. **Four kinds of stop and stop codes** (part 3).
6. **Smaller fixes** (part 11).
7. **Readiness pass** (part 9).
8. **Queue** (part 10).

After stories 1 and 2, the tester date is re-checked against the remaining work.
