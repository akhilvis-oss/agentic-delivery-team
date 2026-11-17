# Target architecture: batches, loops and gates

Status: proposed for owner approval, 17 November 2026. This note sets the direction after M1. It changes no code, and each part below is delivered as ordinary stories.

## The goal

The owner approves a batch of stories, for example 25, and walks away. The framework works out the order, builds stories in parallel where it is safe to, checks each one with code first, merges what policy allows, and stops only where a person must decide. Each batch leaves the framework better at the next one.

Today the owner does many steps in the middle: relays to operator agents, re-pins and smoke runs. As one source puts it, a human in the middle of the process "becomes the slowest node in it". The target is a human at two points only: approving the batch, and merging the changes that are hard to undo.

## Five principles

1. **Checks before agents.** If code can check something, code does it. An agent saying "the tests pass" doesn't count; CI decides. Every acceptance criterion names its check, end-to-end where possible. Merging, ordering, removing duplicates and comparing before and after are code, not agent judgement.
2. **No change can edit its own judge.** Gate rules, protected tests and the reviewer's instructions are read from the target branch, never from the change under review.
3. **Every run has a budget and ends with a report.** Attempts, time, and later agent usage are capped. A stopped run leaves a readable reason on the issue and changes what runs next. A verdict that changes nothing next is only a report.
4. **Isolation.** Each agent session gets a fresh context, its own sign-in, and no credentials it doesn't need. Builds and tests run where a mistake can't reach keys or other repositories.
5. **Learning from results.** Every confirmed cause becomes a permanent rule: a checklist item, a test case or a constraint on how future work is split. A miss that doesn't become a rule will happen again.

## How a batch runs

1. **Intake.** The product agent writes stories. Each criterion names its check. A story whose criteria code can't check goes back as a draft instead of being built.
2. **Batch approval.** The owner approves the stories. Each approval stays bound to its story's exact text.
3. **The architect plans.**
   - A new agent role reads the batch, the design notes and a map of the code (what depends on what).
   - It posts a delivery plan on a batch issue: the order, parallel lanes, real dependencies, and the stories that need the owner's merge.
   - A dependency is allowed only where the architect can name what one story uses from another. Stories whose files don't overlap run in parallel.
4. **The plan is checked.** A fresh session verifies the plan before anything runs: no cycles, lanes whose files really don't overlap, and every story traced to its approved text. A wrong plan affects every story; a wrong build affects one.
5. **The loop runs inside each story:** build, check, correct, repeat until the checks pass.
6. **Code joins the results.** Results are merged, ranked and de-duplicated by code, and each lane's combined result is smoke-tested before it merges. An integration agent runs only when two lanes conflict. Its resolution is new code, so it is reviewed again.
7. **Gates decide by what a mistake would cost, not by confidence** (next section).
8. **Re-planning.** After each merge, the architect re-plans the remaining stories against the new `main`. The readiness check runs again when each story is dispatched, because a story written before the batch may no longer fit.
9. **The batch ends with one report:** what merged, what stopped and why, time and usage per story, and the P2 findings filed as drafts for the next batch.

## Gates by blast radius

| Lane | Examples | Opens when |
| --- | --- | --- |
| **Contained and reversible** | Docs, tests, a function with test coverage | Checks pass and review is clean. Merges by itself. |
| **Wide but reversible** | Shared code, schema additions, anything many callers use | Checks pass, the run history is clean, and smoke passes on the merged result. Merges by itself. |
| **Hard to reverse** | Framework control code, migrations, deletions, anything that moves money (for example a trading project's order code) | **Never by itself.** The owner merges. |

The third lane is a closed lane, not a high threshold. Thresholds get adjusted; closed lanes don't. A project's tier rules decide which lane each path belongs to.

Inside an open lane, the gate reads evidence in this order:
1. deterministic results;
2. this run's history;
3. how often work from this area has been reverted;
4. the agent's own assessment, last.

## Two ways back

- **Correction: return the failing story, not the batch.**
  - The return carries the story, the verdict, the reason, the evidence and a scope line ("fix only this").
  - Correct work is never rebuilt.
  - After three failed corrections, the problem is in the plan, so the story goes to the architect, and then to the owner.
  - Stories that depend on it wait; every other lane carries on.
- **Learning: an accepted cause becomes a rule.** Each confirmed cause becomes:
  - a constraint in the architect's brief, for how future work is split;
  - an item in the incident checklist (next section);
  - when it's a bug class, a replay case for the reviewer.

## The verification layer

This is what makes parallel work safe. In the Claude Code team's words, without verification loops "you can really only run one or two Claudes at a time".

- **An incident checklist triggered by the diff.** Each item has:
  - a trigger visible in a diff;
  - a check;
  - the incident behind it.
  
  The reviewer checks only the items that match. The first items come from incidents we've already had: #75, #110, #115, #127, the trailing-slash request (#153) and the shared agent sign-in (#151).
- **Property tests for the lifecycle's rules,** for example:
  - a finished story never reopens;
  - exactly one status label;
  - an approval is bound to the story's text;
  - a retry has the same effect as running once.
- **Differential tests for upgrades.** The old and new versions run on the same recorded GitHub inputs, and their decisions are compared against a list of approved differences.
- **End-to-end evidence for each criterion,** judged per criterion by the reviewer.
- **The reviewer quotes the code it flags.** It also reads its instructions from the target branch.
- **The reviewer is measured.** Known bugs are replayed against it, and its catch rate, reverts and over-flagging are tracked. Its instructions are changed only after they pass those replays.
- **Observe before enforce.** A new gate runs in observe mode first, reporting what it would have blocked, then enforces.
- **Housekeeping:**
  - flaky tests are never skipped, delayed or retried to get a pass; they're fixed, or deleted with the reason written down;
  - checklists, tests and instructions are pruned on a schedule.

## Roles

| Role | Does | Never |
| --- | --- | --- |
| Owner | Approves batches; merges the closed lane; decides what a story should do | Reviews intermediate output |
| Product agent | Writes stories with criteria that can be checked; files P2 findings as drafts | Approves |
| Architect | Plans the order, lanes and dependencies; re-plans after each merge | Edits stories or code |
| Plan checker | Verifies the architect's plan in a fresh session | Changes the plan |
| Builder | Builds one story in its own context | Changes the plan or the story |
| Reviewer | Judges one story's change against its criteria, quoting code | Edits anything |
| Integration agent | Resolves conflicts between lanes | Adds or changes behaviour |
| Code | Gates, merging, ranking, de-duplicating, comparing, budgets | Uses judgement |

## What stays with people

Architecture, new domains, large refactors, performance work, product discovery, the closed lane, and anything the plan checker or the readiness check can't settle. The framework handles work that is well specified and checkable.

## What we won't build

- **A hosted model gateway.** Switching agents is a configuration change through agent profiles and #53.
- **Tracking of engineers' own AI tools.**
- **Anything that only works while code sits on a vendor's servers.** Test: if a vendor stops tomorrow, the framework keeps running on the owner's machine and GitHub, with another agent.

## Build order

The verification layer comes first, then the lanes, then the learning edge, last, because there's nothing to learn from until something accepts results.

| Step | Adds |
| --- | --- |
| **Safety net (now)** | Smoke test passing; automatic upgrades with rollback (#152), with a differential test before each upgrade |
| **M1** | As designed, plus property tests for its guards, criteria that name their checks, and a queue built for lanes and batches |
| **M3** | Per-criterion verdicts with quoted code; reviewer instructions from the target branch; protected tests; the incident checklist; lanes by blast radius |
| **Workers** (from M4, brought forward) | Builds and reviews as separate processes, one agent sign-in each, a configurable number of lanes, tests in a sandbox without keys |
| **Batch delivery** | Architect, plan checker, code map, integration step, budgets, per-story measurement, the learning edge, the batch report |

**Exit test for batch delivery:**
- **On a scratch project:** an approved batch of at least ten stories, including one that fails and one with a real dependency, completes with no owner action except merges in the closed lane.
- **The failing story** stops without blocking unrelated lanes.
- **The P2 findings** arrive as drafts.
- **A constraint** from the first batch appears in the second batch's plan.

## Decisions for the owner

1. **Merge policy.** Keep merging by blast radius (contained and wide lanes merge by themselves), or have a person merge every pull request, as some setups do. This note assumes blast radius.
2. **Plan approval.** The architect's plan runs without the owner's approval (it changes order, not scope), and the owner can veto it with a label. Alternatively, require approval for batches over a size.
3. **Fixed batches or a rolling backlog.** This note assumes fixed batches.
4. **Budgets:** attempts per story (this note assumes three corrections), time per batch, and usage per batch.

## Sources

- [How the Claude Code team thinks about verification](https://claude.dev/blog/how-the-claude-code-team-thinks-about-verification/) (Addy Osmani, 16 November 2026).
- "Loops and graphs", a course article on splitting work into checked units: loops inside units, a graph between them, gates by blast radius, and the two return paths.
- A "software factory" post listing ten parts: intake, plan, orchestrator, sandbox, gateway, context, gates, evals, measurement and seats.
- A "harness engineering" field guide on the Fable 5.2 launch: seats, briefs, gates and receipts.
