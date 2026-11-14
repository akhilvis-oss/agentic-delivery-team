# Safety net: design note

Status: proposed for owner approval, 14 November 2026. Part of the lifecycle plan (step "Safety net", before M1).

## Why

Five failures between 12 and 14 November passed every test and review, then broke live:

| Failure | What the tests assumed | What GitHub or the agent actually did |
| --- | --- | --- |
| #127: every review failed in 13 seconds | Stand-in agents accept any output schema | Codex strict mode rejects a schema whose `required` doesn't list every property |
| Auto-merge never fired | The `review-gate` check run sits on the PR's base commit | GitHub attaches it to the PR's head commit |
| Delivery retried forever | An auto-merge refusal is temporary | A repository without auto-merge refuses it permanently |
| "Continue from" could be ignored | Story text uses `\n` line endings | Text edited in the web form can use `\r\n` |
| Gate and lifecycle at risk from 2 December | `pull_request_target` always runs | GitHub blocks it by default without an Actions policy |

Unit tests used fakes written from the same assumptions as the code, so they couldn't catch any of these. The safety net adds two kinds of evidence the fakes can't fake: **recorded real responses** and **a live run with the real agent**.

## What it is

### 1. Strict output schemas (done)

`tests/test_runner.py` checks every schema the runner sends to an agent against strict mode (#129).

### 2. Recorded GitHub responses as test fixtures

- Real responses are captured from the scratch repository during smoke runs, cleaned of tokens and account details, and stored under `tests/fixtures/github/`.
- The first set covers what broke: check runs for a `pull_request_target` gate on a PR, a PR's `closed` event after a merge, an auto-merge refusal when the repository doesn't allow it, an issue body edited in the web form, and the order of label events when the owner approves with one click.
- Tests replay them through the real client and parsing code, not through hand-written fakes. A new GitHub behaviour is added as a new recording, not as a new fake.

### 3. Live smoke test

One fixed, tiny story, run with the real agent through the real lifecycle, on candidate code, before that code is used.

- **Where it runs:** on the owner's machine, through a `lifecycle smoke` command. Never in GitHub CI: agent sign-ins and keys stay on the owner's machine.
- **Target:** a scratch repository (`akhilvis-oss/adt-smoke`): public, `main` protected with the gate as a required check, auto-merge off. Nothing private ever goes in it.
- **Identities:** two GitHub Apps installed only on the scratch repository, with keys stored locally like the runner's:
  - a **smoke runner** App (Contents, Issues and Pull requests read and write; Checks and Actions read);
  - a **smoke owner** App (Issues read and write only), which plays the owner and approves the smoke story. The lifecycle allows any identity to be the owner as long as it differs from the runner, product and workflow identities, so this needs no change to the framework's rules.
  
  Candidate code never holds a key that can touch this repository or an adopter's.
- **Candidate:** the framework commit under test (a PR head or a release tag), exported to a temporary install. The running service and its pinned install are not touched.
- **The run:**
  1. The smoke owner files the smoke story (`tests/smoke/story.md`: add one function and its test) and approves it.
  2. The candidate claims, builds, tests, reviews and delivers it.
  3. The command waits for an end state, with a 30-minute limit.
- **Pass means all of these happened:**
  1. the approval was attested;
  2. the story was dispatched and the build committed;
  3. the review returned `ready_for_integration`;
  4. the smoke runner opened a PR;
  5. the `review-gate` check passed on the PR head;
  6. the story reached the expected end. Until M3 that's a stop for the owner's merge, because auto-merge is off; after M3 it's a verified direct merge.
  
  Each run then closes its PR and issue, so the next run starts clean.
- **Result:** the framework's runner App posts a commit status, `smoke/codex`, on the candidate commit in this repository, with a public one-paragraph summary. A failure names the stage and the stop category, never private detail.
- **When it runs:**
  - on every PR that changes `lifecycle/`, `runner/`, `gate/` or `scripts/`, before merge;
  - for runner-built PRs, automatically before the PR is marked ready;
  - before every release tag.
  
  The status is advisory at first, and becomes a required check once it has passed ten runs in a row.
- **Agents:** Codex first. Claude Code (#53) becomes a second status, `smoke/claude-code`, before testers start.
- **Cost:** two agent sessions per run (build and review) on a trivial story, from the owner's subscription.
- **Adopters:** the same command works against an adopter's own scratch repository, with their agent and settings. The docs describe it as an optional step after install and after each upgrade.

### 4. macOS and Python 3.11 in CI

Unit tests run on Linux and macOS, with Python 3.11 and 3.12. This is a workflow change, so it's owner-delivered.

## Exit test

- The smoke test **fails** on the #127 merge commit (the reviewer schema is rejected) and **passes** on `main`, with Codex.
- After #53, it passes with Claude Code too.

## Stories, in order

1. **Fixtures and replay tests:** the recordings above and tests that use them. Freeze-exempt as test-only work.
2. **Smoke command:** `lifecycle smoke`, the smoke story, the end-state checks and the commit status.
3. **Claude Code as builder and reviewer (#53).**
4. **macOS CI:** owner-delivered.

Each story has five criteria or fewer and is built by the runner, except story 4.

## Owner setup (one-time)

- Done: the scratch repository and the smoke runner App.
- To do:
  - create the smoke owner App (Issues read and write only, installed only on the scratch repository) and store its key locally;
  - add a branch ruleset on the scratch repository's `main` requiring the `review-gate` and `checks` checks.
  
  The framework files (gate, lifecycle workflow and rules) are copied into the scratch repository by an owner-merged PR there.

## Not in scope

Running agents in GitHub CI. Load or soak testing. Testing adopters' own build steps: that's their environment profile (M6).
