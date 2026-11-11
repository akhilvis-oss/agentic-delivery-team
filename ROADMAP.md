# Roadmap

Where the kit is heading. Plans change with feedback; each item links to its issue, and [milestones](https://github.com/akhilvis-oss/agentic-delivery-team/milestones) show progress.

## v0.5.3: Approve and walk away (released)
From the owner's approval to a merged pull request, with no prompts in between.
- **Automatic integration** ([#78](https://github.com/akhilvis-oss/agentic-delivery-team/issues/78)): after a passing review, the runner opens the pull request, records its review as the Checker, and enables auto-merge when the category allows. Control-critical work still goes to the owner.
- **Approve with one label** ([#77](https://github.com/akhilvis-oss/agentic-delivery-team/issues/77)): adding `status:approved` replaces the draft label.
- **One kit version with feature switches** ([#86](https://github.com/akhilvis-oss/agentic-delivery-team/issues/86)): one `.github/agentic-kit.json` records the version and which pieces are on.
- **Every review finding becomes a test** ([#84](https://github.com/akhilvis-oss/agentic-delivery-team/issues/84)).
- **Service status and heartbeat** ([#67](https://github.com/akhilvis-oss/agentic-delivery-team/issues/67)): see at a glance that the service is running.

## v0.5.4: Parallel stories and readable stand-ups (next)
- **Lifecycle with the agent pool** ([#76](https://github.com/akhilvis-oss/agentic-delivery-team/issues/76)): approved stories go to free slots and run in parallel when they don't overlap.
- **Readable stand-ups** ([#68](https://github.com/akhilvis-oss/agentic-delivery-team/issues/68)): ignore pre-lifecycle issues, optional titles, a one-line summary.

## v0.6: Set up and run without touching config
For owners who only talk to their product agent.
- **Setup wizard** ([#82](https://github.com/akhilvis-oss/agentic-delivery-team/issues/82)): one command opens a local page that checks prerequisites, creates the bot Apps with one click, stores keys in the keychain, lets you choose agents for each role and set up slots, installs the repository pieces as a pull request, guides the dedicated OS user, starts the service and shows its status.
- **Owner-tunable settings** ([#81](https://github.com/akhilvis-oss/agentic-delivery-team/issues/81)): inactivity-based time limits instead of a fixed timeout; behaviour settings in an owner-merged repository file the product agent can propose changes to; a story size field.
- **Coding-agent identity** ([#31](https://github.com/akhilvis-oss/agentic-delivery-team/issues/31)): pull requests authored by a bot without workflow or merge rights, so GitHub itself enforces the merge policy.
- **Protected regression suite** ([#80](https://github.com/akhilvis-oss/agentic-delivery-team/issues/80)): an owner-owned list of tests the builder can't change, run from the trusted base branch.
- **Failed checks go back to the builder** ([#59](https://github.com/akhilvis-oss/agentic-delivery-team/issues/59)), with red checks shown in the stand-up.
- **Milestone progress and story readiness** ([#54](https://github.com/akhilvis-oss/agentic-delivery-team/issues/54)): the stand-up shows completion by roadmap milestone, and stories are checked before approval.
- **Optional test sandbox** ([#73](https://github.com/akhilvis-oss/agentic-delivery-team/issues/73)) for runner-run tests.
- **Claude Code as a tested agent** ([#53](https://github.com/akhilvis-oss/agentic-delivery-team/issues/53)), as builder and reviewer.

## Later
- Resuming agent sessions ([#20](https://github.com/akhilvis-oss/agentic-delivery-team/issues/20)).
- Unattended product agent through an API key, and stories published under the product agent's own identity.
- Key stores beyond the macOS Keychain.
- An optional second reviewer from a different model provider, as a pool slot.

## How to influence it
Open an issue, or comment on an existing one with your use case. Real needs from people using the kit move items up.
