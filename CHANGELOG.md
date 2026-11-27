# Changelog

All notable changes, newest first. Versions follow semantic versioning; while the major version is 0, any release may change behaviour. See each [release](https://github.com/akhilvis-oss/agentic-delivery-team/releases) for details.

## v0.6.0 (2026-11-27): Approve and walk away, installed by your own agent
- **Install and upgrade by your own coding agent:**
  - `INSTALL.md` and `UPGRADE.md` at each release tag;
  - `release-files.json`, listing every carried file;
  - a read-only checker, `scripts/verify_install.py`;
  - a release hashes asset.

  A switched-off lifecycle feature sleeps, and a missing kit file keeps the guard on (#246).
- **The review tier comes from the gate's rules.** The runner chooses each PR's tier from the target's `.github/review-gate.json` and runs the gate's own check before pushing.
  - **New "Needs a fix" stops:** `pr_evidence_incomplete` and `requirement_ids_unsupported`.
  - **The example rules** default `requirement_ids` to `null` (#245).
- **Approve and walk away:**
  - one finished-story admission check (#177);
  - a priority and dependency queue (#205);
  - one continuation path, with no pasted commit IDs (#203);
  - owner withdrawal with `status:draft` (#212);
  - owner finish with `status:merged` (#184);
  - same-job retries after sign-in and usage-limit failures (#211);
  - an opt-in readiness check (#187);
  - a no-change correction round goes back to review (#252);
  - the continuation limit counts only re-approvals without new information (#257).
- **Stops:** every stop has one notice, prefixed with its kind (#182, #202). Prerequisite stops name their fixed cause, and agent CLIs should be installed pinned (#253).
- **The service:**
  - per-story error isolation, and reused App tokens (#204);
  - idle polls within a request budget (#176);
  - records validated from trusted data (#175);
  - one status-label writer (#221);
  - status-name aliases and a rules version check (#222);
  - following framework `main` after a passing smoke test, with a deadline for confirming a pending upgrade (#152, #169, #171);
  - a bounded retry when macOS refuses to signal an exited process group (#197);
  - the watchdog waits once after a wake (#251).
- **Smoke test and CI:**
  - smoke approval and guard-wait fixes (#160, #162, #170, #228), and Claude Code in smoke configuration (#172);
  - macOS CI checks (#173), with a 30-minute limit (#242);
  - tests run in parallel (#244);
  - CI ignores PR description edits (#217).
- **Process:**
  - the product agent dry-runs every story (#164);
  - Impact budgets in stories, dry runs and PRs (#237);
  - quoted Owner decisions (#178);
  - closed PR gates recorded as not checked (#179).
- **Breaking, for adopters:** `.github/task-lifecycle.json` gains owner `draft` and `merged` transitions and `system_retry_minutes`. Apply them with `UPGRADE.md`, as listed in the release notes.

## v0.5.7 (2026-11-17): Delivery fix and the smoke test harness
- **Fixed:** delivery asked GitHub for the repository's settings with a trailing slash, which GitHub rejects. Every light or standard story that reached the merge decision failed there, since v0.5.5 (#153).
- **Fixed:** the replay test helper now serves a recording only for the exact request it was captured with, so a request built wrongly fails its test (#153).
- **Fixed:** Keychain lookups use the account's home folder from the system, not `$HOME` (#153).
- **New, optional:** a live smoke test runs one tiny story through the real lifecycle on candidate code, against a scratch repository, and posts `smoke/<agent>` on the commit (#149). A watcher runs it on new commits and on the owner's and runner's pull requests (#151). Merge notices can show the result with `integration.smoke_reporting` (off by default). Failures print one local diagnostic line (#153). See [docs/smoke-test.md](docs/smoke-test.md).
- **Tests:** recorded GitHub responses and replay tests for the trusted gate, delivery and one-click approval (#139, #140, #146).
- **Design:** the M1 design note, [docs/design/m1-consolidate.md](docs/design/m1-consolidate.md) (#148).

## v0.5.6 (2026-11-15): Self-contained hosted lifecycle guard
- Delivery attempts auto-merge when GitHub omits `allow_auto_merge`; an explicit disabled setting or a not-allowed refusal still stops for the owner's merge (#144).
- Fixed the hosted guard failing on stop summaries, review findings and builder questions: shared private-content rules now ship inside the lifecycle sparse checkout (#141).
- Missing rules stop the guard with a plain message before records are checked. The private-content CLI retains its rules, output and exit codes.
- Added sparse-checkout regression coverage and an upgrade file inventory for adopters. Upgrade the complete lifecycle package and private-content script together; workflow sparse paths stay the same.

## v0.5.5 (2026-11-15): Readable stops, saved work, a service that stays up
- **Stops explain themselves** on the issue: reason, next step, the builder's quoted question or stop summary, open review findings, failing test names and refused files, with paths, tokens, account names and @-mentions removed (#96, #105, #131).
- **Saved work and "Continue from":** stops save the builder's work under a named reference; re-approving with the posted block continues from it, at most twice per story (#106, #110). Automatic continuations are delivered (#116).
- **Merges recorded and cleaned up:** an owner merge after "needs your merge" is recorded as `merged`, and worktrees, branches and saved references are removed, including after squash merges (#113, #120). One-click approval always counts (#120).
- **Service resilience:** temporary GitHub errors are retried with back-off; other errors exit for launchd to restart and are reported; finished stories are never resumed; a rejected record or a broken status comment can't stop work (#114, #122, #124, #131).
- **Delivery never waits forever:** the review check is found on the pull request head, the build-check name is configurable (`checks_name`), and repositories without auto-merge stop for the owner's merge (#132).
- **Fixed:** reviews failing under Codex strict mode, now tested for every output format (#129); a crash loop after resuming old jobs (#122, #124); API-limit risks in status and catch-up scans (#119, #124).
- **Breaking:** both private runner templates (main and continuation) must set `integration` explicitly; `.github/task-lifecycle.json` gains `needs-owner` → `merged` for the workflow and runner roles; `starter-kit/*` is control-critical (#133).

## v0.5.4 (2026-11-12): Fresh story bases and bounded check corrections
- Lifecycle dispatch fetches each story's target branch with the Runner App and pins the fetched commit before creating its worktree. Failed fetches wait for retry without falling back to a stale local branch; recovery preserves existing job bases.
- Workers can correct check failures caused by their own changes within a configured attempt limit (default three). Unavailable tools and unrelated failures still stop immediately; the runner's required checks remain authoritative.
- Lifecycle stop comments distinguish unavailable checks, exhausted attempts and unrelated failures using fixed reason codes and owner next steps, without exposing private output.

## v0.5.3 (2026-11-11): Approval through integration
- **Opt-in standalone PR delivery:** after independent review, the runner can push the reviewed commit, open a pull request, record Checker evidence and merge when policy and trusted checks permit. Control-critical work remains owner-only; delivery uses repository-scoped Runner App permissions.
- **Approve with one label:** the owner adds `status:approved` to a draft; the guard removes the draft label and binds approval to the event's issue body.
- **Service observability:** local status, successful-poll heartbeat, stand-up heartbeat age and an optional independently scheduled watchdog with deduplicated outage alerts.
- Runner verification reports allowlisted blocked-check categories; pool completion uses runner verification evidence.
- One kit version and feature-switch record, adopter documentation and a public roadmap.
- Agent rules require a regression test for every P0 or P1 review fix, or an explanation when a test is not possible.

## v0.5.2 (2026-11-10): Runner-owned verification
- Required checks run in disposable copies of the exact reviewed head before pool integration, with private verification receipts; worker test claims alone cannot approve integration.
- Documented that tests execute with the host user's access, and the supported dedicated OS user setup for the pool, separate from lifecycle credentials.

## v0.5.1 (2026-11-10): Independent review policy
- Merge policy requires one independent Checker; a second reviewer from a different model provider is required only when configured. The Checker may use the same model as the Maker, in a fresh session.
- Updated repository and starter-kit rules, with a regression test for the single-model policy.

## v0.5.0 (2026-11-10): Agent pool and launch kit
- **Agent pool** (opt-in): approved, non-conflicting stories run in parallel across agent slots, each in its own worktree. Every story is reviewed by a fresh session from a different slot; lead-and-team mode lets a lead slot delegate and review. Owner-set budgets, rate-limit cool-downs, conflict and dependency waits, and refresh with a new-head review after main moves.
- **Delivery planning** in the product agent rules: roadmap sections, a first milestone with story weights, story size, test scenarios per story and a critical regression set. The story template gains test scenarios, roadmap section and milestone fields.
- **Troubleshooting playbook** and a help-desk rule: the product agent explains errors in plain words and routes agent-fixable problems to the coding agent.
- **Importable templates:** a branch ruleset and an Actions policy that allows `pull_request_target` only for the trusted workflows (needed on public repositories from 2 December 2026).
- **Claude Code as the product agent:** setup guide and a permission template.
- Merge policy: an agent may merge directly when GitHub refuses auto-merge on an already-mergeable PR, after re-checking every condition on the exact head.
- Lifecycle: pull requests that don't close a runner-managed story skip the lifecycle check.
- Launch README and a three-level quickstart.

## v0.4.0 (2026-11-10): Task lifecycle and stand-up
- One status label per issue with a trusted guard workflow; owner-only approval and acceptance; act-once transition claims.
- Factual stand-up digest; optional model prose off by default.
- GitHub App identities with repository-scoped, short-lived tokens; the owner's token is refused for automation.
- Starter kit, `CONTRIBUTING.md`, `SECURITY.md`, this changelog and the merge policy by review category.

## v0.3.0 (2026-11-09): Task runner
- Local task runner: bounded delegation from a primary to a second agent with a fresh, read-only reviewer and limited correction rounds.
- The runner, not the agent, makes the commit, after validating paths; git hooks, filters and signing are disabled.
- Any coding agent, defined in configuration; completion judged by exit code, a schema-validated answer and git state.
- Built-in lifecycle notes so reviewers expect the runner-made commit.
- Safe recovery, isolation, a configurable version guard and local receipts; optional GitHub receipt comments.
- Fresh sessions only; resuming threads is deferred.

## v0.2.0 (2026-11-05): Review gate
- Four review categories (light, standard, domain-critical, control-critical) derived from changed paths.
- Trusted enforcement through `pull_request_target`, running only the base branch's gate and config.
- Fail-closed, revision-bound evidence; clear failure messages.
- Private-content check for public repositories; adopter templates and docs.
