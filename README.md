# agentic-delivery-team

An open-source kit for building software with AI coding agents while you stay the product owner. Agents do the building; the kit adds the checks and balances a good team has, so you only spend time on decisions that need you.

**Guardrails the agent can't talk its way around.** Most agent frameworks guide agents with prompts. This kit enforces the owner's rules outside the agent, on GitHub: a review gate in CI the agent can't edit, a runner that makes the commit instead of the agent, approval only the owner can give, and separate bot identities so the record shows who did what. The guardrails are code, not prompts, so they behave the same whichever AI model or agent you use.

> **Status: experimental (version 0).** Each piece is used on a real project before release. Expect changes between versions. See [Limits](#limits) before relying on it.

## How it works

You work the way a product owner works with a team:

1. **You shape the product** with any AI assistant (the *product agent*): vision, a roadmap split into sections and milestones, and stories with acceptance criteria and test scenarios. Everything agreed is written to the repository, so no decision lives only in chat memory.
2. **You approve a story.** Nothing starts before that.
3. **A coding agent builds it.** It records reasonable assumptions and keeps going, and asks you only when a choice conflicts with what you approved, touches a must-ask area (money, personal data, credentials and so on) or would be costly to undo.
4. **A separate agent reviews it**, fresh, read-only, against your criteria. Serious findings go back for rework; at most two rounds, then it escalates.
5. **A pull request opens with its evidence.** A review gate decides how much evidence it needs from the files it changes. Low-risk changes merge on passing checks; high-risk ones wait for you.
6. **You read a short stand-up** listing only what needs you, each item linked to the issue or PR to act on.

GitHub is the record throughout: issues, labels, pull requests and reviews hold the history, so work survives a lost chat or a restarted agent.

## What's in the kit

| Piece | What it does | Status | Docs |
| --- | --- | --- | --- |
| **Starter kit** | Agent rules (`AGENTS.md`), product agent rules, product docs and a story template to copy into your project | Unreleased | [`starter-kit/`](starter-kit/) |
| **Review gate** | Four review categories (light, standard, domain-critical, control-critical) derived from changed files and enforced in CI | [v0.2.0](https://github.com/akhilvis-oss/agentic-delivery-team/releases/tag/v0.2.0) | [Review categories](docs/review-categories.md) |
| **Task runner** | Delegates one approved task to a coding agent, gets an independent review, and makes the commit itself after checking the changed paths | [v0.3.0](https://github.com/akhilvis-oss/agentic-delivery-team/releases/tag/v0.3.0) | [Task runner](docs/task-runner.md) |
| **Task lifecycle and stand-up** | One status label per issue, guarded transitions, owner-only approval, bot identities, and a factual stand-up digest | [v0.4.0](https://github.com/akhilvis-oss/agentic-delivery-team/releases/tag/v0.4.0) | [Lifecycle and digest](docs/v04-lifecycle-digest.md) |
| **Agent pool** | Several agent slots build approved stories in parallel and review each other's work, within owner-set budgets | [v0.5.0](https://github.com/akhilvis-oss/agentic-delivery-team/releases/tag/v0.5.0) | [Agent pool](docs/agent-pool.md) |
| **Private-content check** | Catches personal paths and private names before they reach a public repository | Released with v0.2.0 | [`scripts/`](scripts/check_private_content.py) |

The kit works with any coding agent you can run from the command line: agents are defined as commands in configuration, with no per-agent code. Tested with Codex.

## Proven on a real project

The kit was built by using it. Its first outside proof was a real story in a private stock-research app: a discovery screen that silently cut results at 20 was changed to show match counts, the limit and a clear-search control.

- The story was approved by the owner on its issue before any agent started.
- The first two runs **stopped safely**: one on a mis-quoted test command, one on a too-strict stop rule. Nothing was committed, and both receipts were kept.
- The third run completed in one round. The runner made the commit, a fresh read-only reviewer checked it against the acceptance criteria and found nothing, and 56 tests passed.

The task lifecycle was proven separately in a live trial on this repository, including a bot that tried to approve its own work and was reverted by the guard ([evidence](https://github.com/akhilvis-oss/agentic-delivery-team/issues/38#issuecomment-6745339854)).

## Get started

Start small and add pieces as you need them. **[Quickstart](docs/quickstart.md)** walks through each level.

| Level | You get | Time |
| --- | --- | --- |
| 1. Rules and review gate | Agents follow one working agreement; every PR is reviewed in proportion to its risk | About 15 minutes |
| 2. Task runner | Bounded delegation with an independent reviewer and runner-made commits | About an hour |
| 3. Lifecycle and stand-up | Approval-driven dispatch, guarded states and a daily digest | Half a day, plus bot identities |

## Limits

Read these before relying on the kit:

- **The gate checks structure, not truth.** It confirms a PR carries the evidence its category needs; it can't prove the evidence is true or that a review was independent.
- **Coding agents may push through your GitHub account.** Until they have their own identity ([#31](https://github.com/akhilvis-oss/agentic-delivery-team/issues/31)), the merge policy is followed by agents, not enforced by GitHub. The lifecycle and stand-up already use separate bot identities.
- **macOS first.** Bot tokens are minted from keys in the macOS Keychain; other platforms need an equivalent key store.
- **Public repositories need one setting from 2 December 2026.** GitHub will block `pull_request_target` workflows by default. Allow it with an Actions policy that targets only the gate and lifecycle workflows; both run trusted code from your default branch and never run pull-request code. See the [quickstart](docs/quickstart.md).
- **Private repositories on GitHub Free can't block merges.** Required checks need a public repository or a paid plan; there the gate warns but can't stop a merge.
- **Experimental.** Version 0 may change behaviour between releases. The task runner is tested with Codex; other agents need a configuration entry and a test run.
- **Parallel stories need the opt-in agent pool.** Without it, stories in a project run one after another. Pool budgets rely on usage the agents report, so treat them as advisory; the fixed reservation per phase is the real safeguard.
- **You remain responsible.** The kit reduces the review burden; it doesn't remove the need for the owner's judgement on what ships.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md); report vulnerabilities privately, not in public issues. Changes are listed in [CHANGELOG.md](CHANGELOG.md).

This repository uses its own gate on its own pull requests: [`.github/review-gate.json`](.github/review-gate.json) is both its config and a worked example. Local checks:

```sh
python -m pip install -r requirements-dev.txt
python scripts/check_private_content.py
python -m ruff check .
python -m pytest -q
```

## Licence

[MIT](LICENSE).
