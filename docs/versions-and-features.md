# Versions and feature switches

A project uses **one kit version** for everything, and turns **features on or off**. One small file in your repository records both: `.github/agentic-kit.json`, copied from [`templates/agentic-kit.example.json`](../templates/agentic-kit.example.json).

The hosted lifecycle guard reads `features.lifecycle` from the trusted default branch after
checkout. `false` skips Python setup and the guard; `true` or a missing kit file keeps
the existing guard on. Invalid or unreadable settings fail with a fixed message. Other
switches are conventions: agents are told to read and respect it; changing one does not
itself start or stop a local component. See the [roadmap](../ROADMAP.md#later).

```json
{
  "kit": "agentic-delivery-team",
  "version": "v0.5.6",
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

"Where is this project?" then has a one-line answer, for example: *v0.5.6, everything on except the lifecycle and stand-up.*

## Features

| Feature | What it is | Needs |
| --- | --- | --- |
| `review_gate` | Trusted review gate and its categories | Gate script, config, workflow; Actions policy on public repositories |
| `product_files` | `PRODUCT_AGENT.md`, `docs/product/`, story template, troubleshooting playbook, dry-run guide (`docs/dry-run.md`) | Copy from the starter kit |
| `runner` | Task runner: delegated builder, fresh reviewer, runner-made commit, runner-run tests | Private runner install and config on your machine |
| `pool` | Several agent slots in parallel with cross-review | `runner`, plus a private pool config |
| `lifecycle` | Status labels, guard workflow, approval by label, automatic dispatch | `runner`, plus the guard workflow, public policy file, labels, bot Apps and the service on your machine |
| `standup` | Daily stand-up issue | `lifecycle`, plus a stand-up config |

## Upgrading
Follow `UPGRADE.md` at the verified release tag. Every framework file is updated from
that release; adopter-owned settings change only as the release notes specify. A
missing current version, a locally edited framework file, or an existing lifecycle
workflow without an explicit `true` switch is an owner question. The release's checker
validates the committed result; private service work is listed separately.

## Turning a feature on

Use one owner-approved story to set its boolean and populate its installed settings,
then perform only the authorized private setup. All carried files are already present.
For lifecycle, `false` skips the hosted guard; `true` loads its public policy. A missing
kit record remains compatible with older installations by keeping the guard on. Do not
remove the kit record to disable a feature. Local runner, pool and stand-up startup
remain explicit owner-authorized operations.

## Rules
- The file is owner-merged: it sits under `.github/`, which the gate template already treats as control-critical.
- Agents read it to know which features they may rely on; a feature that is off must not be assumed.
- The `version` is the kit release your files came from, not the version of any single piece.
