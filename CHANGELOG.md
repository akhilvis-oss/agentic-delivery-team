# Changelog

All notable changes, newest first. Versions follow semantic versioning; while the major version is 0, any release may change behaviour. See each [release](https://github.com/akhilvis-oss/agentic-delivery-team/releases) for details.

## Unreleased
- **Starter kit** (`starter-kit/`): base `AGENTS.md`, product-session rules, product docs (vision, roadmap, decision log) and a story template.
- `CONTRIBUTING.md`, `SECURITY.md` and this changelog.
- Merge policy by review category in `AGENTS.md`.

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
