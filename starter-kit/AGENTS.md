# <PROJECT_NAME>: agent working agreement

Every coding agent working in this repository follows this agreement. The **owner** decides scope, accepts outcomes and authorises merges. Read `docs/product/vision.md`, `docs/product/roadmap.md` and `docs/product/decision-log.md` before starting a task.

## Repository boundaries
- This agreement covers **<OWNER>/<REPOSITORY> only**. Do not create, edit, push, merge, or open or close issues or PRs in any other repository.
- On the local machine, do not edit other projects' folders or worktrees, and do not start, stop or reconfigure their services or scheduled jobs.
- If a task seems to need another repository, stop and ask the owner.

## Source of truth
- The product docs and the issue are the truth, not chat memory or earlier conversations. If they disagree with what you remember, follow the repository and say so.
- Work only on stories the owner has approved.
- Copy each story's acceptance criteria into the PR **verbatim**, with their `AC-N` IDs.

## How work is done
- Work on a feature branch and open a PR. Never push to the default branch, and never merge by hand; merges follow **Merge policy**.
- One coherent story per PR. Keep control-critical changes in their own PR, and list risky files first in the description.
- Every behaviour change has tests. Report results exactly as run, with counts; mark anything not run with the reason.
- Build and test commands: `<BUILD_COMMAND>`, `<TEST_COMMAND>`.
- When the local task runner is used, **leave your changes uncommitted**; the runner validates and commits them. Never write to `.git`.

## Assumptions and escalation
- Proceed on reasonable assumptions within approved scope, and record each one in the PR's `## Assumptions` section. They don't need individual approval.
- **Escalate instead of assuming** when a choice:
  - conflicts with an approved decision or the product docs;
  - is on the must-ask list below;
  - creates a commitment that would be costly to reverse.
- When the story itself is unclear or contradictory, stop and ask; don't guess the product intent.

## Must-ask (stop and ask the owner)
- Changing scope, priorities or anything in `docs/product/`.
- Schema or data migrations; deleting or renaming user-facing features, URLs or data.
- New dependencies, external APIs or paid services.
- Authentication, permissions, credentials or personal data.
- <DOMAIN_SPECIFIC_ITEMS: for example real payments, live orders, messages to real users>
- CI workflows, the review gate or its config, and this file.

## Review
- The author never reviews their own work. An independent reviewer, a fresh session that didn't build it, checks the work against the original issue and the exact head commit. It may use the same model as the author.
- A second reviewer from a different model provider is optional. If you configure one, its PASS is required for domain-critical and control-critical work; if you don't, nothing waits for it. One model reviewing its own kind of work shares its blind spots; a second provider reduces that.
- Findings are graded **P0/P1** (wrong behaviour, missed criterion, safety: must be fixed) or **P2** (improvement: becomes a follow-up issue, doesn't block).
- At most two correction rounds; then escalate. One disagreement exchange, then technical questions go to the lead engineer and product questions to the owner.

## Merge policy
The owner's advance permission, by the category the trusted `review-gate` check reports:

| Category | Conditions on the current head | Who merges |
| --- | --- | --- |
| Light | Required checks pass | Auto-merge |
| Standard | + independent review PASS | Auto-merge |
| Domain-critical | + a second review from a different model provider, **only if one is configured** | Auto-merge |
| Control-critical | Everything above | **Owner only** |
| Releases | Owner approved the notes | **Owner only** |

If any condition is missing, unclear or negative, leave the PR for the owner. Never bypass checks, dismiss reviews or change branch rules.

## Reporting
End each task with: **what now works; how to run it; tests actually run; assumptions made; limitations; only the owner decisions actually required.**
