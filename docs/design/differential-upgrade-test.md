# Differential upgrade test

Design spike for #174. Proposal only; implements no upgrade check. This supports
the [target architecture's verification layer](target-architecture.md#the-verification-layer).

## Decision contract

A comparison runs a pinned installed commit and candidate commit against identical
recorded inputs, initial local state, configuration, clock and synthetic identity.
A decision comprises:

- Ordered attempted external writes: method, exact endpoint including query,
  canonical JSON payload, and GraphQL operation/variables. Preserve array and
  request order; exclude authentication headers. Capture before transport, even
  when an attempted write is rejected.
- Local outcome: job status, delivery stage, reason code, pending write intent,
  durable stop/outbox state, and completion versus exception or timeout.

Compare both parts. A merged delivery stage need not change the enclosing job
status during the same call. Normalize only explicitly named volatile fields;
never erase heads, approval bindings, labels, write ordering or stop reasons.
Keep read traces as diagnostics and coverage evidence.

An unrecorded method, endpoint, query or response sequence means **incomplete
comparison**, never equivalence. Fail closed without network fallback, including
when both versions miss the same route. Do not allowlist missing coverage as an
approved behavior difference. Registered writes need recorded responses before
execution can continue; capturing an intent does not prove successful delivery.

## Available evidence and gaps

`tests/fixtures/github/` contains 12 response files, catalogued in its README.
`tests/test_github_replay.py` defines scenarios; `tests/recorded_github.py` supplies
exact method/path replay through the production client's opener. The manifest
and replay layer currently accept reads only.

Existing scenarios cover open-PR head-bound trusted gate evidence, unavailable
gate evidence after closure, no gate on the base, merged versus closed-unmerged
delivery, disabled auto-merge, and one-click approval retaining its meaning after
workflow draft removal. Rejection tests cover unknown requests and manifest
mismatches. Some older gate responses are provenance references rather than
active routes.

None covers a full service poll. Delivery scenarios substitute admission,
review, category and merge evidence; notification and persistence are isolated.
Missing recordings include write responses, publication and merge failures,
pagination sequences, retry/recovery, concurrent head changes and complete poll
inputs/outcomes. Repository metadata reflects an owner view, not an App view.
New captures need separately authorized owner access; none were made here.

## Fresh offline experiment

Source archives of `f08ce2d5eee63d294e0353119c5c74a3941bdfb9` and
`a1ccc4158ab31bf54d2c53fa6b200860e92de113` were exported to an OS temporary
directory outside all checkouts. A newly written driver ran three delivery
scenarios through each commit's production `integration.advance` and GitHub
client. Both used the newer commit's unchanged recordings, replay transport and
example policy; the older commit lacks the replay module. No preserved spike
was used.

The driver supplied identical synthetic admission/review evidence and initial
jobs with stop notices already attempted, suppressed notifications and disk
persistence, and captured attempted writes at the opener. Every request used the
recording transport; socket creation and external process execution were also
denied during scenarios. Separate unknown GET and POST probes were rejected on
both commits.

| Recorded scenario | Old outcome | New outcome | Ordered writes |
| --- | --- | --- | --- |
| Merged PR | `ready_for_integration`, stage `merged`, no reason | Same | Both `[]` |
| Closed unmerged PR | `needs_owner`, stage `owner`, `merge_conditions_missing` | Same | Both `[]` |
| Open PR, auto-merge disabled | Replay abort; status `ready_for_integration`, stage `checking`, no reason | `needs_owner`, stage `owner`, `owner_merge_required` | Both `[]` |

The last scenario made two PR reads, then a repository metadata read. The old
client appended a trailing slash absent from the registered route; the new client
matched the recorded endpoint. Thus writes alone falsely suggest equality. The
old run is incomplete, not a successful baseline. These observations establish
limited delivery behavior, not full-poll or write-response coverage.

## Approved differences and placement

Propose a versioned JSON list bound to exact baseline/candidate SHAs, fixture
digest and scenario ID. Each entry specifies an exact decision field or ordered
write index, old value, new value, rationale and approval reference. Reject
wildcards, stale entries, unused entries and unexplained differences.

Propose owner approval of this list in a separate reviewed baseline change;
the candidate cannot approve its own exceptions. The owner decides this policy
before implementation.

Recommend both CI and `upgrade-check`, initially observe-only as the architecture
requires. CI validates the comparator and replays pinned versions; upgrades
compare the actual installed/candidate pair before switching installs. Keep this
offline phase separate from existing credential-minting upgrade checks. Enforcement
and workflow changes need their own approved story.

## Cost and proposed build stories

Three scenarios per version took approximately 0.25 seconds combined including
child startup, excluding exports. Scenario execution alone totaled approximately
0.014 seconds. This tiny sample is not a full-poll estimate. Expect two executions
per scenario, zero API calls or agent sessions, temporary exports and small JSON
artifacts. Measure full-suite runtime and bound time/disk before enforcement.

1. **Offline comparator.** Acceptance: isolated pinned versions receive identical
   inputs; reports include ordered writes and local outcomes; exact difference
   validation rejects stale/unused entries; negative tests prove empty writes
   cannot hide an abort and unknown requests cannot escape. Reuse existing inputs
   and report incomplete coverage.
2. **Coverage.** Acceptance: separately authorized captures support write responses
   and at least one full poll; replay tests demonstrate failure, recovery and
   concurrent-change outcomes without network access.
3. **Integration.** Acceptance: CI and upgrades produce observe-only reports;
   tests verify installed-pair selection, timeouts and preservation of the active
   install on failure. After owner review of observed results, separately approve
   enforcement.
