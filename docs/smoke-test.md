# Live lifecycle smoke test

Run one tiny story with the real configured agent before adopting candidate framework code. The harness belongs to the installed, pinned framework; it downloads the public candidate into a disposable install and starts that candidate's standalone lifecycle service as a child. It never reconfigures the running service, pinned install or lifecycle state. Pool execution and automatic PR/release smoke checks are outside this command's scope.

## Setup

Copy `runner/smoke.example.json` to a private local file and populate every placeholder. Use numeric App IDs, an agent name of at most 80 lowercase letters/digits/underscores/hyphens starting with a letter or digit, absolute executable and agent home paths, and the CLI version and model settings used by your agent. No dependency is installed by the harness. Its Python must already provide the required development tools. `runner.agent` follows the standalone runner interface; the supplied command is a Codex example. The only permitted agent environment setting is `CODEX_HOME={agent_home}`. Agent sign-in lives in the separately configured smoke agent home. The candidate's operating-system HOME is disposable.

Use a dedicated public scratch repository, different from the framework repository. Install the starter rules, trusted lifecycle workflow/policy and review gate from your approved release there. Match the scratch policy's owner and runner to the smoke Apps; the runner also has the Product role. Use the standard template transitions, a `main` base, and set `trusted_gate_workflow` to the installed gate workflow path. Require `checks` and `review-gate` on main, and disable auto-merge. The scratch owner must be an App identity distinct from the smoke runner. The smoke story adds only `smoke_add.py` and `test_smoke_add.py`; remove previous merged copies before running it.

Create two Apps installed **only** on that scratch repository:

- Smoke runner: Contents, Issues and Pull requests read/write; Checks and Actions read.
- Smoke owner: Issues read/write only.

Store their private keys in the local Keychain using the existing App authentication setup. The framework runner App remains installed on the framework repository. The owner must add **Commit statuses: Read and write** to that App and accept the updated installation permissions. The harness requests a separate, one-repository token with only `statuses: write` (plus GitHub's mandatory metadata read). No Contents or Issues write permission is requested for this token.

Use the same private `lock_dir` for every invocation targeting a scratch repository. Keep it outside the running service's state. The per-repository nonblocking lock refuses concurrent runs; any open issue or PR also refuses admission. Use a separate scratch repository for each independent harness machine; the local lock is not a distributed lock.

## Run

From the installed framework root, with that install's Python:

```sh
python -m lifecycle.service --config /absolute/private/smoke.json smoke --candidate <COMMIT_OR_TAG>
```

The `lifecycle/service.py` CLI accepts the same arguments through its module entry point. Candidate commits, tags and branch refs are fetched anonymously over HTTPS from `framework_repository`. The resolved full commit receives `smoke/<agent_name>` in that repository. Adopters set `framework_repository` to their public framework source and `repository` to their own public scratch repository; scratch and framework credentials must remain distinct.

The owner App files the installed harness's `tests/smoke/story.md` and adds `status:approved` once. The scratch workflow attests approval; the candidate claims, builds, verifies, reviews and publishes. The default deadline is 1,800 seconds; `timeout_seconds` changes it. Child and agent processes are stopped at completion or timeout.

A pass requires GitHub evidence for all six stages: attested approval; dispatch and committed build; a review returning `ready_for_integration`; a smoke-runner PR on that reviewed commit; a successful trusted `review-gate` on the PR head; and `needs-owner` with `owner_merge_required` on that same job, commit and PR. Until M3, an actual merge is not a pass. No candidate state file is used as evidence. Failures print the furthest completed stage and a fixed stop category; private agent output and exception text are never published.

After pass, failure or timeout, the harness attempts to close the story PR and issue, close the candidate's heartbeat report, and delete matching story branches and saved continuation references. Local installs, worktrees and continuation refs disappear with the temporary directory. Cleanup errors make the run fail; reconcile leftover scratch resources manually before retrying. An uncertain issue creation is looked up by its unique run marker and closed without approving or repeating the write. A process crash, machine shutdown or unavailable GitHub can still require manual reconciliation. The harness does not retry ambiguous writes.

Exit status is zero only when every stage and cleanup passed and the commit status was posted. A refused status post prints the result plus a missing/unavailable permission message and exits nonzero. Check the App permission and installation acceptance before retrying. This advisory status is not automatically wired into PR delivery or releases.

## Verification and limits

Offline tests use synthetic clients and the existing GitHub recordings; they never contact GitHub or run an agent. Run `python -m pytest -q -p no:cacheprovider`, `python -m ruff check --no-cache .`, and `python scripts/check_private_content.py` before review.

The live exit test is an owner step after merge: grant status permission, write the private config, re-pin the service, then smoke-test main and the owner-prepared schema-regression branch. Main must pass; the regression must fail at review. This worker does not perform the live run or change the scratch repository.

Known limit: the candidate runs as the **same OS user** as the harness. Its configuration and environment contain no framework key, token, Keychain reference or framework repository authority. Only scratch runner App references are exported; the smoke owner key stays in the harness. This is credential minimization, not an OS security boundary: candidate code can still attempt to access that user's files or Keychain. Operating-system separation of keys from code under test comes in M4. Use only owner-approved candidate code under this temporary exception.
