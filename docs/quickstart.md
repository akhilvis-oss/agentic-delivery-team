# Quickstart

Three levels; each works on its own. Start with level 1 and add the others when you need them.

You need: a GitHub repository you own, Python 3.11 or later, and at least one coding agent you can run from the command line.

## Level 1: rules and review gate (about 15 minutes)

You get one working agreement for every agent, and pull requests reviewed in proportion to their risk.

### Add the rules (5 minutes)

1. Copy the files listed in the [starter kit](../starter-kit/README.md) into your repository, and copy [`templates/agentic-kit.example.json`](../templates/agentic-kit.example.json) to `.github/agentic-kit.json`. It records your kit version and which features are on; see [versions and feature switches](versions-and-features.md).
2. In `AGENTS.md`, replace every `<PLACEHOLDER>`: project name, repository, build and test commands, and the must-ask items for your domain.
3. Paste the rules from `PRODUCT_AGENT.md` into the instructions of the chat you use to plan the product. To plan with Claude Code instead, see [Claude Code as the product agent](product-agent-claude-code.md).
4. Write a first version of `docs/product/vision.md`. A few lines is enough; you will refine it with the product agent.

### Install the review gate (10 minutes)

1. Copy [`gate/review_gate.py`](../gate/review_gate.py) to `scripts/review_gate.py`.
2. Copy [`templates/review-gate.example.json`](../templates/review-gate.example.json) to `.github/review-gate.json` and edit:
   - `repo_url` and `allowed_bases`;
   - `requirement_ids` (your ID scheme, or `null`);
   - `domain_evidence`: what domain-critical evidence means for your project;
   - the path patterns for each category; add `docs/product/*` to **control-critical** so only you can change scope;
   - `gate_paths`: the gate's own code, config, workflow and tests, which must stay control-critical.
3. Copy [`templates/pull_request_template.md`](../templates/pull_request_template.md) to `.github/pull_request_template.md`.
4. Copy [`templates/review-gate.yml`](../templates/review-gate.yml) to `.github/workflows/review-gate.yml`.
5. Commit all of this in one pull request and merge it yourself. The gate only enforces once it is on the default branch; it never runs code from a pull request.
6. **Public repository?** From 2 December 2026 GitHub blocks `pull_request_target` workflows by default, and the gate needs that event. Go to **Settings → Actions → Policies → New policy → Import a ruleset** and choose [`templates/actions-policy-pull-request-target.json`](../templates/actions-policy-pull-request-target.json). It allows `pull_request_target` (and `issues`, for the lifecycle) only for `.github/workflows/review-gate.yml` and `.github/workflows/task-lifecycle.yml`. That is safe for these workflows because they run only your default branch's code and never run pull-request code. Don't allow `pull_request_target` for all workflows.
7. Open a small test pull request and check that the **`review-gate`** check runs.
8. Protect your default branch: **Settings → Rules → Rulesets → New ruleset → Import a ruleset**, and choose [`templates/branch-ruleset.json`](../templates/branch-ruleset.json). It requires pull requests, blocks force-pushes and deletion, and requires the `checks` and `review-gate` checks with the branch up to date. If your CI job isn't called `checks`, change that name in the file first. Required checks need a public repository or a paid plan.
   - **Private repository on GitHub Free?** Required checks aren't available, so the gate warns but can't block a merge. Never merge a PR whose `review-gate` check failed.

### Use it

- Write each story with the story template and approve it before an agent starts.
- Agents open PRs with the template filled in; the gate tells them if the category they declared is too low.
- Merge by category, as the merge policy in `AGENTS.md` describes: light on passing checks, higher categories with an independent review, control-critical by you.

Details and limits: [review categories](review-categories.md).

## Level 2: task runner (about an hour)

You get bounded delegation: a coding agent does one approved task, a fresh read-only reviewer checks it against the criteria, at most two correction rounds, and the runner makes the commit after checking the changed paths.

1. Read [the task runner guide](task-runner.md) first, especially the safety and recovery sections.
2. Copy the placeholder config and profile from [`runner/`](../runner/) into private local files, outside your repository.
3. Define your agents (command, version check, permissions) in that config.
4. Create a dedicated feature worktree for the runner.
5. Enqueue one small, approved task and run it once. Check the receipt and the commit before trusting it with more.

## Level 3: lifecycle and stand-up (half a day, plus bot identities)

You get approval-driven dispatch from issue labels, guarded state changes that only the right identity can make, and a factual stand-up issue listing what needs you.

This level needs separate GitHub identities for the automation, never your own token: create one GitHub App for the product agent and one for the runner, each limited to issues (and, for the runner, pull requests) on the repositories that use them.

Follow [the lifecycle and digest guide](v04-lifecycle-digest.md). It covers labels, the guard workflow, identities, local polling and limits.

> **Status:** released in [v0.4.0](https://github.com/akhilvis-oss/agentic-delivery-team/releases/tag/v0.4.0) after a combined live trial. Bot tokens currently use the macOS Keychain.
