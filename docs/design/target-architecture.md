# Target architecture: a rolling backlog, loops and gates

Status: proposed for owner approval, 17 November 2026. The owner's decisions from 17 November are included (see the end). This note sets the direction after M1. It changes no code, and each part below is delivered as ordinary stories.

## The goal

The owner approves stories as they're ready, and the framework keeps delivering. Approved stories join a rolling backlog. The framework works out a build order from the design, not from approval order. It builds in parallel where that's safe, checks each story with code first, merges what policy allows, and stops only where a person must decide. Urgent work, such as P1 bugs, can move ahead of the queue. The owner keeps testing features as they arrive, and what they find feeds back into the backlog. Every merge leaves the framework a little better at the next story.

Today the owner does many steps in the middle: relays to operator agents, re-pins and smoke runs. As one source puts it, a human in the middle of the process "becomes the slowest node in it". The target is a human at two points only: approving stories, and merging the changes that are hard to undo.

## Five principles

1. **Checks before agents.** If code can check something, code does it. An agent saying "the tests pass" doesn't count; CI decides. Every acceptance criterion names its check, end-to-end where possible. Merging, ordering, removing duplicates and comparing before and after are code, not agent judgement.
2. **No change can edit its own judge.** Gate rules, protected tests and the reviewer's instructions are read from the target branch, never from the change under review.
3. **The record is the truth, and every run ends with one.**
   - Attempts, time, and later agent usage are capped.
   - A status never changes without the record that explains it. Optional extras, such as notifications, never block that record (the #158 lesson).
   - A stopped run's reason changes what runs next. A verdict that changes nothing next is only a report.
4. **Isolation.** Each agent session gets a fresh context, its own sign-in, and no credentials it doesn't need. Builds and tests run where a mistake can't reach keys or other repositories.
5. **Learning from results.** Every confirmed cause becomes a permanent rule: a checklist item, a test case or a constraint on how future work is split. A miss that doesn't become a rule will happen again.

## How the backlog runs

1. **Intake.** The product agent writes stories, and each criterion names its check. A story whose criteria code can't check goes back as a draft instead of being built.
2. **Approval.** The owner approves stories at any time, singly or several at once. Each approval stays bound to its story's exact text. An approved story joins the backlog.
3. **Priority.**
   - A story labelled `priority:high`, by the owner or by the product agent for a P1 bug, goes ahead of normal work.
   - It takes the next free lane once its dependencies have merged. Running stories aren't interrupted.
   - If it changes files that a queued story also changes, the queued story waits for it.
   - The product agent files P1 bugs found in delivered features as `priority:high` drafts, so a one-click approval puts them at the front.
4. **The architect plans, and re-plans.**
   - A new agent role keeps one delivery plan for the backlog, on a pinned plan issue. The plan sets the order, the parallel lanes, the real dependencies, and the stories that need the owner's merge.
   - A dependency is allowed only where the architect can name what one story uses from another. Stories whose files don't overlap run in parallel.
   - The architect re-plans when a story is approved or merged, when a story stops, and when priority changes. It reads a map of the code (what depends on what) to do this.
5. **The plan is checked.** A fresh session checks each plan change before it takes effect: no cycles, lanes whose files really don't overlap, and every story traced to its approved text. The owner can veto a plan change with a label. A wrong plan affects many stories; a wrong build affects one.
6. **Readiness, just in time.** The readiness check runs when each story is dispatched, not only when it's approved. A story written weeks earlier may no longer fit the code.
7. **The loop runs inside each story:** build, check, correct, repeat until the checks pass. A story gets three corrections.
8. **Code joins the results.**
   - Results are merged, ranked and de-duplicated by code.
   - Each lane's result is smoke-tested together with `main` before it merges.
   - An integration agent runs only when two lanes conflict. Its resolution is new code, so it's reviewed again.
9. **Gates decide by what a mistake would cost, not by confidence** (next section).
10. **Stop the line.** If `main` fails the smoke test after any merge, no new work is dispatched and nothing merges. Running stories may finish their builds. The fix goes ahead of everything else as `priority:high`, and the line restarts when `main` passes again. This stops one bad merge from spreading through the backlog.
11. **Reporting.** The daily stand-up and the plan issue show:
    - what merged, what stopped and why;
    - time and agent usage per story;
    - the queue with each story's priority;
    - P2 findings, filed as drafts at normal priority.

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

- **Correction: return the failing story, not the others.**
  - The return carries the story, the verdict, the reason, the evidence and a scope line ("fix only this").
  - Correct work is never rebuilt.
  - After three failed corrections, the problem is in the plan, so the story goes to the architect, and then to the owner.
  - Stories that depend on it wait; every other lane carries on.
- **Learning: a confirmed cause becomes a rule.**
  - Each confirmed cause becomes a constraint in the architect's brief, an item in the incident checklist, and, when it's a bug class, a replay case for the reviewer.
  - A proposed rule needs at least two occurrences, or one incident with a known cause.
  - A fresh session tries to show the rule is wrong, or would flag correct changes.
  - The owner approves it in a pull request, like any other control-critical change.

## The verification layer

This is what makes parallel work safe. In the Claude Code team's words, without verification loops "you can really only run one or two Claudes at a time".

- **An incident checklist triggered by the diff.** Each item has:
  - a trigger visible in a diff;
  - a check;
  - the incident behind it.
  
  The reviewer checks only the items that match. The first items come from incidents we've already had: #75, #110, #115, #127, the trailing-slash request (#153), the shared agent sign-in (#151) and the blocked stop record (#158).
- **Property tests for the lifecycle's rules,** for example:
  - a finished story never reopens;
  - exactly one status label;
  - no status changes without its record;
  - an approval is bound to the story's text;
  - a retry has the same effect as running once.
- **Differential tests for upgrades.** The old and new versions run on the same recorded GitHub inputs, and their decisions are compared against a list of approved differences.
- **End-to-end evidence for each criterion,** judged per criterion by the reviewer.
- **The reviewer:**
  - quotes the code it flags;
  - reads its instructions from the target branch;
  - builds each correction round on the previous round's findings, instead of starting over.
- **Each merge records how it was verified.** The checks run, the smoke result, the review round and the lane are recorded, so unverified merges can be found later.
- **The reviewer is measured.** Known bugs are replayed against it, and its catch rate, reverts and over-flagging are tracked. Its instructions are changed only after they pass those replays.
- **Observe before enforce.** A new gate runs in observe mode first, reporting what it would have blocked, then enforces.
- **Housekeeping:**
  - flaky tests are never skipped, delayed or retried to get a pass; they're fixed, or deleted with the reason written down;
  - checklists, tests and instructions are pruned on a schedule.

## Roles

| Role | Does | Never |
| --- | --- | --- |
| Owner | Approves stories; sets priority; merges the closed lane; decides what a story should do | Reviews intermediate output |
| Product agent | Writes stories with criteria that can be checked; files P1 bugs as `priority:high` drafts and P2 findings as normal drafts | Approves |
| Architect | Keeps the delivery plan: order, lanes, dependencies; re-plans as the backlog changes | Edits stories or code |
| Plan checker | Checks each plan change in a fresh session | Changes the plan |
| Scout | Gathers context for the architect and builders: the code map, affected files | Writes code |
| Builder | Builds one story in its own context | Changes the plan or the story |
| Reviewer | Judges one story's change against its criteria, quoting code | Edits anything |
| Integration agent | Resolves conflicts between lanes | Adds or changes behaviour |
| Code | Gates, merging, ranking, de-duplicating, comparing, budgets, priority order | Uses judgement |

Each role has its own agent and model in configuration. A cheaper model can scout, and a stronger one can build or plan. Changing a role's model is a configuration change, tested against the reviewer and smoke replays before use.

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
| **Safety net (now)** | Stop records always posted (#158); smoke test passing; automatic upgrades with rollback (#152), with a differential test before each upgrade |
| **M1** | As designed, plus property tests for its guards, criteria that name their checks, and a queue built for priority, lanes and a rolling backlog (`priority:high` replaces the M1 note's one-off `queue:next` label) |
| **M3** | Per-criterion verdicts with quoted code; reviewer instructions from the target branch; protected tests; the incident checklist; lanes by blast radius; a verification record on each merge |
| **Workers** (from M4, brought forward) | Builds and reviews as separate processes, one agent sign-in each, a configurable number of lanes, tests in a sandbox without keys |
| **Backlog delivery** | Architect, plan checker, scout and code map, integration step, priority, stop the line, budgets, per-story measurement, the learning edge |

**Exit test for backlog delivery.** On a scratch project, a backlog of at least ten approved stories is delivered with no owner action except merges in the closed lane. The ten include:
- one story that fails, which stops without blocking unrelated lanes;
- one pair with a real dependency, built in order;
- one `priority:high` story approved mid-run, which takes the next free lane;
- one deliberately broken merge, which stops the line until it's fixed.

Also:
- P2 findings arrive as drafts.
- A constraint learned from an early story appears in a later plan.

## Decided (owner, 17 November)

1. **Merges by blast radius.** Contained and wide lanes merge by themselves; the owner merges the closed lane.
2. **The architect's plan runs without approval.** The owner can veto a plan change with a label.
3. **A rolling backlog, not fixed batches.** Stories keep joining the queue as they're approved, and high-priority work (P1) can go ahead of it, so the owner keeps testing as features are delivered.
4. **Three corrections per story** before it goes back to the architect, then to the owner.

## Sources

- [How the Claude Code team thinks about verification](https://claude.dev/blog/how-the-claude-code-team-thinks-about-verification/) (Addy Osmani, 16 November 2026).
- "Loops and graphs", a course article on splitting work into checked units: loops inside units, a graph between them, gates by blast radius, and the two return paths.
- A "software factory" post listing ten parts: intake, plan, orchestrator, sandbox, gateway, context, gates, evals, measurement and seats.
- A "harness engineering" field guide on the Fable 5.2 launch: seats, briefs, gates and receipts.
