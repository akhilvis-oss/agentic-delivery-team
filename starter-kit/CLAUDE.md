# Product-session rules

Use these rules in the chat where the owner shapes the vision, roadmap and stories with an AI assistant. Put this file in the repository root, and paste the same rules into your chat project's instructions.

## 1. Read the repository first
Before any product answer, recommendation or change, read the latest `docs/product/vision.md`, `docs/product/roadmap.md`, `docs/product/decision-log.md` and the relevant issues from the default branch. Do not rely on earlier parts of the conversation for what the product docs say.

## 2. State what you read
Start every product answer with one line naming the version you used, for example:

> Based on roadmap at commit `abc1234` (9 Nov).

If that commit is older than the latest one, say so and re-read before answering.

## 3. The repository wins
If the conversation and the repository disagree, point out the conflict and ask which is right. Never silently use the conversation's version.

## 4. Nothing agreed lives only in chat
A decision is real only once it is written to the repository. End every session with a **handoff**:
- update `docs/product/roadmap.md` and append to `docs/product/decision-log.md`;
- create or update the issues, using the story template, with context, rationale and verbatim acceptance criteria;
- leave new stories unapproved: the owner approves them.

Product-doc changes go through a pull request that the owner merges.

## 5. Keep sessions short
Start a fresh conversation for each roadmap or story session, seeded from the repository. Long conversations are where stale context builds up.
