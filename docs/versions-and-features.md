# Versions and feature switches

A project uses **one kit version** for everything, and turns **features on or off**. One small file in your repository records both: `.github/agentic-kit.json`, copied from [`templates/agentic-kit.example.json`](../templates/agentic-kit.example.json).

```json
{
  "kit": "agentic-delivery-team",
  "version": "v0.5.3",
  "features": {
    "review_gate": true,
    "product_files": true,
    "runner": false,
    "pool": false,
    "lifecycle": false,
    "standup": false
  }
}
```

"Where is this project?" then has a one-line answer, for example: *v0.5.3, everything on except the lifecycle.*

## Features

| Feature | What it is | Quickstart level | Needs |
| --- | --- | --- | --- |
| `review_gate` | Trusted review gate and its categories | 1 | Gate script, config, workflow; Actions policy on public repositories |
| `product_files` | `PRODUCT_AGENT.md`, `docs/product/`, story template, troubleshooting playbook | 1 | Copy from the starter kit |
| `runner` | Task runner: delegated builder, fresh reviewer, runner-made commit, runner-run tests | 2 | Private runner install and config on your machine |
| `pool` | Several agent slots in parallel with cross-review | 2 | `runner`, plus a private pool config |
| `lifecycle` | Status labels, guard workflow, approval by label, automatic dispatch | 3 | Guard workflow, public policy file, labels, bot Apps, the service on your machine |
| `standup` | Daily stand-up issue | 3 | `lifecycle`, plus a stand-up config |

## Upgrading
Upgrade **all pieces together**, never one at a time:
1. One pull request copies every kit file you use from the new release tag (gate, workflows, templates, starter-kit files you haven't customised) and sets `version` to that tag. Your own configuration and edited product docs stay yours.
2. Update the private install on your machine (runner, pool, service) to the same tag.
3. Read the release notes for anything that needs a setting or a migration.

## Turning a feature on
Turning a feature on is one story: flip its switch to `true` in the same pull request that adds what it needs (for the lifecycle: the guard workflow, policy file and labels), then do the on-machine steps (for example start the service). Turning a feature off is the reverse.

## Rules
- The file is owner-merged: it sits under `.github/`, which the gate template already treats as control-critical.
- Agents read it to know which features they may rely on; a feature that is off must not be assumed.
- The `version` is the kit release your files came from, not the version of any single piece.
