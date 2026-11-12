# Changelog

All notable changes, newest first. Versions follow semantic versioning; while the major version is 0, any release may change behaviour. See each [release](https://github.com/akhilvis-oss/agentic-delivery-team/releases) for details.

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
