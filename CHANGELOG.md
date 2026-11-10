# Changelog

All notable changes, newest first. Versions follow semantic versioning; while the major version is 0, any release may change behaviour. See each [release](https://github.com/akhilvis-oss/agentic-delivery-team/releases) for details.

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
