# Recorded GitHub responses

Real responses captured from GitHub, for tests that must not rely on hand-written fakes (see `docs/design/safety-net.md`). Captured on 15 November 2026 with read-only `GET` requests and an owner token, unmodified. Every repository involved is public and contains no private content.

| File | Request | What it shows | Used by |
| --- | --- | --- | --- |
| `smoke-pr2-head-check-runs.json` | `GET /repos/akhilvis-oss/adt-smoke/commits/481c020dbe22cdf5dc088b7266c5839abe967d0e/check-runs` | The `review-gate` check run of a `pull_request_target` workflow is attached to the pull request's **head** commit, beside `checks` and `lifecycle`. | Provenance reference; open-PR gate replay uses PR #3 below. |
| `smoke-pr2-base-check-runs.json` | `GET /repos/akhilvis-oss/adt-smoke/commits/59b42d0fa8592be51cbec044ec595edead325467/check-runs?filter=all&per_page=100&page=1` | The base commit carries only checks from its own push. There is no `review-gate` run for the pull request there. | `test_recorded_base_has_no_gate`; `test_recorded_gate_binds_open_pr_head` (regression route) |
| `smoke-pr2-review-gate-run.json` | `GET /repos/akhilvis-oss/adt-smoke/actions/runs/43620608660` | The trusted gate's workflow run: event `pull_request_target`, workflow path, `head_sha` equal to the pull request's head. Its `pull_requests` list is **empty**, because PR #2 was already closed when this was recorded. | Provenance reference; open-PR gate replay uses PR #3 below. |
| `smoke-pr2-closed-unmerged.json` | `GET /repos/akhilvis-oss/adt-smoke/pulls/2` | A pull request closed without merging. | `test_recorded_merge_delivery` |
| `smoke-pr1-merged.json` | `GET /repos/akhilvis-oss/adt-smoke/pulls/1` | A merged pull request, with its merge commit. | `test_recorded_merge_delivery` |
| `smoke-pr3-open.json` | `GET /repos/akhilvis-oss/adt-smoke/pulls/3` | An open pull request with its branch present (a probe opened for these recordings). | `test_recorded_gate_binds_open_pr_head` |
| `smoke-pr3-head-check-runs.json` | `GET /repos/akhilvis-oss/adt-smoke/commits/c8220095becc13e362fee2c4f8c3c3de668230a7/check-runs?filter=all&per_page=100&page=1` | The open pull request's head carries `review-gate`, `checks` and `lifecycle`, with `total_count` for paging. | `test_recorded_gate_binds_open_pr_head` |
| `smoke-pr3-review-gate-run-open.json` | `GET /repos/akhilvis-oss/adt-smoke/actions/runs/43807983303` | The trusted gate's run **while the pull request is open**: `pull_requests` holds PR #3, with head and base SHAs. This is the binding `trusted_gate` checks. | `test_recorded_gate_binds_open_pr_head` |
| `smoke-pr3-review-gate-run-closed.json` | `GET /repos/akhilvis-oss/adt-smoke/actions/runs/43807983303` | The same run once the pull request is closed: `pull_requests` is empty. | `test_recorded_gate_binds_open_pr_head` |
| `smoke-review-gate-workflow.json` | `GET /repos/akhilvis-oss/adt-smoke/actions/workflows/review-gate.yml` | Workflow metadata: `id` and `path`, which `trusted_gate` compares with the run's `workflow_id` and `path`. | `test_recorded_gate_binds_open_pr_head` |
| `smoke-repo-owner-view.json` | `GET /repos/akhilvis-oss/adt-smoke` | A repository with auto-merge disabled (`allow_auto_merge: false`), as seen by the owner. Whether a GitHub App token sees `allow_auto_merge` is recorded by the live smoke test, not here. | `test_recorded_disabled_auto_merge_stops_delivery` |
| `adt-issue122-one-click-approval-events.json` | `GET /repos/akhilvis-oss/agentic-delivery-team/issues/122/events?per_page=100&page=1` | A one-click approval: the owner adds `status:approved` while `status:draft` is on, and the workflow removes `status:draft` seconds later. | `test_recorded_one_click_approval_ignores_workflow_draft_removal` |

The PR #3 recordings were captured on 16 November 2026. Opening and closing PR #3 in adt-smoke was the only write; the recordings themselves are read-only `GET` responses.

To add a recording, capture it with a read-only request, check it with `scripts/check_private_content.py`, add a row here, and don't edit the response by hand.

## Replay tests

Run `python -m pytest -q -p no:cacheprovider tests/test_github_replay.py`.
`RecordedGitHub` in `tests/recorded_github.py` replaces the real client's HTTP
opener with exact method/path routes (including query strings). It returns the
recorded bytes; an unknown request fails with its method and path. Authentication
uses a synthetic preverified identity. No network transport is available.

Delivery replay isolates unrelated admission, review and check-evidence
prerequisites with synthetic results; repository metadata and pull requests pass
through the real client and delivery decisions. Recorded JSON is never modified.
The closed unmerged PR has a nonempty `merge_commit_sha`, exercising the need to
check `merged` rather than merely the presence of that SHA.

### Regression evidence

Temporary source mutations were applied individually, tested, and restored:

| Temporary regression | Selected test | Observed result |
| --- | --- | --- |
| Read gate checks from `pr["base"]["sha"]` instead of head | `test_recorded_gate_binds_open_pr_head` | 2 failed, 8 deselected |
| Skip the `allow_auto_merge: false` stop | `test_recorded_disabled_auto_merge_stops_delivery` | 1 failed, 9 deselected; unrecorded auto-merge request rejected |
| Treat an unlabeled event as returning canonical state to draft | `test_recorded_one_click_approval_ignores_workflow_draft_removal` | 1 failed, 9 deselected |

These are deliberate negative experiments, not failures of the restored code.
The approval replay also checks the exact prefix before and after the workflow's
draft removal: both retain the same owner approval event. Final full-suite results
are reported in the worker handoff for the runner to include in PR evidence.
