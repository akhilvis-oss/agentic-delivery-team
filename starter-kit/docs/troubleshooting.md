# Troubleshooting

The product agent reads this file when the owner asks about a problem. Each entry says what the message means, **who fixes it**, and how.

**Who fixes it**
- **Agent:** the coding agent that owns the pull request fixes it. The owner doesn't need to act unless it fails twice.
- **Owner:** only the owner can do it (settings, approvals, merges, releases, keys). The steps are written for clicking, not typing.
- **Wait:** caused by GitHub or a provider, not by the project. Wait, then re-run.

The owner should never need to run Git commands. If a step needs the terminal, run one command at a time and copy only the command, not any text after a `#`.

## Pull request checks

### `review-gate` fails: "Tested head must equal the current full PR head SHA"
**Agent.** The pull request changed after its tests were recorded, often because someone clicked **Update branch**. The agent re-runs the tests on the new head, confirms its own changes are unchanged, and updates "Tested head" in the description.

### `review-gate` fails: "Missing section/content: …"
**Agent.** The pull request description lacks a section its review category needs (for example acceptance criteria, evidence or a security review). The agent completes the description from the pull request template. The failure message names the missing sections.

### `review-gate` fails: Checker "Reviewed head" doesn't match
**Agent (the Checker).** The independent Checker recorded its verdict on an older head. The Checker re-verifies the current head and updates its record. Nobody else may edit the Checker's record.

### "Required to update: Update your branch with the base branch before merging"
**Agent.** Another pull request merged first, so this one is out of date. The agent updates the branch, re-runs the checks and refreshes "Tested head". If the owner clicks **Update branch** instead, the gate fails as described above until the agent refreshes the description.

### Auto-merge refused: "Pull request is in clean status"
**Agent.** GitHub won't queue auto-merge on a pull request that is already ready. Under the merge policy, the agent may merge it directly after re-checking every condition on the exact head. Control-critical pull requests are always merged by the owner.

### A check shows "startup failure", or "Expected — waiting for status to be reported"
**Wait**, or **Owner** if it keeps happening. The trusted workflow didn't start. Common causes:
- GitHub blocked `pull_request_target` workflows during a scheduled test window, or from 2 December 2026 because the repository has no allowance for them. If it happens outside a test window, the owner adds the allowance (see the quickstart).
- A GitHub outage.

To re-trigger, the agent makes a small edit to the pull request description, or re-runs the workflow from the **Actions** tab.

### `lifecycle` check fails: "PR head has no trusted successful task review"
**Agent.** The pull request closes a story that the runner manages, but the runner's review of this exact head is missing. The story goes back through review. Pull requests that don't close a runner-managed story skip this check.

## Issues and labels

### An issue was labelled `status:draft` automatically
Expected. Every issue carries one status label, and a new issue starts as a draft. It means "not yet approved for work".

### A status label changed back on its own
Expected. The guard workflow reverts status changes made by anyone who isn't allowed to make them. Only the owner can approve or accept work. If the owner's own change was reverted, report it as a bug.

## Bot identities and keys

### The bot gets "404" when looking up its installation
**Owner.** The GitHub App isn't installed on that repository. Open the App's settings, then **Install App**, choose **Only select repositories**, and add the repository.

### The bot can't get a token, or gets a permission error
**Owner.** The App asks for a permission it wasn't given. Open the App's **Permissions & events**, add the permission named in the error, save, and then **accept the updated permissions** on the installation (GitHub sends an email or shows a banner).

### Keychain says "The specified item already exists"
**Owner.** A key with that name is already stored. Add `-U` to the `security add-generic-password` command to replace it.

### Terminal says "operation not permitted" when reading Downloads
**Owner.** macOS hasn't allowed Terminal to read that folder. Open **System Settings → Privacy & Security → Files & Folders → Terminal**, switch on **Downloads**, then reopen Terminal.

### Push fails: not authenticated
**Owner**, once. Sign in for the account the agents run under:
```sh
gh auth login
```
```sh
gh auth setup-git
```
```sh
gh auth status
```

## Task runner

### A runner receipt says `blocked`
**Agent first.** The runner stopped before committing anything, which is the safe outcome. Read the reason; common ones:
- a prerequisite failed (dependency missing, preflight not passed): fix the story or its dependencies;
- a configured command failed (wrong path, missing program): fix the configuration;
- the worker stopped on a harmless exploratory command because its instructions said "stop on any failure": stop rules should cover required checks only.

Never reuse the blocked run's changes. Queue a new task after fixing the cause.

### An agent says "Not logged in"
**Owner.** The runner gives each agent its own private home, which needs its own sign-in or API key. Sign in using the command the coding agent gives you, then check with its status command.

## Notifications

### Too many emails from bots
**Owner.** In GitHub **Settings → Notifications**, send notifications to a separate address, or filter on the mailing-list header for the repository. Everything that needs the owner also appears in the stand-up.
