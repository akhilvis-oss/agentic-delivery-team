# Recorded GitHub responses

Real responses captured from GitHub, for tests that must not rely on hand-written fakes (see `docs/design/safety-net.md`). Captured on 15 November 2026 with read-only `GET` requests and an owner token, unmodified. Every repository involved is public and contains no private content.

| File | Request | What it shows |
| --- | --- | --- |
| `smoke-pr2-head-check-runs.json` | `GET /repos/akhilvis-oss/adt-smoke/commits/<PR #2 head>/check-runs` | The `review-gate` check run of a `pull_request_target` workflow is attached to the pull request's **head** commit, beside `checks` and `lifecycle`. |
| `smoke-pr2-base-check-runs.json` | `GET /repos/akhilvis-oss/adt-smoke/commits/<PR #2 base>/check-runs` | The base commit carries only checks from its own push. There is no `review-gate` run for the pull request there. |
| `smoke-pr2-review-gate-run.json` | `GET /repos/akhilvis-oss/adt-smoke/actions/runs/43620608660` | The trusted gate's workflow run: event `pull_request_target`, workflow path, `head_sha` equal to the pull request's head, and the pull request it belongs to. |
| `smoke-pr2-closed-unmerged.json` | `GET /repos/akhilvis-oss/adt-smoke/pulls/2` | A pull request closed without merging. |
| `smoke-pr1-merged.json` | `GET /repos/akhilvis-oss/adt-smoke/pulls/1` | A merged pull request, with its merge commit. |
| `smoke-repo-owner-view.json` | `GET /repos/akhilvis-oss/adt-smoke` | A repository with auto-merge disabled (`allow_auto_merge: false`), as seen by the owner. Whether a GitHub App token sees `allow_auto_merge` is recorded by the live smoke test, not here. |
| `adt-issue122-one-click-approval-events.json` | `GET /repos/akhilvis-oss/agentic-delivery-team/issues/122/events` | A one-click approval: the owner adds `status:approved` while `status:draft` is on, and the workflow removes `status:draft` seconds later. |

To add a recording, capture it with a read-only request, check it with `scripts/check_private_content.py`, add a row here, and don't edit the response by hand.
