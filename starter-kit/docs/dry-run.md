# Dry run before approval

Writing a story ends with a dry run: an independent agent acts as the builder would, just before building, and reports everything that would make the builder stop. The owner then approves a story that has already been checked against the real code and environment. Rule 6 of `PRODUCT_AGENT.md` makes it part of every story.

## What goes in, and what doesn't

Give the dry-run agent only:
- the draft story;
- the repository at a named commit;
- the product rules and standards (`AGENTS.md`, `docs/product/`) and the review-gate rules (`.github/review-gate.json`);
- a scratch copy of the repository with the pinned tool versions where they can be installed. The report names any difference.

Don't give it the conversation or your reasoning. It hasn't seen how the story was written, so it isn't grading your work.

## Ground rules

- **It never changes the real checkout.** It runs commands only in the scratch copy. If a setup command fails (for example a `cd` into the scratch copy), it stops; it never falls back to the real checkout.
- **It separates what it verified (read or ran) from what it inferred.**
- **It makes no product decisions.** It proposes wording, and the product agent answers in the story. The owner confirms.

## Steps

1. **Check every fact the story cites.** File paths, line numbers, function names and current behaviour. Wrong line numbers are the most common finding.
2. **Find every caller and consumer.** Search widely for everything that uses what's changing: scripts, the UI, exports, other modules, tests that list files, docs indexes. This finds the biggest problems.
3. **Check safety and integrity.** Could data be attributed to the wrong record? Do request and security limits still hold? Is anything stored or published that shouldn't be?
4. **Run the baseline.** Run the repository's full checks in the scratch copy first, so a later failure can be told apart from one that already existed.
5. **Prototype the change.** Build the full change where that's affordable, and the riskiest part at least. List each existing test that fails, by type of change, and check it against the story's must-asks. A failure they don't allow becomes a blocker.
   - **Measure the story's Impact budgets** against the baseline from step 4: suite and CI time, agent sessions per story, GitHub requests per poll, production lines added and removed, new configuration keys, statuses, codes or adopter-copied files, and new owner steps. A breach the story doesn't justify is a blocker.
6. **Check the criteria against each other, the standards and the code.** Report as a **blocker** any check required by the story’s criteria that is missing from the runner’s required check commands (`verification.commands`, or the pool’s `test_commands`). The operator configures these commands; stories do not choose them. Look for contradictions, ambiguity that would force the builder to stop and ask, and criteria that can't be tested with the harness that exists.
7. **Work out the review tier.** Use the gate's own path rules, and name anything that would raise the tier. Also check which paths the coding agent can't commit: some deny rules live in the runner's private configuration, so ask the owner for them, or treat agent rule files and workflow files as owner-delivered.
8. **Check the environment.** Separate what passed in the scratch copy from what needs the owner's machine (live data, a browser, credentials, sign-ins).
9. **Report.** Write the findings and the environment section in the format below.

## Report format

- **Findings,** numbered, each tagged **blocker**, **should-fix** or **note**, with file and line evidence and a suggested wording fix.
- **Environment:** what ran, on which versions, the results, and what only the owner's machine can check.

The product agent fixes every blocker and dry-runs again, up to three passes in total, then records a `Dry run` line in the story (rule 6).

## What it can miss

- **A prototype narrower than the real build.** A full implementation can touch tests the rough prototype never reached.
- **Test assertions that contain a value the story changes.** The dry run may name the test but miss that its expected text includes the value. Writing must-asks as the types of change allowed, not as lists of specific tests, makes this sturdier.
- **Paths the runner refuses to commit** when its deny rules live in private configuration the dry-run agent can't see (step 7).

## Cost

About 6 to 15 minutes and 160,000 to 215,000 tokens per story, in one adopting project. That's cheap next to one builder stop and re-approval.

## Setting it up in Claude Code

Define a sub-agent in the product checkout's `.claude/agents/` folder (for example `<PRODUCT_CHECKOUT>/.claude/agents/dry-run.md`) whose instructions are this guide, and create the scratch copy outside the checkout (for example `<SCRATCH_DIR>`).

Be aware that:
- with the default settings, a sub-agent inherits the session's permissions, so keeping it off the real checkout is by instruction only;
- the product settings will ask before it creates the scratch copy or runs tests;
- a `git worktree` scratch copy also writes to the real checkout's `.git`;
- the settings' deny rules may also block edits to rule files in the scratch copy;
- the sub-agent file should stay uncommitted (for example listed in `.git/info/exclude`), so coding agents don't load it.
