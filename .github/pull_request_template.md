<!-- Review categories: CI derives the minimum category from the changed paths
(.github/review-gate.json in this repo); declare that category or a higher one. Fill the
sections your category needs and delete the rest:
  light            -> Common
  standard         -> Common + Standard
  domain-critical  -> Common + Standard + Domain
  control-critical -> Common + Control (+ Domain if domain files changed)
The gate checks structure, not truthful evidence or authenticated approval.
Never include credentials or private data.
-->

<!-- ===== Common: every PR ===== -->
## Review tier
Tier: REQUIRED: light | standard | domain-critical | control-critical

## Maker summary
Maker context: REQUIRED: task/run/person reference (not a secret).
REQUIRED: what changed and why, scope/exclusions, known limitations and rollback.

## Test evidence
Tested head: REQUIRED: full 40-character PR head SHA (standard and above).
REQUIRED: exact commands and results/counts; mark skipped or NOT RUN with reason.

## Assumptions
REQUIRED: reasonable choices made within approved scope, or "None". Escalate instead
if an assumption conflicts with approved direction, enters a must-ask category or
creates a costly-to-reverse commitment.

<!-- ===== Standard (also domain-critical). Control-critical uses the Control
sections instead of Acceptance evidence. Delete for light. ===== -->
## Original acceptance criteria
Issue: REQUIRED: full issue URL in this repository.
- AC-1: REQUIRED: original criterion, verbatim.

## Acceptance evidence
| Criterion | Evidence | Result |
| --- | --- | --- |
| AC-1 | REQUIRED: test/command/path | REQUIRED: PASS / FAIL / XFAIL / SKIP / NOT RUN + detail |

## Security review
REQUIRED: one line on credentials, external access/cost and data (standard), or the
three labelled lines below (control-critical):
Secrets: no values; metadata-only findings or justified N/A.
External access/cost: actual access, costs, approvals or justified N/A.
Data/provenance: sources, privacy and limits or justified N/A.

<!-- ===== Domain: when runner or digest files changed (domain_evidence in
.github/review-gate.json). ===== -->
## Failure-mode verification
Failure cases: REQUIRED: tests for crashes, timeouts, bad input and partial results, or N/A with reason.
Recovery: REQUIRED: tests that restart or retry safely without duplicating work, or N/A with reason.
Concurrency: REQUIRED: tests for locks, races and parallel runs, or N/A with reason.

<!-- ===== Control-critical. Delete for other categories. ===== -->
## Acceptance and evidence matrix
| Criterion | Changed paths | Commands and results | Evidence link | Maker state | Checker verdict |
| --- | --- | --- | --- | --- | --- |
| AC-1 | REQUIRED: paths | REQUIRED: command and result, or NOT RUN with reason | REQUIRED: durable evidence | NOT VERIFIED | PENDING |

## Risk and scope
- [ ] SCOPE-1: Correct target branch; no direct change to a protected branch or unauthorized merge.
- [ ] SCOPE-2: No secret or private data exposure.
- [ ] SCOPE-3: Small reversible scope; restricted actions have linked owner approval; rollback documented.

## Independent Checker
Verdict: PENDING
Checker: NOT ASSIGNED
Checker context: NOT ASSIGNED
Reviewed head: NOT RECORDED
Review: NOT RECORDED

<!-- The Maker leaves PENDING. An independent reviewer posts a GitHub review against
the original criteria and the exact head, with their own commands/results, and a
verdict of PASS / CHANGES REQUIRED / BLOCKED. Every new head invalidates an old
verdict. Owner merge authority is separate from this record. -->
