# Roadmap

Where the kit is heading. Plans change with feedback; each item links to its issue, and [milestones](https://github.com/akhilvis-oss/agentic-delivery-team/milestones) show progress.

## v0.5.3: Approve and walk away (released)
[Released v0.5.3](https://github.com/akhilvis-oss/agentic-delivery-team/releases/tag/v0.5.3): opt-in delivery from the owner's approval through PR integration when policy permits; control-critical merges remain owner-only.
- **Automatic integration** ([#78](https://github.com/akhilvis-oss/agentic-delivery-team/issues/78)): after a passing review, the runner opens the pull request, records its review as the Checker, and enables auto-merge when the category allows. Control-critical work still goes to the owner.
- **Approve with one label** ([#77](https://github.com/akhilvis-oss/agentic-delivery-team/issues/77)): adding `status:approved` replaces the draft label.
- **One kit version with feature switches** ([#86](https://github.com/akhilvis-oss/agentic-delivery-team/issues/86)): one `.github/agentic-kit.json` records the version and which pieces are on.
- **Every review finding becomes a test** ([#84](https://github.com/akhilvis-oss/agentic-delivery-team/issues/84)).
- **Service status and heartbeat** ([#67](https://github.com/akhilvis-oss/agentic-delivery-team/issues/67)): see at a glance that the service is running.

## v0.5.4: Fresh story bases and bounded check corrections (released)
[Released v0.5.4](https://github.com/akhilvis-oss/agentic-delivery-team/releases/tag/v0.5.4): safer sequential lifecycle dispatch and clearer check failure handling.
- Fetch each story's target branch before creating its worktree ([#97](https://github.com/akhilvis-oss/agentic-delivery-team/issues/97)), pin the fetched commit and preserve it during recovery; failed fetches wait without a stale local fallback.
- Bounded worker corrections for check failures caused by the worker's changes, with fixed stop reasons and owner next steps ([#101](https://github.com/akhilvis-oss/agentic-delivery-team/issues/101)). Missing tools and unrelated failures still stop immediately.

## v0.5.5: Readable stops, saved work, a service that stays up (released)
[Released v0.5.5](https://github.com/akhilvis-oss/agentic-delivery-team/releases/tag/v0.5.5): stops explain themselves on the issue, stopped work is saved and can be continued, merges are recorded and cleaned up, and the service stays up through GitHub errors. See the [changelog](CHANGELOG.md).

[v0.5.6](https://github.com/akhilvis-oss/agentic-delivery-team/releases/tag/v0.5.6) (patch): the hosted lifecycle guard no longer fails on issues whose stop records quote text. Adopt v0.5.6 rather than v0.5.5.

[v0.5.7](https://github.com/akhilvis-oss/agentic-delivery-team/releases/tag/v0.5.7) (patch): fixes delivery for light and standard stories, and adds the optional live smoke test and its watcher. Adopt v0.5.7 if you use delivery.

## v0.6.0: Approve and walk away, installed by your own agent (released)
v0.6.0 bundles the safety net and the consolidated lifecycle:
- **Safety net:** recorded GitHub responses as test fixtures, and a live smoke test.
- **The consolidated lifecycle:**
  - one way to continue a stopped story, and four kinds of stop with one notice each;
  - a queue with dependencies, and a readiness pass;
  - guards so finished stories never reopen and one story's error can't stop another;
  - owner withdrawal, and finishing a story with `status:merged`.
- **The review tier** is chosen from the gate's rules ([#245](https://github.com/akhilvis-oss/agentic-delivery-team/issues/245)).
- **Install and upgrade** by your own coding agent, from `INSTALL.md` and `UPGRADE.md` with a read-only checker ([#246](https://github.com/akhilvis-oss/agentic-delivery-team/issues/246)).

See the [changelog](CHANGELOG.md).

## Next: parallel lanes and speed
- **Runner-coordinated parallel lanes** ([#56](https://github.com/akhilvis-oss/agentic-delivery-team/issues/56)): two members build at once. The runner starts and resumes agents, relays questions, assigns slots, and merges one PR at a time while main's CI is green.
- **Speed:** fewer git processes per runner check, and a shared test repository.
- **Later:** per-criterion review evidence, protected tests ([#80](https://github.com/akhilvis-oss/agentic-delivery-team/issues/80)), failed checks back to the builder ([#59](https://github.com/akhilvis-oss/agentic-delivery-team/issues/59)), and display-only status groups ([#180](https://github.com/akhilvis-oss/agentic-delivery-team/issues/180)).
## v0.6: Set up and run without touching config
For owners who only talk to their product agent.
- **Owner-tunable settings** ([#81](https://github.com/akhilvis-oss/agentic-delivery-team/issues/81)): inactivity-based time limits instead of a fixed timeout; behaviour settings in an owner-merged repository file the product agent can propose changes to; a story size field.
- **Coding-agent identity** ([#31](https://github.com/akhilvis-oss/agentic-delivery-team/issues/31)): pull requests authored by a bot without workflow or merge rights, so GitHub itself enforces the merge policy.
- **Protected regression suite** ([#80](https://github.com/akhilvis-oss/agentic-delivery-team/issues/80)): an owner-owned list of tests the builder can't change, run from the trusted base branch.
- **Failed checks go back to the builder** ([#59](https://github.com/akhilvis-oss/agentic-delivery-team/issues/59)), with red checks shown in the stand-up.
- **Milestone progress and story readiness** ([#54](https://github.com/akhilvis-oss/agentic-delivery-team/issues/54)): the stand-up shows completion by roadmap milestone, and stories are checked before approval.
- **Optional test sandbox** ([#73](https://github.com/akhilvis-oss/agentic-delivery-team/issues/73)) for runner-run tests.
- **Claude Code as a tested agent** ([#53](https://github.com/akhilvis-oss/agentic-delivery-team/issues/53)), as builder and reviewer.

## Later
- The hosted guard reads `features.lifecycle` from the trusted kit record; other switches remain conventions for agents. Install and upgrade follow the tagged runbooks and checker (see [versions and feature switches](docs/versions-and-features.md)).
- Resuming agent sessions ([#20](https://github.com/akhilvis-oss/agentic-delivery-team/issues/20)).
- Unattended product agent through an API key, and stories published under the product agent's own identity.
- Key stores beyond the macOS Keychain.
- An optional second reviewer from a different model provider, as a pool slot.

## How to influence it
Open an issue, or comment on an existing one with your use case. Real needs from people using the kit move items up.
