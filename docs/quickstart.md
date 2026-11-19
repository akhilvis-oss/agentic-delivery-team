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

## Upgrading

For adopting projects, upgrade every piece together, from one release tag:
1. Read that release's notes. They list each breaking change and the steps it needs.
2. Replace the repository files (gate, lifecycle code and workflow, templates you copied) in one pull request and merge it yourself.
3. Update your private configuration as the notes describe, then re-pin the lifecycle service to the tag.

For example, v0.5.5 requires an explicit `integration` setting in both private runner templates, and a new `needs-owner` → `merged` rule in `.github/task-lifecycle.json`.

This repository alone can opt into automatic `main` upgrades. Add a private
`upgrade` object (omit it to stay off):

```json
{"track": "main", "smoke_agent": "example-agent", "versions_dir": "/absolute/private/versions", "initial_commit": "0123456789abcdef0123456789abcdef01234567"}
```

Use the launcher's exact versions folder; symlinks and trailing slashes resolve
to the same folder. `initial_commit` is the last manually pinned commit and stays
the running baseline until the first confirmation. The configured service
repository is the framework repository; this is not an adopter release updater.
Only `serve` checks for upgrades. An idle poll requires the newest main commit's
successful `smoke/<smoke_agent>` status from `actors.runner`, read using a separate
runner status-only token, and a Product-token comparison proving it is ahead.
Active claims, finished claims with reconciliation errors and pending switches
all delay upgrades. Read/export outages retry while claims continue normally.

The service exports into `<versions_dir>/<commit>/install`, removes the download,
and runs `upgrade-check` in an isolated Python child with a 120-second limit.
That action imports code, loads config and mints configured App-role tokens;
it never starts an agent. Temporary GitHub failures (exit 75) retry. Unsafe
archives, definite check failures and check timeouts refuse that commit.
Passing checks save an atomic pending switch and exit cleanly for launchd.
The candidate ignores the saved cursor and confirms after reads and status
publication succeed, before any claim or job. Failed publication runs no jobs.
Upgrade and rollback notices persist in the status comment.

Install the launcher and replacement plist once as described in the
[lifecycle guide](v04-lifecycle-digest.md#optional-owner-run-macos-login-setup).
The launcher rolls back at the fourth unconfirmed start, or a start at least ten
minutes after the first. A hanging process still needs an owner restart after
the existing stale-heartbeat alert; automatic watchdog termination is a later
story. The launcher does not supervise the process. Keep watchdog and smoke-watch
jobs on a fixed approved install, rarely updated by hand. An unreadable switch
runs the first install and turns upgrades off until repaired or removed. Folder
mismatches also disable upgrades, with an explanatory status-comment notice.

### Files to carry by feature

Paths below are release-source paths. Keep your customised policy/config values when
upgrading; replace code and workflows from the same tag. Directory entries mean
**every file recursively**, including `__init__.py` and new modules; never copy only
`core.py`. The service uses a full private release export, not the guard's sparse
checkout.

| Feature | Release files to carry |
| --- | --- |
| Gate | `gate/review_gate.py`, `templates/review-gate.yml`, `templates/review-gate.example.json`, `templates/pull_request_template.md` |
| Lifecycle guard sparse checkout | `lifecycle/`, `.github/task-lifecycle.json` |
| Lifecycle guard workflow and policy source | `templates/task-lifecycle.yml`, `templates/task-lifecycle.example.json` |
| Lifecycle service | `lifecycle/`, `runner/`, `digest/`, `gate/review_gate.py`, `scripts/check_private_content.py`, `templates/task-lifecycle.example.json`, `templates/app-auth.example.json`, `templates/standup.example.json`, `templates/lifecycle-serve.plist.example`, `templates/lifecycle-launch.sh`, `templates/lifecycle-serve-upgrade.plist.example`, `templates/lifecycle-watchdog.plist.example` |
| Private-content check | `scripts/check_private_content.py`, `lifecycle/__init__.py`, `lifecycle/private_content.py` |
| Version record (all features) | `templates/agentic-kit.example.json` |

Install the gate code as `scripts/review_gate.py`, its workflow as
`.github/workflows/review-gate.yml`, its config as
`.github/review-gate.json`, and its PR template as `.github/pull_request_template.md`.
Install the lifecycle workflow as `.github/workflows/task-lifecycle.yml` and populate
`.github/task-lifecycle.json` from its example. Install the version record as
`.github/agentic-kit.json`. Private service configuration stays outside the repository;
copy the runner config/profile examples within `runner/` for both normal and primary
runners, and use the optional pool and scheduler examples only when enabled.

For v0.5.6 and later, replace the whole `lifecycle/` directory and the private-content script
together. v0.5.7 changes no workflow or `.github/` file; the optional smoke test needs its own
setup ([docs/smoke-test.md](smoke-test.md)). The guard workflow's sparse paths do not change. If the guard reports that
`lifecycle/private_content.py` cannot be loaded, restore the complete package from
the same release before retrying; no records were checked by that failed run.
