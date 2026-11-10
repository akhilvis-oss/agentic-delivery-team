# agentic-delivery-team

An open-source kit that lets a product owner steer an app's build by AI coding agents, with checks and balances, once the product vision exists.

> **Status: v0, experimental.** Pieces are extracted one at a time from a working setup and proven on a real project before being called stable. Expect changes.

## The idea

You are the customer and product owner. You agree the vision, roadmap and stories with a product agent. Engineering agents build in thin, demoable slices. The kit supplies the guardrails so you only spend time on what needs you:

- **Proportional review.** Every pull request gets a review category from the files it changes. Low-risk changes need little ceremony; changes to financial logic, credentials or the review rules themselves need full evidence and an independent checker.
- **Nothing irreversible without you.** Scope approval, product acceptance and merge authority stay with the owner and are separate recorded steps.
- **Assumptions over interruptions.** Agents proceed on recorded assumptions within approved scope, and escalate only when a choice conflicts with approved direction or would be costly to reverse.
- **GitHub as the record.** Issues, PRs and reviews hold the durable history, so work survives a lost chat or a restarted agent.

## What's in the kit

| Piece | Status | Where |
| --- | --- | --- |
| Review gate: four review categories enforced in CI | **v0.2, unreleased** | [`gate/`](gate/), [`templates/`](templates/), [docs](docs/review-categories.md) |
| Private-content check for public repos | **v0.1, this release** | [`scripts/check_private_content.py`](scripts/check_private_content.py) |
| Local task runner: bounded delegation, review-only jobs and safe recovery | **v0.3, unreleased** | [`runner/`](runner/), [setup and safety](docs/task-runner.md) |
| Issue lifecycle and stand-up digest: approval claims, phase history, guarded states and owner decisions | **v0.4, unreleased** | [`lifecycle/`](lifecycle/), [`digest/`](digest/), [setup and limits](docs/v04-lifecycle-digest.md) |
| Base agent rules and project profile (assumptions, escalation, permissions) | Planned | `templates/` |

## Quick start: the review gate

1. Copy [`gate/review_gate.py`](gate/review_gate.py) to `scripts/review_gate.py` in your project.
2. Copy [`templates/review-gate.example.json`](templates/review-gate.example.json) to `.github/review-gate.json` and edit:
   - `repo_url` and `allowed_bases`
   - `requirement_ids` (your ID scheme, or `null`)
   - `domain_evidence`: what "domain-critical" evidence means for your project
   - the path patterns for each category
   - `gate_paths`: the code, config, workflows and tests that must stay control-critical
3. Copy [`templates/pull_request_template.md`](templates/pull_request_template.md) to `.github/pull_request_template.md`.
4. Copy [`templates/review-gate.yml`](templates/review-gate.yml) to `.github/workflows/review-gate.yml`.
5. Have the owner install the workflow, gate and config together on the default branch in a reviewed merge. There is no fallback to PR code. Target branches must contain the gate and config too.
6. Open a fresh test PR after that merge: the trusted workflow only starts enforcing once installed on the default branch. Have the owner make its **`review-gate`** job a required status check. In this repository, add it to the **Protect Main** ruleset alongside **`checks`**.
   - **Private repository on GitHub Free?** Required checks aren't available there, so the gate runs but can't block a merge: never merge a PR whose `review-gate` check failed. Making it blocking needs a public repository or GitHub Pro, Team or Enterprise Cloud. See [Limits](docs/review-categories.md#limits).

The trusted workflow uses `pull_request_target`, checks out only the base SHA's gate and config, and runs no PR code. It uses the automatic read-only GitHub token for metadata; it needs no configured secrets. Complete API file lists are bound to the PR's head SHA, base SHA, description and changed-file count. Missing, malformed, partial or stale API evidence fails closed.

Upgrading from v0.1: replace the adopter workflow and gate together, add `gate_paths` from the example with your installed paths, and keep every listed path control-critical. Rules must have nonempty reasons and patterns. Bare N/A evidence now needs a reason, domain labels must occur once, and result cells need an exact result word. `ci.yml` remains PR-controlled build/test feedback; it is not the trusted enforcement path.

The gate checks a PR description's structure. It does not prove evidence is true or that a review is authentic, and it never grants merge authority. See [docs/review-categories.md](docs/review-categories.md) for the rules and their limits.

## Quick start: the local task runner

Copy the placeholder config/profile in [`runner/`](runner/) into private local files, choose a dedicated feature worktree and agent home, and enqueue an explicitly approved assignment. Review-only jobs let the primary request an independent read-only check of committed work. Workers leave files uncommitted; the trusted runner validates paths and makes commits under a locally configured runner identity. Local receipts always persist, including commit/refusal metadata for the future digest; GitHub receipt comments are opt-in and contain only fixed summaries and commit metadata.

Read [setup, commands, compatibility limits and recovery](docs/task-runner.md) before running it. Agents are defined by local command/version/permission configuration; all phases start fresh and the runner never reads agent session files. The shipped Codex example accepts one recorded CLI version. Nothing installs or starts a service automatically; the launchd file is a placeholder template.

## Optional v0.4 lifecycle and digest

The [combined setup guide](docs/v04-lifecycle-digest.md) describes separate bot identities, owner approval, a default-branch guard template and local polling. It is opt-in: existing standalone runner configurations keep working. No workflow, token, label or local service is installed automatically. The digest uses fixed-field facts and a bounded optional Claude API introduction; model failure still produces the factual sections. Live acceptance remains pending dedicated identities and the owner's merge.

## Optional v0.5 agent pool

The [agent pool](docs/agent-pool.md) runs approved, non-conflicting stories concurrently in isolated worktrees, with cross-slot fresh review, owner-set budgets and post-merge revalidation. Pool mode accepts any command-line agent satisfying the runner contract; pool and lead-and-team modes are [live-tested with two Codex slots](docs/agent-pool-live-trial.md). Setup stays private and opt-in. Lifecycle-managed refresh is covered by real-policy integration regressions; see the [setup and limits](docs/agent-pool.md).

## Using this repository

This repository uses its own gate on its own pull requests: [`.github/review-gate.json`](.github/review-gate.json) is both its config and a worked example.

Local checks:

```sh
python -m pip install -r requirements-dev.txt
python scripts/check_private_content.py
python -m ruff check .
python -m pytest -q
```

## Licence

[MIT](LICENSE).
