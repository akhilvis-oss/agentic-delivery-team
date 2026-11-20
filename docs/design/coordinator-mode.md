# Coordinator mode: design note

Status: a proposal for discussion, filed by the product session on 20 Nov 2026 after an owner discussion. It's planned for after M1. Nothing here is approved.

## The idea

The runner hosts one long-lived **coordinating agent** session. That agent plans the work, starts sub-agents for parallel tasks, uses a second member when a second slot is free, and asks product-clarification agents simple questions. When it needs the owner, it moves a story to a stop.

This is how the 20 Nov batch ran by hand. An agent coordinated two members and their sub-agents, GitHub was the record, and the owner relayed questions. The result was 15 PRs merged in a day, with 0 blockers found in post-merge review.

## Two principles

### 1. The agent directs the work; the runner keeps the authority

The coordinating agent and its sub-agents never hold GitHub credentials, and never commit, push, label, merge or post records themselves. Every action that changes something outside a worktree goes through a small set of runner tools. The runner checks each call as it checks phases today:

| Tool | What the runner checks |
| --- | --- |
| `checkpoint(worktree)` | Commits only through the runner's commit policy and deny patterns, on that sub-agent's own branch |
| `submit_delivery(story, worktree)` | Today's admission, required tests (the trusted run), fingerprints and delivery rules |
| `request_review(story, head)` | Starts a **fresh** reviewer session that the runner runs, with no coordinator context |
| `ask_owner(story, question)` | Posts through the public-question filter, and stops the story as "Needs your decision" |
| `ask_product(story, question)` | A clarification agent answers "how" questions from the product docs. The answer is labelled as an agent answer. Scope or criteria changes always go to the owner |
| `mark_blocked(story, reason)` | A stop with a fixed reason code |
| `start_subagent(role, story)` | Hands out a worktree and branch, and records the sub-agent in the ledger |

The coordinating agent gets read-only GitHub access. So text in a story, a comment or code that misleads it can't act with the owner's rights. That's the main risk of today's manual setup, where the coordinating agent works through the owner's account.

### 2. "Always active" means it can be resumed from saved state at any moment

Long sessions end: their memory fills up, a process crashes, a usage limit is hit, or a machine restarts. On 20 Nov the coordinating agent sat idle for about two hours, and the product session's own memory filled once. So nothing may depend on what's in a session's memory. The runner can always start a new coordinating session that carries on, **including the sub-agents' work**.

## The ledger: what's saved, and when

The runner keeps a private ledger in its state folder. The agent writes to it only through the runner's tools. Each entry is written **before** the step it describes (write-ahead), so a crash at any point leaves a known state.

**For the coordinator:**
- the plan: the stories in flight and their order;
- the decisions taken, and the owner answers received, each with its GitHub link;
- open questions, and what each is waiting for;
- the next intended action of each story;
- a short handoff note, rewritten at every step boundary.

**For each sub-agent:**
- its ID, role and story;
- its worktree, branch and base commit;
- its last checkpoint commit;
- the CLI session ID, when the CLI can resume it;
- its status: running, waiting, done or failed;
- what it was asked to do, and its own handoff note at its last checkpoint;
- its budget used.

GitHub stays the public record. The coordinator posts its progress through the runner, as Codex did on #188 on 20 Nov.

## Resuming

1. **A heartbeat.** The runner watches the coordinator's heartbeat, and its own phase timeouts. A stall, a crash, a full memory or a usage limit (#211) triggers a resume.
2. **A new coordinator session** starts with the ledger, the current GitHub state and a fixed resume prompt. It doesn't need the old conversation.
3. **It reconciles each sub-agent before starting anything new:**
   - **still running:** it reattaches, by session ID or by waiting for the next checkpoint;
   - **stopped, with a resumable session ID:** it resumes that session with its handoff note;
   - **stopped, with no resumable session:** it starts a fresh sub-agent from its last checkpoint commit and handoff note;
   - **uncommitted changes in a worktree:** never trusted silently. The runner's integrity rules apply, as for saved work today. The diff is shown to the new sub-agent, which either commits it through `checkpoint` or discards it.
4. **No work is done twice.** A step whose ledger entry exists but whose effect isn't confirmed is checked against GitHub or git before it's retried. That's the same rule the outbox already uses for records.

## What carries over from M1

- **Records, admission, `can_start`, kinds of stop and one notice per stop:** they now sit at the runner's tool boundary, not in a fixed phase sequence.
- **The trusted test run and the fresh reviewer:** unchanged, and started by the runner.
- **Owner decisions** arrive as before, through labels and comments.

## A cheaper step first

Let the worker phase use sub-agents inside its own session, which both CLIs support, with the runner unchanged. That gets most of the parallelism within one story and changes nothing about trust. The coordinator trial below adds parallel work across stories, and resuming.

## The trial, and its exit test

Run on an adopting project, never this repository, with read-only GitHub access for the agent.

1. **Kill mid-story:** stop the coordinator while two sub-agents are running. It resumes, both finish, and nothing is done twice.
2. **A full memory:** force the coordinator's memory to fill. A new session carries on from the ledger alone.
3. **A usage limit:** it waits and resumes under #211's rules.
4. **No direct writes:** the agent's GitHub token can't write. Every change shows the runner's identity.
5. **Independent review:** the reviewer session has none of the coordinator's context.
6. **Cost:** measure cost and time per story against the runner's per-phase sessions on the same stories.

## Questions for the owner

1. Should clarification agents also answer "how" questions on approved stories, as the product session did for #202 on 20 Nov, or only before approval?
2. What's the right heartbeat: how long can the coordinator be silent before the runner resumes it?
3. Does the coordinator get a budget per story, or per night?
