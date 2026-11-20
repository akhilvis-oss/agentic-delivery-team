# Seven statuses: design note for #180

Status: proposal by the product session, 20 Nov 2026, for the owner's review. It settles the questions #180 needs answered before its dry run. Facts are from `main` at `9748e2a`.

## What #180 changes

M1 design note section 1 replaces today's 13 statuses with 7:

| New | Replaces |
| --- | --- |
| `draft` | `draft` |
| `approved` | `approved` |
| `working` | `dispatched`, `in-progress`, `committed`, `changes-requested`, `needs-primary` |
| `in-review` | `in-review` |
| `needs-owner` | `needs-owner`, `blocked` |
| `pr-open` | `pr-open` |
| `merged` | `merged`, `accepted` |

## Findings that shape the design

1. **An adopter's rules file and guard stay as last copied (#171, AC-5), but the service follows `main`.**
   - `load_config` refuses a rules file whose states aren't exactly `STATES` (`lifecycle/core.py`, `set(cfg["transitions"]) != STATES`). So a service on the new code refuses the old 13-state file. #169's confirmation timer would then roll the upgrade back. That's safe, but the adopter stays stuck.
   - Worse, if the new service ran against the old guard copy, the guard would treat `status:working` as an unknown label and restore the old one. The two would fight.
2. **The smoke test can't pass on the #180 candidate** until the scratch repository's installed rules and guard are updated. A project following `main` then never receives #180, which is safe but needs an owner step.
3. **`needs-primary` is both a stop and a trigger.**
   - It's in every stop set (`core.py`, about 10 places, and `service.py`).
   - The service starts the automatic continuation from the label: `claim_event` admits only `approved` and `needs-primary`.
   - #203 keeps that trigger. Folding `needs-primary` into `working` removes it.
4. **Label history is replayed by name.** `canonical_state` and #184's `owner_finish_event` walk `labeled` events with today's names. Every open issue's history holds old names, and so does the history of every issue that ever existed.
5. **`record_status` doesn't exist.** #180's AC-3 cites it as coming from #175, but #175 didn't add it. Records store `from` and `to` with today's names.
6. **`accepted` behaves differently from `merged` in one place.** #177's `can_start` lets a re-approved `accepted` story start again, because only the latest record counts for `accepted`. Under `merged`, no re-approval can start a finished story.
7. **Status labels are written in four places:** `set_state`'s `PUT`, used for every move and restoration, plus three direct `DELETE` calls in the guard (one removing `status:approved`, two removing `status:draft`). The smoke harness also adds `status:approved` and `status:draft` to its own scratch issues.
8. **Job statuses are a separate vocabulary** (`queued`, `running_worker`, `failed`, …), mapped to lifecycle states by `job_records`. They don't change; only the mapping does.

## Decisions proposed

### A. Names are mapped wherever they're read, and never rewritten

- **One table, `LEGACY_STATUS`,** maps each old name to its new one. It's used by `state()`, `canonical_state`, `owner_finish_event`, `records()` (for `from` and `to`), `permitted`, the stand-up and the smoke harness.
- **Stored records and label history are never rewritten.** This is new code, not the missing `record_status` (finding 5).

### B. The rules file carries a version, and the service refuses a mismatch instead of fighting

- **`.github/task-lifecycle.json` gains `"statuses_version": 2`.** A file without the field is version 1.
- **The service on the new code accepts only version 2.** On version 1, it doesn't start, and its notice says: "This framework version needs the seven-status rules and guard. Owner: copy them from the framework, run `migrate-labels`, then restart."
- **An upgrade check refuses a candidate whose required version is higher than the installed rules file's.** It posts the same notice, so a project following `main` stays on its current version with a clear reason, never half-upgraded (finding 1).
- **The guard on the new code accepts only version 2 as well.** The guard and rules file ship together in the owner-delivered PR, so in one repository they can't disagree.

### C. The automatic continuation is triggered by a record, not a label

- **The `needs_primary_review` stop becomes `working` with that reason.** The service starts the automatic continuation when the story is `working` and its newest runner record has that reason, with no claim running for it. Limits are unchanged.
- **This changes #203's trigger.** #180 lands after #203, so it edits #203's code path and says so (finding 3).

### D. `blocked` and `needs-owner` merge; the kind of stop says why

#182's four kinds already tell the owner what's needed. Both labels become `needs-owner`, and the notice's kind ("Needs a fix", "Waiting on the system", …) carries the difference. The stand-up groups by kind, not by label.

### E. `accepted` becomes `merged`; `delegate_acceptance` is removed

- A story the owner accepted is finished. Re-approving a finished story can't start it, which is the same rule as for `merged` (finding 6).
- To redo work, write a new story, as #184 already says for a mistaken finish.
- **This changes #177's `can_start`,** and the story says so.

### F. One function writes status labels

- **`set_status(issue, target)`** adds the target, then removes each other `status:*` label it read. `remove_status` removes one named label. A single `PUT` would lose a label added after its read, so it isn't used. If a guard run sees the brief two-label moment, it restores the same target with one extra notice, which is accepted (#221).
- **Every other status write goes,** including the guard's direct `DELETE` calls, which become calls to these. A test fails if another call site writes a `status:*` label. `set_state` stays as a wrapper.
- **The exceptions,** named in the test: the smoke harness's own scratch-issue labels, and creating an issue with its first label (`followups`).

### G. Migration

- **`migrate-labels` runs as the workflow identity** and rewrites only open issues, and only those with an old label: `dispatched`, `in-progress`, `committed`, `changes-requested`, `needs-primary`, `blocked` and `accepted`. `draft`, `approved`, `in-review`, `needs-owner`, `pr-open` and `merged` keep their names.
- **Each change is a same-state move under the new rules,** for example `working` → `working` or `needs-owner` → `needs-owner`, so the guard accepts it with no restoration and no notice. `accepted` → `merged` is the one real move, and the workflow is already allowed to make it.
- **It needs the service stopped.** It refuses to run while any claim is active.
- **It has `--dry-run`, it's idempotent, and it never touches closed issues.**

## Rollout, in order

1. **This repository:** stop the service, merge the owner-delivered PR (code, rules file and guard together), run `migrate-labels --dry-run`, then `migrate-labels`, then restart the service.
2. **The smoke scratch repository:** the owner installs the new rules and guard. Then the smoke test can pass on `main` (finding 2).
3. **Each adopting project:** its service stays on the last pre-#180 version, with the notice from B, until the owner copies the rules and guard and runs `migrate-labels`.

## Proposed split

#180 is the largest M1 story. Split it into three, each with its own dry run:

| Story | Contains | Delivery | Weight (of #180's 15%) |
| --- | --- | --- | --- |
| **#180a, one status function** | F only, with today's 13 names. No visible change. | Codex may merge (rule 9) | 4% |
| **#180b, read-side mapping and versioned rules** | A and B. Version 1 only; a missing field means 1. No label changes yet. | Codex may merge (no `.github/` change) | 4% |
| **#180c, seven statuses** | C, D, E and G, the docs, and dropping version 1 | Owner-delivered | 7% |

These are filed as #221 (after #202), #222 (after #203) and #180, which is last. Only #180 needs the owner's merge.

## Questions for the owner

1. **Is E right?** That means an accepted story can't be restarted by re-approving; you write a new story.
2. **Should an adopting project left on the old version be told on its status comment only,** or also by a notice on a pinned issue?
3. **Is the three-way split right,** or do you want #180 kept as one story with the migration split out, as its builder note suggests?
