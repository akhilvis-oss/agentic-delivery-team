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
| Local task runner: primary agent delegates to a second agent, with independent read-only review and safe recovery | Planned | `runner/` |
| Stand-up digest: one plain-language summary of decisions, demos and risks waiting for the owner | Planned | `digest/` |
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

The trusted workflow uses `pull_request_target`, checks out only the base SHA's gate and config, and runs no PR code. It uses the automatic read-only GitHub token for metadata; it needs no configured secrets. Complete API file lists are bound to the PR's head SHA, base SHA, description and changed-file count. Missing, malformed, partial or stale API evidence fails closed.

Upgrading from v0.1: replace the adopter workflow and gate together, add `gate_paths` from the example with your installed paths, and keep every listed path control-critical. Rules must have nonempty reasons and patterns. Bare N/A evidence now needs a reason, domain labels must occur once, and result cells need an exact result word. `ci.yml` remains PR-controlled build/test feedback; it is not the trusted enforcement path.

The gate checks a PR description's structure. It does not prove evidence is true or that a review is authentic, and it never grants merge authority. See [docs/review-categories.md](docs/review-categories.md) for the rules and their limits.

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

<!-- Throwaway trusted-gate verification: documentation-only diff. -->
