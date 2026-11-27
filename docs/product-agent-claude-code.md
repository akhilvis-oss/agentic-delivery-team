# Claude Code as the product agent

Use Claude Code to plan your product roadmap, milestones and stories. It reads your repository, follows `PRODUCT_AGENT.md`, drafts stories for GitHub and proposes product-doc changes.

This setup is for **sessions you run yourself**. Everything it does on GitHub shows as you, which is correct: these are your product decisions. For unattended product work, use the Product App identity from the lifecycle guide instead.

The file-read boundary requires Claude Code 2.1.257 or later. Check your version with `claude --version`. [Claude Code settings reference](https://code.claude.com/docs/en/settings#settings-precedence)

## 1. Make a separate product checkout

Clone your repository into its own folder, used only for product sessions. Don't use a coding agent's worktree, and don't use the folder you code in.

```sh
git clone https://github.com/<OWNER>/<REPOSITORY>.git <PRODUCT_CHECKOUT>
```

The repository needs the starter kit's product files: `PRODUCT_AGENT.md`, `docs/product/`, `docs/agentic-delivery-troubleshooting.md`, `docs/dry-run.md` and the story template.

## 2. Install the settings file

Copy [`templates/claude-code-product-agent.settings.json`](../templates/claude-code-product-agent.settings.json) to a private location outside the checkout, for example `<PRIVATE_CONFIG_DIR>/product-agent.settings.json`.

The session can read files in the product checkout and any explicitly added working directory. The template also denies reads of standard credential and configuration paths under your home directory. Claude Code applies deny rules before ask or allow rules. [Claude Code permissions](https://code.claude.com/docs/en/permissions)

The template allows `git fetch`, `git fetch origin`, and `git pull --ff-only` as written. It also allows inspecting Git history and status, listing and viewing issues and pull requests, viewing checks and runs, and editing files under `docs/product/`.

The four posting commands `gh issue comment`, `gh issue create`, `gh pr comment`, and `gh pr create` each ask for your approval. In Manual mode, `gh issue edit` also asks for approval, including when it changes labels. Check the destination, content and any labels before approving a prompt. Creating a branch for a product-doc pull request also asks.

For GitHub CLI, the template denies `gh pr merge`, `gh pr review`, `gh release`, `gh label`, `gh api`, `gh repo`, `gh secret`, `gh variable`, `gh workflow`, and `gh ruleset`, along with repository overrides using `-R` and `--repo`.

For Git, it denies `git push --force`, `git push -f`, `git push origin main`, `git reset --hard`, and the options `--upload-pack`, `--no-index`, and `--output`. It also denies edits to `.github/`, `AGENTS.md`, and `PRODUCT_AGENT.md`.

## 3. Start a session

From the product checkout, start Claude Code in Manual mode with the settings and the product rules.

```sh
cd "<PRODUCT_CHECKOUT>"
```
```sh
git pull --ff-only
```
```sh
claude --permission-mode default \
  --settings "<PRIVATE_CONFIG_DIR>/product-agent.settings.json" \
  --append-system-prompt "$(cat PRODUCT_AGENT.md)"
```

For a dry run, create a separate scratch folder outside the product checkout first. Replace `<SCRATCH_DIR>` with that folder's path when launching the session.

```sh
claude --permission-mode default \
  --settings "<PRIVATE_CONFIG_DIR>/product-agent.settings.json" \
  --add-dir "<SCRATCH_DIR>" \
  --append-system-prompt "$(cat PRODUCT_AGENT.md)"
```

Check the session before relying on it.

- Type `/permissions` and confirm the allow, ask and deny rules above are listed.
- Ask "Which commit of the roadmap did you read?" It should answer with a commit, as rule 2 of `PRODUCT_AGENT.md` requires.

## 4. What a good session looks like

1. You discuss the roadmap. It reads `docs/product/` first and states the commit it read.
2. It proposes changes to the roadmap, milestone weights or decision log, edits files under `docs/product/`, and asks before creating a branch and pull request for them. You merge product-doc pull requests yourself.
3. It drafts a story with the story template: section, milestone and weight, acceptance criteria and test scenarios.
4. It runs the dry run (`docs/dry-run.md` in the kit) with a Claude Code sub-agent, and fixes what it finds.
5. It asks before filing the story as an issue.
6. Approve each story yourself on GitHub. Decline any product-agent prompt to apply `status:approved`.
7. If you ask about a failing check, it answers from `docs/agentic-delivery-troubleshooting.md` and says who fixes it.

## Limits

- **The file-read boundary applies to Claude Code's file tools.** Shell permissions match command patterns. For stronger isolation, run under a dedicated operating-system user with access limited to the product checkout and any scratch folder.
- **Your own Claude Code settings still load.** Review the effective permissions with `/permissions`. Add `--setting-sources project` to skip user and local settings for the session.
- **Instruction files.** Claude Code automatically loads `CLAUDE.md` files. Keep coding rules out of the product checkout's `CLAUDE.md`, or leave it out, so coding rules don't mix with product rules.
- **Actions show as you.** GitHub can't tell this session's issues from ones you typed yourself. That's intended for attended sessions.
