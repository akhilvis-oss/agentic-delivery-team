# Starter kit

Everything a new project copies to adopt the whole way of working, not only the review gate and task runner. Copy the files, replace every `<PLACEHOLDER>`, and commit them in one reviewed PR.

## What's here

| File | Copy to | Purpose |
| --- | --- | --- |
| [`AGENTS.md`](AGENTS.md) | `AGENTS.md` | Working agreement every coding agent reads: boundaries, assumptions, escalation, must-ask items, review and merge policy |
| [`PRODUCT_AGENT.md`](PRODUCT_AGENT.md) | `PRODUCT_AGENT.md` (and your product chat's instructions) | Rules for whichever AI assistant you use to shape the roadmap and stories |
| [`docs/product/vision.md`](docs/product/vision.md) | `docs/product/vision.md` | Why the product exists, for whom, and what it will not do |
| [`docs/product/roadmap.md`](docs/product/roadmap.md) | `docs/product/roadmap.md` | Ordered outcomes, each linked to its stories |
| [`docs/product/decision-log.md`](docs/product/decision-log.md) | `docs/product/decision-log.md` | Settled decisions and accepted assumptions, newest first |
| [`.github/ISSUE_TEMPLATE/story.yml`](.github/ISSUE_TEMPLATE/story.yml) | `.github/ISSUE_TEMPLATE/story.yml` | Story template: context, verbatim acceptance criteria, must-ask overrides |

From the kit's own folders, also copy:
- the review gate: [`gate/review_gate.py`](../gate/review_gate.py), [`templates/review-gate.example.json`](../templates/review-gate.example.json), [`templates/pull_request_template.md`](../templates/pull_request_template.md) and [`templates/review-gate.yml`](../templates/review-gate.yml) ([setup](../docs/quickstart.md#install-the-review-gate-10-minutes));
- the task runner, if you delegate to coding agents locally ([setup](../docs/task-runner.md)).

## Set up a new project, in order

1. **Write the product docs first.** Fill in `vision.md` and `roadmap.md` with your product chat (see `PRODUCT_AGENT.md`). Agents read these as the truth, so keep them short and current.
2. **Make product docs owner-controlled.** In `.github/review-gate.json`, add `docs/product/*` to the **control-critical** patterns, so no agent can change scope or priorities without your decision.
3. **Fill in `AGENTS.md`:** your repository name, the must-ask list for your domain (payments, personal data, real-world actions, and so on), and your build and test commands.
4. **Install the review gate** and make its check required where your GitHub plan allows.
5. **Write stories with the story template.** Approve them before any agent starts.
6. **End every product session with a handoff:** the chat updates the product docs and creates or updates the issues, so nothing agreed lives only in chat memory.

## Principles behind the files

- **The repository is the memory.** Chats and agents start fresh; vision, roadmap, decisions and acceptance criteria live in versioned files and issues.
- **The owner decides; agents propose.** Approval, acceptance, merges of control-critical changes and releases are the owner's.
- **Assumptions over interruptions.** Agents record reasonable assumptions and keep going; they escalate only on conflicts, must-ask items or costly-to-reverse choices.
- **Evidence over claims.** Every change carries its tests and verbatim criteria; reviews are independent of the author.
