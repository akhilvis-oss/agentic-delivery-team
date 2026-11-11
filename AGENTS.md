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
- Owner-approved exception: owner and bot GitHub logins may be committed in the public-policy `.github/task-lifecycle.json`. Paths, App IDs and Keychain references remain private local settings. This exception does not permit credentials or other private content.
- Run `python scripts/check_private_content.py` before every commit. Exempt a line with the allow marker only for a reviewed placeholder, and say why in the PR.
- **No project specifics in code.** Anything particular to one project (paths, requirement IDs, domain rules, model names) becomes configuration with a documented example.
- Write for strangers: docs explain what a piece does, how to adopt it, and its limits.

## How work is done
- Work on a feature branch and open a PR to `main`. Never push to `main` directly. Merges follow **Merge policy** below.
- Each PR declares its review category (`## Review tier`), as defined in `.github/review-gate.json`. Declare the category the changed paths require, or a higher one.
- Size a PR to one coherent story; a larger work package is fine when it shares one goal and one live trial. Keep control-critical changes in their own PR, and list risky files (workflows, gate, credentials, runner safety code) first in the PR description so reviews focus there.
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
- **Every review finding becomes a test.** A fix for a P0 or P1 finding includes a test that fails without the fix and passes with it, so the runner checks that lesson on every later story. If a test isn't possible, say why in the PR.
- A passing gate or an AI review is evidence, not approval on its own. The owner's approval is either their merge or the advance permission in **Merge policy**.

## Merge policy (owner's advance permission, 2026-11-09)
The owner pre-authorises merges by review category. Once a PR meets every condition for its category on its **current head commit**, the Maker may enable GitHub auto-merge (merge commit); GitHub then merges when the required checks pass. Any new push or description edit restarts the conditions.

If GitHub refuses to enable auto-merge because the PR is already mergeable ("clean status"), the Maker may merge it directly with a merge commit, pinned to the verified head (`sha` on the merge request), after re-checking every condition on that head immediately before merging. Record the merge in the PR with the head SHA and the reviews relied on.

| Category (as decided by the trusted `review-gate` check) | Conditions | Who merges |
| --- | --- | --- |
| Light | Required checks (`checks`, `review-gate`) pass | Auto-merge |
| Standard | Required checks pass; independent Checker PASS on the current head | Auto-merge |
| Domain-critical | Required checks pass; independent Checker PASS on the current head; and, **only if a second reviewer from a different model provider is configured**, its PASS on the same head | Auto-merge |
| Control-critical | Everything above | **Owner only**; never enable auto-merge |
| Release tags and release notes | Owner approved the notes | **Owner only** |

- The category is the one the trusted `review-gate` check reports, never a lower one declared or assumed by the Maker.
- Record each review as a GitHub review or PR comment that starts with `Checker: PASS` or `Second review: PASS`, names the exact head SHA, and is written by a session separate from the Maker.
- **Independence means a fresh session that didn't build the work.** The Checker may use the same model as the Maker. A second reviewer from a different provider is optional: when one is configured its PASS is required, and when none is configured nothing waits for it. One model reviewing its own kind of work shares that model's blind spots; a second provider reduces that risk but isn't required.
- Reviews recorded as `Claude review: PASS` before 11 November 2026 count as a second review.
- If any condition is missing, unclear or contradicted (a CHANGES REQUIRED or BLOCKED review, a failing or pending check, an unanswered must-ask item), do not enable auto-merge; leave the PR for the owner.
- Never merge a control-critical PR or a PR that misses any condition, and never bypass checks, dismiss reviews, or change branch rules to get a PR merged.
- **Limit:** agents currently act through the owner's GitHub account, so GitHub cannot tell an agent's action from the owner's. This policy is therefore enforced by agents following it; required checks and the trusted gate remain the technical controls.

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
