# Recorded GitHub responses

Real responses captured from GitHub, for tests that must not rely on hand-written fakes (see `docs/design/safety-net.md`). Captured on 15 November 2026 with read-only `GET` requests and an owner token, unmodified. Every repository involved is public and contains no private content.

| File | Request | What it shows |
| --- | --- | --- |
| `smoke-pr2-head-check-runs.json` | `GET /repos/akhilvis-oss/adt-smoke/commits/<PR #2 head>/check-runs` | The `review-gate` check run of a `pull_request_target` workflow is attached to the pull request's **head** commit, beside `checks` and `lifecycle`. |
| `smoke-pr2-base-check-runs.json` | `GET /repos/akhilvis-oss/adt-smoke/commits/<PR #2 base>/check-runs` | The base commit carries only checks from its own push. There is no `review-gate` run for the pull request there. |
| `smoke-pr2-review-gate-run.json` | `GET /repos/akhilvis-oss/adt-smoke/actions/runs/43620608660` | The trusted gate's workflow run: event `pull_request_target`, workflow path, `head_sha` equal to the pull request's head. Its `pull_requests` list is **empty**, because PR #2 was already closed when this was recorded. |
| `smoke-pr2-closed-unmerged.json` | `GET /repos/akhilvis-oss/adt-smoke/pulls/2` | A pull request closed without merging. |
| `smoke-pr1-merged.json` | `GET /repos/akhilvis-oss/adt-smoke/pulls/1` | A merged pull request, with its merge commit. |
| `smoke-pr3-open.json` | `GET /repos/akhilvis-oss/adt-smoke/pulls/3` | An open pull request with its branch present (a probe opened for these recordings). |
| `smoke-pr3-head-check-runs.json` | `GET /repos/akhilvis-oss/adt-smoke/commits/<PR #3 head>/check-runs?filter=all&per_page=100&page=1` | The open pull request's head carries `review-gate`, `checks` and `lifecycle`, with `total_count` for paging. |
| `smoke-pr3-review-gate-run-open.json` | `GET /repos/akhilvis-oss/adt-smoke/actions/runs/43807983303` | The trusted gate's run **while the pull request is open**: `pull_requests` holds PR #3, with head and base SHAs. This is the binding `trusted_gate` checks. |
| `smoke-pr3-review-gate-run-closed.json` | the same request, seconds after closing PR #3 | The same run once the pull request is closed: `pull_requests` is empty. |
| `smoke-review-gate-workflow.json` | `GET /repos/akhilvis-oss/adt-smoke/actions/workflows/review-gate.yml` | Workflow metadata: `id` and `path`, which `trusted_gate` compares with the run's `workflow_id` and `path`. |
| `smoke-repo-owner-view.json` | `GET /repos/akhilvis-oss/adt-smoke` | A repository with auto-merge disabled (`allow_auto_merge: false`), as seen by the owner. Whether a GitHub App token sees `allow_auto_merge` is recorded by the live smoke test, not here. |
| `adt-issue122-one-click-approval-events.json` | `GET /repos/akhilvis-oss/agentic-delivery-team/issues/122/events` | A one-click approval: the owner adds `status:approved` while `status:draft` is on, and the workflow removes `status:draft` seconds later. |

The PR #3 recordings were captured on 16 November 2026. Opening and closing PR #3 in adt-smoke was the only write; the recordings themselves are read-only `GET` responses.

To add a recording, capture it with a read-only request, check it with `scripts/check_private_content.py`, add a row here, and don't edit the response by hand.
