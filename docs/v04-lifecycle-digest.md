# v0.4: issue lifecycle and stand-up digest

This is one opt-in work package for #19 and #25. The owner still controls scope approval, product acceptance, control-critical merges and releases. No service, token, workflow, label or model account is installed by running tests or importing modules. A private GitHub Free repository uses the same labels, comments and Actions; Projects, paid rulesets and project-board fields are not required. GitHub Actions quotas and provider charges still apply.

## Risky components first

1. `templates/task-lifecycle.yml` and `lifecycle/core.py`: default-branch guard, actor verification, repository-scoped writes and durable outbox.
2. `templates/task-lifecycle.example.json` and `lifecycle/service.py`: permissions, approved scope snapshots, event-specific claims, primary dispatch and owned worktree cleanup.
3. `runner/task_runner.py`: opt-in lifecycle hooks and push-back. Existing path/commit/fingerprint/process controls remain enforced.
4. `digest/standup.py` and `templates/standup.example.json`: privacy filters, publication, skip/cadence and model-budget reservations.
5. `.github/review-gate.json`: lifecycle code/tests are classified control-critical. The trusted review gate itself is unchanged.

## Installation and identities

Keep the trusted `runner/`, `lifecycle/` and `digest/` directories together outside agent-writable worktrees. Use Python 3.11+, Git and a POSIX host. The implementation has no new Python runtime dependencies. Existing one-file runner installations work with lifecycle disabled; enabling it requires the complete directory layout.

Copy the two example JSON files into private files. Fill absolute paths locally, outside source/agent worktrees. Protect files with mode 0600 and directories with 0700. Never commit the populated private configuration or credentials. For service dispatch, populate both a normal `runner_config` and a `primary_runner_config`; the latter supplies a primary worker profile and the same independent-review controls. The service creates isolated feature worktrees under the configured private `worktree_root`, from the exact fetched commit of the configured target branch on GitHub. It never pushes a branch or opens a PR automatically.

Configure explicit `identity_roles`: the owner identity has `["owner", "product"]`, and the Product App identity has `["product"]`. Runner and Actions identities have their respective roles. The separate `actors` map selects the concrete publishing identity for each automated channel; it is not a one-role-per-person restriction. Role membership controls permitted actions, while owner-only decisions still require the designated owner identity. Configure the canonical `owner`, `runner`, `product` and `workflow` publishing identities. Runner/Product identities must differ from the owner. Use a repository-installed GitHub App installation token or a dedicated machine account's fine-grained token. The runtime performs a fixed read-only GraphQL viewer query before **any write**, and refuses a mismatched/owner identity. An installation/token that cannot satisfy that check is unsupported until verified; use a dedicated account rather than falling back to the owner's credentials. Authentication is explicit environment variables (`token_env`, `product_token_env`), with no `gh`/owner credential discovery or automatic account switching. The optional shared App helper below mints installation tokens in memory.

Grant repository-scoped **Issues write** for labels, comments, follow-ups, assignments and digest issues, and **Contents read** for lifecycle target-branch fetches. Reads of PRs, trusted gate checks and workflow-run metadata additionally require Pull requests read, Checks read and Actions read. With delivery disabled, the fetch token requests exactly Issues write and Contents read (plus implicit Metadata read); ordinary issue-only job tokens retain their existing scope. Delivery-enabled fetch tokens retain the delivery permissions described below. Do not grant repository settings, administration or workflow write. The local client restricts issue transport writes to issue/label endpoints in the single configured repository, rejects redirects, and never interpolates issue/comment text into shell commands.

The Product App publishing identity posts stand-ups and minor follow-up issues. The owner also has the Product role, but the automated client never falls back to the owner's token. The model is disabled by default, including the live trial. Only explicit `model.enabled: true` opts into API prose; then set a separate Claude API key in `model.key_env`. The digest's model identifier, token cap, current prices and monthly USD estimate budget are all private configuration; no model or price is hardcoded. The owner's subscription is not used for this API path.

## Private App authentication and first identity check

Owner and bot GitHub logins are public policy and may be committed in `.github/task-lifecycle.json`. This file contains the repository, identities/roles and lifecycle rules; it contains no paths, App IDs, Keychain references or credentials. Keep an owner-reviewed policy checkout outside agent-writable worktrees. The workflow reads public policy from the default branch.

Copy `templates/app-auth.example.json` to a private local file outside every repository/worktree, for example under the user's OS configuration directory. Use directory mode 0700 and file mode 0600. Set `policy_file` to the absolute trusted public policy file. `app_id` is a positive integer; configure each explicit Keychain service and account locally. Store the base64-encoded, unencrypted RSA PEM in the named macOS generic-password item through the owner's Keychain tooling. Never put the PEM, JWT or installation token in a config, command argument, environment variable, receipt or log. The helper reads only that item using `/usr/bin/security`, captures its output, and signs via `/usr/bin/openssl` with inherited anonymous pipes. No Python runtime dependency or key file is added. This authentication mode requires macOS and its system tools; explicit environment-token authentication remains available for other hosts.

`lifecycle/app_tokens.py` authenticates the JWT with `GET /app`, checks its App ID and bot login, looks up only the configured repository installation, and requests a token with an explicit one-element repository list and permissions. It verifies returned scope/permissions/expiry and independently checks the token's accessible repository list. Tokens remain in memory and refresh before expiry; refresh repeats the GraphQL identity preflight before repository access. A configured App failure never falls back to an environment/owner token. Runtime Runner permissions are Issues write; Product additionally needs Contents, Pull requests, Checks and Actions read for citations and digest facts. Metadata read is implicit. Neither gets Contents write, administration or workflow write.

Install **both** Apps on the single target repository before this check. A 404 installation lookup can indicate the App has not been installed on the repository; stop and fix installation rather than broadening access or changing the expected identity. Grant the configured runtime read permissions and Issues write at installation; the first-comment tokens are further narrowed to Issues write only.

```sh
python -m lifecycle.preflight --config '<PRIVATE_APP_CONFIG>' --issue '<TEST_ISSUE_NUMBER>'
```

This check runs the same GraphQL identity preflight used by the lifecycle transport and posts one fixed comment per App. It stops on an identity mismatch and reports the actual public login. Local receipts persist an attempt **before** a POST; a later invocation can reconcile an existing exact comment, but never blindly repeat an uncertain write. Receipts contain only public identities/comment links and retry state. No workflow labels or task states change. A mismatch is a failed check, never permission to edit policy to match the returned login.

The read-only CI step `Verify guard token identity (read-only)` runs `python -I lifecycle/core.py --config .github/task-lifecycle.json guard-identity` with `GITHUB_TOKEN`, requiring `github-actions[bot]`. Its permissions/triggers are unchanged. This proves that token's identity query in Actions using the guard code, not that the event-driven lifecycle guard has been installed or that transitions have been live-tested. The owner still installs the reviewed guard separately on the default branch for the combined trial.

Sources: [GitHub JWT claims and RS256](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app), [repository installation and token scope endpoints](https://docs.github.com/en/rest/apps/apps), [installation repository listing](https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-app-installation).

## Policy, labels and guard setup

`transitions` is the complete, version-controlled allowed state/actor graph. `approved` is owner-only; `accepted` is owner-only unless acceptance is explicitly delegated to the Product actor. Configuration cannot grant approval to an automated actor. Each issue has exactly one current `status:*` label:

`draft → approved → dispatched → in-progress → committed → in-review`, then correction, primary/owner escalation, PR, acceptance and merge states as allowed by the graph. A passing review stays `in-review` with a distinct result record until its PR opens. Owner approvals later in the lifecycle are new events, not retries of an old approval.

Run setup once with the dedicated runner credentials:

```sh
python -m lifecycle.core --config '<PRIVATE_LIFECYCLE_JSON>' setup-labels
```

The owner installs `templates/task-lifecycle.yml` as `.github/workflows/task-lifecycle.yml` through a reviewed merge. Provide a **public-policy-only** `.github/task-lifecycle.json`: repository, actor logins, transitions, thresholds and environment-variable names. Leave machine paths as placeholders; the guard uses `RUNNER_TEMP` for its own ephemeral state. Do not copy the populated local machine configuration into GitHub. Nothing in this work package activates the template in this repository automatically.

The template handles issue opened/edited/label events and PR opened/edited/reopened/updated/closed events. It checks out only the repository's default branch, never PR head code, uses pinned actions, has no secrets and runs no dependencies/build scripts. Its write permission is limited to issues. Install it on the default branch before expecting enforcement; labels alone are not a security boundary. Events may be processed after a delay; service admission fails closed on ambiguous/mismatched states.

To approve a draft, sign in as the configured owner, open the issue's **Labels** picker and add `status:approved`. Leave `status:draft` selected: for a permitted draft → approved transition, the guard removes it and attests approval from that owner label event. Other combinations of status labels, or approval by another identity, do not authorize work.

For owner `approved` label events, the guard records an approved body SHA with the authenticated owner and a unique matching label-event ID. The hash uses the issue body in that event, even if the body changes before draft cleanup. A missing/ambiguous timestamp/event snapshot is not guessed: dispatch waits for reconciliation/re-approval. The service requires the trusted guard attestation and an unchanged issue body before claiming work. Editing approved scope invalidates approval, and edits/closure/revocation observed before dispatch or a later phase stop further model phases. During a GitHub outage an already-admitted frozen assignment may continue; use local `CANCEL` for immediate cancellation of the currently owned process group.

If approval fails, read the guard comment and check the lifecycle workflow in **Actions**. First fix the reported cause, such as a label-removal permission error. In the issue's **Labels** picker, first remove every other status label, including `status:draft`. Then, as the configured owner, remove `status:approved` if present and add it again, leaving it as the only status label. The transition must still be allowed from the last permitted status in the issue history. A failed cleanup can already have advanced that history to approved, so retaining draft on a retry can fail again. This creates a fresh owner event for the current body; merely re-running an old workflow does not approve later edits. Wait for the guard's approval record before expecting work to start. See the [troubleshooting playbook](../starter-kit/docs/troubleshooting.md#approval-failed-or-the-story-did-not-start).

PR synchronization accepts only same-repository PRs from configured trusted actors with `Closes #N`, an exact matching successful task-review record, and the configured base branch. A name-colliding check cannot provide gate evidence: complete check-run collections, exact head, registered trusted workflow, run event and PR/head binding must agree. Missing evidence is explicitly unavailable. Closed-unmerged PRs go to `needs-owner`. Merged PRs record the merge SHA; **GitHub**, not the runner, closes the issue through its closing keyword on the default branch. Acceptance and merge authority are not inferred from a digest or an agent verdict.

## Polling, records and recovery

```sh
python -m lifecycle.service --config '<PRIVATE_LIFECYCLE_JSON>' run-once
python -m lifecycle.service --config '<PRIVATE_LIFECYCLE_JSON>' serve
python -m lifecycle.service --config '<PRIVATE_LIFECYCLE_JSON>' reconcile
python -m lifecycle.core --config '<PRIVATE_LIFECYCLE_JSON>' flush
```

`serve` uses `poll_seconds`; no launchd installation is performed. An optional private `digest_config` lets the same loop check digest cadence. Only owner-approved labels dispatch normal work; `needs-primary` dispatches the configured Primary once. Other states wait. The service claims `(issue, approval event, deterministic job ID)` in durable private JSON before changing GitHub or enqueuing. Records use atomic, exclusive publication; crashes after labels/comments/enqueue are reconciled against the same claim/job rather than creating another job. Owner re-approval has a new event ID and a new job.

Before dispatching each new approved story, the service fetches the configured
`base_branch` (default `main`) directly from the configured GitHub repository,
including private repositories. It authenticates only with the Runner App, using
an isolated temporary Git repository with no credential helpers or inherited Git
configuration. The exact fetched commit is saved as `start_commit` in the private
claim before worktree creation; the story branch starts from that SHA, never a
local branch. Polling executes jobs one at a time, so a merge that lands before
the next story starts is included by its next fetch. Primary continuations keep
their existing reviewed source commit. This does not select GitHub's default
branch automatically or change PR delivery.

A failed fetch, missing remote branch or App authentication failure records the
fixed reason `target_branch_fetch_failed` in the claim. The story remains
`status:approved`, without a worktree or queued job; a later poll retries. There
is no local-branch fallback. A claim already pinned before a crash keeps its
recorded start commit during recovery. Use `status` to identify waiting claims
and inspect private claim state for the fixed reason; no Git output or credential
is copied into public evidence.

For older dispatched claims without `start_commit`, recovery first checks the
registered worktree and any durable runner assignment. It records the existing
job's original base commit, or the worktree HEAD if enqueue never completed,
without fetching again or replacing receipts or history. Only claims with no
existing work fetch a new start. Conflicting branch, assignment or history
evidence requires reconciliation rather than starting replacement work.

Issue history consists of fixed-field machine blocks authored by trusted identities. Repository, enclosing issue, author/role, transition, key, types and timestamps are checked. Owner label events supply approval authority; arbitrary comments, including digest replies, never do. Public records contain state, actor, job/event IDs, round, commit SHA, outcome, severity counts and fixed reason codes. Finding prose, arguments, paths, logs, task text and worker summaries stay private. P2 follow-up issues contain fixed metadata and reference their source issue; Primary consults private detail during triage.

GitHub updates have a private outbox. Labels/assignments are idempotent; append-only writes are marked attempted **before** sending. A lost response is searched for by its trusted record key. If an attempted comment/issue cannot be found, it remains uncertain for manual reconciliation; it is never blindly reposted. Known rejected writes can retry. A conflict on one issue does not authorize overwriting an owner's state or starting another job. Actions runs have ephemeral local storage: a rerun with missing prior comment proof refuses to repost and requires manual reconciliation. Distinct PR event deliveries are separately keyed; a rerun retains the same delivery/run ID. This conservative rule can leave missing metadata after a failed workflow, rather than risking a duplicate append. `reconcile` reports local/GitHub mismatches without changing either side. Unsynced/uncertain records appear in the stand-up.

Do not delete claims, outbox, digest journals or budgets to force a retry. A process crash is not permission to replay a model phase: an unclean running record becomes interrupted and requires manual reconciliation. Storage loss, compromised trusted accounts and multiple independent private state roots for the same repository are outside the exactly-once claim guarantee. Use one coordinator/state root, protect it, and retain backups. GitHub label updates have no cross-client transaction with local JSON; the guard repairs invalid labels and ambiguous evidence blocks action.

Managed cleanup is restricted to registered worktrees/branches created under the service root with the recorded ownership/head binding. Dirty work, changed history, closed-unmerged tasks and branches not yet included in the local base are retained for owner reconciliation. Refresh the local base yourself before expecting branch deletion after a server-side merge. Cleanup never force-deletes history, signals a recovered PID, edits an adopting project, or controls another service.

## Product clarifications and owner decisions

Product-role membership does not grant scope or story approval. The Product App may answer a clarification only with verified repository evidence and citations. `product_policy` requires repository sources and citations and routes `scope_change`, `acceptance_criteria_change` and `story_approval` to the owner. Those constraints cannot be disabled by config. Unknown requests and missing/unsupported citations also escalate; no new approval is inferred from an answer.

The bounded v0.4 clarification interface is extractive: one to five short quotes from configured repository text-file globs, each pinned to a full commit SHA and exact line range. Contents are fetched only from the configured repository, Git blob integrity is checked, quotes must match exactly, and the reply contains computed commit/line permalinks. There is no free-form model answer that could attach a valid citation to an invented claim. Broader Product acceptance/planning automation remains out of scope.

Submit a private structured request through the Product App client:

```sh
python -m lifecycle.product --config '<PRIVATE_LIFECYCLE_JSON>' --request '<PRIVATE_REQUEST_JSON>'
```

A clarification request has `id`, `issue`, `kind: "clarification"`, and `citations` entries with `path`, `ref` (full commit SHA), `start`, `end` and exact `quote`. Decision requests use the owner-only kind; they cannot be answered through repository quoting. The App marks `needs-owner` and notifies the owner without copying request prose. This CLI/API accepts explicit structured requests; it never polls comments for commands. A Product clarification is informational and does not change the issue's current state. Both the owner's and App's Product roles are recognized, but neither Product-role membership nor a forged owner comment can substitute for a trusted guard approval snapshot or independent Runner review.

## Push-back and the single disagreement exchange

Current reviewer schema uses **P0/P1/P2 only**. Critical maps to P0; high and medium map to P1; low/minor map to P2. Historical mapping is documented for interpretation, not permission to rewrite old receipts or reuse old verdicts. Unknown severity fails validation.

- P0/P1 force rework; P2 defaults to nonblocking follow-up issues. Projects can strengthen severity blocking but cannot make P0/P1 nonblocking.
- Lifecycle jobs allow an initial attempt plus at most **two correction rounds**, configured in `pushback`. Each new head has a fresh independent review. Exhaustion routes to `needs-primary`; an unresolved Primary job routes to the owner instead of creating an endless chain.
- Reviewer `blocked` or missing actionable findings goes directly to `needs-owner`, without a correction loop. Review-only jobs never run a correction worker.
- The lifecycle worker schema adds nullable `disagreement: {kind: technical|product|scope, reason: string}`. Once, on a clean previously reviewed head, the worker can record a disagreement without changing files. The reviewer reconsiders that same exact head once. Remaining technical disagreement goes to Primary; product/scope questions go to the owner. The reason stays private; no arbitrary argument is posted or executed.
- Primary receives the previous head and private findings and keeps the original review base, so its resulting work is independently reviewed across the complete feature range. PR gate formatting failures do not consume these rounds.

Standalone runner operation without `lifecycle_config` keeps its existing two-attempt bound and default worker schema. Configure/migrate review prompts to the canonical severity names before enabling the new mode.

## Stand-up setup and cadence

```sh
python -m digest.standup --config '<PRIVATE_DIGEST_JSON>'
python -m digest.standup --config '<PRIVATE_DIGEST_JSON>' --id '<UNIQUE_ON_DEMAND_ID>'
```

The first command checks configured IANA timezone, weekdays and local time. The on-demand ID bypasses the scheduled time, not pause/identity/budget/privacy controls; reuse of the ID never creates another issue. `paused` or `pause_dates` skip a day. An owner-authored `digest:skip` label on a published report defers its reporting window. Skips never approve or merge anything; the next digest becomes overdue after `overdue_after_skips` consecutive skips (default three). Items carry forward, including completed items from skipped windows.

Facts come from complete GitHub collections and trusted lifecycle records, plus protected local runner jobs and commit/refusal records. There are six sections: decisions, meaningful assumptions, demo readiness, completed work, progress/risk, usage/cost. Empty sections are omitted; if the first three are all empty, **no report or model call** is made. Stale thresholds are per state and use state label-event age; stale work is flagged, not retried. Urgent `blocked`/`needs-owner` states assign the owner independently of digest cadence, pause or model availability.

Issue/PR titles and comment prose are not copied. Every factual item links to its issue/PR; Product usage links to the repository issue list. Ready-to-demo work requires the exact reviewed head. Commits deduplicate by job/round/SHA and only `state: committed` counts as done. Prepared publication is uncertain and needs reconciliation. Refusals expose fixed reason codes, never paths. Runner cost is explicitly unavailable when not recorded.

For assumptions, configure approved public code-to-summary/view pairs in `public_assumptions` and matching `public_assumption_codes` in lifecycle policy. The Product identity can publish a metadata-only code using:

```sh
python -m lifecycle.core --config '<PRIVATE_LIFECYCLE_JSON>' assumption \
  --issue '<ISSUE_NUMBER>' --code '<APPROVED_PUBLIC_CODE>'
```

Only trusted Product records with configured codes enter that section. Preview URLs are explicit operator-approved HTTPS config, with no credentials/query data. These configured public strings/links are an intentional publication boundary; review them before enabling a public repository. No raw agent text is used as an assumption summary or Product view.

## Model privacy, cost and failure behavior

The Claude Messages API writes only a short non-factual introduction, constrained to eligible safe sentences. This intentionally limits stylistic variety: unsupported prose is discarded, so model output cannot introduce a number, state, link, SHA, name, list item or alleged fact. All six factual sections and fallback wording are deterministic. The API receives no issue bodies, findings, refusal paths, logs or task text. If the API fails or prose validation fails, factual sections still post with “summary unavailable”.

With `model.enabled: false` (or omitted), the digest requires no model identifier/key/prices, makes no model call or budget reservation, and publishes deterministic facts with “Model disabled”. Enabling the model explicitly requires all configured model/budget fields.

A UTC-month private budget ledger records a conservative byte-based input-token estimate, configured output cap, configured prices and reserved USD estimate before the call. It refuses a model call whose reservation would exceed `monthly_budget_usd`; factual publication continues. Failed/ambiguous calls keep their reservation and are not called again for the same digest ID. Reported API token usage is retained when valid; estimates are not an exact provider invoice or a substitute for provider-side limits. Set and review current prices yourself; no credential, price, model name or subscription assumption is shipped in code.

Private state contains full assignments and findings. Public GitHub reports do not: they use whitelisted fields, fixed prose, IDs, issue links and commit metadata only. Public-repository users must explicitly accept that these facts become public. Replies to digest issues are never collected as decisions, approvals or instructions. A report issue has a dedicated marker/author/label and is excluded from task automation, preventing recursive digests.

Sources: [GitHub issue events](https://docs.github.com/en/rest/issues/events), [labels](https://docs.github.com/en/rest/issues/labels), [assignees](https://docs.github.com/en/rest/issues/assignees), [GitHub App installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation), and [Claude Messages API](https://platform.claude.com/docs/en/api/messages/create).

## Validation and combined live acceptance

Tests use fake GitHub/model clients and synthetic actors, timestamps, scopes and temporary Git repositories. They cover repeated polling, re-approval, crash boundaries, outages, uncertain writes, actor/scope guard failures, fixed-field privacy, budget/skip behavior, push-back and disagreement. Run the repository's scanner, Ruff and full pytest suite before publishing a PR. No test uses real tokens, model accounts or other projects/services.

The owner chose **implementation and tests now; combined live trial pending separate runner/Product identities**. After identities and the owner-merged default-branch workflow are ready, run one combined trial using disposable local worktrees/state and clearly marked throwaway issues in the authorized repository. Verify owner approval and guard rejection; owner/App Product role mapping, a cited repository clarification, and owner escalation for scope/AC/story-approval requests; repeated polling without duplicate jobs; runner commit, one correction/disagreement path and fresh review; PR open/close/merge lifecycle as authorized; stale/unsynced risk; one Product-authored digest; model/budget fallback; skip without approval; and urgent notification independent of cadence. Capture exact source/head, transition keys, sanitized receipts, budget and digest evidence. Close/delete throwaway issues/branches as authorized and remove local worktrees/state/auth copies. Never substitute synthetic evidence for this live acceptance or use the owner's token as an automated identity. No release tag is authorized by this package.

## Service status and independent outage alerts

```sh
python -m lifecycle.service --config '<PRIVATE_LIFECYCLE_JSON>' status
python -m lifecycle.service --config '<PRIVATE_LIFECYCLE_JSON>' watchdog
```

`status` reads local files only: it does not initialize state, change file permissions,
load credentials or contact GitHub. It reports whether a coordinator holds the lifetime
lock, the last successful poll and its age, the scheduled next poll, claims and jobs
with their issues, and locally recorded reconciliation work. With no state it reports
not running, never polled, and unknown next poll. Running means the local coordinator
lock is held; it does not prove GitHub is reachable. Remote discrepancies still require
the existing read-only `reconcile` command. A stopped service's next poll is only a
schedule, not a promise that work will run.

Each completed successful poll writes `heartbeat.json` in private lifecycle state.
Skipped polls (interval not reached or another poll holding the lock) and failed polls
do not refresh it. The factual stand-up includes “Service last polled N minutes ago”,
or “never” before its first successful poll. Polls execute jobs synchronously, so a
long-running job can delay the next heartbeat; choose the threshold to include the
expected job duration as well as the poll interval.

In `serve` mode, `Unavailable` (including `Uncertain`) from a poll or stand-up
keeps the loop alive. The retry wait starts at `poll_seconds`, doubles on each
consecutive failure, caps at 600 seconds, and resets after success. Retry receipts
in private state record times, a count and the exception class, never its message.
`run-once` and `enqueue-only` still exit on the first error.

After the first successful poll and stand-up following failures, the existing
`agentic-last-poll-v1` status comment adds one line: “Polls first failed: …;
recovered: …; failed polls: N; exception class: Unavailable.” Times are UTC;
the class can also be `Uncertain`. The next successful cycle removes the line.
Other exceptions propagate and exit non-zero. Before exiting, the loop writes
only the exception class and originating framework function to private
`service-error.json`. On the next start, after a successful cycle, the same comment
adds “Service restarted after … in ….” and removes the private record. This line
also disappears after the next successful cycle. Neither line includes exception
messages, request paths, URLs or tokens. Failed notice publication retains the
private receipts for retry; these notices do not resolve ambiguous writes.

Run `serve` under the supplied launchd template with `KeepAlive` (see below), so
unexpected errors restart automatically. Retrying temporary GitHub failures does
not replace process supervision. Polls skipped by the interval or lock do not
consume recovery receipts or count as successful recovery.

Alerts are **opt-in**. Add `"heartbeat_alert_seconds": 600` to your private combined
lifecycle JSON (or the policy JSON referenced by `policy_file`), choosing an integer
at least as large as `poll_seconds`. Existing configs without this key keep alerts
disabled. This example is a starting point; use a longer threshold for long jobs.
Do not edit the kit's existing public policy to try the feature.

The owner must schedule `watchdog` independently of `serve`, for example every minute
using the separate optional launchd template below. It runs even when `serve` has
stopped, never dispatches work, and does not depend on digest cadence, pauses, or a
model. It uses the configured **Product** identity to create an urgent issue assigned
to the owner. The first watchdog check alerts immediately if no successful poll exists.
These report issues cannot dispatch stories. One private journal and lock per state
root suppress repeated alerts for the same outage; a fresh heartbeat clears the active
outage on the next watchdog check. Later outages can raise a new issue. Recovery does
not close the earlier issue automatically.

Known rejected writes can retry. A lost create response is reconciled using the
trusted Product author and deterministic marker before another write is considered.
An attempted alert absent from GitHub remains uncertain, visible in `status`, and
requires owner reconciliation; do not delete journals to force a retry. Watchdog
errors and JSON results go to the owner's logs. Keep this state durable and shared
with `serve`. Neither command can send alerts while the host is asleep/offline, the
watchdog itself is stopped, credentials are unavailable, or GitHub is unavailable.

## Optional owner-run macOS login setup

The kit supplies `templates/lifecycle-serve.plist.example` and
`templates/lifecycle-watchdog.plist.example`; it never installs or loads them.
The owner may copy them into private files named `org.example.agentic-lifecycle.plist`
and `org.example.agentic-lifecycle-watchdog.plist` under `$HOME/Library/LaunchAgents`.
Use a distinct pair of labels and filenames for each private state root.

Before loading either file:

1. Replace every `__PLACEHOLDER__` with an absolute local value, XML-escaping values
   such as `&`. Use the Python executable from your existing environment and the kit
   directory containing `lifecycle/`. Argument array entries preserve spaces.
2. Set private lifecycle config and separate stdout/stderr log files for each agent.
   Create their parent directories with owner-only permissions and protect both
   config and logs. Keep rendered plists and local paths out of source control.
3. Use the existing App/Keychain authentication setup for Runner and Product. Login
   agents do not inherit an interactive shell's environment; never place tokens or
   private keys in a plist. Confirm the configured identities can authenticate in
   that login context. No new dependencies or OS settings are required by the kit.
4. Opt into the heartbeat threshold described above and inspect `status`. Run a
   deliberate watchdog check only when ready for it to assign a real outage issue.

Owner-run installation, after preparing and inspecting those private files:

```sh
plutil -lint "$HOME/Library/LaunchAgents/org.example.agentic-lifecycle.plist"
plutil -lint "$HOME/Library/LaunchAgents/org.example.agentic-lifecycle-watchdog.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.example.agentic-lifecycle.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.example.agentic-lifecycle-watchdog.plist"
```

The serve template uses `RunAtLoad` and `KeepAlive` to start at login and restart after
exit, with throttling to avoid a tight crash loop. The watchdog is a separate short
command with `RunAtLoad` and `StartInterval` (60 seconds). Both are login agents,
so they require a logged-in owner; neither is a system daemon. Inspect their private
logs and use `status` to verify polling. Do not run another coordinator on the same
state root; the lifetime lock rejects a concurrent `serve` command.

Owner-run uninstall (unload the watchdog first to avoid alerts for an intentional stop):

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.example.agentic-lifecycle-watchdog.plist"
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/org.example.agentic-lifecycle.plist"
rm "$HOME/Library/LaunchAgents/org.example.agentic-lifecycle-watchdog.plist"
rm "$HOME/Library/LaunchAgents/org.example.agentic-lifecycle.plist"
```

Retain private state and logs for reconciliation. On other platforms, owners can
schedule the same `serve` and separate `watchdog` commands with their own service
manager; non-macOS templates and installation automation are outside this story.

After an owner merges a reviewed PR, a story stopped at `needs-owner` for
`owner_merge_required` becomes `merged`. Both the workflow and the service verify
the linked PR is merged and its head matches a trusted successful task review;
other stop reasons remain for owner reconciliation. The service backfills closed
stories on its next poll and uses the existing guarded cleanup path. Local
`status` reports removed worktrees and continuation references as counts only.
Rejected queued transitions comment on the issue and fail the workflow instead
of silently discarding the transition. Merge records grant neither approval nor
acceptance.
