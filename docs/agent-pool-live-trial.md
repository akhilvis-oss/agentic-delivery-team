# Agent-pool live trial: 10 November 2026

PASS on the historical issue #56 trial source (runtime corresponding to PR #58 head `98f8cd9fc223dc92adb7404cac2aab2d4903c60d`), based on main `99d43080a6dbb208ea121837523f70f52a988d27` (includes #51). The runtime source fingerprints below bind the evidence to the implementation; the source was not changed during this trial. Lifecycle enqueue-only and digest integration are covered by synthetic tests, not this local CLI trial.

A new local-only Git repository, four trivial approved tasks and two isolated Codex homes were used. No GitHub repository, issue, PR or service was created for the trial. All phases started fresh with Codex CLI `0.164.0-alpha.11.1`; reviewers used the configured read-only flags. Authentication-only copies were opaque; no session files were read.

Each task added a small Python double function and one focused unittest. The profile and task did not ask agents to commit. The trusted runner made the delivery commits. Pool mode built alpha and beta concurrently, then exchanged reviewer slots. A local-only fast-forward of alpha advanced main. Beta was rebased, explicit tests reran and a new exact-head cross-slot review passed. Lead-and-team then built delta on slot-1 and gamma on lead slot-0; lead reviewed delta, and slot-1 reviewed the lead's gamma. Queued branches were refreshed to new main before executing. No slot phase intervals overlapped on the same slot.

| Story | Phase | Slot | Exact input head |
| --- | --- | --- | --- |
| alpha | worker | slot-0 | `22bc59be5792707d6e7e7c39c2463e32c1193434` |
| beta | worker | slot-1 | `22bc59be5792707d6e7e7c39c2463e32c1193434` |
| beta | reviewer | slot-0 | `42d55ed2038fec3f3cb2b8c8f8d6298f53d9c2e3` |
| alpha | reviewer | slot-1 | `cbd1ca1310e8fe98b862548e0807df199cec6c3d` |
| delta | worker | slot-1 | `cbd1ca1310e8fe98b862548e0807df199cec6c3d` |
| beta | reviewer | slot-0 | `ce820c3643e95252c66251ffeb878f8635c8aa53` |
| delta | reviewer | slot-0 | `0f769fbf450eda7bc93bdce06206cbd7b0e5e756` |
| gamma | worker | slot-0 | `cbd1ca1310e8fe98b862548e0807df199cec6c3d` |
| gamma | reviewer | slot-1 | `c9dac05725ae4a772d1713196c0607c46aad1628` |

Final local states: alpha merged into the disposable local main; beta, gamma and delta ready_for_integration. These are local trial states, not merges into this kit or release approval. Every reviewer returned ready_for_integration without correction. The first two worker intervals overlapped; all nine phases ended normally.

Retained sanitized evidence includes job JSON, fixed receipts, phase times, budget ledger, local commit history and final states. Temporary repository, worktrees, state, logs, profiles and authentication-only copies were deleted after completion. Original credentials and unrelated projects/services were untouched.

## Usage and limits

Independent review subsequently reproduced a queued lifecycle-admission blocker after a clean main update. The owner authorized one additional correction round: invalidation is now local-only, normal admission is retained, and an actual new-head review is published only after tests, fresh review and renewed admission pass. Ten additional real-policy integration regressions cover queued and completed refresh plus revocations. That correction is synthetic-test evidence, not a rerun of this historical CLI trial. The original runtime fingerprints below are deliberately retained and do not describe the later corrected pool source.

All nine phases reported unavailable usage; reservations were retained rather than reporting zero cost. Budget used was slot-0: 25/100 owner units (5 unknown phases), slot-1: 20/100 owner units (4 unknown phases). This is an owner-set allowance estimate, not provider-credit or billing evidence. Live quota exhaustion, conflicts and outages were not induced; their failure/recovery paths are covered by synthetic tests. This local trial does not establish other-vendor compatibility or a required GitHub integration gate.

An earlier successful runtime trial encountered an evidence-wrapper save error when a generic helper attempted to chmod the shared temporary parent. Its completed results were recovered without rerunning models and its data deleted. The final trial used a corrected evidence wrapper and completed evidence retention and cleanup normally.

## Runtime source fingerprints

- `runner/pool.py`: SHA-256 `f3ebf3e316b586324cb91f1536764041247860634530436afeaf5adb356b6dcf`
- `runner/task_runner.py`: SHA-256 `66090cb22937581d6884bf6904abed7a2d74506d8caecba486b00314d980680b`
- `digest/standup.py`: SHA-256 `0351b934992f51f503c27797861667741e122749c97af5220fac25350f9c4c00`
