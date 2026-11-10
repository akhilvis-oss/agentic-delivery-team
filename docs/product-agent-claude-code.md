# Claude Code as the product agent

Use Claude Code as the assistant you plan the product with: roadmap, milestones and stories. It reads your repository, follows `PRODUCT_AGENT.md`, drafts stories as issues and proposes product-doc changes, while settings stop it from merging, approving, changing workflows or editing code.

This setup is for **sessions you run yourself**. Everything it does on GitHub shows as you, which is correct: these are your product decisions. For unattended product work, use the Product App identity from the lifecycle guide instead.

Tested flags: Claude Code 2.1.322. Check yours with `claude --version` and `claude --help`, because flags can change between versions.

## 1. Make a separate product checkout

Clone your repository into its own folder, used only for product sessions. Don't use a coding agent's worktree, and don't use the folder you code in.

```sh
git clone https://github.com/<OWNER>/<REPOSITORY>.git <PRODUCT_CHECKOUT>
```

The repository needs the starter kit's product files: `PRODUCT_AGENT.md`, `docs/product/`, `docs/troubleshooting.md` and the story template.

## 2. Install the settings file

Copy [`templates/claude-code-product-agent.settings.json`](../templates/claude-code-product-agent.settings.json) to a private location outside the checkout, for example `<PRIVATE_CONFIG_DIR>/product-agent.settings.json`.

What it allows without asking:
- reading files, and fetching or fast-forwarding from GitHub;
- listing, viewing, creating and commenting on issues; viewing pull requests, checks and runs;
- editing files under `docs/product/` only.

What it always refuses: merging or reviewing pull requests, releases, labels (so it can't approve a story by labelling it), raw API calls, repository settings, secrets, workflows, force-pushes or pushes to `main`, and editing `.github/`, `AGENTS.md` or `PRODUCT_AGENT.md`.

Anything else, such as editing an issue or creating a branch for a product-doc pull request, asks you first.

## 3. Start a session

From the product checkout, start Claude Code with the settings and the product rules:

```sh
cd <PRODUCT_CHECKOUT>
```
```sh
git pull --ff-only
```
```sh
claude --settings <PRIVATE_CONFIG_DIR>/product-agent.settings.json --append-system-prompt "$(cat PRODUCT_AGENT.md)"
```

Then check the session before relying on it:
- type `/permissions` and confirm the allow and deny rules above are listed;
- ask "Which commit of the roadmap did you read?" It should answer with a commit, as rule 2 of `PRODUCT_AGENT.md` requires.

## 4. What a good session looks like

1. You discuss the roadmap. It reads `docs/product/` first and states the commit it read.
2. It proposes changes to the roadmap, milestone weights or decision log, edits files under `docs/product/`, and asks before creating a branch and pull request for them. You merge product-doc pull requests yourself.
3. It drafts stories as issues with the story template: section, milestone and weight, acceptance criteria and test scenarios.
4. You approve each story yourself. The settings stop it from labelling one approved.
5. If you ask about a failing check, it answers from `docs/troubleshooting.md` and says who fixes it.

## Limits

- **Settings are guard rails, not a sandbox.** Permission rules match commands by pattern, so an unusual spelling of a command may not match a deny rule. The defaults ask for anything not listed, so read each permission prompt before accepting it. Run sessions under an operating-system user with access only to the product checkout if you want stronger isolation.
- **Your own Claude Code settings still load.** If your user settings allow more, those allowances apply too. To use only this file, add `--setting-sources project` (this ignores your user and local settings for the session).
- **Instruction files.** Claude Code automatically loads `CLAUDE.md` files. Keep coding rules out of the product checkout's `CLAUDE.md`, or leave it out, so coding rules don't mix with product rules.
- **Actions show as you.** GitHub can't tell this session's issues from ones you typed yourself. That's intended for attended sessions.
