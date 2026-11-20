# Live lifecycle smoke test

Run one tiny story with the real configured agent before adopting candidate framework code. The harness belongs to the installed, pinned framework; it downloads the public candidate into a disposable install and starts that candidate's standalone lifecycle service as a child. It never reconfigures the running service, pinned install or lifecycle state. The watcher below schedules main and PR candidates; pool execution and release-tag upgrades remain outside its scope. This repository can opt into main-track service upgrades using these results (see [Upgrading](quickstart.md#upgrading)).

## Setup

Copy `runner/smoke.example.json` to a private local file and populate every placeholder. Use numeric App IDs, an agent name of at most 80 lowercase letters/digits/underscores/hyphens starting with a letter or digit, absolute executable and agent home paths, and the CLI version and model settings used by your agent. No dependency is installed by the harness. Its Python must already provide the required development tools. `runner.agent` follows the standalone runner interface; the supplied command is a Codex example. The permitted agent environment mappings are empty, `CODEX_HOME={agent_home}`, or `CLAUDE_CONFIG_DIR={agent_home}`; no other keys or combinations are accepted. Agent authentication lives in the separately configured smoke agent home. The candidate's operating-system HOME is disposable.

Use a dedicated public scratch repository, different from the framework repository. Install the starter rules, trusted lifecycle workflow/policy and review gate from your approved release there. Match the scratch policy's owner and runner to the smoke Apps; the runner also has the Product role. Use the standard template transitions, a `main` base, and set `trusted_gate_workflow` to the installed gate workflow path. Require `checks` and `review-gate` on main, and disable auto-merge. The scratch owner must be an App identity distinct from the smoke runner. The smoke story adds only `smoke_add.py` and `test_smoke_add.py`; remove previous merged copies before running it.

Create two Apps installed **only** on that scratch repository:

- Smoke runner: Contents, Issues and Pull requests read/write; Checks and Actions read.
- Smoke owner: Issues read/write only.

Store their private keys in the local Keychain using the existing App authentication setup. The framework runner App remains installed on the framework repository. The owner must add **Commit statuses: Read and write** to that App and accept the updated installation permissions. The harness requests a separate, one-repository token with only `statuses: write` (plus GitHub's mandatory metadata read). No Contents or Issues write permission is requested for this token.

Use the same private `lock_dir` for every invocation targeting a scratch repository. Keep it outside the running service's state. The per-repository nonblocking lock refuses concurrent runs; any open issue or PR also refuses admission. Use a separate scratch repository for each independent harness machine; the local lock is not a distributed lock.

## Claude Code agent

Keep the base `runner/smoke.example.json` unchanged. In your private copy, set
`agent_name`, `runner.agent_home`, and both model placeholders for Claude Code,
and replace `runner.agent` with this entry:

<!-- claude-smoke-agent -->
```json
{
  "command": ["<ABSOLUTE_CLAUDE_EXECUTABLE>", "--bare", "--settings", "{agent_home}/settings.json", "--print", "--no-session-persistence", "--model", "{model}", "{role_flags}"],
  "version_command": ["<ABSOLUTE_CLAUDE_EXECUTABLE>", "--version"],
  "version_pattern": "^2\\.1\\.311 \\(Claude Code\\)$",
  "worker_flags": ["--tools", "Read,Edit,Bash", "--permission-mode", "dontAsk", "--allowedTools", "Read", "Edit", "Bash"],
  "reviewer_flags": ["--tools", "Read,Edit", "--permission-mode", "dontAsk", "--allowedTools", "Edit(/{output_file})"],
  "environment": {"CLAUDE_CONFIG_DIR": "{agent_home}"}
}
```

These argv templates were checked against installed `claude --help` and
`claude --version`, version **2.1.311 (Claude Code)**. Recheck your installed
version's help before changing the pinned pattern. The runner supplies the
prompt on stdin and starts each phase in its worktree; no shell wrapper or
stdout redirection is needed. Its prompt supplies the answer-file path and JSON
schema. Each phase must write its JSON answer there using Edit; stdout is not
the runner's answer channel. The worker can edit files and run the required
tests. The reviewer has exactly Read and Edit, no shell, and only the answer
file is allowed for edits. The existing runner fingerprint check rejects any
reviewer worktree mutation.

In the private agent home's `settings.json`, use only an `apiKeyHelper` setting:

```json
{
  "apiKeyHelper": "/usr/bin/security find-generic-password -w -s '<KEYCHAIN_SERVICE>' -a '<KEYCHAIN_ACCOUNT>' '<ABSOLUTE_KEYCHAIN_FILE>'"
}
```

Replace all placeholders privately. Store a capped Anthropic API key in that
Keychain item; never put the key itself in settings, smoke/runner/candidate
configuration or environment. The framework never reads, stores or passes the
Anthropic key. Only Claude Code invokes the helper, whose stdout must contain
the key. The helper names the Keychain file explicitly because its HOME is the
harness's disposable HOME. Do not add allow rules to this settings file.
Both phases use `--bare` **and** `--settings`: bare mode ignores the agent home's
automatic settings loading and excludes OAuth and built-in Keychain auth.
Unattended Claude smoke runs use API billing only, never a subscription login.

The [official CLI reference](https://code.claude.com/docs/en/cli-reference)
describes the command flags. For permission syntax, the
[official Read and Edit rules](https://code.claude.com/docs/en/permissions#read-and-edit)
cover writes (including creating a file), and its
[path pattern table](https://code.claude.com/docs/en/permissions#how-path-patterns-match)
defines `//` as the filesystem-root prefix. Thus `Edit(/{output_file})` expands
to `Edit(//...)` for the runner's absolute output path. The harness resolves its
temporary root before constructing candidate paths, avoiding macOS's symlinked
temporary-directory prefix in this exact-file permission rule.

Offline tests check the documented argv against an allowlist and run the
unchanged runner with a stand-in Claude command. They do not establish live
Claude permission or authentication behavior. After merge, the owner creates
the capped key, configures this private home and smoke config, and runs the
smoke test with Claude Code on main; main must pass. The schema-regression
exit test remains Codex-only because it exercises Codex's strict schema check.

## Run

From the installed framework root, with that install's Python:

```sh
python -m lifecycle.service --config /absolute/private/smoke.json smoke --candidate <COMMIT_OR_TAG>
```

The `lifecycle/service.py` CLI accepts the same arguments through its module entry point. Candidate commits, tags and branch refs are fetched anonymously over HTTPS from `framework_repository`. The resolved full commit receives `smoke/<agent_name>` in that repository. Adopters set `framework_repository` to their public framework source and `repository` to their own public scratch repository; scratch and framework credentials must remain distinct.

The owner App files the installed harness's `tests/smoke/story.md`. Before filing, the harness snapshots the latest issue workflow run ID. Before approving, it requires a successful run newer than that snapshot and waits for queued or running issue workflows to finish. Initially empty results do not establish that the new story's guard has finished. The wait has a two-minute budget from entry, including API reads; each polling request is capped by the remaining budget. Actions does not identify issue numbers on these runs, so this check conservatively includes all issue workflows in the dedicated scratch repository, including waiting, pending and requested runs. If processing cannot be confirmed within the budget, cleanup closes the story and the run ends as `busy` without a candidate status, allowing a later retry. The owner App then adds `status:approved` once. If that request times out or returns a server error, the harness reads the issue labels to confirm approval; it never repeats the write. The scratch workflow attests approval; the candidate claims, builds, verifies, reviews and publishes. The default deadline is 1,800 seconds; `timeout_seconds` changes it. Child and agent processes are stopped at completion or timeout.

A pass requires GitHub evidence for all six stages: attested approval; dispatch and committed build; a review returning `ready_for_integration`; a smoke-runner PR on that reviewed commit; a successful trusted `review-gate` on the PR head; and `needs-owner` with `owner_merge_required` on that same job, commit and PR. Until M3, an actual merge is not a pass. No candidate state file is used as evidence. Failures print the furthest completed stage and a fixed stop category; private agent output and exception text are never published. A `blocked` or `needs-owner` label without a matching stop record for three minutes ends the run with `stop_unrecorded`.

An approved label without an approval record for three continuous minutes ends the run with `approval_unrecorded`. The timer clears if the label disappears or the approval record arrives. The harness does not repair missing guard records.

After pass, failure or timeout, the harness attempts to close the story PR and issue, close the candidate's heartbeat report, and delete matching story branches and saved continuation references. After a pass, local installs, state and worktrees disappear with the temporary directory. After a failure, the harness retains that directory, including private state and the child's combined output in `candidate.log`, in a private folder under the operating system's temporary directory, outside the source repositories. It prints the location on local standard error only and keeps the last three failures for the local operating system user. Temporary-directory cleanup by the operating system can remove this evidence; copy it privately elsewhere if longer retention is needed. Cleanup errors make the run fail; reconcile leftover scratch resources manually before retrying. An uncertain issue creation is looked up by its unique run marker and closed without approving or repeating the write. A process crash, machine shutdown or unavailable GitHub can still require manual reconciliation. The harness does not retry ambiguous writes.

Exit status is zero only when every stage and cleanup passed and the commit status was posted. A refused status post prints the result plus a missing/unavailable permission message and exits nonzero. Check the App permission and installation acceptance before retrying. This status remains advisory for adopters. This repository's opt-in main-track upgrader requires a matching success from its configured runner identity before checking and switching.

## Verification and limits

Offline tests use synthetic clients and the existing GitHub recordings; they never contact GitHub or run an agent. Run `python -m pytest -q -p no:cacheprovider`, `python -m ruff check --no-cache .`, and `python scripts/check_private_content.py` before review.

The live exit test is an owner step after merge: grant status permission, write the private config, re-pin the service, then smoke-test main with each configured agent. With Codex, also test the owner-prepared schema-regression branch: main must pass and that regression must fail at review. Claude Code must pass on main. This worker does not perform the live run or change the scratch repository.

Known limit: the candidate runs as the **same OS user** as the harness. Its configuration and environment contain no framework key, token, Keychain reference or framework repository authority. Only scratch runner App references are exported; the smoke owner key stays in the harness. This is credential minimization, not an OS security boundary: candidate code can still attempt to access that user's files or Keychain. Operating-system separation of keys from code under test comes in M4. Use only owner-approved candidate code under this temporary exception.

## Automatic watcher

Run the watcher from the separately exported, approved release, using the same
private smoke config and Python as the manual command:

```sh
python -m lifecycle.service --config /absolute/private/smoke.json smoke-watch
```

Set `framework_owner` to the framework repository owner's login and
`framework_runner` to its runner App login (the same value as
`actors.framework_runner`). These optional additions are required for the
watcher; existing manual configs remain compatible. `daily_cap` defaults to 12.
The framework App installation must already permit Contents, Pull requests,
Checks and Actions read, Issues write, and Commit statuses write. Discovery and
service-result comments use a separate repository-scoped Product token; status
writes retain the existing status-only token. No workflow permissions change.

Copy `templates/lifecycle-smoke.plist.example` into a private launchd definition,
replace its placeholders, and load it once after owner review. It runs every
300 seconds and at load and exits after at most one candidate. The service's separate opt-in upgrade check consumes successful main results. Keep the watcher on a fixed approved install, updated by the owner by hand; do not point this job at candidate code.

Each invocation considers the current main commit first, then open PR heads in
PR-number order. Only owner- or framework-runner-authored PRs whose head and base
repositories match the configured framework repository qualify. It refreshes PR
metadata before admission to skip superseded heads. A status in `smoke/<agent>`
from the framework runner App, including failure or pending, suppresses automatic
execution; statuses from other identities do not. The manual `smoke --candidate`
command above explicitly reruns a tested commit.

A complete docs-only comparison against the last locally recorded successful
commit posts success naming that passed commit instead of executing code. An
existing successful candidate status can seed that baseline on first use. Only
`docs/` and `*.md` differences qualify, including both sides of renames;
ambiguous or potentially truncated comparisons run the smoke instead.

The watcher keeps a private per-repository/agent journal beside the shared scratch
locks. The daily cap counts candidates, including docs shortcuts, in UTC. Busy
scratch repositories and held locks leave no status and consume no run, allowing
retry at the next tick. Completed failures count and never automatically rerun.
Definite candidate-fetch or export failures also release the reservation and
daily count without posting a status, so the next invocation retries preparation.
Durable reservations prevent repeat execution after crashes or uncertain writes;
these require operator reconciliation rather than automatic replay. Preserve the
journal across job restarts. The journal and local locks are not distributed:
use one watcher machine per scratch repository.

After completion, a separate last-smoke comment on the service status issue shows
the candidate, result and stage without private output. Comment publication can
retry independently of execution. Runner owner-merge notices report whether the
runner App's smoke status passed on the reviewed head or is still pending. These
results remain advisory for PR integration and adopters; this repository's opt-in main-track service upgrades consume a matching successful status. No required check is added.
