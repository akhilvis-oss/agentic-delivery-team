# Contributing

Thanks for your interest. This kit is young and changes quickly; small, focused contributions are easiest to review.

## Before you start
- **Open an issue first** for anything beyond a small fix, so we can agree the approach before you build it.
- Read [`AGENTS.md`](AGENTS.md). It's written for AI agents but applies to everyone: boundaries, review categories, must-ask items and the merge policy.

## How issues are labelled
This repository runs its own task lifecycle. Every issue carries one `status:` label; a new or edited issue without one is labelled `status:draft` automatically. That's expected: it means not yet approved for work. Only the owner approves work.

## Making a change
1. Fork the repository and work on a branch.
2. Keep the change to one coherent purpose. Don't mix control-critical changes (workflows, the review gate, `AGENTS.md`) with other work.
3. Run the local checks:
   ```sh
   python -m pip install -r requirements-dev.txt
   python scripts/check_private_content.py
   python -m ruff check .
   python -m pytest -q
   ```
4. Open a pull request using the template. Declare the review category your changed files require (the `review-gate` check will tell you if it's too low), copy the issue's acceptance criteria verbatim, and report test results exactly as run.

## What we look for
- Generic code: anything specific to one project becomes configuration with a documented example.
- Tests for every behaviour change, including failure cases for the runner and gate.
- Plain, short documentation written for someone new to the kit.
- No private content: personal paths, account names, tokens or project data.

## Licence
By contributing, you agree that your contributions are licensed under the [MIT licence](LICENSE).
