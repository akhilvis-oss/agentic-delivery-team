# agentic-delivery-team: agent working agreement

This repository is a **public, generic** kit for steering AI-built software with checks and balances. Every agent working here follows this agreement. The Product Owner decides scope, accepts outcomes and authorises merges.

Read `README.md` and `docs/review-categories.md` before starting a task.

## Repository boundaries
- This agreement covers **akhilvis-oss/agentic-delivery-team only**. Do not create, edit, commit, push, branch, tag, merge, or open/close issues or PRs in any other repository. That includes the projects that adopt this kit.
- On the local machine, do not edit files in other projects' folders or worktrees, and do not start, stop or reconfigure their services, task runners or launchd jobs.
- Reading an adopting project is allowed only when the Product Owner asks for it, as evidence of real use. Nothing project-specific is copied in.
- Adoption happens in the adopting project, through its own PR that copies a tagged release of this kit. Never push kit changes into another repository.
- If a task appears to require changing another repository, stop and ask the owner instead.

## Public and generic by default
- **No private content.** Never commit personal paths, account or seat names, machine names, credentials, tokens, billing details or private project data. Local settings live in uncommitted config; the repository holds only generic code and `*.example` files.
- Run `python scripts/check_private_content.py` before every commit. Exempt a line with the allow marker only for a reviewed placeholder, and say why in the PR.
- **No project specifics in code.** Anything particular to one project (paths, requirement IDs, domain rules, model names) becomes configuration with a documented example.
- Write for strangers: docs explain what a piece does, how to adopt it, and its limits.

## How work is done
- Work on a feature branch and open a PR to `main`. Never push to `main` directly, and never merge your own PR.
- Each PR declares its review category (`## Review tier`), as defined in `.github/review-gate.json`. Declare the category the changed paths require, or a higher one.
- Keep PRs small and single-purpose. Split unrelated changes, especially anything control-critical.
- Every behaviour change has tests. Domain-critical code (`runner/`, `digest/`) also needs failure-mode evidence: failure cases, recovery and concurrency.
- Before opening or updating a PR, run the local checks:
  ```sh
  python scripts/check_private_content.py
  python -m ruff check .
  python -m pytest -q
  ```
- Report test results exactly as run, including counts, and mark anything not run with the reason. Never claim a test passed without running it.

## Review
- The Maker never reviews their own work. An independent Checker (a separate agent or session) reviews control-critical and domain-critical PRs against the original issue and the exact head commit, and records PASS, CHANGES REQUIRED or BLOCKED.
- At most two correction rounds; then escalate unresolved findings to the owner. Continue other independent work meanwhile.
- A passing gate or an AI review is evidence, not approval. The owner's merge is the approval.

## Assumptions and escalation
- Proceed on reasonable, recorded assumptions within approved scope. List them in the PR's `## Assumptions` section; they don't need individual approval.
- Escalate to the owner, instead of assuming, when a choice:
  - conflicts with approved direction or an existing decision;
  - is in the must-ask list below;
  - creates a commitment that would be costly to reverse.

## Must-ask (stop and ask the owner)
- Changing the licence, the repository's visibility or any repository setting.
- Adding a runtime dependency, or changing pinned development tools.
- Changing workflow triggers or permissions. Especially: never use `pull_request_target` with a checkout or execution of PR code, and never grant write permissions.
- Anything that weakens the review gate, its category rules or the private-content check.
- Breaking changes to config files or templates that adopters copy.
- Creating a release tag or publishing release notes.

## Workflow security
- Workflows use least-privilege permissions, actions pinned by full commit SHA, and no secrets unless the owner approves.
- PR text, branch names and file names are data: never interpolate them into shell commands.
- The trusted review gate must judge a PR using rules from the base branch, never rules the PR itself can change.

## Releases
- Releases use semantic version tags (`v0.1.0`, ...) with short release notes listing changes, breaking changes and migration steps for adopters.
- Adopting projects record which kit version they use; upgrading is their own reviewed PR.

## Reporting
End each task with: **what now works; how to run it; tests actually run; limitations; only the owner decisions actually required.**
